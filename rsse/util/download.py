"""Corpus acquisition (spec/01-CORPUS.md §2, §4).

Event archives, the game logs used to check the replay against numbers
Retrosheet published independently of the event files, and the Negro Leagues
archive that carries the whole-corpus reference CSVs and the 704 roster files
the season archives do not ship.

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
import json
import re
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

BASE = "https://www.retrosheet.org"
INDEX = f"{BASE}/game.htm"
UA = "rsse-corpus-sweep/0.1 (research; contact via repository)"
DELAY_SECONDS = 5.0
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = 30.0
TIMEOUT_SECONDS = 120


class NotModified(Exception):
    """The server answered 304: the copy on disk is current.

    An exception rather than a return value because every caller must handle
    it -- a 304 carries no body, so anything that treats the result as bytes
    would silently write an empty file.
    """


#: Retried with backoff. Everything else in the 4xx range is a statement about
#: the request, not the server, and retrying it four times with exponential
#: backoff is indistinguishable from hammering -- which is how this project
#: was rate-limited off the server once (BUILD-LOG §3.3). A wrong URL now
#: costs one request instead of four and 210 seconds of sleeping.
_RETRYABLE = frozenset({408, 429, 500, 502, 503, 504})


def _get(url: str, attempts: int = MAX_ATTEMPTS, etag: str | None = None,
         last_modified: str | None = None,
         with_headers: bool = False) -> bytes | tuple[bytes, dict]:
    """Fetch a URL, backing off on failure rather than retrying immediately.

    With `etag` or `last_modified`, the request is conditional and a 304
    raises `NotModified`. Retrosheet reissues corrected files, so "is the copy
    on disk still the published one" is a question the corpus has to be able
    to ask; asking it costs one request and no bytes.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            wait = BACKOFF_SECONDS * (2 ** (attempt - 1))
            print(f"    retry {attempt}/{attempts - 1} in {wait:.0f}s", flush=True)
            time.sleep(wait)
        headers = {"User-Agent": UA}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                body = resp.read()
                return (body, dict(resp.headers)) if with_headers else body
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                raise NotModified(url) from None
            if exc.code not in _RETRYABLE:
                raise RuntimeError(f"{url}: HTTP {exc.code} {exc.reason}") from None
            last = exc
        except Exception as exc:  # noqa: BLE001 - retried, then reported
            last = exc
    raise RuntimeError(f"{url}: giving up after {attempts} attempts: {last}")


#: Sidecar beside the season archives, not a table in the corpus database.
#: `rsse fetch` runs before any database exists, and what it is tracking is
#: the state of a directory of zip files.
STATE_FILE = ".fetch-state.json"


def load_state(dest: Path) -> dict:
    """Per-URL `{etag, last_modified, sha256, fetched_at}`, or empty."""
    try:
        return json.loads((dest / STATE_FILE).read_text())
    except (OSError, ValueError):
        return {}


def save_state(dest: Path, state: dict) -> None:
    tmp = dest / (STATE_FILE + ".part")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(dest / STATE_FILE)


def _validators(headers: dict) -> dict:
    """The two headers a conditional request can be built from."""
    lower = {k.lower(): v for k, v in headers.items()}
    out = {}
    if lower.get("etag"):
        out["etag"] = lower["etag"]
    if lower.get("last-modified"):
        out["last_modified"] = lower["last-modified"]
    return out


#: Candidate locations for the game log index, tried in order.
#:
#: The first was **verified against the live site on 2026-09-13**: it answers
#: 200 and carries 173 `gl*.zip` links. It was written blind -- retrosheet.org
#: was unreachable from the development machine that day, DNS resolving while
#: TCP to :443 timed out -- and the other two remain unconfirmed guesses, kept
#: as fallback. A list rather than a single path because a wrong one costs a
#: request and requests are the scarce resource: this project has already been
#: rate-limited off the server once (BUILD-LOG §3.3).
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


#: The Negro Leagues data archive.
#:
#: **Verified against the live site on 2026-09-13**, found from the link on
#: `www.retrosheet.org/NegroLeagues/downloads.html`: 334,966,934 bytes of
#: `application/zip`,
#: answering with both an `ETag` and a `Last-Modified`. That pair is what makes
#: `--refresh` a real conditional request rather than a 335 MB re-download.
NGL_ARCHIVE = f"{BASE}/downloads/allngldata.zip"

#: Tried in order, so a moved path can be added without touching the loop. A
#: wrong entry costs one request, not four -- 404 is not in `_RETRYABLE`.
NGL_ARCHIVE_CANDIDATES = (NGL_ARCHIVE,)

#: Where the Negro Leagues roster files are extracted.
#:
#: A directory of their own rather than the season directories, for a reason
#: that is not tidiness: `auxiliary.discover()` finds rosters by walking the
#: events root and takes the season from the *filename*, so placement carries
#: no meaning -- while `fetch_season` diffs a season directory to report which
#: files Retrosheet reissued. Extracting 704 rosters into those directories
#: would put files there that no season archive placed, and the first
#: `fetch --refresh` would report every one of them as changed.
NGL_ROSTER_DIR = "ngl-rosters"


def aux_csv_names() -> frozenset[str]:
    """Basenames `ingest --aux` reads, from the one list that defines them.

    Imported rather than restated: a second copy of this list would drift, and
    the failure would be a file silently not extracted -- which is the shape
    of bug this project keeps finding (BUILD-LOG §3.25).
    """
    from ..database.auxiliary import WHOLE_CORPUS
    return frozenset(name.lower() for name, _ in WHOLE_CORPUS)


@dataclass(frozen=True)
class FetchedAux:
    """What one auxiliary fetch did.

    `csvs` and `rosters` are counted from what was *extracted*, not from what
    the archive was expected to hold: the point of this pass is to find out
    that something is missing, not to assume it is not.
    """

    #: present | downloaded | unchanged | changed | failed
    status: str
    url: str | None = None
    csvs: tuple[Path, ...] = ()
    rosters: int = 0

    @property
    def ok(self) -> bool:
        return self.status != "failed"


def _extract_aux(zip_path: Path, events_dest: Path,
                 parks_dest: Path) -> tuple[tuple[Path, ...], int]:
    """Split the archive between the two places its contents belong.

    Members are matched on basename alone, so the archive's internal layout is
    not depended on -- and, incidentally, a member named `../../etc/passwd`
    cannot escape either destination.
    """
    wanted = aux_csv_names()
    roster_dir = events_dest / NGL_ROSTER_DIR
    parks_dest.mkdir(parents=True, exist_ok=True)
    roster_dir.mkdir(parents=True, exist_ok=True)

    csvs: list[Path] = []
    rosters = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name
            if name.lower() in wanted:
                out = parks_dest / name
            elif name.upper().endswith(".ROS"):
                out = roster_dir / name
                rosters += 1
            else:
                continue
            with zf.open(info) as src:
                out.write_bytes(src.read())
            if out.parent == parks_dest:
                csvs.append(out)
    return tuple(sorted(csvs)), rosters


def fetch_aux(events_dest: Path, parks_dest: Path, refresh: bool = False,
              state: dict | None = None,
              urls: tuple[str, ...] | None = None) -> FetchedAux:
    """Download the Negro Leagues archive and place what `ingest --aux` reads.

    One download, two destinations. The archive carries both the whole-corpus
    CSVs (`ballparks.csv`, `biofile.csv`, `teams.csv`, `allplayers.csv`) and
    the 704 Negro Leagues roster files that the season archives do not ship --
    without which 283 team-seasons have a lineup and no names behind it
    (BUILD-LOG §3.25).

    Conditional on `refresh`, exactly as `fetch_season` is, and for the same
    reason: this is a 335 MB file against a volunteer-run server, and "is the
    copy on disk still the published one" should cost one request and no
    bytes. Skipping on the zip being *present* is what hid a reissue for the
    season archives until `--refresh` existed.
    """
    events_dest.mkdir(parents=True, exist_ok=True)
    parks_dest.mkdir(parents=True, exist_ok=True)
    zip_path = parks_dest / "allngldata.zip"

    if zip_path.exists() and not refresh:
        csvs, rosters = _extract_aux(zip_path, events_dest, parks_dest)
        return FetchedAux("present", None, csvs, rosters)

    candidates = urls if urls is not None else NGL_ARCHIVE_CANDIDATES
    conditional = zip_path.exists() and refresh
    tried: list[str] = []
    for url in candidates:
        entry = (state or {}).get(url, {})
        try:
            blob, headers = _get(
                url, with_headers=True,
                etag=entry.get("etag") if conditional else None,
                last_modified=entry.get("last_modified") if conditional else None)
        except NotModified:
            csvs, rosters = _extract_aux(zip_path, events_dest, parks_dest)
            return FetchedAux("unchanged", url, csvs, rosters)
        except Exception as exc:  # noqa: BLE001 - reported below
            tried.append(f"{url}: {exc}")
            time.sleep(DELAY_SECONDS)
            continue

        if conditional and _digest_bytes(blob) == entry.get("sha256"):
            status = "unchanged"
        else:
            status = "changed" if conditional else "downloaded"

        tmp = zip_path.with_suffix(".zip.part")
        tmp.write_bytes(blob)
        tmp.replace(zip_path)
        if state is not None:
            state[url] = {"sha256": _digest_bytes(blob),
                          "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                      time.gmtime()),
                          **_validators(headers)}
        time.sleep(DELAY_SECONDS)

        try:
            csvs, rosters = _extract_aux(zip_path, events_dest, parks_dest)
        except zipfile.BadZipFile:
            print(f"  {url}: corrupt archive, removing", flush=True)
            zip_path.unlink(missing_ok=True)
            return FetchedAux("failed", url)
        return FetchedAux(status, url, csvs, rosters)

    print("could not download the Negro Leagues archive. Tried:\n  "
          + "\n  ".join(tried)
          + "\nDownload allngldata.zip by hand, then unzip its CSVs into"
            f" {parks_dest}/ and its .ROS files into"
            f" {events_dest / NGL_ROSTER_DIR}/;"
            " `rsse ingest --aux` needs no network access.", flush=True)
    return FetchedAux("failed", None)


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


@dataclass(frozen=True)
class Fetched:
    """What one season's fetch did.

    `status` is reported rather than inferred from the return value, because
    "present, not checked" and "checked, unchanged" are different facts and
    only one of them cost a request.
    """

    year: int
    path: Path | None
    #: present | downloaded | unchanged | changed | failed
    status: str
    changed_files: tuple[Path, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status != "failed"


def _dir_digests(season_dir: Path) -> dict[str, str]:
    if not season_dir.is_dir():
        return {}
    return {p.name: sha256(p) for p in sorted(season_dir.iterdir()) if p.is_file()}


def fetch_season(year: int, dest: Path, refresh: bool = False,
                 state: dict | None = None) -> Fetched:
    """Download and extract one season.

    Without `refresh`, a season archive already on disk is taken as current
    and costs no request -- which is the default because a present file is
    almost always the published one, and requests are the scarce resource.

    With `refresh`, the archive is re-requested *conditionally*: the stored
    `ETag`/`Last-Modified` go out as `If-None-Match`/`If-Modified-Since`, and
    a 304 ends it with no body transferred. Only a 200 rewrites anything.
    Retrosheet does reissue corrected files, and until this existed a reissue
    was invisible -- `fetch` skipped on the zip being *present*, never on it
    being *current*, so the exact digest check in `ingest_file` downstream
    never got the chance to fire.
    """
    dest.mkdir(parents=True, exist_ok=True)
    season_dir = dest / str(year)
    zip_path = dest / f"{year}eve.zip"
    url = f"{BASE}/events/{year}eve.zip"
    status = "present"

    if zip_path.exists() and not refresh:
        return _extract(year, zip_path, season_dir, status)

    entry = (state or {}).get(url, {})
    conditional = zip_path.exists() and refresh
    try:
        blob, headers = _get(
            url, with_headers=True,
            etag=entry.get("etag") if conditional else None,
            last_modified=entry.get("last_modified") if conditional else None)
    except NotModified:
        return _extract(year, zip_path, season_dir, "unchanged")
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        print(f"  {year}: download failed: {exc}", flush=True)
        return Fetched(year, None, "failed")

    # A conditional request the server answered with a body may still be the
    # same bytes: not every server sends validators, and some send weak ones.
    # The digest is the authority, so nothing downstream is disturbed by a
    # server that simply declines to answer 304.
    if conditional and zip_path.exists() and _digest_bytes(blob) == entry.get("sha256"):
        status = "unchanged"
    else:
        status = "changed" if conditional else "downloaded"

    before = _dir_digests(season_dir) if status == "changed" else {}

    # Write to a temporary name first: a truncated archive left at the real
    # path would be taken for a complete one on the next run.
    tmp = zip_path.with_suffix(".zip.part")
    tmp.write_bytes(blob)
    tmp.replace(zip_path)
    if state is not None:
        state[url] = {"sha256": _digest_bytes(blob),
                      "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                  time.gmtime()),
                      **_validators(headers)}
    time.sleep(DELAY_SECONDS)

    result = _extract(year, zip_path, season_dir, status)
    if status == "changed" and result.path is not None:
        after = _dir_digests(season_dir)
        changed = tuple(season_dir / name for name in sorted(after)
                        if before.get(name) != after[name])
        return Fetched(year, result.path, status, changed)
    return result


def _digest_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _extract(year: int, zip_path: Path, season_dir: Path,
             status: str) -> Fetched:
    season_dir.mkdir(exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(season_dir)
    except zipfile.BadZipFile:
        print(f"  {year}: corrupt archive, removing")
        zip_path.unlink(missing_ok=True)
        return Fetched(year, None, "failed")
    return Fetched(year, season_dir, status)
