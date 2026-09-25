#!/usr/bin/env python3
"""
CLIA CSV Splitter

Splits the CLIA listing CSV downloaded by download_clia.py into one JSON file
per laboratory, laid out for use as a git-backed text database:

    <json_dir>/<STATE>/<CLIA_ID>.json

The output directory is normally the cache repository that sits alongside this
one, npd_slurp_clia_lab_cache, so that each run produces a reviewable commit
showing exactly which laboratories were added, changed, or decertified.

To make that history meaningful, a file is written only when its contents
actually differ from what is already on disk. Files whose data has not changed
are left completely untouched, so git reports only genuine changes rather than
roughly 298,000 rewrites every week.

Two details of the source data drive the implementation:

  * The CSV is encoded cp1252, not UTF-8. Decoding it as UTF-8 raises
    UnicodeDecodeError partway through the file. Output JSON is always UTF-8.

  * The real header is on the third line. Two lines of search-criteria
    preamble come first and are skipped.

Usage:
    python split_clia_to_json.py
    python split_clia_to_json.py --json-dir some/other/dir
    python split_clia_to_json.py --dry-run
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Set

from dotenv import load_dotenv


# Number of leading lines before the real header row.
PREAMBLE_LINES = 2

# The source CSV is cp1252. See the module docstring.
SOURCE_ENCODING = 'cp1252'

# Column whose value names the per-laboratory file, after key normalization.
ID_KEY = 'CLIA_ID_Number'

# Column used to shard laboratories into subdirectories.
STATE_KEY = 'State'

# Laboratories outside the United States carry no state in the source data.
# They all share the CLIA prefix 99 and are filed together here.
INTERNATIONAL_DIR = 'INTL'

# Defaults used when neither .env nor a command-line option says otherwise.
DEFAULT_CACHE_DIR = 'local_cache'
DEFAULT_JSON_DIR = '../npd_slurp_clia_lab_cache/cache/json'

# Name of the CSV produced by download_clia.py.
CSV_FILENAME = 'clia_listing.csv'


class ConfigLoader:
    """Loads configuration from the .env file, with sane fallbacks."""

    @staticmethod
    def load_config() -> None:
        """Read .env into the environment, if a .env file exists."""
        load_dotenv()

    @staticmethod
    def get_cache_dir() -> str:
        """Get the directory holding the downloaded CSV."""
        value = os.getenv('CLIA_CACHE_DIR', '').strip()
        return value if value else DEFAULT_CACHE_DIR

    @staticmethod
    def get_json_dir() -> str:
        """Get the directory the per-laboratory JSON files are written into."""
        value = os.getenv('CLIA_JSON_DIR', '').strip()
        return value if value else DEFAULT_JSON_DIR


class CliaSplitter:
    """Turns the CLIA listing CSV into one JSON file per laboratory."""

    @staticmethod
    def normalize_key(*, column_name: str) -> str:
        """
        Turn a CSV column heading into a JSON key.

        Runs of characters that are neither letters nor digits collapse into a
        single underscore, and any leading or trailing underscores are removed.
        "CLIA ID Number" becomes "CLIA_ID_Number" and "Zip Code" becomes
        "Zip_Code".
        """
        return re.sub(r'[^A-Za-z0-9]+', '_', column_name).strip('_')

    @staticmethod
    def read_records(*, csv_path: Path) -> List[Dict[str, str]]:
        """
        Read the CSV and return one dict per laboratory.

        Raises:
            RuntimeError: If the file is missing or not shaped as expected.
        """
        if not csv_path.is_file():
            raise RuntimeError(
                f'no CSV found at {csv_path}. Run download_clia.py first.'
            )

        logging.info(f'Reading {csv_path}')

        with csv_path.open('r', encoding=SOURCE_ENCODING, newline='') as f:
            reader = csv.reader(f)
            rows = list(reader)

        if len(rows) <= PREAMBLE_LINES:
            raise RuntimeError(
                f'{csv_path} has no data rows; it may be a truncated or '
                'failed download'
            )

        header = [
            CliaSplitter.normalize_key(column_name=name)
            for name in rows[PREAMBLE_LINES]
        ]

        # Fail loudly rather than silently writing files named "None.json" if
        # CMS ever renames or reorders the columns.
        for required in (ID_KEY, STATE_KEY):
            if required not in header:
                raise RuntimeError(
                    f'expected a {required!r} column but the header is '
                    f'{header}. The CLIA export format may have changed.'
                )

        records = []
        for line_number, row in enumerate(
            rows[PREAMBLE_LINES + 1:],
            start=PREAMBLE_LINES + 2,
        ):
            # Trailing blank lines are normal at the end of the export.
            if not row or not any(cell.strip() for cell in row):
                continue

            if len(row) != len(header):
                raise RuntimeError(
                    f'line {line_number} has {len(row)} fields but the header '
                    f'has {len(header)}. The CLIA export format may have '
                    'changed.'
                )

            records.append(dict(zip(header, row)))

        logging.info(f'Read {len(records):,} laboratories')

        return records

    @staticmethod
    def relative_path_for(*, record: Dict[str, str]) -> Path:
        """
        Work out the <STATE>/<CLIA_ID>.json path for one laboratory.

        Raises:
            RuntimeError: If the identifier is missing or unsafe as a filename.
        """
        clia_id = record.get(ID_KEY, '').strip()

        if not clia_id:
            raise RuntimeError(
                f'a row has no {ID_KEY}, so it cannot be given a filename: '
                f'{record}'
            )

        # Observed identifiers are always ten characters of [0-9A-Z]. Verify
        # rather than assume, since these become paths on disk.
        if not re.fullmatch(r'[A-Za-z0-9]+', clia_id):
            raise RuntimeError(
                f'{ID_KEY} {clia_id!r} contains characters that are not safe '
                'in a filename'
            )

        state = record.get(STATE_KEY, '').strip().upper()

        # International laboratories have no state in the source data.
        if not state:
            state = INTERNATIONAL_DIR
        elif not re.fullmatch(r'[A-Z0-9]+', state):
            raise RuntimeError(
                f'{STATE_KEY} {state!r} for laboratory {clia_id} contains '
                'characters that are not safe in a directory name'
            )

        return Path(state) / f'{clia_id}.json'

    @staticmethod
    def serialize(*, record: Dict[str, str]) -> str:
        """
        Render one laboratory as JSON text.

        Key order follows the CSV columns and the formatting is fixed, so that
        an unchanged laboratory always produces byte-identical output and git
        sees no diff.
        """
        return json.dumps(record, indent=2, ensure_ascii=False) + '\n'

    @staticmethod
    def write_if_changed(*, path: Path, content: str, dry_run: bool) -> str:
        """
        Write content to path only if it differs from what is already there.

        Leaving unchanged files alone is the whole point of this script: it
        keeps their modification times intact and keeps them out of git's
        change list.

        Returns:
            'unchanged', 'changed', or 'created'.
        """
        desired = content.encode('utf-8')

        if path.is_file():
            if path.read_bytes() == desired:
                return 'unchanged'
            outcome = 'changed'
        else:
            outcome = 'created'

        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            CliaSplitter._atomic_write(path=path, payload=desired)

        return outcome

    @staticmethod
    def _atomic_write(*, path: Path, payload: bytes) -> None:
        """Write payload to path via a temporary file in the same directory."""
        handle, temp_name = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f'.{path.name}.',
            suffix='.tmp',
        )
        temp_path = Path(temp_name)

        try:
            with os.fdopen(handle, 'wb') as temp_file:
                temp_file.write(payload)

            # mkstemp creates the file 0600; this is public data in a shared
            # repository, so use the normal default for new files instead.
            os.chmod(temp_path, 0o666 & ~CliaSplitter._current_umask())

            os.replace(temp_path, path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _current_umask() -> int:
        """
        Read the process umask without permanently changing it.

        There is no read-only way to query the umask, so it must be set and
        then restored.
        """
        mask = os.umask(0o022)
        os.umask(mask)
        return mask

    @staticmethod
    def find_existing(*, json_dir: Path) -> Set[Path]:
        """
        List the JSON files already present, relative to json_dir.

        Used to spot laboratories that have dropped out of the CLIA listing
        since the last run.
        """
        if not json_dir.is_dir():
            return set()

        return {
            path.relative_to(json_dir)
            for path in json_dir.glob('*/*.json')
        }

    @staticmethod
    def remove_stale(
        *,
        json_dir: Path,
        stale: Set[Path],
        dry_run: bool,
    ) -> None:
        """
        Delete JSON files for laboratories no longer in the listing.

        A laboratory that loses its certification simply stops appearing in
        the export. Removing its file here means the loss shows up as a
        deletion in the cache repository's history.
        """
        for relative_path in sorted(stale):
            logging.debug(f'  Removing {relative_path}')

            if dry_run:
                continue

            full_path = json_dir / relative_path
            full_path.unlink(missing_ok=True)

            # Tidy up a state directory once its last laboratory is gone.
            try:
                full_path.parent.rmdir()
            except OSError:
                pass

    @staticmethod
    def run(*, csv_path: Path, json_dir: Path, dry_run: bool) -> Dict[str, int]:
        """
        Split the CSV into per-laboratory JSON files.

        Returns:
            Counts keyed by 'created', 'changed', 'unchanged' and 'deleted'.
        """
        records = CliaSplitter.read_records(csv_path=csv_path)

        existing = CliaSplitter.find_existing(json_dir=json_dir)
        logging.info(f'Found {len(existing):,} existing JSON files')

        if dry_run:
            logging.info('Dry run: no files will be written or removed')

        counts = {'created': 0, 'changed': 0, 'unchanged': 0, 'deleted': 0}
        written: Set[Path] = set()

        for record in records:
            relative_path = CliaSplitter.relative_path_for(record=record)

            # Duplicate identifiers would silently overwrite one another.
            if relative_path in written:
                raise RuntimeError(
                    f'two laboratories share the path {relative_path}; '
                    f'{ID_KEY} was expected to be unique'
                )

            outcome = CliaSplitter.write_if_changed(
                path=json_dir / relative_path,
                content=CliaSplitter.serialize(record=record),
                dry_run=dry_run,
            )

            counts[outcome] += 1
            written.add(relative_path)

            if outcome != 'unchanged':
                logging.debug(f'  {outcome}: {relative_path}')

        stale = existing - written
        counts['deleted'] = len(stale)

        if stale:
            logging.info(
                f'Removing {len(stale):,} laboratories no longer in the listing'
            )
            CliaSplitter.remove_stale(
                json_dir=json_dir,
                stale=stale,
                dry_run=dry_run,
            )

        return counts


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description=(
            'Split the downloaded CLIA listing CSV into one JSON file per '
            'laboratory, arranged as <STATE>/<CLIA_ID>.json.'
        ),
        epilog=(
            'Configuration is read from .env (see example.env). '
            'Command-line options take precedence over .env values.'
        ),
    )
    parser.add_argument(
        '--csv-path',
        default=None,
        help=(
            'Path to the CLIA CSV. Defaults to '
            f'<CLIA_CACHE_DIR>/{CSV_FILENAME}.'
        ),
    )
    parser.add_argument(
        '--json-dir',
        default=None,
        help=(
            'Directory to write the JSON files into. Overrides CLIA_JSON_DIR '
            f'from .env. Defaults to {DEFAULT_JSON_DIR}.'
        ),
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Report what would change without writing or removing anything.',
    )
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='List every file that is created, changed, or removed.',
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(message)s',
    )

    ConfigLoader.load_config()

    if args.csv_path:
        csv_path = Path(args.csv_path).expanduser()
    else:
        csv_path = Path(ConfigLoader.get_cache_dir()).expanduser() / CSV_FILENAME

    json_dir = Path(args.json_dir or ConfigLoader.get_json_dir()).expanduser()

    logging.info(f'JSON directory: {json_dir}')

    try:
        counts = CliaSplitter.run(
            csv_path=csv_path,
            json_dir=json_dir,
            dry_run=args.dry_run,
        )
    except RuntimeError as exc:
        logging.error(f'split_clia_to_json.py Error: {exc}')
        sys.exit(1)
    except OSError as exc:
        logging.error(f'split_clia_to_json.py Error: file error: {exc}')
        sys.exit(1)
    except KeyboardInterrupt:
        logging.error('split_clia_to_json.py Error: interrupted')
        sys.exit(130)

    logging.info(
        f"Created {counts['created']:,}, changed {counts['changed']:,}, "
        f"unchanged {counts['unchanged']:,}, deleted {counts['deleted']:,}"
    )


if __name__ == '__main__':
    main()
