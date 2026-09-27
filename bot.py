#!/usr/bin/env python3
"""CoinSpot trading bot.

paper mode: live CoinSpot prices, simulated fills, separate ledger.
live mode:  real signed buy/sell orders, locked behind a confirm phrase,
            API keys from env vars, and a lifetime spend cap.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
# Paper and live keep separate books so test fills never mix with real ones.
PATHS = {
    "paper": (ROOT / "state.json", ROOT / "trades.csv"),
    "live": (ROOT / "state_live.json", ROOT / "trades_live.csv"),
}

PUBLIC_LATEST = "https://www.coinspot.com.au/pubapi/v2/latest/{coin}"
PUBLIC_BUY = "https://www.coinspot.com.au/pubapi/v2/buyprice/{coin}"
PUBLIC_SELL = "https://www.coinspot.com.au/pubapi/v2/sellprice/{coin}"
PRIVATE_BASE = "https://www.coinspot.com.au/api/v2"
LIVE_CONFIRM_PHRASE = "I understand this trades real money"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config(path: Path = CONFIG_PATH) -> dict:
    with path.open() as f:
        return yaml.safe_load(f)


def mode_of(cfg: dict) -> str:
    mode = cfg.get("mode", "paper")
    if mode not in PATHS:
        raise ValueError(f"mode must be paper or live, got {mode!r}")
    return mode


def state_path(cfg: dict) -> Path:
    return PATHS[mode_of(cfg)][0]


def log_path(cfg: dict) -> Path:
    return PATHS[mode_of(cfg)][1]


def default_state(cfg: dict) -> dict:
    return {
        "mode": mode_of(cfg),
        "aud": float(cfg["starting_aud"]),
        "positions": {
            c: {"units": 0.0, "cost_aud": 0.0} for c in cfg["universe"]
        },
        "daily": {"date": "", "spent_aud": 0.0, "sold_aud": 0.0},
        "lifetime_spent_aud": 0.0,
        "realised_pnl_aud": 0.0,
        "last_dca": {},
        "last_dip": {},
        "last_tp": {},
        "last_sl": {},
        "ticks": {c: [] for c in cfg["universe"]},
        "equity_history": [],
    }


def load_state(cfg: dict) -> dict:
    path = state_path(cfg)
    if not path.exists():
        return default_state(cfg)
    with path.open() as f:
        state = json.load(f)
    fresh = default_state(cfg)
    for k, v in fresh.items():
        state.setdefault(k, v)
    for c in cfg["universe"]:
        state["positions"].setdefault(c, {"units": 0.0, "cost_aud": 0.0})
        state["ticks"].setdefault(c, [])
    return state


def save_state(cfg: dict, state: dict) -> None:
    path = state_path(cfg)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(path)


LOG_FIELDS = [
    "ts",
    "mode",
    "action",
    "reason",
    "coin",
    "units",
    "price",
    "gross_aud",
    "fee_aud",
    "net_aud",
    "aud_after",
    "units_after",
    "avg_cost",
    "realised_pnl",
]


def log_trade(cfg: dict, row: dict) -> None:
    path = log_path(cfg)
    if not path.exists():
        path.write_text(",".join(LOG_FIELDS) + "\n")
    line = ",".join(str(row.get(k, "")) for k in LOG_FIELDS) + "\n"
    with path.open("a") as f:
        f.write(line)


@dataclass
class Quote:
    coin: str
    bid: float
    ask: float
    last: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


def fetch_quote(coin: str) -> Quote:
    c = coin.lower()
    latest = requests.get(PUBLIC_LATEST.format(coin=c), timeout=15).json()
    if latest.get("status") != "ok":
        raise RuntimeError(f"latest failed for {coin}: {latest}")
    prices = latest.get("prices") or latest
    bid = float(prices["bid"])
    ask = float(prices["ask"])
    last = float(prices.get("last") or prices["ask"])
    try:
        buy = requests.get(PUBLIC_BUY.format(coin=c), timeout=15).json()
        sell = requests.get(PUBLIC_SELL.format(coin=c), timeout=15).json()
        if buy.get("status") == "ok" and buy.get("rate"):
            ask = float(buy["rate"])
        if sell.get("status") == "ok" and sell.get("rate"):
            bid = float(sell["rate"])
    except requests.RequestException:
        pass
    return Quote(coin=coin.upper(), bid=bid, ask=ask, last=last)


# ---------------------------------------------------------------- live client


class CoinSpotClient:
    """Signed CoinSpot v2 private API. HMAC-SHA512 of the JSON body with the secret."""

    def __init__(self, key: str, secret: str):
        if not key or not secret:
            raise RuntimeError("Set COINSPOT_API_KEY and COINSPOT_API_SECRET env vars for live mode.")
        self.key = key
        self.secret = secret.encode()

    @classmethod
    def from_env(cls) -> "CoinSpotClient":
        return cls(os.environ.get("COINSPOT_API_KEY", ""), os.environ.get("COINSPOT_API_SECRET", ""))

    def _post(self, path: str, payload: dict | None = None) -> dict:
        body = dict(payload or {})
        body["nonce"] = int(time.time() * 1000)
        raw = json.dumps(body, separators=(",", ":"))
        sign = hmac.new(self.secret, raw.encode(), hashlib.sha512).hexdigest()
        r = requests.post(
            PRIVATE_BASE + path,
            data=raw,
            headers={"Content-Type": "application/json", "key": self.key, "sign": sign},
            timeout=20,
        )
        data = r.json()
        if data.get("status") != "ok":
            raise RuntimeError(f"CoinSpot {path} failed: {data}")
        return data

    def balances(self) -> dict[str, float]:
        """Available balance per coin (AUD included), read-only endpoint."""
        data = self._post("/ro/my/balances")
        out: dict[str, float] = {}
        for entry in data.get("balances", []):
            for coin, info in entry.items():
                out[coin.upper()] = float(info.get("available", info.get("balance", 0)))
        return out

    def buy_now_aud(self, coin: str, aud: float) -> dict:
        return self._post("/my/buy/now", {"cointype": coin, "amounttype": "aud", "amount": round(aud, 2)})

    def sell_now_units(self, coin: str, units: float) -> dict:
        return self._post("/my/sell/now", {"cointype": coin, "amounttype": "coin", "amount": round(units, 8)})


def live_gate(cfg: dict) -> str | None:
    """Return a reason live trading must not run, or None if every lock is open."""
    live = cfg.get("live") or {}
    if live.get("confirm") != LIVE_CONFIRM_PHRASE:
        return f'live.confirm must be exactly "{LIVE_CONFIRM_PHRASE}"'
    if not os.environ.get("COINSPOT_API_KEY") or not os.environ.get("COINSPOT_API_SECRET"):
        return "COINSPOT_API_KEY / COINSPOT_API_SECRET env vars are not set"
    if float(live.get("max_lifetime_spend_aud", 0)) <= 0:
        return "live.max_lifetime_spend_aud must be > 0"
    return None


# ---------------------------------------------------------------- bookkeeping


def reset_daily(state: dict) -> None:
    today = utcnow().strftime("%Y-%m-%d")
    if state["daily"].get("date") != today:
        state["daily"] = {"date": today, "spent_aud": 0.0, "sold_aud": 0.0}


def mark_ticks(state: dict, quotes: dict[str, Quote], lookback_hours: int) -> None:
    cutoff_s = iso(utcnow() - timedelta(hours=lookback_hours + 1))
    for coin, q in quotes.items():
        ticks = state["ticks"].setdefault(coin, [])
        ticks.append({"ts": iso(), "bid": q.bid, "ask": q.ask, "last": q.last})
        state["ticks"][coin] = [t for t in ticks if t["ts"] >= cutoff_s][-5000:]


def rolling_high(state: dict, coin: str, hours: int) -> float | None:
    cutoff = iso(utcnow() - timedelta(hours=hours))
    vals = [t["last"] for t in state["ticks"].get(coin, []) if t["ts"] >= cutoff]
    return max(vals) if vals else None


def hours_since(ts: str | None) -> float:
    if not ts:
        return 1e9
    then = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (utcnow() - then).total_seconds() / 3600


def equity(state: dict, quotes: dict[str, Quote]) -> float:
    total = state["aud"]
    for coin, pos in state["positions"].items():
        if coin in quotes:
            total += pos["units"] * quotes[coin].bid
    return total


def avg_cost(pos: dict) -> float:
    if pos["units"] <= 0:
        return 0.0
    return pos["cost_aud"] / pos["units"]


# ---------------------------------------------------------------- execution


def buy(cfg: dict, state: dict, coin: str, aud_amt: float, quotes: dict[str, Quote],
        reason: str, client: CoinSpotClient | None = None) -> dict | None:
    risk = cfg["risk"]
    quote = quotes[coin]
    min_trade = float(risk.get("min_trade_aud", 1))
    aud_amt = min(aud_amt, float(risk["max_trade_aud"]))
    aud_amt = min(aud_amt, state["aud"] - float(risk["min_aud_reserve"]))
    aud_amt = min(aud_amt, float(risk["max_daily_spend_aud"]) - state["daily"]["spent_aud"])
    aud_amt = round(aud_amt, 2)
    if aud_amt < min_trade:
        return None

    eq = equity(state, quotes)
    pos_val = state["positions"][coin]["units"] * quote.bid + aud_amt
    cap = (risk.get("max_position_pct") or {}).get(coin)
    if cap is not None and eq > 0 and pos_val / eq > cap:
        return None

    fee = aud_amt * float(cfg["fee_rate"])
    net_into_coin = aud_amt - fee
    units = net_into_coin / quote.ask

    if client is not None:
        live = cfg.get("live") or {}
        if state["lifetime_spent_aud"] + aud_amt > float(live.get("max_lifetime_spend_aud", 0)):
            return None
        real_aud = client.balances().get("AUD", 0.0)
        if real_aud < aud_amt:
            print(f"live: skip buy {coin}, account AUD {real_aud:.2f} < {aud_amt:.2f}", file=sys.stderr)
            return None
        resp = client.buy_now_aud(coin, aud_amt)
        if resp.get("amount"):
            units = float(resp["amount"])

    pos = state["positions"][coin]
    state["aud"] -= aud_amt
    pos["units"] += units
    pos["cost_aud"] += aud_amt  # fee is part of cost basis
    state["daily"]["spent_aud"] += aud_amt
    state["lifetime_spent_aud"] += aud_amt
    row = {
        "ts": iso(),
        "mode": mode_of(cfg),
        "action": "BUY",
        "reason": reason,
        "coin": coin,
        "units": f"{units:.8f}",
        "price": f"{quote.ask:.8f}",
        "gross_aud": f"{aud_amt:.2f}",
        "fee_aud": f"{fee:.4f}",
        "net_aud": f"{net_into_coin:.2f}",
        "aud_after": f"{state['aud']:.2f}",
        "units_after": f"{pos['units']:.8f}",
        "avg_cost": f"{avg_cost(pos):.8f}",
        "realised_pnl": "",
    }
    log_trade(cfg, row)
    return row


def sell(cfg: dict, state: dict, coin: str, fraction: float, quote: Quote,
         reason: str, client: CoinSpotClient | None = None) -> dict | None:
    pos = state["positions"][coin]
    units = pos["units"] * min(1.0, max(0.0, fraction))
    if units <= 0:
        return None
    min_trade = float(cfg["risk"].get("min_trade_aud", 1))
    # If the leftover would be too small to ever sell, sell the lot.
    if (pos["units"] - units) * quote.bid < min_trade:
        units = pos["units"]
    if units * quote.bid < min_trade:
        return None

    if client is not None:
        # Never sell more than the bot bought, nor more than the account holds.
        units = min(units, client.balances().get(coin, 0.0))
        if units * quote.bid < min_trade:
            return None
        client.sell_now_units(coin, units)

    gross = units * quote.bid
    fee = gross * float(cfg["fee_rate"])
    net = gross - fee
    cost_removed = avg_cost(pos) * units
    pnl = net - cost_removed
    pos["units"] -= units
    pos["cost_aud"] = max(0.0, pos["cost_aud"] - cost_removed)
    if pos["units"] <= 1e-12:
        pos["units"] = 0.0
        pos["cost_aud"] = 0.0
    state["aud"] += net
    state["daily"]["sold_aud"] += net
    state["realised_pnl_aud"] += pnl
    row = {
        "ts": iso(),
        "mode": mode_of(cfg),
        "action": "SELL",
        "reason": reason,
        "coin": coin,
        "units": f"{units:.8f}",
        "price": f"{quote.bid:.8f}",
        "gross_aud": f"{gross:.2f}",
        "fee_aud": f"{fee:.4f}",
        "net_aud": f"{net:.2f}",
        "aud_after": f"{state['aud']:.2f}",
        "units_after": f"{pos['units']:.8f}",
        "avg_cost": f"{avg_cost(pos):.8f}",
        "realised_pnl": f"{pnl:.2f}",
    }
    log_trade(cfg, row)
    return row


def run_strategies(cfg: dict, state: dict, quotes: dict[str, Quote],
                   client: CoinSpotClient | None = None) -> list[dict]:
    fills: list[dict] = []

    # Sells first so a stop-loss frees cash before any new buy on the same tick.
    sl = cfg.get("stop_loss") or {}
    if sl.get("enabled"):
        loss = float(sl.get("loss_pct", 10)) / 100.0
        for coin, quote in quotes.items():
            pos = state["positions"][coin]
            ac = avg_cost(pos)
            if ac > 0 and quote.bid <= ac * (1 - loss):
                row = sell(cfg, state, coin, float(sl.get("sell_fraction", 1.0)), quote, f"sl_{loss:.0%}", client)
                if row:
                    state["last_sl"][coin] = iso()
                    fills.append(row)

    tp = cfg.get("take_profit") or {}
    if tp.get("enabled"):
        gain = float(tp.get("gain_pct", 12)) / 100.0
        frac = float(tp.get("sell_fraction", 0.25))
        cool = float(tp.get("cooldown_hours", 24))
        for coin, quote in quotes.items():
            pos = state["positions"][coin]
            ac = avg_cost(pos)
            if ac <= 0 or pos["units"] <= 0:
                continue
            if quote.bid >= ac * (1 + gain) and hours_since(state["last_tp"].get(coin)) >= cool:
                row = sell(cfg, state, coin, frac, quote, f"tp_{gain:.0%}", client)
                if row:
                    state["last_tp"][coin] = iso()
                    fills.append(row)

    sl_pause = float(sl.get("pause_buys_hours", 0)) if sl.get("enabled") else 0.0

    def buys_paused(coin: str) -> bool:
        return hours_since(state["last_sl"].get(coin)) < sl_pause

    dca = cfg.get("dca") or {}
    if dca.get("enabled"):
        every = float(dca.get("every_hours", 24))
        for coin, amt in (dca.get("amounts_aud") or {}).items():
            if coin not in quotes or not amt or buys_paused(coin):
                continue
            if hours_since(state["last_dca"].get(coin)) >= every:
                row = buy(cfg, state, coin, float(amt), quotes, "dca", client)
                if row:
                    state["last_dca"][coin] = iso()
                    fills.append(row)

    dip = cfg.get("dip") or {}
    if dip.get("enabled"):
        look = int(dip.get("lookback_hours", 72))
        drop = float(dip.get("drop_pct", 5)) / 100.0
        cool = float(dip.get("cooldown_hours", 12))
        for coin, amt in (dip.get("buy_aud") or {}).items():
            if coin not in quotes or not amt or buys_paused(coin):
                continue
            high = rolling_high(state, coin, look)
            if not high:
                continue
            if quotes[coin].last <= high * (1 - drop) and hours_since(state["last_dip"].get(coin)) >= cool:
                row = buy(cfg, state, coin, float(amt), quotes, f"dip_{drop:.0%}_from_{high:.2f}", client)
                if row:
                    state["last_dip"][coin] = iso()
                    fills.append(row)

    return fills


# ---------------------------------------------------------------- CLI


def print_status(cfg: dict, state: dict, quotes: dict[str, Quote]) -> None:
    eq = equity(state, quotes)
    start = float(cfg["starting_aud"])
    pnl = eq - start
    print(f"\n=== CoinSpot {mode_of(cfg)}  {iso()} ===")
    print(f"AUD cash     {state['aud']:,.2f}")
    print(f"Equity       {eq:,.2f}   PnL {pnl:+,.2f}  ({pnl/start*100:+.2f}%)")
    print(f"Realised     {state['realised_pnl_aud']:+,.2f}")
    print(f"Spent today  {state['daily']['spent_aud']:.2f} / {cfg['risk']['max_daily_spend_aud']}")
    print(f"{'COIN':<6} {'UNITS':>14} {'AVG COST':>12} {'BID':>12} {'ASK':>12} {'VALUE':>10} {'U/L':>8}")
    for coin in cfg["universe"]:
        pos = state["positions"][coin]
        q = quotes[coin]
        val = pos["units"] * q.bid
        ul = val - pos["cost_aud"]
        print(
            f"{coin:<6} {pos['units']:14.8f} {avg_cost(pos):12.2f} "
            f"{q.bid:12.2f} {q.ask:12.2f} {val:10.2f} {ul:8.2f}"
        )


def tick(cfg: dict, client: CoinSpotClient | None = None,
         quotes: dict[str, Quote] | None = None) -> tuple[dict, dict[str, Quote], list[dict]]:
    state = load_state(cfg)
    reset_daily(state)
    if quotes is None:
        quotes = {c: fetch_quote(c) for c in cfg["universe"]}
    look = int((cfg.get("dip") or {}).get("lookback_hours", 72))
    mark_ticks(state, quotes, look)
    try:
        fills = run_strategies(cfg, state, quotes, client)
    finally:
        # Save even on a mid-tick error so a live fill is never forgotten.
        state["equity_history"].append({"ts": iso(), "equity": round(equity(state, quotes), 2)})
        state["equity_history"] = state["equity_history"][-5000:]
        save_state(cfg, state)
    return state, quotes, fills


def client_for(cfg: dict) -> CoinSpotClient | None:
    if mode_of(cfg) != "live":
        return None
    reason = live_gate(cfg)
    if reason:
        raise SystemExit(f"Live mode locked: {reason}. Switch to mode: paper to keep testing.")
    return CoinSpotClient.from_env()


def print_fills(fills: list[dict]) -> None:
    if not fills:
        print("\nNo fills this tick.")
        return
    print("\nFills this tick:")
    for r in fills:
        pnl = f"  pnl {r['realised_pnl']}" if r.get("realised_pnl") else ""
        print(f"  {r['action']:4} {r['coin']} {r['units']} @ {r['price']}  ${r['gross_aud']}  {r['reason']}{pnl}")


def cmd_once() -> int:
    cfg = load_config()
    client = client_for(cfg)
    state, quotes, fills = tick(cfg, client)
    print_status(cfg, state, quotes)
    print_fills(fills)
    return 0


def cmd_run() -> int:
    cfg = load_config()
    start_mode = mode_of(cfg)
    client = client_for(cfg)
    interval = max(15, int(cfg.get("poll_seconds", 60)))
    print(f"{start_mode} loop every {interval}s. Ctrl-C to stop.")
    while True:
        try:
            cfg = load_config()
            if mode_of(cfg) != start_mode:
                print(f"mode changed to {mode_of(cfg)} — stopping. Restart the bot to switch.")
                return 2
            state, quotes, fills = tick(cfg, client)
            print_status(cfg, state, quotes)
            print_fills(fills)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print(f"tick error: {exc}", file=sys.stderr)
        time.sleep(interval)


def cmd_status() -> int:
    cfg = load_config()
    state = load_state(cfg)
    quotes = {c: fetch_quote(c) for c in cfg["universe"]}
    print_status(cfg, state, quotes)
    return 0


def cmd_reset() -> int:
    cfg = load_config()
    if mode_of(cfg) == "live":
        print("Refusing to reset the live ledger. Delete state_live.json by hand if you mean it.")
        return 2
    save_state(cfg, default_state(cfg))
    if log_path(cfg).exists():
        log_path(cfg).unlink()
    print(f"Paper book reset to ${float(cfg['starting_aud']):.2f} AUD and trade log cleared.")
    return 0


def cmd_check_keys() -> int:
    """Read-only call to prove the API keys work. Places no orders."""
    client = CoinSpotClient.from_env()
    bals = client.balances()
    print("Keys OK. Available balances:")
    for coin, amt in sorted(bals.items()):
        if amt:
            print(f"  {coin:<6} {amt}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="CoinSpot trading bot (paper first)")
    cmds = {"once": cmd_once, "run": cmd_run, "status": cmd_status, "reset": cmd_reset,
            "check-keys": cmd_check_keys}
    p.add_argument("command", choices=list(cmds), nargs="?", default="once")
    args = p.parse_args()
    return cmds[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
