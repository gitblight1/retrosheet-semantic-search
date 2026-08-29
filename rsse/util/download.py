"""Corpus acquisition (spec/01-CORPUS.md §2, §4).

Downloads Retrosheet's per-season event archives. Source files are treated as
immutable: each is recorded with its SHA-256 so a later re-ingest can detect
that Retrosheet has reissued a corrected file rather than silently mixing
corpus vintages.

Retrosheet is a volunteer-run nonprofit and the fetch is deliberately slow:
sequential, with a delay between requests and exponential backoff on failure.
A first pass at 1s between requests had the server stop responding after ~54
archives, so the default delay is conservative. If a fetch stalls, wait rather
than retrying harder.
"""

from __future__ import annotations

import hashlib
import re
import time
import urllib.request
import zipfile
from pathlib import Path

BASE = "https://www.retrosheet.org"
INDEX = f"{BASE}/game.htm"
UA = "rsse-corpus-sweep/0.1 (research; contact via repository)"
DELAY_SECONDS = 5.0
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = 30.0
TIMEOUT_SECONDS = 120


def _get(url: str, attempts: int = MAX_ATTEMPTS) -> bytes:
    """Fetch a URL, backing off on failure rather than retrying immediately."""
    last: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            wait = BACKOFF_SECONDS * (2 ** (attempt - 1))
            print(f"    retry {attempt}/{attempts - 1} in {wait:.0f}s", flush=True)
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001 - retried, then reported
            last = exc
    raise RuntimeError(f"{url}: giving up after {attempts} attempts: {last}")


def available_years() -> list[int]:
    """Years for which a season event archive is published."""
    html = _get(INDEX).decode("latin-1")
    years = {int(m) for m in re.findall(r"/events/(\d{4})eve\.zip", html)}
    return sorted(years)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_season(year: int, dest: Path) -> Path | None:
    """Download and extract one season. Returns the season directory."""
    dest.mkdir(parents=True, exist_ok=True)
    season_dir = dest / str(year)
    zip_path = dest / f"{year}eve.zip"

    if not zip_path.exists():
        try:
            blob = _get(f"{BASE}/events/{year}eve.zip")
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            print(f"  {year}: download failed: {exc}", flush=True)
            return None
        # Write to a temporary name first: a truncated archive left at the real
        # path would be taken for a complete one on the next run.
        tmp = zip_path.with_suffix(".zip.part")
        tmp.write_bytes(blob)
        tmp.replace(zip_path)
        time.sleep(DELAY_SECONDS)

    season_dir.mkdir(exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(season_dir)
    except zipfile.BadZipFile:
        print(f"  {year}: corrupt archive, removing")
        zip_path.unlink(missing_ok=True)
        return None
    return season_dir
