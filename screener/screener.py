#!/usr/bin/env python3
"""
Momentum screener + 4h Trade Playbook decisions, built to run on a schedule (GitHub Actions).

Every run (every 15 minutes during market hours):
  1. Ranks the tickers in universe.txt by 6-month return, once per trading day (cached in state.json).
  2. Builds finished regular-session bars for the top N and your holdings:
       4h  (09:30-13:30, 13:30-16:00 ET)  -> the tested playbook decision and the only source of alerts
       1h and 15m                          -> the same rules on faster bars, shown as CONTEXT ONLY
  3. Checks the 15-minute entry rule under test: is the last finished 15-min close above session VWAP?
  4. Appends the run to docs/data/history/<date>.json so the dashboard can show how states changed.
  5. Writes docs/data/latest.json and docs/index.html (served by GitHub Pages).
  6. Sends ntfy alerts only when a new 4h bar finishes: a new BUY, or SELL for a holding.
  7. Exits without changes if no new 15-minute bar has finished since the last run.

Environment variables (GitHub repo secrets / variables):
  NTFY_TOPIC, ALPACA_KEY_ID, ALPACA_SECRET_KEY, HOLDINGS_JSON, DASHBOARD_URL, NOTIFY_SUMMARY,
  SCREENER_PROVIDER ("yahoo" default, "alpaca", "fake" for offline tests), FORCE=true to rebuild anyway.
"""
import json
import os
import sys
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

TZ = "America/New_York"
ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
DATA = DOCS / "data"
HIST = DATA / "history"
CFG = dict(top=20, min_price=10.0, min_dollar_vol=200e6, fast=20, slow=250, atr_n=14,
           m_trail=4.0, m_safe=6.0, earn_skip_sessions=2, history_days=10)


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


# ===================================================================== bars
def _rth(df):
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(TZ)
    m = df.index.hour * 60 + df.index.minute
    return df[(m >= 570) & (m < 960)]


def finished(df, minutes):
    """Drop the last bar if it hasn't closed yet (bars end at start + size, capped at 16:00 ET)."""
    if df is None or df.empty:
        return df
    s = df.index[-1]
    end = min(s + pd.Timedelta(minutes=minutes), s.normalize() + pd.Timedelta(hours=16))
    return df.iloc[:-1] if now_ny() < end else df


def to_4h(intra):
    """Regular-session intraday bars -> two 4h bars per day (09:30-13:30, 13:30-16:00 ET), finished only."""
    df = _rth(intra)
    if df.empty:
        return df
    m = df.index.hour * 60 + df.index.minute
    key = np.where(m < 810, 0, 1)
    g = df.groupby([df.index.date, key])
    out = pd.DataFrame({"Open": g["Open"].first(), "High": g["High"].max(), "Low": g["Low"].min(),
                        "Close": g["Close"].last(), "Volume": g["Volume"].sum()})
    out.index = pd.DatetimeIndex([pd.Timestamp(f"{d} {'09:30' if s == 0 else '13:30'}", tz=TZ) for d, s in out.index])
    out = out.sort_index()
    if len(out):
        s = out.index[-1]
        end = s.normalize() + (pd.Timedelta(hours=13, minutes=30) if s.hour == 9 else pd.Timedelta(hours=16))
        if now_ny() < end:
            out = out.iloc[:-1]
    return out


def resample_rth(intra, minutes):
    """Regular-session bars of `minutes` length anchored at 09:30, finished only."""
    df = _rth(intra)
    if df.empty:
        return df
    out = df.resample(f"{minutes}min", origin=pd.Timestamp("2000-01-03 09:30", tz=TZ), label="left", closed="left").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna(subset=["Open"])
    return finished(_rth(out), minutes)


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

    def _dl(self, tickers, **kw):
        import yfinance as yf
        return self._split(yf.download(tickers, auto_adjust=True, group_by="ticker", threads=True,
                                       progress=False, **kw), tickers)

    def daily(self, tickers):
        return self._dl(tickers, period="1y", interval="1d")

    def hourly(self, tickers):
        return self._dl(tickers, period="1y", interval="60m", prepost=False)

    def m15(self, tickers):
        return self._dl(tickers, period="30d", interval="15m", prepost=False)

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

    def hourly(self, tickers):
        return self._bars(tickers, "30Min", 380)     # 30-min bars, rebuilt into 09:30-anchored hours and 4h bars

    def m15(self, tickers):
        return self._bars(tickers, "15Min", 40)

    def earnings(self, tk):
        return Yahoo().earnings(tk)


class Fake:
    """Deterministic synthetic prices for offline testing."""
    name = "simulated data"

    def _series(self, tk, idx, vol, salt=0):
        rng = np.random.default_rng(zlib.crc32(tk.encode()) + salt)
        c = 100 * np.exp(np.cumsum(rng.normal(vol * 0.06, vol, len(idx))))
        return pd.DataFrame({"Open": c * (1 + rng.normal(0, vol / 5, len(idx))), "High": c * (1 + vol), "Low": c * (1 - vol),
                             "Close": c, "Volume": rng.integers(2e5, 2e6, len(idx)).astype(float)}, index=idx)

    def _days(self, n):
        return pd.bdate_range(end=now_ny().normalize().tz_localize(None), periods=n)

    def daily(self, tickers):
        idx = pd.DatetimeIndex([pd.Timestamp(f"{d.date()} 16:00", tz=TZ) for d in self._days(252)])
        return {tk: self._series(tk, idx, 0.022) for tk in tickers}

    def hourly(self, tickers):
        idx = pd.DatetimeIndex([pd.Timestamp(f"{d.date()} 09:30", tz=TZ) + pd.Timedelta(hours=h)
                                for d in self._days(252) for h in range(7)])
        return {tk: self._series(tk, idx, 0.008, 1) for tk in tickers}

    def m15(self, tickers):
        idx = pd.DatetimeIndex([pd.Timestamp(f"{d.date()} 09:30", tz=TZ) + pd.Timedelta(minutes=15 * k)
                                for d in self._days(22) for k in range(26)])
        return {tk: self._series(tk, idx, 0.003, 2) for tk in tickers}

    def earnings(self, tk):
        return (now_ny() + pd.Timedelta(days=zlib.crc32(tk.encode()) % 40)).date().isoformat()


def get_provider():
    want = os.environ.get("SCREENER_PROVIDER", "yahoo").lower()
    if want == "fake":
        return Fake()
    if want == "alpaca" and os.environ.get("ALPACA_KEY_ID"):
        return Alpaca()
    return Yahoo()


def fetch(fn, provider, tickers):
    try:
        res = getattr(provider, fn)(tickers)
        if len(res) >= max(1, len(tickers) // 2):
            return res, provider
        log(f"[warn] {provider.name} returned {len(res)}/{len(tickers)} for {fn}")
    except Exception as ex:
        log(f"[warn] {provider.name} {fn} failed: {ex}")
    if not isinstance(provider, Alpaca) and os.environ.get("ALPACA_KEY_ID"):
        log("[info] falling back to Alpaca")
        alp = Alpaca()
        return getattr(alp, fn)(tickers), alp
    return {}, provider


# ===================================================================== rules
def indicators(b):
    c, h, l = (b[k].to_numpy(float) for k in ("Close", "High", "Low"))
    pc = np.r_[np.nan, c[:-1]]
    tr = np.nanmax(np.vstack([h - l, np.abs(h - pc), np.abs(l - pc)]), axis=0)
    return dict(c=c, s20=pd.Series(c).rolling(CFG["fast"]).mean().to_numpy(),
                s250=pd.Series(c).rolling(CFG["slow"]).mean().to_numpy(),
                atr=pd.Series(tr).rolling(CFG["atr_n"]).mean().to_numpy(), idx=b.index)


def busdays_until(date_str):
    if not date_str:
        return None
    return int(np.busday_count(now_ny().date(), pd.Timestamp(date_str).date()))


def state_of(b):
    """Playbook state on any bar size: BUY / WATCH / WAIT / NO TRADE / AVOID (or NO DATA)."""
    if b is None or len(b) < CFG["slow"] + 25:
        return "NO DATA", None
    x = indicators(b)
    c, s20, s250 = x["c"], x["s20"], x["s250"]
    i = len(c) - 1
    up = c[i] > s250[i] and s250[i] > s250[i - 20]
    down = c[i] < s250[i] and s250[i] < s250[i - 20]
    if up and c[i - 1] <= s20[i - 1] and c[i] > s20[i]:
        return "BUY", x
    if up and c[i] <= s20[i]:
        return "WATCH", x
    if up:
        return "WAIT", x
    return ("AVOID" if down else "NO TRADE"), x


def decide(b4, earn):
    d, x = state_of(b4)
    if x is None:
        return {"decision": "NO DATA", "why": "Not enough 4h history."}
    c, s20, s250, atr = x["c"], x["s20"], x["s250"], x["atr"]
    i = len(c) - 1
    ext = (c[i] - s20[i]) / atr[i]
    why = {"BUY": "The dip to SMA20 just ended in an uptrend. Buy at the next 4h bar's open.",
           "WATCH": f"Dipping {abs(ext):.1f} ATR below SMA20 in an uptrend. Buy on a 4h close above ~{s20[i]:.2f}.",
           "WAIT": f"{ext:.1f} ATR above SMA20 with no dip yet. Wait for a pullback toward ~{s20[i]:.2f}.",
           "AVOID": "Downtrend: below a falling SMA250.",
           "NO TRADE": f"Below SMA250 ({s250[i]:.2f}) while it still rises. Needs a close back above it and a fresh dip."}[d]
    bd = busdays_until(earn)
    if d in ("BUY", "WATCH") and bd is not None and 0 <= bd <= CFG["earn_skip_sessions"]:
        d, why = "SKIP", f"Setup, but earnings on {earn}. Re-check after the report."
    return {"decision": d, "why": why, "close": round(c[i], 2), "sma20": round(s20[i], 2), "sma250": round(s250[i], 2),
            "sma250_rising": bool(s250[i] > s250[i - 20]), "atr": round(atr[i], 2), "bar": x["idx"][-1].isoformat()}


def vwap_check(b15):
    """Entry rule under test: last finished 15-min close vs that session's VWAP."""
    if b15 is None or b15.empty:
        return None
    day = b15[b15.index.date == b15.index[-1].date()]
    tp = (day["High"] + day["Low"] + day["Close"]) / 3
    v = day["Volume"].clip(lower=1)
    vwap = float((tp * v).sum() / v.sum())
    close = float(day["Close"].iloc[-1])
    return {"vwap": round(vwap, 2), "close15": round(close, 2), "above": close > vwap, "bar15": day.index[-1].isoformat()}


def check_holding(h, b4):
    if b4 is None or len(b4) < CFG["slow"] + 25:
        return {"ticker": h["ticker"], "decision": "NO DATA", "why": ""}
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
    return {"ticker": h["ticker"], "decision": d, "why": why}


# ============================================================ notifications
def notify(title, body, priority="default", tags="chart_with_upwards_trend"):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        log(f"[notify skipped: no NTFY_TOPIC] {title}")
        return
    import requests
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), timeout=20,
                      headers={"Title": title, "Priority": priority, "Tags": tags, "Click": os.environ.get("DASHBOARD_URL", "")})
    except Exception as ex:
        log(f"[warn] ntfy failed: {ex}")


# ===================================================================== ranking
def rank_universe(prov, universe):
    daily, prov = fetch("daily", prov, universe)
    rows = []
    for tk, d in daily.items():
        d = d.dropna(subset=["Close"])
        if len(d) < 130:
            continue
        c, v = d["Close"].astype(float), d["Volume"].astype(float)
        rows.append({"ticker": tk, "price": float(c.iloc[-1]), "ret_6m": float(c.iloc[-1] / c.iloc[-127] - 1),
                     "ret_1m": float(c.iloc[-1] / c.iloc[-22] - 1), "dollar_vol": float((c * v).iloc[-20:].mean())})
    rk = pd.DataFrame(rows)
    if rk.empty:
        return []
    rk = rk[(rk.price >= CFG["min_price"]) & (rk.dollar_vol >= CFG["min_dollar_vol"])]
    return rk.sort_values("ret_6m", ascending=False).head(CFG["top"]).to_dict("records")


# ===================================================================== main
def main():
    DATA.mkdir(parents=True, exist_ok=True)
    HIST.mkdir(parents=True, exist_ok=True)
    state_f = DATA / "state.json"
    state = json.loads(state_f.read_text()) if state_f.exists() else {}
    force = os.environ.get("FORCE", "").lower() == "true"
    prov = get_provider()
    universe = load_universe()
    holdings = [{**h, "ticker": h["ticker"].upper()} for h in json.loads(os.environ.get("HOLDINGS_JSON") or "[]")]
    today = now_ny().date().isoformat()

    # 1) ranking, once per trading day
    if state.get("rank_date") == today and state.get("rank") and not force:
        rank = state["rank"]
        log(f"using today's cached ranking ({len(rank)} names)")
    else:
        rank = rank_universe(prov, universe)
        if not rank:
            log("[error] no daily data"); sys.exit(1)
    top = [r["ticker"] for r in rank]
    need = list(dict.fromkeys(top + [h["ticker"] for h in holdings]))

    # 2) bars
    m15raw, p15 = fetch("m15", prov, need)
    b15 = {tk: resample_rth(df, 15) for tk, df in m15raw.items()}
    last15 = max((b.index[-1] for b in b15.values() if len(b)), default=None)
    last15_s = last15.isoformat() if last15 is not None else None
    if last15_s and state.get("last_bar_15") == last15_s and not force:
        log(f"No new 15-minute bar since {last15_s}; nothing to do."); return
    hraw, ph = fetch("hourly", prov, need)
    b1h = {tk: resample_rth(df, 60) for tk, df in hraw.items()}
    b4 = {tk: to_4h(df) for tk, df in hraw.items()}
    last4 = max((b.index[-1] for b in b4.values() if len(b)), default=None)
    last4_s = last4.isoformat() if last4 is not None else None
    new_4h = last4_s is not None and last4_s != state.get("last_bar")

    # 3) decisions
    earn_cache = state.get("earnings", {}) if state.get("rank_date") == today else {}
    rows = []
    for k, r in enumerate(rank, 1):
        tk = r["ticker"]
        if tk not in earn_cache:
            earn_cache[tk] = ph.earnings(tk)
        d = decide(b4.get(tk), earn_cache[tk])
        live = float(b15[tk]["Close"].iloc[-1]) if tk in b15 and len(b15[tk]) else r["price"]
        rows.append({"rank": k, "ticker": tk, "ret_6m": round(100 * r["ret_6m"], 1), "ret_1m": round(100 * r["ret_1m"], 1),
                     "live": round(live, 2), "earnings": earn_cache[tk], **d,
                     "ctx_1h": state_of(b1h.get(tk))[0], "ctx_15m": state_of(b15.get(tk))[0],
                     "vwap": vwap_check(b15.get(tk))})

    # 4) alerts: only when a new 4h bar finished
    prev = state.get("decisions", {})
    bar_label = pd.Timestamp(last4_s).strftime("%a %b %d, %I:%M %p ET") if last4_s else "?"
    if new_4h:
        buys = [r for r in rows if r["decision"] == "BUY" and prev.get(r["ticker"]) != "BUY"]
        if buys:
            notify(f"BUY signal: {', '.join(r['ticker'] for r in buys)}",
                   "\n".join(f"{r['ticker']}: close {r['close']}, trail stop {round(r['close'] - CFG['m_trail'] * r['atr'], 2)}, "
                             f"safety {round(r['close'] - CFG['m_safe'] * r['atr'], 2)}"
                             + (f", 15m {'above' if r['vwap']['above'] else 'below'} VWAP" if r.get("vwap") else "")
                             for r in buys) + f"\nBar: {bar_label}", priority="high")
        sells = [h for h in (check_holding(h, b4.get(h["ticker"])) for h in holdings) if h["decision"] == "SELL"]
        if sells:
            notify(f"SELL: {', '.join(h['ticker'] for h in sells)}", "\n".join(f"{h['ticker']}: {h['why']}" for h in sells),
                   priority="high", tags="warning")
        if os.environ.get("NOTIFY_SUMMARY", "").lower() == "true" and not (buys or sells):
            cnt = pd.Series([r["decision"] for r in rows]).value_counts().to_dict()
            notify("Screener updated", ", ".join(f"{k} {v}" for k, v in cnt.items()) + f" · bar {bar_label}", priority="low", tags="memo")

    # 5) history (one file per trading day)
    run_t = now_ny()
    hf = HIST / f"{run_t.date().isoformat()}.json"
    hist = json.loads(hf.read_text()) if hf.exists() else {"date": run_t.date().isoformat(), "runs": []}
    hist["runs"].append({"t": run_t.isoformat(), "bar15": last15_s, "bar4": last4_s,
                         "s": {r["ticker"]: [r["decision"], r["ctx_1h"], r["ctx_15m"], r["live"],
                                             r["vwap"]["vwap"] if r.get("vwap") else None,
                                             bool(r["vwap"]["above"]) if r.get("vwap") else None] for r in rows}})
    hf.write_text(json.dumps(hist, separators=(",", ":")))
    files = sorted(HIST.glob("*.json"))
    for old in files[:-CFG["history_days"]]:
        old.unlink()
    days = sorted(p.stem for p in HIST.glob("*.json"))

    payload = {"generated": run_t.isoformat(), "last_bar": last4_s, "last_bar_15": last15_s, "provider": ph.name,
               "universe_size": len(universe), "rules": CFG, "history_days": days, "rows": rows}
    (DATA / "latest.json").write_text(json.dumps(payload, indent=1, default=str))
    state.update({"last_bar": last4_s, "last_bar_15": last15_s, "decisions": {r["ticker"]: r["decision"] for r in rows},
                  "rank_date": today, "rank": rank, "earnings": earn_cache})
    state_f.write_text(json.dumps(state, indent=1, default=str))
    render(payload)
    log(f"Done: {len(rows)} rows, 4h bar {last4_s} (new: {new_4h}), 15m bar {last15_s}")


def render(payload):
    tpl = (ROOT / "screener" / "template.html").read_text()
    (DOCS / "index.html").write_text(tpl.replace("/*__DATA__*/null", json.dumps(payload, default=str)))


if __name__ == "__main__":
    main()
