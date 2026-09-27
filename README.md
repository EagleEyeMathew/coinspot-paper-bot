# CoinSpot paper bot

Uses **live CoinSpot AUD prices**. Fills are simulated.

- Buy at the public **ask / buyprice**
- Sell at the public **bid / sellprice**
- Extra 0.1% fee on top of the spread
- Daily spend cap, per-trade cap, cash reserve, position caps
- Live mode is a hard stop until signed API keys are wired

Repo: https://github.com/EagleEyeMathew/coinspot-paper-bot

## Default rules (edit `config.yaml`)

1. **DCA** — $50 BTC and $30 ETH per 24 hours
2. **Dip** — extra buy if price is 5% under the 72h high
3. **Take profit** — sell 25% of a coin if bid is 12% above average cost

Starting paper cash: **$10,000 AUD**.

## Run

```bash
pip install -r requirements.txt
python bot.py once      # one tick
python bot.py run       # loop
python bot.py status    # prices + book, no new decisions
python bot.py reset     # wipe paper book
```

`state.json` is the paper ledger. `trades.csv` is the fill log. Both are gitignored.

## Going live later

Do not flip `mode: live` yet. That path raises on purpose.

Live will need:

- CoinSpot API key with trade permission (prefer Agentic + tight daily limits)
- Signed POST to `/api/v2` buy-now / sell-now
- Same risk gates as paper
- A stretch of paper fills you are willing to have run with real money
