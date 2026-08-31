"""Raw-layer ingest (spec/01-CORPUS.md §4, spec/05-DATABASE.md §1).

Loads every record of every event file verbatim, with provenance, and enforces
the round-trip gate as it goes: if a play's event string cannot be reproduced
byte-for-byte from its parse, information has been lost and the ingest says so
rather than storing a silently lossy corpus.

Source files are opened read-only and never modified.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ..parser.parser import ParseError, parse
from ..parser.records import detect_line_ending, read_records
from . import schema

BATCH = 20_000
EVENT_FILE = re.compile(r"\.EV[ANFR]$", re.IGNORECASE)


@dataclass
class IngestStats:
    files: int = 0
    records: int = 0
    games: int = 0
    plays: int = 0
    skipped_files: int = 0
    parse_failures: list[tuple[str, str, str]] = field(default_factory=list)
    roundtrip_failures: list[tuple[str, str, str]] = field(default_factory=list)
    repeated_game_ids: list[tuple[str, int, int]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"files {self.files} (skipped {self.skipped_files}), "
            f"games {self.games}, records {self.records}, plays {self.plays}, "
            f"parse failures {len(self.parse_failures)}, "
            f"round-trip failures {len(self.roundtrip_failures)}"
        )


class CorpusChanged(Exception):
    """A source file's digest differs from the one recorded for this corpus.

    Retrosheet reissues corrected files. Continuing would mix vintages within
    one corpus, producing results that are wrong in a way nothing detects, so
    this is fatal rather than a warning (spec/01-CORPUS.md §4).
    """


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def event_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if EVENT_FILE.search(p.name))


def open_corpus(conn, rsse_version: str, parser_version: str, notes: str = "") -> int:
    """Reuse the newest corpus row, or start one if the database is empty."""
    row = conn.execute("SELECT max(corpus_id) FROM corpus").fetchone()
    if row[0] is not None:
        return row[0]
    cur = conn.execute(
        "INSERT INTO corpus (ingested_at, rsse_version, parser_version, notes)"
        " VALUES (?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"),
         rsse_version, parser_version, notes),
    )
    conn.commit()
    return cur.lastrowid


def ingest_file(conn, corpus_id: int, path: Path, stats: IngestStats,
                verify: bool = True) -> bool:
    """Load one event file in a single transaction.

    Returns False if the file was already present and unchanged. Raises
    CorpusChanged if it is present but different.
    """
    digest = sha256(path)
    stat = path.stat()
    existing = conn.execute(
        "SELECT file_id, sha256 FROM source_files WHERE corpus_id = ? AND path = ?",
        (corpus_id, str(path)),
    ).fetchone()
    if existing:
        if existing[1] == digest:
            stats.skipped_files += 1
            return False
        raise CorpusChanged(
            f"{path}: digest changed since ingest "
            f"({existing[1][:12]} -> {digest[:12]}). Retrosheet has reissued "
            f"this file. Rebuild the corpus rather than mixing vintages."
        )

    rows: list[tuple] = []
    spans: list[tuple] = []
    game_id = ""
    span_start: int | None = None
    span_count = 0
    n_records = n_plays = n_games = 0
    try:
        conn.execute("BEGIN")
        cur = conn.execute(
            "INSERT INTO source_files"
            " (corpus_id, path, season, sha256, byte_length, mtime, line_ending)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (corpus_id, str(path), _season(path), digest, stat.st_size,
             datetime.fromtimestamp(stat.st_mtime, timezone.utc)
             .isoformat(timespec="seconds"),
             detect_line_ending(path)),
        )
        file_id = cur.lastrowid

        next_id = (conn.execute("SELECT max(record_id) FROM raw_records")
                   .fetchone()[0] or 0) + 1
        for rec in read_records(path):
            if rec.type == "id":
                if span_start is not None:
                    spans.append((game_id, file_id, span_start,
                                  next_id - 1, span_count))
                game_id = rec.fields[0] if rec.fields else ""
                span_start, span_count = next_id, 0
                n_games += 1
            elif rec.type == "play" and verify:
                n_plays += 1
                _verify_play(rec, game_id, stats)
            rows.append((file_id, rec.line_no, game_id or None, rec.type, rec.raw))
            next_id += 1
            span_count += 1
            n_records += 1
            if len(rows) >= BATCH:
                _flush(conn, rows)
        if span_start is not None:
            spans.append((game_id, file_id, span_start, next_id - 1, span_count))
        _flush(conn, rows)
        if spans:
            conn.executemany(
                "INSERT INTO game_spans (game_id, occurrence, file_id,"
                " first_record_id, last_record_id, record_count)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                _number_occurrences(conn, spans, stats))
        conn.execute(
            "UPDATE source_files SET record_count = ? WHERE file_id = ?",
            (n_records, file_id),
        )
        conn.commit()
    except Exception:
        # Roll the whole file back: a partially loaded game is worse than none.
        conn.rollback()
        raise

    stats.files += 1
    stats.records += n_records
    stats.plays += n_plays
    stats.games += n_games
    return True


def _number_occurrences(conn, spans: list[tuple], stats: IngestStats) -> list[tuple]:
    """Assign each span an occurrence number within its game_id.

    Retrosheet game ids are not unique -- see spec/01-CORPUS.md §5.4. Dropping
    a repeat would lose data, so they are kept and numbered instead.
    """
    out = []
    counts: dict[str, int] = {}
    for game_id, file_id, first, last, count in spans:
        if game_id not in counts:
            row = conn.execute(
                "SELECT count(*) FROM game_spans WHERE game_id = ?", (game_id,)
            ).fetchone()
            counts[game_id] = row[0]
        counts[game_id] += 1
        n = counts[game_id]
        if n > 1:
            stats.repeated_game_ids.append((game_id, n, first))
        out.append((game_id, n, file_id, first, last, count))
    return out


def _flush(conn, rows: list[tuple]) -> None:
    if rows:
        conn.executemany(
            "INSERT INTO raw_records"
            " (file_id, line_no, game_id, record_type, raw_line)"
            " VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        rows.clear()


def _verify_play(rec, game_id: str, stats: IngestStats) -> None:
    """The round-trip gate, applied at ingest (spec/02-GRAMMAR.md §6)."""
    if len(rec.fields) < 6:
        return
    event = rec.fields[5]
    try:
        parsed = parse(event)
    except ParseError as exc:
        stats.parse_failures.append((game_id, event, exc.message))
        return
    if not parsed.roundtrips():
        stats.roundtrip_failures.append((game_id, event, parsed.emit()))


def _season(path: Path) -> int | None:
    m = re.match(r"(\d{4})", path.name)
    return int(m.group(1)) if m else None


def ingest(conn, paths: Iterable[Path], corpus_id: int, verify: bool = True,
           progress=None) -> IngestStats:
    stats = IngestStats()
    for path in paths:
        loaded = ingest_file(conn, corpus_id, path, stats, verify=verify)
        if progress:
            progress(path, loaded, stats)
    return stats
