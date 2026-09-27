#!/usr/bin/env python3
"""CoinSpot paper bot — live prices, simulated fills. Live trading is stubbed off."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests
import yaml

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
LOG_PATH = ROOT / "trades.csv"
CONFIG_PATH = ROOT / "config.yaml"

PUBLIC_LATEST = "https://www.coinspot.com.au/pubapi/v2/latest/{coin}"
PUBLIC_BUY = "https://www.coinspot.com.au/pubapi/v2/buyprice/{coin}"
PUBLIC_SELL = "https://www.coinspot.com.au/pubapi/v2/sellprice/{coin}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config() -> dict:
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f)


def default_state(cfg: dict) -> dict:
    return {
        "mode": "paper",
        "aud": float(cfg["starting_aud"]),
        "positions": {
            c: {"units": 0.0, "cost_aud": 0.0} for c in cfg["universe"]
        },
        "daily": {"date": "", "spent_aud": 0.0, "sold_aud": 0.0},
        "last_dca": {},
        "last_dip": {},
        "last_tp": {},
        "ticks": {c: [] for c in cfg["universe"]},
        "equity_history": [],
    }


def load_state(cfg: dict) -> dict:
    if not STATE_PATH.exists():
        return default_state(cfg)
    with STATE_PATH.open() as f:
        state = json.load(f)
    for c in cfg["universe"]:
        state["positions"].setdefault(c, {"units": 0.0, "cost_aud": 0.0})
        state["ticks"].setdefault(c, [])
    return state


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_PATH)


def log_trade(row: dict) -> None:
    new = not LOG_PATH.exists()
    fields = [
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
    ]
    if new:
        LOG_PATH.write_text(",".join(fields) + "\n")
    line = ",".join(str(row.get(k, "")) for k in fields) + "\n"
    with LOG_PATH.open("a") as f:
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


def reset_daily(state: dict) -> None:
    today = utcnow().strftime("%Y-%m-%d")
    if state["daily"].get("date") != today:
        state["daily"] = {"date": today, "spent_aud": 0.0, "sold_aud": 0.0}


def mark_ticks(state: dict, quotes: dict[str, Quote], lookback_hours: int) -> None:
    cutoff = utcnow() - timedelta(hours=lookback_hours + 1)
    cutoff_s = iso(cutoff)
    for coin, q in quotes.items():
        ticks = state["ticks"].setdefault(coin, [])
        ticks.append({"ts": iso(), "bid": q.bid, "ask": q.ask, "last": q.last})
        state["ticks"][coin] = [t for t in ticks if t["ts"] >= cutoff_s][-2000:]


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


def paper_buy(cfg: dict, state: dict, coin: str, aud_amt: float, quote: Quote, reason: str) -> dict | None:
    risk = cfg["risk"]
    aud_amt = min(aud_amt, risk["max_trade_aud"])
    if aud_amt < 10:
        return None
    if state["aud"] - aud_amt < risk["min_aud_reserve"]:
        return None
    if state["daily"]["spent_aud"] + aud_amt > risk["max_daily_spend_aud"]:
        return None

    eq = equity(state, {coin: quote})
    pos_val = state["positions"][coin]["units"] * quote.bid + aud_amt
    cap = risk.get("max_position_pct", {}).get(coin)
    if cap is not None and eq > 0 and pos_val / eq > cap:
        return None

    fee = aud_amt * float(cfg["fee_rate"])
    spend = aud_amt
    net_into_coin = spend - fee
    units = net_into_coin / quote.ask
    state["aud"] -= spend
    state["positions"][coin]["units"] += units
    state["positions"][coin]["cost_aud"] += net_into_coin
    state["daily"]["spent_aud"] += spend
    row = {
        "ts": iso(),
        "mode": "paper",
        "action": "BUY",
        "reason": reason,
        "coin": coin,
        "units": f"{units:.8f}",
        "price": f"{quote.ask:.8f}",
        "gross_aud": f"{spend:.2f}",
        "fee_aud": f"{fee:.4f}",
        "net_aud": f"{net_into_coin:.2f}",
        "aud_after": f"{state['aud']:.2f}",
        "units_after": f"{state['positions'][coin]['units']:.8f}",
        "avg_cost": f"{avg_cost(state['positions'][coin]):.8f}",
    }
    log_trade(row)
    return row


def paper_sell(cfg: dict, state: dict, coin: str, fraction: float, quote: Quote, reason: str) -> dict | None:
    pos = state["positions"][coin]
    units = pos["units"] * fraction
    if units <= 0:
        return None
    gross = units * quote.bid
    if gross < 10:
        return None
    fee = gross * float(cfg["fee_rate"])
    net = gross - fee
    cost_removed = avg_cost(pos) * units
    pos["units"] -= units
    pos["cost_aud"] = max(0.0, pos["cost_aud"] - cost_removed)
    if pos["units"] <= 1e-12:
        pos["units"] = 0.0
        pos["cost_aud"] = 0.0
    state["aud"] += net
    state["daily"]["sold_aud"] += net
    row = {
        "ts": iso(),
        "mode": "paper",
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
    }
    log_trade(row)
    return row


def live_execute(*_args: Any, **_kwargs: Any) -> None:
    raise RuntimeError(
        "Live trading is locked. Keep mode: paper until the paper log looks right, "
        "then we wire signed /buy/now and /sell/now with key limits."
    )


def run_strategies(cfg: dict, state: dict, quotes: dict[str, Quote]) -> list[dict]:
    fills: list[dict] = []
    if cfg.get("mode") == "live":
        live_execute()

    dca = cfg.get("dca") or {}
    if dca.get("enabled"):
        every = float(dca.get("every_hours", 24))
        for coin, amt in (dca.get("amounts_aud") or {}).items():
            if coin not in quotes or not amt:
                continue
            if hours_since(state["last_dca"].get(coin)) >= every:
                row = paper_buy(cfg, state, coin, float(amt), quotes[coin], "dca")
                if row:
                    state["last_dca"][coin] = iso()
                    fills.append(row)

    dip = cfg.get("dip") or {}
    if dip.get("enabled"):
        look = int(dip.get("lookback_hours", 72))
        drop = float(dip.get("drop_pct", 5)) / 100.0
        cool = float(dip.get("cooldown_hours", 12))
        for coin, amt in (dip.get("buy_aud") or {}).items():
            if coin not in quotes or not amt:
                continue
            high = rolling_high(state, coin, look)
            if not high:
                continue
            if quotes[coin].last <= high * (1 - drop) and hours_since(state["last_dip"].get(coin)) >= cool:
                row = paper_buy(cfg, state, coin, float(amt), quotes[coin], f"dip_{drop:.0%}_from_{high:.2f}")
                if row:
                    state["last_dip"][coin] = iso()
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
                row = paper_sell(cfg, state, coin, frac, quote, f"tp_{gain:.0%}")
                if row:
                    state["last_tp"][coin] = iso()
                    fills.append(row)
    return fills


def print_status(cfg: dict, state: dict, quotes: dict[str, Quote]) -> None:
    eq = equity(state, quotes)
    start = float(cfg["starting_aud"])
    pnl = eq - start
    print(f"\n=== CoinSpot paper  {iso()}  mode={cfg.get('mode')} ===")
    print(f"AUD cash     {state['aud']:,.2f}")
    print(f"Equity       {eq:,.2f}   PnL {pnl:+,.2f}  ({pnl/start*100:+.2f}%)")
    print(f"Spent today  {state['daily']['spent_aud']:.2f} / {cfg['risk']['max_daily_spend_aud']}")
    print(f"{'COIN':<6} {'UNITS':>14} {'AVG COST':>12} {'BID':>12} {'ASK':>12} {'VALUE':>12} {'U/L':>10}")
    for coin in cfg["universe"]:
        pos = state["positions"][coin]
        q = quotes[coin]
        val = pos["units"] * q.bid
        ul = val - pos["cost_aud"]
        print(
            f"{coin:<6} {pos['units']:14.8f} {avg_cost(pos):12.2f} "
            f"{q.bid:12.2f} {q.ask:12.2f} {val:12.2f} {ul:10.2f}"
        )


def tick(cfg: dict) -> tuple[dict, dict[str, Quote], list[dict]]:
    state = load_state(cfg)
    reset_daily(state)
    quotes = {c: fetch_quote(c) for c in cfg["universe"]}
    look = int((cfg.get("dip") or {}).get("lookback_hours", 72))
    mark_ticks(state, quotes, look)
    fills = run_strategies(cfg, state, quotes)
    state["equity_history"].append({"ts": iso(), "equity": round(equity(state, quotes), 2)})
    state["equity_history"] = state["equity_history"][-5000:]
    save_state(state)
    return state, quotes, fills


def cmd_once() -> int:
    cfg = load_config()
    if cfg.get("mode") == "live":
        print("Refusing to run: config mode is live. Set mode: paper.")
        return 2
    state, quotes, fills = tick(cfg)
    print_status(cfg, state, quotes)
    if fills:
        print("\nFills this tick:")
        for r in fills:
            print(f"  {r['action']:4} {r['coin']} {r['units']} @ {r['price']}  {r['reason']}")
    else:
        print("\nNo fills this tick.")
    return 0


def cmd_run() -> int:
    cfg = load_config()
    if cfg.get("mode") == "live":
        print("Refusing to run: config mode is live. Set mode: paper.")
        return 2
    interval = max(15, int(cfg.get("poll_seconds", 60)))
    print(f"Paper loop every {interval}s. Ctrl-C to stop.")
    while True:
        try:
            cfg = load_config()
            if cfg.get("mode") == "live":
                print("mode flipped to live — stopping. Live is not wired yet.")
                return 2
            state, quotes, fills = tick(cfg)
            print_status(cfg, state, quotes)
            if fills:
                for r in fills:
                    print(f"  FILL {r['action']} {r['coin']} {r['units']} @ {r['price']} ({r['reason']})")
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
    save_state(default_state(cfg))
    if LOG_PATH.exists():
        LOG_PATH.unlink()
    print("Paper state and trade log cleared.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="CoinSpot paper trading bot")
    p.add_argument("command", choices=["once", "run", "status", "reset"], nargs="?", default="once")
    args = p.parse_args()
    return {"once": cmd_once, "run": cmd_run, "status": cmd_status, "reset": cmd_reset}[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
