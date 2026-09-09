"""Corpus acquisition (spec/01-CORPUS.md §2, §4).

Event archives, and the game logs used to check the replay against numbers
Retrosheet published independently of the event files.

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


#: Candidate locations for the game log index, tried in order.
#:
#: **Unverified.** retrosheet.org was unreachable from the development machine
#: when this was written -- DNS resolved and TCP to :443 timed out, while other
#: hosts connected immediately -- so no path here has been confirmed against
#: the live site. A list is used rather than a single guess because a wrong
#: path costs a request and requests are the scarce resource: this project has
#: already been rate-limited off the server once (BUILD-LOG §3.3).
#:
#: If all of these fail, the game logs can be unzipped into `data/gamelogs/`
#: by hand and `rsse gamelogs` will load them without any network access.
GAMELOG_INDEX_CANDIDATES = (
    f"{BASE}/gamelogs/index.html",
    f"{BASE}/gamelogs/",
    f"{BASE}/gamelogs.htm",
)
#: The first candidate, for error messages that need something to name.
GAMELOG_INDEX = GAMELOG_INDEX_CANDIDATES[0]
GAMELOG_LINK = re.compile(r'href="([^"]*gl[^"]*\.zip)"', re.I)


def available_gamelogs() -> list[str]:
    """URLs of the published game log archives.

    Tries each candidate index once -- not `MAX_ATTEMPTS` times -- because a
    wrong path is not a transient failure and retrying it four times with
    backoff is indistinguishable from hammering.
    """
    html = None
    tried = []
    for index in GAMELOG_INDEX_CANDIDATES:
        try:
            html = _get(index, attempts=1).decode("latin-1")
        except Exception as exc:  # noqa: BLE001 - reported below
            tried.append(f"{index}: {exc}")
            time.sleep(DELAY_SECONDS)
            continue
        if GAMELOG_LINK.search(html):
            break
        tried.append(f"{index}: fetched, but no gl*.zip links found")
        html = None
        time.sleep(DELAY_SECONDS)

    if html is None:
        raise RuntimeError(
            "could not find the game log index. Tried:\n  "
            + "\n  ".join(tried)
            + "\nDownload the gl*.zip archives by hand and unzip them into "
              "data/gamelogs/; `rsse gamelogs` needs no network access.")

    urls = []
    for href in GAMELOG_LINK.findall(html):
        if href.startswith("http"):
            urls.append(href)
        elif href.startswith("/"):
            urls.append(BASE + href)
        else:
            urls.append(f"{BASE}/gamelogs/{href.lstrip('./')}")
    return sorted(set(urls))


def fetch_gamelogs(dest: Path, urls: list[str] | None = None) -> list[Path]:
    """Download and extract the game log archives into ``dest``.

    Same throttling as the event fetch, and for the same reason: a first pass
    at 1s between requests had the server stop responding. The archives are
    small -- one line per game rather than one per play -- so this is a short
    fetch, but it is not a reason to hurry.
    """
    dest.mkdir(parents=True, exist_ok=True)
    if urls is None:
        urls = available_gamelogs()
        time.sleep(DELAY_SECONDS)

    extracted: list[Path] = []
    for url in urls:
        name = url.rsplit("/", 1)[-1]
        zip_path = dest / name
        if not zip_path.exists():
            try:
                blob = _get(url)
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                print(f"  {name}: download failed: {exc}", flush=True)
                continue
            tmp = zip_path.with_suffix(".zip.part")
            tmp.write_bytes(blob)
            tmp.replace(zip_path)
            time.sleep(DELAY_SECONDS)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                names = [n for n in zf.namelist() if not n.endswith("/")]
                zf.extractall(dest)
            extracted.extend(dest / n for n in names)
        except zipfile.BadZipFile:
            print(f"  {name}: corrupt archive, removing", flush=True)
            zip_path.unlink(missing_ok=True)
    return extracted


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
