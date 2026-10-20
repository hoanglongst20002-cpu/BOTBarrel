# Momentum Leaders Screener

A free, self-updating version of the 4h Trade Playbook screener.

- **GitHub Actions** runs `screener/screener.py` every 15 minutes during US market hours, 2 minutes after each 15-minute bar closes.
- Once a day it ranks the stocks in `screener/universe.txt` by 6-month gain and keeps the top 20 liquid names. On every run it applies the playbook rules to each one: BUY, WATCH, WAIT, NO TRADE, AVOID or SKIP.
- **4h is the strategy.** The 1h and 15m states use the same rules on faster bars and are shown as context only. The "15m vs VWAP" check is context too: in a backtest of 417 4h BUY signals it made no measurable difference.
- **Action history:** every run is saved in `docs/data/history/<date>.json` (last 10 trading days). The dashboard's history viewer shows how each stock's 4h, 1h and 15m states changed through the day.
- **GitHub Pages** serves the dashboard from `docs/index.html`, which the workflow regenerates on each update.
- **ntfy** sends a phone notification on a new BUY, or a SELL for something you hold. It stays quiet otherwise.
- If no new 4h bar has finished (weekends, holidays, duplicate runs), the job exits without changing anything.

Long only, because Wealthsimple can't short. These are mechanical rules from a learning project, not financial advice.

## Setup (about 15 minutes)

1. **Create a repository.** Use a public repo: GitHub Pages is free for public repos, and the page only shows public market data. Upload everything in this folder, keeping the `.github/workflows/screener.yml` path.
2. **Let the workflow push.** Go to *Settings → Actions → General → Workflow permissions* and choose **Read and write permissions**.
3. **Turn on the page.** Go to *Settings → Pages*, set Source to **Deploy from a branch**, Branch to **main**, and folder to **/docs**. The page will be at `https://<your-user>.github.io/<repo>/`.
4. **Phone alerts (optional).** Install the free **ntfy** app (iOS or Android) and subscribe to a topic with a long, hard-to-guess name, for example `lngng-screener-7f3k9q`. Anyone who knows the name can read it.
   Add it in *Settings → Secrets and variables → Actions → New repository secret* as `NTFY_TOPIC`.
5. **Run it once.** Go to *Actions → screener → Run workflow*. When it finishes, open the Pages link.

### Optional settings

| Name | Where | What it does |
|---|---|---|
| `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY` | Secrets | Free Alpaca market-data keys. Used automatically if Yahoo fails or blocks GitHub's servers. |
| `HOLDINGS_JSON` | Secret | Positions you hold, to get SELL alerts. Example: `[{"ticker":"MRK","entry":145.20,"entry_date":"2026-10-20"}]`. Only used for notifications, never shown on the page or printed in the logs. Update it when you buy or sell. |
| `DASHBOARD_URL` | Variable | Your Pages link. Tapping a notification opens it. |
| `NOTIFY_SUMMARY` | Variable | `true` sends a short summary after every update, even with no BUY or SELL. |
| `SCREENER_PROVIDER` | Variable | `yahoo` (default) or `alpaca`. |

### Changing the rules or the list

- **Stocks:** edit `screener/universe.txt`.
- **Rule settings:** edit `CFG` at the top of `screener/screener.py`: number of names, price and volume filters, SMA lengths, ATR multiples, earnings window.

## Run it on your own computer

```bash
pip install -r requirements.txt
python screener/screener.py                         # real data
SCREENER_PROVIDER=fake python screener/screener.py  # offline test data (PowerShell: $env:SCREENER_PROVIDER="fake")
```
Then open `docs/index.html`.

## Things to know

- GitHub's scheduled runs can start 5–30 minutes late at busy times, so some 15-minute runs may be skipped or merged. The 4h decisions don't depend on run timing.
- About 26 runs and commits per trading day. Public repos don't pay for Actions minutes. The history folder keeps only the last 10 trading days.
- The history viewer loads files from your Pages site. It won't work if you open `docs/index.html` straight from disk.
- GitHub disables scheduled workflows in a repo with no activity for 60 days. The workflow's own commits normally count as activity. If it stops, re-enable it under *Actions*.
- Yahoo sometimes rate-limits cloud servers. Add the Alpaca keys if runs fail or show few names.
- Earnings dates come from Yahoo and can be estimates.
