# gp-creatives-jobs

Scheduled jobs for Kayzen HTML creative feeds hosted in `s3://gp-creatives/`.

## Binance price feed

`update-binance-feed.py` regenerates
`s3://gp-creatives/binance/binance-prices.json`, which the live Binance ticker
creative fetches at render time.

- **Source:** Binance public 24hr ticker API, with failover across
  `api.binance.com`, `api-gcp.binance.com`, `data-api.binance.vision`.
- **Mapping:** `lastPrice` -> `price`, `priceChangePercent` -> `change24h`.
- **Upload:** gzipped, `Content-Type: application/json`,
  `Content-Encoding: gzip`, `Cache-Control: max-age=60`.
- **Schedule:** hourly (`0 * * * *`), plus manual `workflow_dispatch`.

### Safety

The S3 object is read by a live serving creative, so the job refuses to publish
anything it cannot validate. It uploads only if all three coins are present,
each price falls inside a sane range, and `|change24h| <= 60%`. On any failure
it exits non-zero and leaves the last good feed in place, so the ad shows
stale-but-correct prices rather than zeros or a broken payload.

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
