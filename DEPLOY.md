# Deploying

Three deliverables: the GitHub repo, the GitHub Pages site (project page +
browser simulator), and a running paper-trading instance.

## 1. Push the repo

```bash
unzip near-momentum-bot-v2.0.0.zip && cd near-momentum-bot
git init -b main
git add -A
git commit -m "near-momentum-bot v2.0.0: score-ensemble trend engine"

# with GitHub CLI:
gh repo create ysmouhib/near-momentum-bot --public --source=. --push
# or manually: create an empty repo on github.com, then
#   git remote add origin git@github.com:ysmouhib/near-momentum-bot.git
#   git push -u origin main
```

CI (`.github/workflows/ci.yml`) runs ruff (pinned to 0.17.1), the 123-test suite,
and the JS/Python engine parity check on Python 3.10–3.12 automatically on the
first push.

## 2. Enable the website

Repo **Settings → Pages** → Source: *Deploy from a branch* → Branch: `main`,
folder **`/docs`** → Save. After a minute or two:

- `https://<user>.github.io/near-momentum-bot/` — project page
- `https://<user>.github.io/near-momentum-bot/simulator.html` — the simulator

Verify by opening the simulator, fetching 30 days of NEARUSDT and pressing
*Run backtest*: an equity curve and trade table should appear within seconds.
The data fetch happens in the **visitor's browser** (Binance public API), so it
works from any region where that API is reachable; elsewhere the CSV-upload
fallback applies. GitHub Pages only serves the static files.

## 3. Real 90-day numbers from a terminal

```bash
pip install -e ".[dev,plot]"
python scripts/simulate_90d.py --symbol NEARUSDT --days 90 --capital 1000 --plot
```

Downloads real 1m data from Binance's public dumps (no API key, no geo-block),
then prints the fixed-config backtest **and** the walk-forward out-of-sample
gain/loss in % and USDT; plots land in `reports/`. This legacy script walks back
from *today*, so its dataset changes daily — for reproducible numbers use a fixed
window and the validation battery:

```bash
python scripts/download_data.py --symbols NEARUSDT ETHUSDT SOLUSDT BTCUSDT \
    --start 2023-01-01 --end 2026-01-01
near-bot validate --start 2023-01-01 --end 2026-01-01   # -> reports/validate.md
```

## 4. (Optional) paper trading on the testnet

```bash
cp .env.example .env   # paste keys from https://testnet.binance.vision
export $(cat .env | xargs)
near-bot test-connection
near-bot arm --note "why a directional move is plausible now"   # required: state starts DISARMED
near-bot paper
```

The executor fetches ~2 weeks of 1m klines per cycle (paginated — the v2 warmup
at 60m needs them), evaluates the score on the last closed hour, and manages one
long position with the same rules as the backtester. Paper and live always run
with the **risk overlay**: entries need an armed state, size comes from the
risk-per-trade budget, and the kill-switch ladder is evaluated every cycle
(fail-closed on errors). Use `near-bot status` before each arm/disarm call and
`near-bot journal report` to see how those calls scored.

**Back up `state/`.** `state/journal.csv` (every decision, append-only) and
`state/overlay_state.json` (HWM, kill-switch tier, cooldown, open position — used
to reconcile against exchange balances after a restart) are gitignored on
purpose. Losing the journal resets the human's measured track record to zero.
