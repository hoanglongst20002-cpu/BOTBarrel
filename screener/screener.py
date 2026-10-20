#!/usr/bin/env python3
"""
Momentum screener + 4h Trade Playbook decisions, built to run on a schedule (GitHub Actions).

Each run:
  1. Ranks the tickers in universe.txt by 6-month return (liquid names only).
  2. Builds regular-session 4h bars (09:30-13:30, 13:30-16:00 ET) for the top N and for your holdings,
     using only finished bars.
  3. Applies the playbook rules (long only) -> BUY / WATCH / WAIT / NO TRADE / AVOID / SKIP,
     and for holdings HOLD or SELL with the trailing stop.
  4. Writes docs/data/latest.json and docs/index.html (the dashboard, served by GitHub Pages).
  5. Sends a phone notification through ntfy when something actionable appears.
  6. Exits early, changing nothing, if no new 4h bar has finished since the last run.

Environment variables (GitHub repo secrets):
  NTFY_TOPIC            ntfy topic name for phone alerts (optional)
  ALPACA_KEY_ID         Alpaca market-data key (optional; used if Yahoo fails)
  ALPACA_SECRET_KEY
  HOLDINGS_JSON         optional, e.g. [{"ticker":"MRK","entry":145.20,"entry_date":"2026-10-20"}]
                        Kept in a secret so it never appears on the public page.
  FORCE=true            run even if no new bar finished
  SCREENER_PROVIDER     "yahoo" (default), "alpaca", or "fake" (offline test data)
"""
import json
import zlib
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

TZ = "America/New_York"
ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
DATA = DOCS / "data"
CFG = dict(top=20, min_price=10.0, min_dollar_vol=200e6, fast=20, slow=250, atr_n=14,
           m_trail=4.0, m_safe=6.0, earn_skip_sessions=2)


def log(*a):
    print(*a, flush=True)


def now_ny():
    return pd.Timestamp.now(tz=TZ)


def load_universe():
    out = []
    for line in (ROOT / "screener" / "universe.txt").read_text().splitlines():
        line = line.split("#", 1)[0]
        out += [t.strip().upper() for t in line.split() if t.strip()]
    return list(dict.fromkeys(out))


# ===================================================================== 4h bars
def to_4h(intra):
    """Regular-session intraday bars (any size dividing the session, NY tz) -> two 4h bars per day."""
    if intra is None or intra.empty:
        return pd.DataFrame()
    df = intra.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(TZ)
    mins = df.index.hour * 60 + df.index.minute
    df = df[(mins >= 570) & (mins < 960)]
    if df.empty:
        return pd.DataFrame()
    mins = df.index.hour * 60 + df.index.minute
    key = pd.Series(np.where(mins < 810, 0, 1), index=df.index)
    groups = df.groupby([df.index.date, key.values])
    out = pd.DataFrame({"Open": groups["Open"].first(), "High": groups["High"].max(),
                        "Low": groups["Low"].min(), "Close": groups["Close"].last()})
    starts = [pd.Timestamp(f"{d} {'09:30' if s == 0 else '13:30'}", tz=TZ) for d, s in out.index]
    out.index = pd.DatetimeIndex(starts)
    out = out.sort_index()
    # drop the last bar if it hasn't finished (bar 1 ends 13:30 ET, bar 2 ends 16:00 ET)
    if len(out):
        s = out.index[-1]
        end = s.normalize() + (pd.Timedelta(hours=13, minutes=30) if s.hour == 9 else pd.Timedelta(hours=16))
        if now_ny() < end:
            out = out.iloc[:-1]
    return out


# ================================================================ data providers
class Yahoo:
    name = "Yahoo Finance"

    @staticmethod
    def _split(df, tickers):
        out = {}
        if df is None or df.empty:
            return out
        if isinstance(df.columns, pd.MultiIndex):
            for tk in tickers:
                if tk in df.columns.get_level_values(0):
                    sub = df[tk].dropna(how="all")
                    if not sub.empty:
                        out[tk] = sub
        elif len(tickers) == 1:
            out[tickers[0]] = df.dropna(how="all")
        return out

    def daily(self, tickers):
        import yfinance as yf
        df = yf.download(tickers, period="1y", interval="1d", auto_adjust=True, group_by="ticker",
                         threads=True, progress=False)
        return self._split(df, tickers)

    def intraday(self, tickers):
        import yfinance as yf
        df = yf.download(tickers, period="1y", interval="60m", prepost=False, auto_adjust=True,
                         group_by="ticker", threads=True, progress=False)
        return self._split(df, tickers)

    def earnings(self, tk):
        try:
            import yfinance as yf
            cal = yf.Ticker(tk).calendar
            dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
            today = now_ny().date()
            fut = sorted(pd.Timestamp(d).date() for d in (dates or []) if pd.Timestamp(d).date() >= today)
            return fut[0].isoformat() if fut else None
        except Exception:
            return None


class Alpaca:
    """Free Alpaca market data. SIP data must be at least 15 minutes old on the free plan."""
    name = "Alpaca"
    URL = "https://data.alpaca.markets/v2/stocks/bars"

    def __init__(self):
        self.h = {"APCA-API-KEY-ID": os.environ["ALPACA_KEY_ID"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}

    def _bars(self, tickers, tf, days):
        import requests
        end = datetime.now(timezone.utc) - timedelta(minutes=16)
        start = end - timedelta(days=days)
        rows = {t: [] for t in tickers}
        for k in range(0, len(tickers), 50):
            chunk, token = tickers[k:k + 50], None
            while True:
                p = {"symbols": ",".join(chunk), "timeframe": tf, "start": start.isoformat(), "end": end.isoformat(),
                     "adjustment": "all", "feed": "sip", "limit": 10000}
                if token:
                    p["page_token"] = token
                r = requests.get(self.URL, headers=self.h, params=p, timeout=60)
                r.raise_for_status()
                j = r.json()
                for tk, bars in (j.get("bars") or {}).items():
                    rows[tk] += bars
                token = j.get("next_page_token")
                if not token:
                    break
        out = {}
        for tk, bars in rows.items():
            if bars:
                df = pd.DataFrame(bars)
                df.index = pd.to_datetime(df["t"], utc=True).dt.tz_convert(TZ)
                out[tk] = df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})[
                    ["Open", "High", "Low", "Close", "Volume"]]
        return out

    def daily(self, tickers):
        return self._bars(tickers, "1Day", 380)

    def intraday(self, tickers):
        return self._bars(tickers, "30Min", 380)

    def earnings(self, tk):
        return Yahoo().earnings(tk)


class Fake:
    """Deterministic synthetic prices for offline testing."""
    name = "simulated data"

    def _series(self, tk, idx, step_vol):
        rng = np.random.default_rng(zlib.crc32(tk.encode()))
        drift = rng.normal(0.0012, 0.0015) * step_vol / 0.02
        c = 100 * np.exp(np.cumsum(rng.normal(drift, step_vol, len(idx))))
        return pd.DataFrame({"Open": c * (1 + rng.normal(0, step_vol / 5, len(idx))), "High": c * (1 + step_vol),
                             "Low": c * (1 - step_vol), "Close": c, "Volume": rng.integers(2e6, 2e7, len(idx))}, index=idx)

    def daily(self, tickers):
        idx = pd.bdate_range(end=now_ny().normalize().tz_localize(None), periods=252)
        idx = pd.DatetimeIndex([pd.Timestamp(f"{d.date()} 16:00", tz=TZ) for d in idx])
        return {tk: self._series(tk, idx, 0.022) for tk in tickers}

    def intraday(self, tickers):
        days = pd.bdate_range(end=now_ny().normalize().tz_localize(None), periods=252)
        idx = pd.DatetimeIndex([pd.Timestamp(f"{d.date()} 09:30", tz=TZ) + pd.Timedelta(hours=h) for d in days for h in range(7)])
        return {tk: self._series(tk, idx, 0.008) for tk in tickers}

    def earnings(self, tk):
        return (now_ny() + pd.Timedelta(days=zlib.crc32(tk.encode()) % 40)).date().isoformat()


def get_provider():
    want = os.environ.get("SCREENER_PROVIDER", "yahoo").lower()
    if want == "fake":
        return Fake()
    if want == "alpaca" and os.environ.get("ALPACA_KEY_ID"):
        return Alpaca()
    return Yahoo()


def with_fallback(fn_name, provider, tickers):
    try:
        res = getattr(provider, fn_name)(tickers)
        if len(res) >= max(1, len(tickers) // 2):
            return res, provider
        log(f"[warn] {provider.name} returned {len(res)}/{len(tickers)} for {fn_name}")
    except Exception as ex:
        log(f"[warn] {provider.name} {fn_name} failed: {ex}")
    if not isinstance(provider, Alpaca) and os.environ.get("ALPACA_KEY_ID"):
        alp = Alpaca()
        log("[info] falling back to Alpaca")
        return getattr(alp, fn_name)(tickers), alp
    return {}, provider


# ===================================================================== rules
def indicators(b4):
    c, h, l = (b4[k].to_numpy(float) for k in ("Close", "High", "Low"))
    pc = np.r_[np.nan, c[:-1]]
    tr = np.nanmax(np.vstack([h - l, np.abs(h - pc), np.abs(l - pc)]), axis=0)
    return dict(c=c, s20=pd.Series(c).rolling(CFG["fast"]).mean().to_numpy(),
                s250=pd.Series(c).rolling(CFG["slow"]).mean().to_numpy(),
                atr=pd.Series(tr).rolling(CFG["atr_n"]).mean().to_numpy(), idx=b4.index)


def busdays_until(date_str):
    if not date_str:
        return None
    return int(np.busday_count(now_ny().date(), pd.Timestamp(date_str).date()))


def decide(b4, earn):
    if b4 is None or len(b4) < CFG["slow"] + 25:
        return {"decision": "NO DATA", "why": "Not enough 4h history."}
    x = indicators(b4)
    c, s20, s250, atr = x["c"], x["s20"], x["s250"], x["atr"]
    i = len(c) - 1
    up = c[i] > s250[i] and s250[i] > s250[i - 20]
    down = c[i] < s250[i] and s250[i] < s250[i - 20]
    dip_over = c[i - 1] <= s20[i - 1] and c[i] > s20[i]
    ext = (c[i] - s20[i]) / atr[i]
    if up and dip_over:
        d, why = "BUY", "The dip to SMA20 just ended in an uptrend. Buy at the next 4h bar's open."
    elif up and c[i] <= s20[i]:
        d, why = "WATCH", f"Dipping {abs(ext):.1f} ATR below SMA20 in an uptrend. Buy on a 4h close above ~{s20[i]:.2f}."
    elif up:
        d, why = "WAIT", f"{ext:.1f} ATR above SMA20 with no dip yet. Wait for a pullback toward ~{s20[i]:.2f}."
    elif down:
        d, why = "AVOID", "Downtrend: below a falling SMA250."
    else:
        d, why = "NO TRADE", f"Below SMA250 ({s250[i]:.2f}) while it still rises. Needs a close back above it and a fresh dip."
    bd = busdays_until(earn)
    if d in ("BUY", "WATCH") and bd is not None and 0 <= bd <= CFG["earn_skip_sessions"]:
        d, why = "SKIP", f"Setup, but earnings on {earn}. Re-check after the report."
    return {"decision": d, "why": why, "close": round(c[i], 2), "sma20": round(s20[i], 2), "sma250": round(s250[i], 2),
            "sma250_rising": bool(s250[i] > s250[i - 20]), "atr": round(atr[i], 2), "bar": x["idx"][-1].isoformat()}


def check_holding(h, b4):
    """HOLD or SELL for a position you own, using the 4x ATR trailing stop on closes and the SMA250 trend exit."""
    if b4 is None or len(b4) < CFG["slow"] + 25:
        return {**h, "decision": "NO DATA"}
    x = indicators(b4)
    c, s250, atr, idx = x["c"], x["s250"], x["atr"], x["idx"]
    since = pd.Timestamp(h.get("entry_date", "1900-01-01"), tz=TZ)
    peak = max([float(h["entry"])] + [v for t, v in zip(idx, c) if t >= since])
    i = len(c) - 1
    stop, safe = peak - CFG["m_trail"] * atr[i], peak - CFG["m_safe"] * atr[i]
    if c[i] < stop:
        d, why = "SELL", f"Close {c[i]:.2f} is below the trailing stop {stop:.2f}. Sell at the next bar's open."
    elif c[i] < s250[i]:
        d, why = "SELL", f"Close {c[i]:.2f} is below SMA250 {s250[i]:.2f}: trend over. Sell at the next bar's open."
    else:
        d, why = "HOLD", f"Trailing stop now {stop:.2f}; raise the broker safety stop to {safe:.2f} if it's lower."
    return {"ticker": h["ticker"], "decision": d, "why": why, "close": round(c[i], 2), "trail_stop": round(stop, 2),
            "safety_stop": round(safe, 2), "pnl_pct": round(100 * (c[i] / float(h["entry"]) - 1), 2)}


# ============================================================ notifications
def notify(title, body, priority="default", tags="chart_with_upwards_trend"):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        log(f"[notify skipped: no NTFY_TOPIC] {title} | {body}")
        return
    import requests
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), timeout=20,
                      headers={"Title": title, "Priority": priority, "Tags": tags,
                               "Click": os.environ.get("DASHBOARD_URL", "")})
    except Exception as ex:
        log(f"[warn] ntfy failed: {ex}")


# ===================================================================== main
def main():
    DATA.mkdir(parents=True, exist_ok=True)
    state_f = DATA / "state.json"
    state = json.loads(state_f.read_text()) if state_f.exists() else {}
    prov = get_provider()
    universe = load_universe()
    holdings = json.loads(os.environ.get("HOLDINGS_JSON") or "[]")
    log(f"provider {prov.name}, universe {len(universe)}, holdings {len(holdings)}")

    daily, prov_d = with_fallback("daily", prov, universe)
    rows = []
    for tk, d in daily.items():
        d = d.dropna(subset=["Close"])
        if len(d) < 130:
            continue
        c, v = d["Close"].astype(float), d["Volume"].astype(float)
        rows.append({"ticker": tk, "live": round(c.iloc[-1], 2), "ret_6m": c.iloc[-1] / c.iloc[-127] - 1,
                     "ret_1m": c.iloc[-1] / c.iloc[-22] - 1, "off_high": c.iloc[-1] / c.max() - 1,
                     "dollar_vol": float((c * v).iloc[-20:].mean())})
    rank = pd.DataFrame(rows)
    if rank.empty:
        log("[error] no daily data"); sys.exit(1)
    rank = rank[(rank.live >= CFG["min_price"]) & (rank.dollar_vol >= CFG["min_dollar_vol"])]
    rank = rank.sort_values("ret_6m", ascending=False).head(CFG["top"]).reset_index(drop=True)
    top = rank.ticker.tolist()

    need = list(dict.fromkeys(top + [h["ticker"].upper() for h in holdings]))
    intra, prov_i = with_fallback("intraday", prov_d, need)
    bars = {tk: to_4h(df) for tk, df in intra.items()}
    last_bar = max((b.index[-1] for b in bars.values() if len(b)), default=None)
    last_bar_s = last_bar.isoformat() if last_bar is not None else None
    if last_bar_s and state.get("last_bar") == last_bar_s and os.environ.get("FORCE", "").lower() != "true":
        log(f"No new 4h bar since {last_bar_s}; nothing to do."); return

    out_rows = []
    for k, r in rank.iterrows():
        earn = prov_i.earnings(r.ticker)
        d = decide(bars.get(r.ticker), earn)
        out_rows.append({"rank": k + 1, "ticker": r.ticker, "ret_6m": round(100 * r.ret_6m, 1),
                         "ret_1m": round(100 * r.ret_1m, 1), "live": r.live, "earnings": earn, **d})
    held = [check_holding({**h, "ticker": h["ticker"].upper()}, bars.get(h["ticker"].upper())) for h in holdings]

    # ---- notifications: only for changes worth acting on
    prev = state.get("decisions", {})
    buys = [r for r in out_rows if r["decision"] == "BUY" and prev.get(r["ticker"]) != "BUY"]
    sells = [h for h in held if h["decision"] == "SELL"]
    bar_label = pd.Timestamp(last_bar_s).strftime("%a %b %d, %I:%M %p ET") if last_bar_s else "?"
    if buys:
        notify(f"BUY signal: {', '.join(r['ticker'] for r in buys)}",
               "\n".join(f"{r['ticker']}: close {r['close']}, trailing stop {round(r['close'] - CFG['m_trail'] * r['atr'], 2)}, "
                         f"safety {round(r['close'] - CFG['m_safe'] * r['atr'], 2)}" for r in buys) + f"\nBar: {bar_label}",
               priority="high")
    if sells:
        notify(f"SELL: {', '.join(h['ticker'] for h in sells)}", "\n".join(f"{h['ticker']}: {h['why']}" for h in sells),
               priority="high", tags="warning")
    if os.environ.get("NOTIFY_SUMMARY", "").lower() == "true" and not (buys or sells):
        cnt = pd.Series([r["decision"] for r in out_rows]).value_counts().to_dict()
        notify("Screener updated", ", ".join(f"{k} {v}" for k, v in cnt.items()) + f" · bar {bar_label}", priority="low", tags="memo")

    payload = {"generated": now_ny().isoformat(), "last_bar": last_bar_s, "provider": prov_i.name,
               "universe_size": len(universe), "rules": CFG, "rows": out_rows}
    (DATA / "latest.json").write_text(json.dumps(payload, indent=1, default=str))
    state = {"last_bar": last_bar_s, "decisions": {r["ticker"]: r["decision"] for r in out_rows}}
    state_f.write_text(json.dumps(state, indent=1))
    render(payload)
    # holdings are only sent to your phone, never logged (public repos have public logs) or published
    log(f"Done: {len(out_rows)} rows, last bar {last_bar_s}, buys {len(buys)}, sells {len(sells)}")


def render(payload):
    tpl = (ROOT / "screener" / "template.html").read_text()
    html = tpl.replace("/*__DATA__*/null", json.dumps(payload, default=str))
    (DOCS / "index.html").write_text(html)


if __name__ == "__main__":
    main()
