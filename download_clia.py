#!/usr/bin/env python3
"""
CLIA Downloader

Downloads the full, unfiltered Active CLIA Laboratory listing as a CSV from the
CMS QCOR website and saves it into a local cache directory.

The QCOR site has no public API for this file. The "Download CSV" button on

    https://qcor.cms.gov/advanced_find_provider.jsp?which=4&backReport=active_CLIA.jsp

calls a JavaScript function, doDownload(), which re-points the providerSearch
form at prov_download.jsp and submits it. Reproducing that by hand takes three
HTTP requests:

  1. GET advanced_find_provider.jsp
     Establishes a session (JSESSIONID cookie). The export generated in step 2
     is tied to this session, so this step cannot be skipped.

  2. POST prov_download.jsp with the search form fields left empty
     Empty fields mean "no filtering", which is what we want: the whole country.
     The server builds the export and answers 302 Found with a Location header
     pointing at a one-time SecDoc URL.

  3. GET the SecDoc URL
     Returns the actual text/csv payload. This must be a GET; the endpoint
     answers 405 Method Not Allowed if it is POSTed to.

The file is written exactly as received, including the two-line "Selection
Criteria" preamble that precedes the real header row. Cleaning it up is a job
for a later step in the pipeline, not for the downloader.

Usage:
    python download_clia.py
    python download_clia.py --cache-dir some/other/dir
    python download_clia.py --verbose
"""

import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv


# The page hosting the "Download CSV" button. Also our Referer for step 2.
SEARCH_PAGE_URL = (
    'https://qcor.cms.gov/advanced_find_provider.jsp'
    '?which=4&backReport=active_CLIA.jsp'
)

# The action that doDownload() assigns to the form before submitting it.
DOWNLOAD_ACTION_URL = (
    'https://qcor.cms.gov/prov_download.jsp'
    '?which=4&provider=22&backReport=active_CLIA.jsp'
)

# The providerSearch form, submitted with no filters applied.
#
# Every text field is deliberately empty and state is "XX", which is the
# sentinel the page uses for "no state selected". The intern and exempt
# checkboxes are omitted entirely, which is how a browser submits an unchecked
# checkbox. apptype empty means "All" certification types.
UNFILTERED_FORM_DATA = {
    'name': '',                    # Facility name
    'prvdr': '',                   # CLIA ID number
    'director': '',                # CLIA lab director
    'state': 'XX',                 # "no state selected"
    'city': '',
    'zip': '',
    'report': 'active_CLIA.jsp',   # "Active CLIA Labs"
    'apptype': '',                 # "All" certification types
}

# QCOR is picky about clients that do not look like browsers.
USER_AGENT = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
)

# Name we save the export under. The server's own filename embeds an epoch
# timestamp (clia_listing1790310999092.csv), which would litter the cache with
# a new ~58MB copy on every run.
OUTPUT_FILENAME = 'clia_listing.csv'

# Used when neither .env nor --cache-dir says otherwise. Kept out of git.
DEFAULT_CACHE_DIR = 'local_cache'

DEFAULT_TIMEOUT_SECONDS = 900

# Streaming chunk size for the ~58MB download.
CHUNK_SIZE = 65536


class ConfigLoader:
    """Loads configuration from the .env file, with sane fallbacks."""

    @staticmethod
    def load_config() -> None:
        """Read .env into the environment, if a .env file exists."""
        load_dotenv()

    @staticmethod
    def get_cache_dir() -> str:
        """
        Get the directory the CSV should be written into.

        Falls back to DEFAULT_CACHE_DIR when CLIA_CACHE_DIR is unset or blank,
        which is also what happens when there is no .env file at all.
        """
        value = os.getenv('CLIA_CACHE_DIR', '').strip()
        return value if value else DEFAULT_CACHE_DIR

    @staticmethod
    def get_timeout_seconds() -> int:
        """Get the per-request network timeout in seconds."""
        raw = os.getenv('CLIA_TIMEOUT_SECONDS', '').strip()
        if not raw:
            return DEFAULT_TIMEOUT_SECONDS
        try:
            return int(raw)
        except ValueError:
            logging.warning(
                'download_clia.py Warning: CLIA_TIMEOUT_SECONDS must be an '
                f'integer, got {raw!r}; using default of '
                f'{DEFAULT_TIMEOUT_SECONDS}'
            )
            return DEFAULT_TIMEOUT_SECONDS


class CliaDownloader:
    """Reproduces the QCOR "Download CSV" button as a sequence of requests."""

    @staticmethod
    def build_session() -> requests.Session:
        """Create a session that presents itself as an ordinary browser."""
        session = requests.Session()
        session.headers.update({
            'User-Agent': USER_AGENT,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        })
        return session

    @staticmethod
    def establish_session(*, session: requests.Session, timeout: int) -> None:
        """
        Load the search page so the server hands us a JSESSIONID.

        The export produced by request_export() is tied to this session, so
        skipping this step causes the download to fail.

        Raises:
            RuntimeError: If the page cannot be loaded or sets no session cookie.
        """
        logging.info(f'Establishing session: {SEARCH_PAGE_URL}')

        try:
            response = session.get(SEARCH_PAGE_URL, timeout=timeout)
        except requests.RequestException as exc:
            raise RuntimeError(
                f'could not load the CLIA search page: {exc}'
            ) from exc

        if response.status_code != 200:
            raise RuntimeError(
                'the CLIA search page returned HTTP '
                f'{response.status_code} (expected 200)'
            )

        if 'JSESSIONID' not in session.cookies:
            raise RuntimeError(
                'the CLIA search page did not set a JSESSIONID cookie; '
                'the QCOR site may have changed'
            )

        logging.debug('  JSESSIONID acquired')

    @staticmethod
    def request_export(*, session: requests.Session, timeout: int) -> str:
        """
        Submit the unfiltered search form and return the export's download URL.

        This is what doDownload() does in the browser. The server generates the
        export (which takes roughly ten seconds) and replies 302 Found with a
        Location header pointing at a SecDoc URL.

        Redirects are deliberately not followed here. requests would downgrade
        the redirected POST to a GET and silently fetch the file, but handling
        it explicitly lets us log the URL and fail with a clear message when
        the response is not the redirect we expect.

        Returns:
            Absolute URL of the generated export.

        Raises:
            RuntimeError: If the server does not answer with a usable redirect.
        """
        logging.info(f'Requesting export: {DOWNLOAD_ACTION_URL}')
        logging.info('  (the server may take ~10 seconds to build the file)')

        try:
            response = session.post(
                DOWNLOAD_ACTION_URL,
                data=UNFILTERED_FORM_DATA,
                headers={'Referer': SEARCH_PAGE_URL},
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f'the export request failed: {exc}') from exc

        if response.status_code not in (301, 302, 303, 307, 308):
            snippet = response.text[:300].replace('\n', ' ').strip()
            raise RuntimeError(
                f'expected a redirect to the generated file but got HTTP '
                f'{response.status_code}. The QCOR site may have changed. '
                f'Response began: {snippet!r}'
            )

        location = response.headers.get('Location', '').strip()
        if not location:
            raise RuntimeError(
                f'the server returned HTTP {response.status_code} but no '
                'Location header, so there is no file to download'
            )

        # Defensive: the observed Location is absolute, but resolve it against
        # the action URL in case that ever changes to a relative path.
        download_url = urljoin(DOWNLOAD_ACTION_URL, location)
        logging.debug(f'  Export URL: {download_url}')

        return download_url

    @staticmethod
    def fetch_export(
        *,
        session: requests.Session,
        download_url: str,
        destination: Path,
        timeout: int,
    ) -> int:
        """
        Stream the generated CSV to disk.

        Must be a GET; the SecDoc endpoint answers 405 Method Not Allowed to a
        POST. The body is written to a temporary file in the destination's
        directory and only moved into place once it has downloaded completely,
        so an interrupted run can never leave a truncated file behind that
        looks like a good one.

        Returns:
            Number of bytes written.

        Raises:
            RuntimeError: If the download fails or does not return a CSV.
        """
        logging.info('Downloading the CSV')

        try:
            with session.get(
                download_url,
                headers={'Referer': SEARCH_PAGE_URL},
                timeout=timeout,
                stream=True,
            ) as response:
                if response.status_code != 200:
                    raise RuntimeError(
                        f'downloading the export returned HTTP '
                        f'{response.status_code} (expected 200)'
                    )

                # Guard against receiving an HTML error page. Without this the
                # script would happily save a few hundred bytes of Tomcat error
                # markup under a .csv name.
                content_type = response.headers.get('Content-Type', '')
                if 'csv' not in content_type.lower():
                    snippet = response.text[:300].replace('\n', ' ').strip()
                    raise RuntimeError(
                        f'expected a CSV but the server sent Content-Type '
                        f'{content_type!r}. The QCOR site may have changed. '
                        f'Response began: {snippet!r}'
                    )

                disposition = response.headers.get('Content-Disposition', '')
                if disposition:
                    logging.debug(f'  Content-Disposition: {disposition}')

                bytes_written = CliaDownloader._stream_to_file(
                    response=response,
                    destination=destination,
                )
        except requests.RequestException as exc:
            raise RuntimeError(f'downloading the export failed: {exc}') from exc

        if bytes_written == 0:
            destination.unlink(missing_ok=True)
            raise RuntimeError('the server sent an empty file')

        return bytes_written

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
    def _stream_to_file(
        *,
        response: requests.Response,
        destination: Path,
    ) -> int:
        """
        Write a streaming response to destination atomically.

        Returns:
            Number of bytes written.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)

        # Create the temp file alongside the destination so the final rename
        # stays on one filesystem and is therefore atomic.
        handle, temp_name = tempfile.mkstemp(
            dir=str(destination.parent),
            prefix=f'.{destination.name}.',
            suffix='.part',
        )
        temp_path = Path(temp_name)

        bytes_written = 0
        try:
            with os.fdopen(handle, 'wb') as temp_file:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        temp_file.write(chunk)
                        bytes_written += len(chunk)

            # mkstemp creates the file 0600. This is public data headed for a
            # shared cache, so relax it to the normal default for new files
            # (0644 before umask) instead of leaving it owner-only.
            os.chmod(temp_path, 0o666 & ~CliaDownloader._current_umask())

            os.replace(temp_path, destination)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

        return bytes_written

    @staticmethod
    def run(*, cache_dir: str, timeout: int) -> Path:
        """
        Download the full CLIA listing into cache_dir.

        Returns:
            Path of the saved CSV.
        """
        destination = Path(cache_dir).expanduser() / OUTPUT_FILENAME

        session = CliaDownloader.build_session()
        try:
            CliaDownloader.establish_session(session=session, timeout=timeout)
            download_url = CliaDownloader.request_export(
                session=session,
                timeout=timeout,
            )
            bytes_written = CliaDownloader.fetch_export(
                session=session,
                download_url=download_url,
                destination=destination,
                timeout=timeout,
            )
        finally:
            session.close()

        megabytes = bytes_written / (1024 * 1024)
        logging.info(
            f'Saved {bytes_written:,} bytes ({megabytes:.1f} MB) to '
            f'{destination}'
        )

        return destination


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description=(
            'Download the complete, unfiltered Active CLIA Laboratory listing '
            'from the CMS QCOR website.'
        ),
        epilog=(
            'Configuration is read from .env (see example.env). '
            'Command-line options take precedence over .env values.'
        ),
    )
    parser.add_argument(
        '--cache-dir',
        default=None,
        help=(
            'Directory to save the CSV into. Overrides CLIA_CACHE_DIR from '
            f'.env. Defaults to {DEFAULT_CACHE_DIR}.'
        ),
    )
    parser.add_argument(
        '--timeout',
        type=int,
        default=None,
        help=(
            'Per-request network timeout in seconds. Overrides '
            'CLIA_TIMEOUT_SECONDS from .env. Defaults to '
            f'{DEFAULT_TIMEOUT_SECONDS}.'
        ),
    )
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Show additional detail about each request.',
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(message)s',
    )

    ConfigLoader.load_config()

    cache_dir = args.cache_dir or ConfigLoader.get_cache_dir()
    timeout = args.timeout or ConfigLoader.get_timeout_seconds()

    logging.info(f'Cache directory: {cache_dir}')

    try:
        CliaDownloader.run(cache_dir=cache_dir, timeout=timeout)
    except RuntimeError as exc:
        logging.error(f'download_clia.py Error: {exc}')
        sys.exit(1)
    except OSError as exc:
        logging.error(f'download_clia.py Error: could not write the file: {exc}')
        sys.exit(1)
    except KeyboardInterrupt:
        logging.error('download_clia.py Error: interrupted')
        sys.exit(130)


if __name__ == '__main__':
    main()
