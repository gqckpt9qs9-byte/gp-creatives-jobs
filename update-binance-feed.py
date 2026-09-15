#!/usr/bin/env python3
"""
Regenerate s3://gp-creatives/binance/binance-prices.json.

The feed carries a broad market snapshot (top N by market cap, stablecoins
filtered) plus a short `display` list naming what the creative should render.
Selection policy lives here, not in the unit: the unit stays small and dumb,
and the policy is testable.

Sources, in priority order: CoinMarketCap, the Smadex xCrypto mirror, then
Binance's own ticker API.

Safety model: this object is read by a live serving creative. The script only
uploads if the anchor coins and every displayed coin pass validation. On any
failure it exits non-zero and leaves the last good feed in place, so the ad
keeps showing stale-but-sane prices rather than zeros or a broken payload.

Usage:
  ./update-binance-feed.py               # fetch, validate, upload, archive
  ./update-binance-feed.py --dry-run     # fetch, validate, print. No upload.
  ./update-binance-feed.py --no-archive  # publish the feed but skip history/
"""

import gzip
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# ---------------------------------------------------------------- policy ----

# How many coins the feed carries. Gainer candidates come only from this set,
# so it doubles as the market-cap floor that keeps micro-caps off the ad.
TOP_N = 50

# Display order when nothing qualifies to displace an anchor.
ANCHORS = ["BNB", "BTC", "ETH"]
# Anchors that are never displaced. BNB is the advertiser's own coin; BTC is
# the reference everyone recognises. ETH holds the flexible slot.
PINNED = {"BNB", "BTC"}
# A non-anchor must be up at least this much over 24h to take the flexible
# slot, and must also beat the anchor it displaces.
GAINER_MIN_PCT = 5.0

# Backstop for sources that carry no tags. CMC/Smadex tag stablecoins; Binance
# does not, and a flat $1.00 at +0.01% is a dead card on a ticker.
STABLE_SYMBOLS = {"USDT", "USDC", "USDe", "DAI", "USD1", "USDG", "PYUSD", "RLUSD",
                  "USDD", "FDUSD", "TUSD", "BUSD", "USDP", "FRAX", "GUSD", "USDS"}

# Brand-approved icons already on the Kayzen CDN. Everything else falls back
# to CMC's deterministic logo URL, keyed by CMC id.
CDN_ICONS = {
    "BNB": "https://disk.akamaized.net/assets/414121f5-c154-4f99-9782-87f5376902df.png",
    "BTC": "https://disk.akamaized.net/assets/3479a9cd-1e45-4041-88f7-5104f1e25132.png",
    "ETH": "https://disk.akamaized.net/assets/af52038a-cd77-4fd0-aa1a-d7069d29e0c5.png",
}
CMC_LOGO = "https://s2.coinmarketcap.com/static/img/coins/64x64/{id}.png"

# --------------------------------------------------------------- sources ----

# CoinMarketCap: primary. Two endpoints that are NOT interchangeable - the Pro
# path requires the key header, the /public-api/ path rejects it with a 401
# indistinguishable from a bad key. Keep the URLs separate.
CMC_PRO_LIST = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"
CMC_PUBLIC_LIST = "https://pro-api.coinmarketcap.com/public-api/v3/cryptocurrency/listings/latest"
CMC_KEY_FILE = os.path.expanduser("~/.cmc_key")

# Smadex xCrypto: public hourly mirror, CMC-derived, 50 coins with tags+logo.
SMADEX_URL = "https://static-content-1.smadex.com/cr84es/templ8s/xCrypto"

# Binance: last resort. It geo-blocks api.binance.com / api-gcp.binance.com
# from US datacenter ranges (HTTP 451) - where GitHub runners live - so the
# .vision market-data mirror goes first. Binance only returns what you ask
# for, so it gets a fixed list of well-known USDT pairs rather than a ranking.
BINANCE_HOSTS = ["data-api.binance.vision", "api.binance.com", "api-gcp.binance.com"]
BINANCE_UNIVERSE = [  # (symbol, name, cmc_id) - cmc_id only used for the icon URL
    ("BNB", "BNB", 1839), ("BTC", "Bitcoin", 1), ("ETH", "Ethereum", 1027),
    ("XRP", "XRP", 52), ("SOL", "Solana", 5426), ("TRX", "TRON", 1958),
    ("DOGE", "Dogecoin", 74), ("ADA", "Cardano", 2010), ("LINK", "Chainlink", 1975),
    ("XLM", "Stellar", 512), ("BCH", "Bitcoin Cash", 1831), ("LTC", "Litecoin", 2),
    ("UNI", "Uniswap", 7083), ("HBAR", "Hedera", 4642), ("AVAX", "Avalanche", 5805),
    ("NEAR", "NEAR Protocol", 6535), ("SUI", "Sui", 20947), ("DOT", "Polkadot", 6636),
    ("AAVE", "Aave", 7278), ("ICP", "Internet Computer", 8916),
]

S3_KEY = os.environ.get("FEED_S3_KEY", "s3://gp-creatives/binance/binance-prices.json")
HISTORY_PREFIX = os.environ.get("FEED_HISTORY_PREFIX", "s3://gp-creatives/binance/history")
HISTORY_KEEP = 5
LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "binance", "binance-prices.json")
AWS = os.environ.get("AWS_CLI") or shutil.which("aws") or "/opt/homebrew/bin/aws"
TIMEOUT = 12

# ------------------------------------------------------------ validation ----

# Wide bounds: these catch broken payloads (nulls, zeros, a decimal shift),
# not normal volatility. Anchor ranges are tighter because we know them.
MAX_ABS_CHANGE_PCT = 60.0
MAX_SOURCE_AGE_H = 3.0
SANITY = {"BTC": (1_000, 10_000_000), "ETH": (50, 500_000), "BNB": (10, 100_000)}


def log(msg):
    print(f"[feed] {msg}", flush=True)


def _get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "kayzen-feed/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode()), r.headers


def _age_hours(hdrs):
    lm = hdrs.get("Last-Modified")
    if not lm:
        return None
    try:
        return (datetime.now(timezone.utc) - parsedate_to_datetime(lm)).total_seconds() / 3600.0
    except Exception:
        return None


def _coin(symbol, name, price, change, rank, cmc_id=None, tags=None, icon=None):
    """Normalised coin row. Every source funnels through here."""
    tags = [t if isinstance(t, str) else (t or {}).get("slug", "") for t in (tags or [])]
    is_stable = "stablecoin" in tags or symbol in STABLE_SYMBOLS
    if not icon:
        icon = CDN_ICONS.get(symbol) or (CMC_LOGO.format(id=cmc_id) if cmc_id else None)
    return {"symbol": symbol, "name": name, "price": float(price), "change24h": float(change),
            "rank": int(rank), "cmc_id": cmc_id, "icon": icon, "is_stable": is_stable}


# ------------------------------------------------------------- fetchers ----

def _cmc_key():
    k = os.environ.get("CMC_API_KEY", "").strip()
    if not k and os.path.isfile(CMC_KEY_FILE):
        k = open(CMC_KEY_FILE).read().strip()
    return "" if k.upper().startswith("YOUR") else k


def fetch_cmc():
    """Top N by market cap from CoinMarketCap. Pro (keyed) then public (keyless)."""
    qs = f"?start=1&limit={TOP_N}&convert=USD"
    key = _cmc_key()
    attempts = [("keyed", CMC_PRO_LIST + qs, {"X-CMC_PRO_API_KEY": key})] if key else []
    attempts.append(("keyless", CMC_PUBLIC_LIST + qs, None))
    last = None
    for label, url, headers in attempts:
        try:
            d, _ = _get(url, headers)
            if str(d.get("status", {}).get("error_code", "0")) not in ("0", "None"):
                raise ValueError(d["status"].get("error_message", "cmc error"))
            raw = d.get("data", [])
            rows = list(raw.values()) if isinstance(raw, dict) else raw
            coins = []
            for r in rows:
                q = r["quote"]
                q = q[0] if isinstance(q, list) else q.get("USD", {})
                coins.append(_coin(r["symbol"], r["name"], q["price"], q["percent_change_24h"],
                                   r.get("cmc_rank") or len(coins) + 1, r.get("id"), r.get("tags")))
            if len(coins) < 10:
                raise ValueError(f"only {len(coins)} rows")
            log(f"fetched {len(coins)} coins from coinmarketcap ({label})")
            return coins, f"{url.split('?')[0]} ({label})"
        except Exception as e:
            last = e
            log(f"coinmarketcap {label} failed: {type(e).__name__}: {e}")
    raise RuntimeError(f"coinmarketcap failed; last: {last}")


def fetch_smadex():
    """Smadex hourly file. Current hour 404s until published, so walk back."""
    now = datetime.now(timezone.utc)
    names = [f"{now - timedelta(hours=h):%Y%m%d%H}" for h in range(0, int(MAX_SOURCE_AGE_H) + 1)]
    names.append(f"{now:%Y%m%d}")
    last = None
    for name in names:
        url = f"{SMADEX_URL}/creative-crypto-api-{name}.json"
        try:
            d, hdrs = _get(url)
            age = _age_hours(hdrs)
            if age is not None and age > MAX_SOURCE_AGE_H:
                raise ValueError(f"stale: {age:.1f}h old (cutoff {MAX_SOURCE_AGE_H}h)")
            coins = [_coin(r["symbol"], r["name"], r["price"], r["usd_price_change_24h"],
                           i + 1, None, r.get("tags"), r.get("logo"))
                     for i, r in enumerate(d.get("data", []))]
            if len(coins) < 10:
                raise ValueError(f"only {len(coins)} rows")
            log(f"fetched {len(coins)} coins from smadex ({name})")
            return coins, url
        except Exception as e:
            last = e
            log(f"smadex {name} failed: {type(e).__name__}: {e}")
    raise RuntimeError(f"smadex failed; last: {last}")


def fetch_binance():
    """Fixed universe of USDT pairs from Binance, trying each host."""
    pairs = [f"{s}USDT" for s, _n, _i in BINANCE_UNIVERSE]
    path = "/api/v3/ticker/24hr?symbols=" + urllib.parse.quote(json.dumps(pairs, separators=(",", ":")))
    last = None
    for host in BINANCE_HOSTS:
        try:
            rows, hdrs = _get(f"https://{host}{path}")
            by = {r["symbol"]: r for r in rows}
            coins = []
            for i, (sym, name, cid) in enumerate(BINANCE_UNIVERSE):
                r = by.get(f"{sym}USDT")
                if r:
                    coins.append(_coin(sym, name, r["lastPrice"], r["priceChangePercent"], i + 1, cid))
            if len(coins) < 10:
                raise ValueError(f"only {len(coins)} rows")
            log(f"fetched {len(coins)} coins from {host} (weight-1m={hdrs.get('x-mbx-used-weight-1m','?')})")
            return coins, f"https://{host}/api/v3/ticker/24hr"
        except Exception as e:
            last = e
            log(f"binance {host} failed: {type(e).__name__}: {e}")
    raise RuntimeError(f"all binance hosts failed; last: {last}")


SOURCES = [("coinmarketcap", fetch_cmc), ("smadex", fetch_smadex), ("binance", fetch_binance)]


def fetch():
    last = None
    for name, fn in SOURCES:
        try:
            return fn()
        except Exception as e:
            last = e
            log(f"source {name} unavailable: {e}")
    raise SystemExit(f"all sources failed; last: {last}")


# -------------------------------------------------------- select + build ----

def _ok(c):
    return math.isfinite(c["price"]) and c["price"] > 0 and math.isfinite(c["change24h"]) \
        and abs(c["change24h"]) <= MAX_ABS_CHANGE_PCT


def select_display(coins):
    """Anchors, with the flexible slot handed to a qualifying big gainer.

    Returns (display_symbols, featured) where featured maps a symbol to the
    reason it was promoted, so the portfolio can explain what it is showing.
    """
    by = {c["symbol"]: c for c in coins}
    display = list(ANCHORS)
    featured = {}
    flexible = [a for a in ANCHORS if a not in PINNED]
    if not flexible:
        return display, featured
    slot = flexible[-1]
    # `icon` is required: a promoted coin renders in the ad, and a missing
    # icon is a broken image on a live impression.
    candidates = [c for c in coins
                  if c["symbol"] not in ANCHORS and not c["is_stable"] and c["icon"]
                  and c["rank"] <= TOP_N and _ok(c) and c["change24h"] >= GAINER_MIN_PCT]
    if not candidates:
        return display, featured
    best = max(candidates, key=lambda c: c["change24h"])
    incumbent = by.get(slot)
    if incumbent and best["change24h"] <= incumbent["change24h"]:
        return display, featured
    display[display.index(slot)] = best["symbol"]
    featured[best["symbol"]] = {"reason": "top_gainer_24h", "replaced": slot,
                                "change24h": round(best["change24h"], 2), "rank": best["rank"]}
    return display, featured


def build(coins, source):
    by = {c["symbol"]: c for c in coins}
    problems = []

    # Anchors are non-negotiable: all present and inside known ranges.
    for a in ANCHORS:
        c = by.get(a)
        if c is None:
            problems.append(f"{a}: missing from response")
            continue
        lo, hi = SANITY[a]
        if not (lo <= c["price"] <= hi):
            problems.append(f"{a}: price {c['price']} outside sane range {lo}-{hi}")
        if not _ok(c):
            problems.append(f"{a}: failed generic checks (change {c['change24h']}%)")

    # Non-anchor rows that fail are dropped, not fatal - a bad row 47 should
    # not block publishing BTC.
    kept, dropped = [], []
    for c in coins:
        (kept if _ok(c) or c["symbol"] in ANCHORS else dropped).append(c)
    if dropped:
        log(f"dropped {len(dropped)} bad rows: {', '.join(c['symbol'] for c in dropped)}")

    display, featured = select_display(kept)
    for s in display:
        if s not in by or not _ok(by[s]):
            problems.append(f"{s}: selected for display but invalid")

    if problems:
        for p in problems:
            log("VALIDATION FAIL: " + p)
        raise SystemExit("refusing to publish; last good feed left in place")

    kept.sort(key=lambda c: c["rank"])
    data = [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in c.items()} for c in kept]
    return {
        "schema": 2,
        "study_id": "binance_ticker_202609",
        "advertiser": "Binance",
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "note": ("Market snapshot for the Binance ticker unit. `display` names what the "
                 "unit renders, in order; `data` is the broader top-N set it can draw "
                 "from. Regenerated hourly by update-binance-feed.py; served gzipped."),
        "display": display,
        "featured": featured,
        "data": data,
    }


# ------------------------------------------------------------- publish ----

def _put_json(body, key, cache):
    tmp = os.path.join(tempfile.mkdtemp(), os.path.basename(key))
    with open(tmp, "wb") as f:
        f.write(gzip.compress(body.encode(), 9))
    subprocess.run([AWS, "s3", "cp", tmp, key, "--content-type", "application/json",
                    "--content-encoding", "gzip", "--cache-control", cache, "--only-show-errors"],
                   check=True)
    return os.path.getsize(tmp)


def archive(doc):
    """Snapshot this run to history/ and roll the index. Never deletes."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    body = json.dumps(doc, indent=2) + "\n"
    n = _put_json(body, f"{HISTORY_PREFIX}/prices-{stamp}.json", "max-age=3600")
    log(f"archived {n}B -> history/prices-{stamp}.json")
    out = subprocess.run([AWS, "s3", "ls", HISTORY_PREFIX + "/"], capture_output=True, text=True, check=True)
    stamps = sorted(m for line in out.stdout.splitlines() for m in [line.split()[-1]]
                    if m.startswith("prices-") and m.endswith(".json"))
    keep = [s[len("prices-"):-len(".json")] for s in stamps][-HISTORY_KEEP:]
    _put_json(json.dumps(keep), f"{HISTORY_PREFIX}/index.json", "max-age=300")
    log(f"index -> {len(keep)} snapshots ({keep[0]}..{keep[-1]})")


def upload(doc):
    body = json.dumps(doc, indent=2) + "\n"
    if os.path.isdir(os.path.dirname(LOCAL)):
        with open(LOCAL, "w") as f:
            f.write(body)
    n = _put_json(body, S3_KEY, "max-age=60")
    log(f"uploaded {n}B gzipped -> {S3_KEY}")


def main():
    dry = "--dry-run" in sys.argv
    coins, source = fetch()
    doc = build(coins, source)
    by = {c["symbol"]: c for c in doc["data"]}
    for s in doc["display"]:
        c = by[s]
        tag = f"  <- {doc['featured'][s]['reason']} (replaced {doc['featured'][s]['replaced']})" if s in doc["featured"] else ""
        log(f"  {s:<5} ${c['price']:>12,.2f}  {c['change24h']:+.2f}%{tag}")
    log(f"  ({len(doc['data'])} coins in feed, {sum(c['is_stable'] for c in doc['data'])} flagged stable)")
    if dry:
        log("dry run, not uploading")
        return
    upload(doc)
    if "--no-archive" not in sys.argv:
        try:
            archive(doc)
        except Exception as e:
            log(f"WARNING archive failed (feed still published): {type(e).__name__}: {e}")
    log("done")


if __name__ == "__main__":
    main()
