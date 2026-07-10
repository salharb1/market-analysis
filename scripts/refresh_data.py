#!/usr/bin/env python3
"""Daily market-data refresh for the dashboard (runs free on GitHub Actions).

Fetches latest quotes (Yahoo primary, Stooq fallback), then updates index.html:
  - metric card values + daily % change
  - DATA_AS_OF timestamp (Riyadh time)
  - appends today's row to METRIC_HISTORY (idempotent; keeps last 30 rows)

Narrative/analysis sections are NOT touched — those are maintained by the
Claude scheduled task. This script only keeps the numbers fresh.
"""
import datetime
import json
import re
import sys
import urllib.parse
import urllib.request

INDEX = "index.html"

# metric key -> (yahoo symbol, stooq symbol, card label, value format)
METRICS = {
    "sp500":  ("^GSPC", "^spx", "S&P 500",     lambda p: f"{p:,.0f}"),
    "nasdaq": ("^IXIC", "^ndq", "Nasdaq",      lambda p: f"{p:,.0f}"),
    "dow":    ("^DJI",  "^dji", "Dow Jones",   lambda p: f"{p:,.0f}"),
    "vix":    ("^VIX",  "^vix", "VIX",         lambda p: f"{p:.2f}"),
    "brent":  ("BZ=F",  "cb.f", "Oil (Brent)", lambda p: f"${p:.2f}"),
    "gold":   ("GC=F",  "gc.f", "Gold",        lambda p: f"${p:,.0f}"),
}

UA = {"User-Agent": "Mozilla/5.0 (dashboard-refresh; +https://github.com/salharb1/market-analysis)"}


def yahoo_quote(symbol):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?range=5d&interval=1d")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    res = data["chart"]["result"][0]
    closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
    price = res["meta"].get("regularMarketPrice") or closes[-1]
    if len(closes) >= 2:
        # If the live price is (nearly) the last close, the market is closed:
        # compare against the prior session instead.
        tol = max(0.01, abs(price) * 5e-4)
        prev = closes[-2] if abs(price - closes[-1]) < tol else closes[-1]
    else:
        prev = price
    return float(price), float(prev)


def stooq_quote(symbol):
    url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(symbol)}&i=d"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        lines = [l for l in r.read().decode().strip().splitlines() if l]
    rows = [l.split(",") for l in lines[1:]]  # Date,Open,High,Low,Close,Volume
    closes = [float(row[4]) for row in rows if len(row) >= 5]
    if not closes:
        raise ValueError(f"no stooq data for {symbol}")
    price = closes[-1]
    prev = closes[-2] if len(closes) >= 2 else price
    return price, prev


def fetch(key):
    ysym, ssym, _, _ = METRICS[key]
    for fn, sym in ((yahoo_quote, ysym), (stooq_quote, ssym)):
        try:
            price, prev = fn(sym)
            if price > 0:
                return price, prev
        except Exception as e:  # noqa: BLE001 - try next source
            print(f"  {key}: {fn.__name__}({sym}) failed: {e}", file=sys.stderr)
    return None, None


def update_card(html, label, value_str, pct):
    arrow = "▲" if pct >= 0 else "▼"
    cls = "positive" if pct >= 0 else "negative"
    if label == "VIX" and pct >= 0:
        cls = "negative"  # rising VIX = rising fear
    change = f"{arrow} {pct:+.2f}%"
    pattern = (
        r'(<div class="metric-card"><div class="metric-label" data-en="'
        + re.escape(label)
        + r'"[^>]*>.*?</div>)<div class="metric-value">.*?</div>'
        r'<div class="metric-change[^>]*>.*?</div>'
    )
    repl = (r"\1" + f'<div class="metric-value">{value_str}</div>'
            f'<div class="metric-change {cls}">{change}</div>')
    new_html, n = re.subn(pattern, repl, html, count=1)
    if n == 0:
        print(f"  WARNING: card not found for label {label!r}", file=sys.stderr)
        return html
    return new_html


def main():
    with open(INDEX, encoding="utf-8") as f:
        html = f.read()

    riyadh = datetime.timezone(datetime.timedelta(hours=3))
    now = datetime.datetime.now(riyadh)
    today = now.strftime("%Y-%m-%d")

    quotes = {}
    for key in METRICS:
        price, prev = fetch(key)
        if price is None:
            print(f"  {key}: all sources failed, keeping existing value")
            continue
        quotes[key] = (price, prev)
        print(f"  {key}: {price:.2f} (prev {prev:.2f})")

    if not quotes:
        print("No data fetched at all — aborting without changes.")
        sys.exit(1)

    # 1. metric cards
    for key, (price, prev) in quotes.items():
        _, _, label, fmt = METRICS[key]
        pct = (price - prev) / prev * 100 if prev else 0.0
        html = update_card(html, label, fmt(price), pct)

    # 2. DATA_AS_OF
    html = re.sub(
        r"const DATA_AS_OF = new Date\('[^']*'\)",
        f"const DATA_AS_OF = new Date('{now.strftime('%Y-%m-%dT%H:%M:%S')}+03:00')",
        html, count=1)

    # 3. METRIC_HISTORY append (idempotent, keep last 30)
    m = re.search(r"(const METRIC_HISTORY = \[\n)(.*?)(\n\s*\];)", html, re.S)
    if m:
        body = m.group(2)
        rows = re.findall(r"\{[^}]*\}", body)
        # Drop any existing row for today so a rerun refreshes it in place.
        prior_rows = [r for r in rows if f"date: '{today}'" not in r]
        ref_row = prior_rows[-1] if prior_rows else (rows[-1] if rows else "")
        risk_m = re.search(r"risk:\s*(\d+)", ref_row)
        risk = risk_m.group(1) if risk_m else "7"

        def hval(key):
            if key in quotes:
                p = quotes[key][0]
                return f"{p:.2f}" if key in ("vix", "brent") else f"{p:.0f}"
            prev_m = re.search(key + r":\s*([\d.]+)", ref_row)
            return prev_m.group(1) if prev_m else "0"

        new_row = ("{ date: '" + today + "', sp500: " + hval("sp500")
                   + ", vix: " + hval("vix")
                   + ", brent: " + hval("brent")
                   + ", gold: " + hval("gold")
                   + ", risk: " + risk + " }")
        rows = (prior_rows + [new_row])[-30:]
        new_body = ",\n            ".join(rows)
        html = html[:m.start()] + m.group(1) + "            " + new_body + m.group(3) + html[m.end():]
    else:
        print("  WARNING: METRIC_HISTORY block not found", file=sys.stderr)

    with open(INDEX, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    print(f"index.html refreshed for {today} at {now.strftime('%H:%M')} Riyadh time")


if __name__ == "__main__":
    main()
