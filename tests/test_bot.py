"""Offline tests: fake quotes, temp ledger, no network."""

import copy
from pathlib import Path

import pytest

import bot

CFG = bot.load_config(Path(__file__).resolve().parents[1] / "config.yaml")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "PATHS", {
        "paper": (tmp_path / "state.json", tmp_path / "trades.csv"),
        "live": (tmp_path / "state_live.json", tmp_path / "trades_live.csv"),
    })
    return copy.deepcopy(CFG)


def q(btc, eth=3000.0):
    return {
        "BTC": bot.Quote("BTC", bid=btc * 0.995, ask=btc * 1.005, last=btc),
        "ETH": bot.Quote("ETH", bid=eth * 0.995, ask=eth * 1.005, last=eth),
    }


def test_starts_with_50_paper_dollars(cfg):
    assert bot.load_state(cfg)["aud"] == 50.0


def test_first_tick_dca_buys_both(cfg):
    state, _, fills = bot.tick(cfg, quotes=q(100000))
    assert {f["coin"] for f in fills} == {"BTC", "ETH"}
    assert state["aud"] == pytest.approx(46.0)
    assert (bot.log_path(cfg)).exists()


def test_take_profit_then_stop_loss_cycle(cfg):
    cfg["dca"]["amounts_aud"]["BTC"] = 8
    bot.tick(cfg, quotes=q(100000))
    state, _, fills = bot.tick(cfg, quotes=q(110000))
    sells = [f for f in fills if f["action"] == "SELL"]
    assert sells and sells[0]["reason"].startswith("tp_")
    assert float(sells[0]["realised_pnl"]) > 0

    state, _, fills = bot.tick(cfg, quotes=q(80000))
    sl = [f for f in fills if f["reason"].startswith("sl_")]
    assert sl and state["positions"]["BTC"]["units"] == 0
    assert float(sl[0]["realised_pnl"]) < 0
    # buys on BTC paused after a stop-loss
    assert not [f for f in fills if f["action"] == "BUY" and f["coin"] == "BTC"]


def test_never_spends_below_reserve(cfg):
    cfg["dca"]["amounts_aud"] = {"BTC": 100, "ETH": 100}
    cfg["risk"]["max_daily_spend_aud"] = 1000
    cfg["risk"]["max_trade_aud"] = 1000
    cfg["risk"]["max_position_pct"] = {}
    state, _, _ = bot.tick(cfg, quotes=q(100000))
    assert state["aud"] >= cfg["risk"]["min_aud_reserve"] - 1e-9


def test_daily_cap(cfg):
    cfg["dca"]["every_hours"] = 0
    for _ in range(10):
        state, _, _ = bot.tick(cfg, quotes=q(100000))
    assert state["daily"]["spent_aud"] <= cfg["risk"]["max_daily_spend_aud"] + 1e-9


def test_live_locked_by_default(cfg, monkeypatch):
    cfg["mode"] = "live"
    monkeypatch.delenv("COINSPOT_API_KEY", raising=False)
    assert bot.live_gate(cfg)
    with pytest.raises(SystemExit):
        bot.client_for(cfg)


def test_live_needs_keys_even_with_phrase(cfg, monkeypatch):
    cfg["mode"] = "live"
    cfg["live"]["confirm"] = bot.LIVE_CONFIRM_PHRASE
    monkeypatch.delenv("COINSPOT_API_KEY", raising=False)
    monkeypatch.delenv("COINSPOT_API_SECRET", raising=False)
    assert "env" in bot.live_gate(cfg)


class FakeClient:
    def __init__(self):
        self.orders = []
        self.bal = {"AUD": 1000.0, "BTC": 0.0, "ETH": 0.0}

    def balances(self):
        return dict(self.bal)

    def buy_now_aud(self, coin, aud):
        self.orders.append(("buy", coin, aud))
        self.bal[coin] += aud / 100000
        return {"status": "ok"}

    def sell_now_units(self, coin, units):
        self.orders.append(("sell", coin, units))
        return {"status": "ok"}


def test_live_lifetime_cap(cfg):
    cfg["mode"] = "live"
    cfg["live"]["max_lifetime_spend_aud"] = 5
    cfg["dca"]["every_hours"] = 0
    client = FakeClient()
    for _ in range(5):
        state, _, _ = bot.tick(cfg, client=client, quotes=q(100000))
    assert sum(a for kind, _, a in client.orders if kind == "buy") <= 5
    assert bot.state_path(cfg).name == "state_live.json"


def test_signing_is_hmac_sha512(monkeypatch):
    import hashlib, hmac, json
    sent = {}

    class R:
        def json(self):
            return {"status": "ok", "balances": [{"AUD": {"available": 12.5}}]}

    def fake_post(url, data, headers, timeout):
        sent.update(url=url, data=data, headers=headers)
        return R()

    monkeypatch.setattr(bot.requests, "post", fake_post)
    c = bot.CoinSpotClient("k", "s")
    assert c.balances() == {"AUD": 12.5}
    assert sent["url"].endswith("/api/v2/ro/my/balances")
    assert "nonce" in json.loads(sent["data"])
    assert sent["headers"]["sign"] == hmac.new(b"s", sent["data"].encode(), hashlib.sha512).hexdigest()
