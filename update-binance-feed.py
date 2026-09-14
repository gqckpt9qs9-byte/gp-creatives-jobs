#!/usr/bin/env python3
"""
Regenerate s3://gp-creatives/binance/binance-prices.json from Binance's public
24hr ticker API.

Safety model: this object is read by a live serving creative. The script only
uploads if every coin passes validation. On any failure it exits non-zero and
leaves the last good feed in place, so the ad keeps showing stale-but-sane
prices rather than zeros or a broken payload.

Usage:
  ./update-binance-feed.py            # fetch, validate, upload
  ./update-binance-feed.py --dry-run  # fetch, validate, print. No upload.
"""

import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# Failover order. All three serve identical public market data.
#
# data-api.binance.vision is deliberately first: Binance geo-blocks the main
# api.binance.com and api-gcp.binance.com endpoints from US datacenter ranges
# (HTTP 451), which is where GitHub Actions runners live. The .vision host is
# Binance's public market-data mirror and answers from everywhere. The other
# two stay as fallbacks because they do work from other networks.
HOSTS = ["data-api.binance.vision", "api.binance.com", "api-gcp.binance.com"]

# (binance symbol, display symbol, display name, coinmarketcap id) in display order.
#
# The CMC id is load-bearing: querying CMC by symbol returns every token
# squatting that ticker (23 rows for BNB,BTC,ETH, including a "Bitcoin AI" at
# $0.001), so a symbol match could render a scam token's price as Bitcoin.
# Numeric ids are unambiguous.
COINS = [
    ("BNBUSDT", "BNB", "BNB", 1839),
    ("BTCUSDT", "BTC", "Bitcoin", 1),
    ("ETHUSDT", "ETH", "Ethereum", 1027),
]

# CoinMarketCap fallback. Used only if every Binance host fails.
#
# Two distinct endpoints, and they are not interchangeable:
#   - Pro path takes the API key and REQUIRES it.
#   - /public-api/ path is keyless and REJECTS the key header with a 401.
# Sending the key to the public path is a 401, which looks exactly like a bad
# key. Keep them separate.
CMC_PRO_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
CMC_PUBLIC_URL = "https://pro-api.coinmarketcap.com/public-api/v3/cryptocurrency/quotes/latest"
CMC_KEY_FILE = os.path.expanduser("~/.cmc_key")

S3_KEY = os.environ.get("FEED_S3_KEY", "s3://gp-creatives/binance/binance-prices.json")
LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "binance", "binance-prices.json")
# Resolve the aws CLI from PATH so this runs on a CI runner as well as locally.
AWS = os.environ.get("AWS_CLI") or shutil.which("aws") or "/opt/homebrew/bin/aws"
TIMEOUT = 12

# Validation bounds. Deliberately wide: these catch broken payloads
# (nulls, zeros, a decimal-shift bug), not normal volatility.
MAX_ABS_CHANGE_PCT = 60.0
SANITY = {"BTC": (1_000, 10_000_000), "ETH": (50, 500_000), "BNB": (10, 100_000)}


def log(msg):
    print(f"[feed] {msg}", flush=True)


def _get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "kayzen-feed/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode()), r.headers


def fetch_binance():
    """{display_symbol: (price, pct_change_24h)} from Binance, trying each host."""
    syms = json.dumps([c[0] for c in COINS], separators=(",", ":"))
    path = "/api/v3/ticker/24hr?symbols=" + urllib.parse.quote(syms)
    last = None
    for host in HOSTS:
        try:
            rows, hdrs = _get(f"https://{host}{path}")
            if not isinstance(rows, list) or len(rows) != len(COINS):
                raise ValueError(f"expected {len(COINS)} rows, got {type(rows).__name__}")
            by = {r["symbol"]: r for r in rows}
            out = {c[1]: (float(by[c[0]]["lastPrice"]), float(by[c[0]]["priceChangePercent"]))
                   for c in COINS}
            log(f"fetched from {host} (weight-1m={hdrs.get('x-mbx-used-weight-1m','?')})")
            return out, f"https://{host}/api/v3/ticker/24hr"
        except Exception as e:
            last = e
            log(f"binance {host} failed: {type(e).__name__}: {e}")
    raise RuntimeError(f"all binance hosts failed; last: {last}")


def _cmc_key():
    k = os.environ.get("CMC_API_KEY", "").strip()
    if not k and os.path.isfile(CMC_KEY_FILE):
        k = open(CMC_KEY_FILE).read().strip()
    # Ignore the placeholder so a half-finished setup falls through to keyless.
    return "" if k.upper().startswith("YOUR") else k


def fetch_cmc():
    """{display_symbol: (price, pct_change_24h)} from CoinMarketCap.

    Queries by numeric id, never by symbol: symbol lookup returns every token
    squatting the ticker. Uses a key when one is available (private quota),
    otherwise the keyless public tier (IP-pooled, so 429-prone on CI).
    """
    ids = ",".join(str(c[3]) for c in COINS)
    qs = f"?id={ids}&convert=USD"
    key = _cmc_key()
    attempts = [("keyed", CMC_PRO_URL + qs, {"X-CMC_PRO_API_KEY": key})] if key else []
    attempts.append(("keyless", CMC_PUBLIC_URL + qs, None))

    last = None
    for label, url, headers in attempts:
        try:
            d, _ = _get(url, headers)
            if str(d.get("status", {}).get("error_code", "0")) not in ("0", "None"):
                raise ValueError(d["status"].get("error_message", "cmc error"))
            # Shape differs by endpoint: Pro v1 returns data as a dict keyed by
            # id, public v3 returns a list. Likewise quote is a dict keyed by
            # currency on Pro, a list on public. Normalise both.
            raw = d.get("data", [])
            rows = list(raw.values()) if isinstance(raw, dict) else raw
            by = {}
            for r in rows:
                r = r[0] if isinstance(r, list) else r
                q = r["quote"]
                q = q[0] if isinstance(q, list) else q.get("USD", {})
                by[int(r["id"])] = (float(q["price"]), float(q["percent_change_24h"]))
            out = {c[1]: by[c[3]] for c in COINS}
            log(f"fetched from coinmarketcap ({label})")
            return out, f"{url.split(chr(63))[0]} ({label})"
        except Exception as e:
            last = e
            log(f"coinmarketcap {label} failed: {type(e).__name__}: {e}")
    raise RuntimeError(f"coinmarketcap failed; last: {last}")


def fetch():
    """Try Binance, then CoinMarketCap. Returns (quotes, source)."""
    try:
        return fetch_binance()
    except Exception as e:
        log(f"primary source unavailable: {e}")
        log("falling back to coinmarketcap")
    try:
        return fetch_cmc()
    except Exception as e:
        raise SystemExit(f"all sources failed; last: {e}")


def build(quotes, source):
    data, problems = [], []
    for _bsym, sym, name, _cmcid in COINS:
        q = quotes.get(sym)
        if q is None:
            problems.append(f"{sym}: missing from response")
            continue
        try:
            price = round(float(q[0]), 2)
            chg = round(float(q[1]), 2)
        except (TypeError, ValueError, IndexError) as e:
            problems.append(f"{sym}: unparseable ({e})")
            continue
        lo, hi = SANITY.get(sym, (0, float("inf")))
        if not (lo <= price <= hi):
            problems.append(f"{sym}: price {price} outside sane range {lo}-{hi}")
        if abs(chg) > MAX_ABS_CHANGE_PCT:
            problems.append(f"{sym}: change {chg}% exceeds +/-{MAX_ABS_CHANGE_PCT}%")
        data.append({"symbol": sym, "name": name, "price": price, "change24h": chg})

    if problems:
        for p in problems:
            log("VALIDATION FAIL: " + p)
        raise SystemExit("refusing to publish; last good feed left in place")

    return {
        "study_id": "binance_ticker_202609",
        "advertiser": "Binance",
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "note": ("Price feed for the Binance ticker unit. Regenerated by "
                 "update-binance-feed.py; served gzipped on S3 "
                 "(Content-Encoding: gzip). Order = display order; unit renders first 3."),
        "data": data,
    }


def upload(doc):
    body = json.dumps(doc, indent=2) + "\n"
    if os.path.isdir(os.path.dirname(LOCAL)):
        with open(LOCAL, "w") as f:
            f.write(body)
    tmp = os.path.join(tempfile.mkdtemp(), "binance-prices.json")
    with open(tmp, "wb") as f:
        f.write(gzip.compress(body.encode(), 9))
    subprocess.run(
        [AWS, "s3", "cp", tmp, S3_KEY,
         "--content-type", "application/json",
         "--content-encoding", "gzip",
         "--cache-control", "max-age=60",
         "--only-show-errors"],
        check=True,
    )
    log(f"uploaded {os.path.getsize(tmp)}B gzipped -> {S3_KEY}")


def main():
    dry = "--dry-run" in sys.argv
    quotes, source = fetch()
    doc = build(quotes, source)
    for c in doc["data"]:
        log(f"  {c['symbol']:<4} ${c['price']:>12,.2f}  {c['change24h']:+.2f}%")
    if dry:
        log("dry run, not uploading")
        return
    upload(doc)
    log("done")


if __name__ == "__main__":
    main()
