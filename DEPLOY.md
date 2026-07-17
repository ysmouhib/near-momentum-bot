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

CI (`.github/workflows/ci.yml`) runs ruff, the 32-test suite, and the JS/Python
engine parity check on Python 3.10–3.12 automatically on the first push.

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
gain/loss in % and USDT; plots land in `reports/`.

## 4. (Optional) paper trading on the testnet

```bash
cp .env.example .env   # paste keys from https://testnet.binance.vision
export $(cat .env | xargs)
near-bot test-connection
near-bot paper
```

The executor fetches ~2 weeks of 1m klines per cycle (paginated — the v2 warmup
at 60m needs them), evaluates the score on the last closed hour, and manages one
volatility-targeted long position with the same rules as the backtester.
