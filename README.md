# gp-creatives-jobs

Scheduled jobs for Kayzen HTML creative feeds hosted in `s3://gp-creatives/`.

## Binance price feed

`update-binance-feed.py` regenerates
`s3://gp-creatives/binance/binance-prices.json`, which the live Binance ticker
creative fetches at render time.

- **Sources, in priority order:**
  1. **CoinMarketCap** - keyed Pro endpoint, falling back to the keyless public
     endpoint. Queried by numeric coin id, never by symbol.
  2. **Smadex xCrypto** - public CMC-derived hourly mirror. The current
     hour's file 404s until published, so the job walks back hour by hour to
     the most recent one that exists.
  3. **Binance** - own ticker API, across `data-api.binance.vision`,
     `api.binance.com`, `api-gcp.binance.com`.
- **Mapping:** CMC `price`/`percent_change_24h`, Smadex
  `price`/`usd_price_change_24h`, Binance `lastPrice`/`priceChangePercent`,
  all normalised to `price`/`change24h`.
- **Upload:** gzipped, `Content-Type: application/json`,
  `Content-Encoding: gzip`, `Cache-Control: max-age=60`.
- **Schedule:** hourly (`0 * * * *`), plus manual `workflow_dispatch`.

## Snapshot archive

After each successful publish the job also writes the same document to
`s3://gp-creatives/binance/history/prices-{YYYYMMDDHH}.json` and rebuilds
`history/index.json` with the newest 5 stamps. The portfolio's time-machine
control reads that index.

Snapshots are immutable and never deleted - a year of hourly runs is ~3.4MB,
which is a better trade than giving a scheduled job a delete path into a bucket
that also holds live creatives. Only the index rolls.

The index is built from a real S3 listing, not by computing the last N
hour-stamps. Runs do get skipped (GitHub drops scheduled jobs under load, and a
validation failure publishes nothing), so computed stamps would point at 404s
and the portfolio would render blank phones.

Archiving is secondary: if it fails, a warning is logged and the run still exits
0, because the live feed is what matters. Skip it with `--no-archive`.

### Safety

The S3 object is read by a live serving creative, so the job refuses to publish
anything it cannot validate. It uploads only if all three coins are present,
each price falls inside a sane range, and `|change24h| <= 60%`. On any failure
it exits non-zero and leaves the last good feed in place, so the ad shows
stale-but-correct prices rather than zeros or a broken payload.

A static source is also rejected if its `Last-Modified` is more than 3 hours
old. This matters for Smadex: its hourly file 404s until published, and the
date-only fallback is a 00:15 UTC snapshot that can be ~24h stale. Validation
alone cannot catch that, because day-old prices are still "sane" - they would
simply render the wrong direction (a red down-arrow during an up market).

### Known source quirks

- Binance returns **HTTP 451** for `api.binance.com` and `api-gcp.binance.com`
  from US datacenter ranges, including GitHub Actions runners. Only
  `data-api.binance.vision` answers there.
- CMC's Pro and keyless endpoints are **not interchangeable**: the public path
  rejects the API key header with a 401, and the two return different shapes
  (`data` dict-by-id vs list, `quote` dict-by-currency vs list).
- Querying CMC by **symbol** returns every token squatting the ticker (23 rows
  for BNB,BTC,ETH, including a "Bitcoin AI" at $0.001). Always use numeric ids.

### Local use

    ./update-binance-feed.py --dry-run   # fetch + validate, no upload
    ./update-binance-feed.py             # fetch + validate + upload

### Required repository secrets

| Secret | Purpose |
| --- | --- |
| `AWS_ACCESS_KEY_ID` | S3-scoped key |
| `AWS_SECRET_ACCESS_KEY` | matching secret |

The key only needs `s3:PutObject` on
`arn:aws:s3:::gp-creatives/binance/binance-prices.json`.
