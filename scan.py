#!/usr/bin/env python3
"""
bestfriendtrades.xyz — Stage 2 scanner
Fetches data from Financial Modeling Prep (FMP).
Run via GitHub Actions or locally.
Results saved to results.json which the site displays.
"""

import json, os, time, requests
from datetime import datetime, timezone, timedelta

FMP_KEY  = os.environ.get("FMP_API_KEY", "O5Q8NtVn14pKJH9k7rTAh6zNMU1AIGyR")
BASE_URL = "https://financialmodelingprep.com/api/v3"

# ── Load tickers ──────────────────────────────────────────────
def load_tickers():
    path = os.path.join(os.path.dirname(__file__), "tickers.txt")
    with open(path) as f:
        raw = f.read()
    tickers = [t.strip().upper() for t in raw.replace(",", "\n").split("\n")]
    return [t for t in tickers if t]

# ── Fetch 14 months of daily bars from FMP ────────────────────
def fetch_bars(ticker):
    from_date = (datetime.now() - timedelta(days=420)).strftime("%Y-%m-%d")
    url = f"{BASE_URL}/historical-price-full/{ticker}?from={from_date}&apikey={FMP_KEY}"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()

    if "Error Message" in data:
        raise ValueError(data["Error Message"])

    hist = data.get("historical", [])
    if not hist:
        return []

    # FMP returns newest-first — reverse to oldest-first
    return list(reversed(hist))

# ── Stage 2 classifier ────────────────────────────────────────
def classify(ticker, bars):
    if len(bars) < 60:
        return None

    closes = [b["close"]  for b in bars if b.get("close")  is not None]
    lows   = [b["low"]    for b in bars if b.get("low")    is not None]
    vols   = [b["volume"] for b in bars if b.get("volume") is not None]

    if len(closes) < 60:
        return None

    price = closes[-1]

    def avg(arr, n):
        s = arr[-n:] if len(arr) >= n else arr
        return sum(s) / len(s)

    def rising(arr, n):
        if len(arr) < n + 10:
            return False
        return avg(arr, n) > avg(arr[:-10], n)

    ma50  = avg(closes, 50)
    ma150 = avg(closes, min(150, len(closes)))

    # Gate 1: price above both MAs
    if price <= ma50 or price <= ma150:
        return None

    # Gate 2: both MAs rising
    if not rising(closes, 50):
        return None
    if len(closes) >= 160 and not rising(closes, 150):
        return None

    # Gate 3: HH >= 2 in last 20 bars
    last20c = closes[-20:]
    hh = sum(1 for i in range(1, len(last20c)) if last20c[i] > last20c[i-1])
    if hh < 2:
        return None

    # Gate 4: HL count for scoring
    last20l = lows[-20:] if len(lows) >= 20 else lows
    hl = sum(1 for i in range(1, len(last20l)) if last20l[i] > last20l[i-1])

    # Gate 5: no new lows in last 5 bars vs prior 20
    if len(lows) >= 25:
        prior_min = min(lows[-25:-5])
        if any(l < prior_min for l in lows[-5:]):
            return None

    # Gate 6: up-vol > down-vol
    up_vol = down_vol = 0.0
    n = min(len(vols), len(closes))
    for i in range(1, n):
        if closes[i] > closes[i-1]: up_vol += vols[i]
        else: down_vol += vols[i]
    vol_ratio = up_vol / down_vol if down_vol > 0 else 1.0

    # ── Score ─────────────────────────────────────────────────
    pct_above_ma50 = (price - ma50) / ma50 * 100
    score = 0

    # Position vs MA50 (25 pts)
    score += 25 if pct_above_ma50 < 5 else 18 if pct_above_ma50 < 15 else 10 if pct_above_ma50 < 30 else 2

    # MA slope (20 pts)
    if len(closes) >= 60:
        s50  = (avg(closes,50)  - avg(closes[:-10],50))  / avg(closes[:-10],50)  * 100
        n150 = min(150, len(closes))
        s150 = (avg(closes,n150) - avg(closes[:-10],n150)) / avg(closes[:-10],n150) * 100
        score += min(20, max(0, (s50 + s150) * 5))

    # HH + HL (20 pts)
    score += min(20, (hh + hl) * 2)

    # Vol ratio (20 pts)
    score += 20 if vol_ratio >= 1.3 else min(20, vol_ratio * 10)

    # 52-wk RS (15 pts)
    lookback = min(252, len(closes))
    low52 = min(closes[-lookback:])
    rs = (price - low52) / low52 * 100
    score += 15 if rs > 30 else min(15, rs / 2)

    score = round(score)

    zone = ("PRIME"        if pct_above_ma50 < 5  else
            "GOOD"         if pct_above_ma50 < 15 else
            "EXTENDED"     if pct_above_ma50 < 30 else
            "OVEREXTENDED")

    zclass = {"PRIME":"z-prime","GOOD":"z-good",
              "EXTENDED":"z-extended","OVEREXTENDED":"z-over"}[zone]

    flags = []
    if vol_ratio >= 1.3:    flags.append("Inst. Accum")
    if score >= 70:         flags.append("High Quality")
    if hh >= 4:             flags.append("Strong HH")
    if pct_above_ma50 < 3: flags.append("At MA")

    return {
        "ticker":       ticker,
        "price":        round(price, 2),
        "ma50":         round(ma50, 2),
        "ma150":        round(ma150, 2),
        "score":        score,
        "zone":         zone,
        "zClass":       zclass,
        "pctDiff":      round((price - ma150) / ma150 * 100, 1),
        "pctAboveMa50": round(pct_above_ma50, 1),
        "flags":        flags,
    }

# ── Main ──────────────────────────────────────────────────────
def main():
    tickers = load_tickers()
    print(f"Scanning {len(tickers)} tickers via FMP...")
    results = []
    errors  = []

    for i, ticker in enumerate(tickers):
        print(f"  [{i+1:02d}/{len(tickers)}] {ticker:<8}", end="  ", flush=True)
        try:
            bars   = fetch_bars(ticker)
            result = classify(ticker, bars)
            if result:
                print(f"PASS  score={result['score']}  zone={result['zone']}")
                results.append(result)
            else:
                print(f"fail  bars={len(bars)}")
        except Exception as e:
            print(f"ERROR  {e}")
            errors.append(ticker)

        # Stay well within FMP rate limits
        time.sleep(0.5)

    results.sort(key=lambda x: x["score"], reverse=True)

    out = {
        "scanned_at":    datetime.now(timezone.utc).isoformat(),
        "total_scanned": len(tickers),
        "stage2_count":  len(results),
        "errors":        errors,
        "results":       results,
    }

    out_path = os.path.join(os.path.dirname(__file__), "results.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)

    print(f"\nDone. {len(results)}/{len(tickers)} passed Stage 2.")
    if errors:
        print(f"Errors ({len(errors)}): {', '.join(errors)}")
    print(f"Saved → results.json")

if __name__ == "__main__":
    main()
