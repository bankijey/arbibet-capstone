# Arbibet web

The public site: market signals, betting slips and fixture deep dives. Next.js
(App Router) on Vercel, Tailwind, ECharts.

## How it gets its data

It never queries Snowflake. `publish/snapshot.py` runs at the end of every
pipeline run, while the warehouse is already awake, and writes gzipped JSON:

| File | What | Written |
| --- | --- | --- |
| `live.json.gz` | headline numbers, arbitrage and EV with charts, backtest, popular slips, deep dives for upcoming and just-played fixtures | every signals run, if anything changed |
| `archive/<day>.json.gz` | deep dives for one Berlin day of played, slipped fixtures | once the day is complete; the last 3 days again daily |
| `flags.json` | viewer "not on the site" flags | by `/api/flags` |

Pages render from those files and are cached (ISR). After each upload the
publisher calls `/api/revalidate`, so the site updates within seconds of a run;
the hourly `revalidate` is only a fallback. Everything interactive (editable
odds, stake split, EV threshold, backtest timing, filters) runs in the browser.

Vercel Blob Hobby includes 2,000 uploads a month and blocks the store for 30
days past that, so the publisher uploads only files whose content changed: one
live file per hourly run is at most ~720 a month.

## Local development

```bash
python publish/snapshot.py   # from the repo root: writes web/.data/
cd web && npm install && npm run dev
```

Without `SNAPSHOT_BASE_URL` the app reads `web/.data/`.

## Deploying on Vercel

1. New Project → import the GitHub repo → **Root Directory `web`**.
2. Storage → Create → **Blob**, access **Public**, connect it to the project.
   That adds `BLOB_READ_WRITE_TOKEN` to the project.
3. Project environment variables:
   - `SNAPSHOT_BASE_URL`: the store's base URL,
     `https://<store-id>.public.blob.vercel-storage.com`
   - `REVALIDATE_SECRET`: any long random string
4. In the pipeline's `.env`: `BLOB_READ_WRITE_TOKEN` (same token),
   `WEB_REVALIDATE_URL=https://<app>.vercel.app/api/revalidate` and
   `WEB_REVALIDATE_SECRET` (same secret).
5. Run `python publish/snapshot.py` once to upload the first snapshot and the
   archive, then redeploy (or wait for the next run).
