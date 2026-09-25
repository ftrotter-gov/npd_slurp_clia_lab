# local_cache

This is the default cache directory for `download_clia.py`.

Running the downloader writes `clia_listing.csv` here (roughly 58 MB, about
298,000 rows). That file is deliberately **not** committed to git: it is large,
it changes weekly, and it can be regenerated at any time by re-running the
downloader.

```bash
python download_clia.py
```

To cache somewhere else, set `CLIA_CACHE_DIR` in your `.env` file, or pass
`--cache-dir`.

Everything in this directory is ignored by git except this ReadMe, which exists
so the directory is present in a fresh clone.
