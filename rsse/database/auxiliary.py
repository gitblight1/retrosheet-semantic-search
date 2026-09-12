"""Archiving the source files that belong to no game (spec/05-DATABASE.md §1.2).

Rosters, team files, ballparks and biographies are Retrosheet data that
`ingest` has never read: it matches `.EV[ANFR]` only. They cannot simply join
`raw_records`, because that table is partitioned exactly by `game_spans` and
`rsse verify` asserts two things about it --
`count(raw_records) == sum(game_spans.record_count)` and
`sum(source_files.record_count) == count(raw_records)`. A record belonging to
no game breaks the first; giving its file a `source_files` row breaks the
second.

So this writes a parallel pair, `aux_files` and `aux_records`, and adds two
checks of the same shape rather than weakening the two that exist. `ingest`
and `verify` keep meaning exactly what they meant.

**The archive's promise is bytes, and it is enforced here too.** Event files
get a round-trip gate on every event string; these get one on the whole file:
reassembling `raw_line` in `line_no` order, with the recorded line ending, must
reproduce the file's SHA-256. Storing lines and *hoping* they rebuild is how an
archive quietly stops being one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .load import CorpusChanged, sha256

BATCH = 20_000

#: Files are read as latin-1, exactly as `records.read_records` reads event
#: files, and for a stronger reason here: latin-1 is a total byte-to-character
#: mapping, so decoding and re-encoding is guaranteed lossless whatever the
#: file actually contains. Every one of these files happens to be valid UTF-8
#: today; that is not something to depend on in an archive.
ENCODING = "latin-1"

_ROSTER = re.compile(r"^(?P<team>[A-Z0-9]{3})(?P<season>\d{4})\.ROS$", re.I)
_TEAM = re.compile(r"^TEAM(?P<season>\d{4})$", re.I)

#: `(filename, kind)` for the whole-corpus files that are not per-season.
#:
#: `parkcode.txt` is **deliberately absent**. It has the same nine columns as
#: `ballparks.csv`, contributes no park id that file lacks, and covers 113 of
#: the corpus's 292 sites against 285. It is a strict subset, not a second
#: source, so ingesting it would create two tables' worth of provenance for
#: one fact.
#:
#: `allplayers.csv` is absent for a different reason: every one of its 3,422
#: ids already appears in a roster or in `biofile.csv`, so it adds no *person*.
#: What it does add is per-season appearance counts by position, which is
#: statistics rather than identity and belongs to a later pass.
WHOLE_CORPUS = (
    ("ballparks.csv", "park"),
    ("biofile.csv", "bio"),
    ("teams.csv", "teamlist"),
)


@dataclass
class AuxStats:
    files: int = 0
    records: int = 0
    skipped_files: int = 0
    by_kind: dict = field(default_factory=dict)
    roundtrip_failures: list = field(default_factory=list)

    def summary(self) -> str:
        kinds = ", ".join(f"{k} {v:,}" for k, v in sorted(self.by_kind.items()))
        return (f"files {self.files} (skipped {self.skipped_files}), "
                f"records {self.records:,} [{kinds}], "
                f"round-trip failures {len(self.roundtrip_failures)}")


def discover(events_root: Path, parks_dir: Path) -> list[tuple[str, Path, int | None]]:
    """`(kind, path, season)` for every auxiliary file, in a stable order."""
    found: list[tuple[str, Path, int | None]] = []
    for path in sorted(events_root.rglob("*")):
        if not path.is_file():
            continue
        roster = _ROSTER.match(path.name)
        if roster:
            found.append(("roster", path, int(roster.group("season"))))
            continue
        team = _TEAM.match(path.name)
        if team:
            found.append(("team", path, int(team.group("season"))))
    for name, kind in WHOLE_CORPUS:
        path = parks_dir / name
        if path.exists():
            found.append((kind, path, None))
    return found


def _read(path: Path) -> tuple[list[str], str, bool]:
    """`(lines, line_ending, final_newline)` -- the file, losslessly split.

    Splitting is done on the bytes rather than with `open(newline=...)` so that
    "does this file end with a terminator" is answerable. It is one file in
    3,435, and without the answer that file cannot be rebuilt.
    """
    blob = path.read_bytes()
    crlf, lf = blob.count(b"\r\n"), blob.count(b"\n")
    ending = "crlf" if crlf and crlf == lf else "mixed" if crlf else "lf"
    final_newline = blob.endswith(b"\n")
    text = blob.decode(ENCODING)
    lines = text.split("\n")
    if final_newline:
        lines.pop()                       # the empty tail after the last \n
    return [ln.rstrip("\r") for ln in lines], ending, final_newline


def rebuild(lines: list[str], ending: str, final_newline: bool) -> bytes:
    """Reassemble a file's bytes from its stored lines.

    The inverse of `_read`, and the whole point of the round-trip gate: if
    these two disagree the archive is not an archive.
    """
    sep = "\r\n" if ending == "crlf" else "\n"
    out = sep.join(lines) + (sep if final_newline and lines else "")
    return out.encode(ENCODING)


def ingest_file(conn, corpus_id: int, kind: str, path: Path,
                season: int | None, stats: AuxStats) -> bool:
    """Load one auxiliary file in a single transaction.

    Returns False if it was already present and unchanged; raises
    `CorpusChanged` if present and different, exactly as `ingest_file` does
    for event files -- mixing vintages is no safer here.
    """
    # Resolved, not as given. `UNIQUE (corpus_id, path)` only prevents a
    # double load if the same file yields the same string, and it does not:
    # ingesting `data/parks` and then `/abs/.../data/parks` produced two full
    # copies of every record with no constraint violated. `source_files` has
    # the same latent hole and is saved from it only by the CLI always passing
    # an absolute path -- a convention, where this is a guarantee.
    canonical = str(path.resolve())
    digest = sha256(path)
    existing = conn.execute(
        "SELECT aux_file_id, sha256 FROM aux_files"
        " WHERE corpus_id = ? AND path = ?", (corpus_id, canonical)).fetchone()
    if existing:
        if existing[1] == digest:
            stats.skipped_files += 1
            return False
        raise CorpusChanged(
            f"{path}: digest changed since ingest "
            f"({existing[1][:12]} -> {digest[:12]}). Retrosheet has reissued "
            f"this file. Rebuild the corpus rather than mixing vintages.")

    lines, ending, final_newline = _read(path)
    if ending == "mixed":
        # Not fatal, but it makes `rebuild` a guess rather than an inverse, so
        # it is recorded as a round-trip failure and the gate below catches it.
        stats.roundtrip_failures.append((str(path), "mixed line endings"))
    stat = path.stat()
    try:
        conn.execute("BEGIN")
        cur = conn.execute(
            "INSERT INTO aux_files (corpus_id, path, kind, season, sha256,"
            " byte_length, mtime, line_ending, final_newline, record_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (corpus_id, canonical, kind, season, digest, stat.st_size,
             datetime.fromtimestamp(stat.st_mtime, timezone.utc)
             .isoformat(timespec="seconds"),
             ending, int(final_newline), len(lines)))
        aux_file_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO aux_records (aux_file_id, line_no, raw_line)"
            " VALUES (?,?,?)",
            [(aux_file_id, n, line) for n, line in enumerate(lines, 1)])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    stats.files += 1
    stats.records += len(lines)
    stats.by_kind[kind] = stats.by_kind.get(kind, 0) + len(lines)
    return True


def check_roundtrip(conn, aux_file_id: int) -> str | None:
    """Rebuild one file from the archive and compare digests.

    Returns None when the bytes match, or a description when they do not.
    """
    import hashlib

    row = conn.execute(
        "SELECT path, sha256, line_ending, final_newline, record_count"
        "  FROM aux_files WHERE aux_file_id = ?", (aux_file_id,)).fetchone()
    if row is None:
        return "no such aux_file"
    path, digest, ending, final_newline, count = row
    lines = [r[0] for r in conn.execute(
        "SELECT raw_line FROM aux_records WHERE aux_file_id = ?"
        " ORDER BY line_no", (aux_file_id,))]
    if len(lines) != count:
        return f"{path}: {len(lines)} records stored, {count} declared"
    rebuilt = hashlib.sha256(
        rebuild(lines, ending, bool(final_newline))).hexdigest()
    if rebuilt != digest:
        return f"{path}: rebuilt digest {rebuilt[:12]} != {digest[:12]}"
    return None


def ingest(conn, corpus_id: int, files: list[tuple[str, Path, int | None]],
           stats: AuxStats, progress=None, verify: bool = True) -> AuxStats:
    """Ingest every auxiliary file, checking each rebuilds byte for byte."""
    for index, (kind, path, season) in enumerate(files, 1):
        loaded = ingest_file(conn, corpus_id, kind, path, season, stats)
        if loaded and verify:
            aux_file_id = conn.execute(
                "SELECT aux_file_id FROM aux_files WHERE corpus_id = ?"
                " AND path = ?", (corpus_id, str(path.resolve()))).fetchone()[0]
            failure = check_roundtrip(conn, aux_file_id)
            if failure:
                stats.roundtrip_failures.append((str(path), failure))
        if progress and index % 500 == 0:
            progress(index, len(files), stats)
    conn.commit()
    return stats
