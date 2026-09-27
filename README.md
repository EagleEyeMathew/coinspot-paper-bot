# CoinSpot trading bot

Automatic buy/sell bot for CoinSpot (AUD). Starts in **paper mode with $50**:
real live CoinSpot prices, simulated fills. Once the paper results look right,
the same rules can run with real money (locked until you unlock it, see below).

## What it does each tick (default every 60s)

| Rule | Action (defaults in `config.yaml`) |
|---|---|
| **Stop-loss** | Sell the whole coin if bid is 10% under your average cost, then pause buying it for 24h |
| **Take-profit** | Sell 50% of a coin if bid is 6% over average cost (6h cooldown) |
| **DCA** | Buy $2.50 BTC + $1.50 ETH every 24h |
| **Dip buy** | Buy $5 BTC / $4 ETH if price is 3% under its 24h high (6h cooldown) |

Risk limits: min order $1, max $10 per trade, max $12 spent per day, always keep
$5 cash, BTC ≤ 70% and ETH ≤ 50% of bot equity.

Paper fills buy at the CoinSpot **ask**, sell at the **bid**, and charge a 1%
fee each way (CoinSpot's instant buy/sell rate), so paper results should not look
better than real ones.

## Run (on your own computer)

```bash
pip install -r requirements.txt
python bot.py reset     # fresh $50 paper book
python bot.py run       # leave running; Ctrl-C to stop
python bot.py status    # prices + current book
python bot.py once      # single tick
```

- `trades.csv` records every paper fill, including realised profit/loss on each sell
- `state.json` is the paper ledger (cash, positions, cooldowns)

The bot only buys and sells while `run` is running, so leave it running on a
computer that stays on for a week or two of testing.

Tune the rules in `config.yaml`. The bot reloads it every tick.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Tests are offline: they use fake prices and a temporary ledger.

## Going live (later, after paper testing)

Live mode has three separate locks, and all three must be open:

1. **API key**: in CoinSpot → Settings → API, create a key with trade access.
   Use the tightest limits CoinSpot offers. Put the key in env vars, **never** in
   `config.yaml`:
   ```bash
   export COINSPOT_API_KEY=...
   export COINSPOT_API_SECRET=...
   python bot.py check-keys     # read-only balance check, places no orders
   ```
2. **Confirm phrase**: set `live.confirm: "I understand this trades real money"`.
3. **Spend cap**: `live.max_lifetime_spend_aud` (default $50) is the most the bot
   will ever spend buying in live mode.

Then set `mode: live` and run `python bot.py run`.

Live mode keeps its own ledger (`state_live.json`, `trades_live.csv`). It only
trades the coins it bought itself, and it checks your real account balance
before every order. Anything else you hold is left alone.

> The live order code (signed `/api/v2/my/buy/now`, `/my/sell/now`,
> `/ro/my/balances`) follows CoinSpot's v2 API but has **not been run against the
> real API yet**. Run `check-keys` first, then do one tiny live trade by hand-watching
> before leaving it unattended.
