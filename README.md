# NPD CLIA Lab Slurp

Downloads the CLIA data from the [QCOR CLIA Lookup page](https://qcor.cms.gov/advanced_find_provider.jsp?which=4&backReport=active_CLIA.jsp).

`download_clia.py` fetches the complete, unfiltered list of active CLIA
laboratories as a CSV and saves it to a local cache directory. No search
filters are applied, so the result is the whole country: roughly 298,000 rows
and about 58 MB.

## Setup

Requires Python 3.9 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

cp example.env .env   # optional; sensible defaults are built in
```

## Usage

The pipeline is two steps: download the CSV, then split it into per-laboratory
JSON.

```bash
python download_clia.py        # step 1: fetch the CSV
python split_clia_to_json.py   # step 2: split it into one JSON file per lab
```

The CSV is written to `local_cache/clia_listing.csv` by default. The JSON goes
to the companion cache repository (see [The JSON cache](#the-json-cache)).

### Options

`download_clia.py`:

| Option | Description |
| :----- | :---------- |
| `--cache-dir DIR` | Where to save the CSV. Overrides `CLIA_CACHE_DIR`. |
| `--timeout SECONDS` | Per-request network timeout. Overrides `CLIA_TIMEOUT_SECONDS`. |
| `--verbose` | Show additional detail about each request. |

`split_clia_to_json.py`:

| Option | Description |
| :----- | :---------- |
| `--csv-path PATH` | CSV to read. Defaults to `<CLIA_CACHE_DIR>/clia_listing.csv`. |
| `--json-dir DIR` | Where to write the JSON. Overrides `CLIA_JSON_DIR`. |
| `--dry-run` | Report what would change without writing anything. |
| `--verbose` | List every file created, changed, or removed. |

### Configuration

Settings are read from `.env` (see `example.env`). Command-line options take
precedence.

| Variable | Default | Description |
| :------- | :------ | :---------- |
| `CLIA_CACHE_DIR` | `local_cache` | Directory the CSV is saved into. Created if missing. |
| `CLIA_TIMEOUT_SECONDS` | `900` | Per-request network timeout in seconds. |
| `CLIA_JSON_DIR` | `../npd_slurp_clia_lab_cache/cache/json` | Directory the per-laboratory JSON is written into. |

If there is no `.env` file, the download goes to `local_cache`, which is
excluded from git.

## The JSON cache

`split_clia_to_json.py` writes one JSON file per laboratory into a **separate
repository**, [npd_slurp_clia_lab_cache](https://github.com/ftrotter-gov/npd_slurp_clia_lab_cache),
checked out alongside this one:

```
../npd_slurp_clia_lab_cache/cache/json/<STATE>/<CLIA_ID>.json
```

For example, `cache/json/TX/45D2058986.json`. Laboratories outside the United
States have no state in the source data; they all carry the CLIA prefix `99`
and are filed under `INTL`. The current listing produces 298,115 files across
57 directories.

Keys come from the CSV column headings with non-alphanumeric characters
replaced by underscores, so `CLIA ID Number` becomes `CLIA_ID_Number` and
`Zip Code` becomes `Zip_Code`.

A file is rewritten **only when its contents actually change**. Unchanged files
are not touched at all, which keeps the cache repository's history meaningful:
each commit shows exactly which laboratories were added, changed, or
decertified. Laboratories that drop out of the listing are deleted.

Because files are filed by state, a laboratory that corrects its state appears
as a deletion in the old state's directory and an addition in the new one.

## Output

The file is saved byte-for-byte as the server sends it. Note that the real
header is on the **third** line; two lines of search-criteria preamble come
first:

```
"Selection Criteria",""
"Search for:","CLIA Laboratory"
"CLIA ID Number","Facility Name","Lab Director","Address","City","State","Zip Code","Phone","Certificate Effective Date","Certificate Expiration Date","Certificate Type","Accrediting Organization","Facility Type"
```

Any cleanup belongs in a later pipeline step, so that the cached copy stays a
faithful record of what CMS actually published.

The CSV is encoded **cp1252, not UTF-8** — decoding it as UTF-8 raises
`UnicodeDecodeError` partway through. `split_clia_to_json.py` handles this and
emits UTF-8 JSON. (One Puerto Rico record arrives already mis-encoded at the
source, as `FRANCISCO DÃ?VILA TORO`; it is preserved as received rather than
guessed at.)

## How the download works

QCOR offers no public API for this file, so the script reproduces what the
browser's "Download CSV" button does. That button calls a JavaScript function,
`doDownload()`, which re-points the `providerSearch` form at
`prov_download.jsp` and submits it. Reproducing it takes three requests:

1. **`GET advanced_find_provider.jsp`** — establishes a session (`JSESSIONID`
   cookie). The export in step 2 is tied to this session, so this cannot be
   skipped.
2. **`POST prov_download.jsp`** with every form field left empty (and
   `state=XX`, the page's "no state selected" sentinel). Empty means no
   filtering. The server spends about ten seconds building the export, then
   answers `302 Found` with a `Location` header pointing at a one-time
   `SecDoc` URL.
3. **`GET` the `SecDoc` URL** — returns the `text/csv` payload. This must be a
   `GET`; the endpoint answers `405 Method Not Allowed` to a `POST`.

The download is streamed to a temporary file and moved into place only once it
completes, so an interrupted run cannot leave a truncated file that looks
valid.

**Caveat:** this is an undocumented internal JSP flow, not a stable public
API. If CMS changes these pages the script will break. Each step is therefore
checked and fails with a specific error rather than silently saving an HTML
error page under a `.csv` name.

