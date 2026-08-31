"""SQLite schemas (spec/05-DATABASE.md §1, §7).

Two databases, deliberately separate:

**The archive** holds 100% of the input verbatim, with provenance. It is
written once at ingest and never revised: every derived table is rebuilt from
here, so a re-parse never re-reads the source files and can be reproduced
against exactly the bytes ingested.

**The query database** holds the derived, typed tables that searches run
against. It never reads the archive.

They are split because the archive is 1.94 GB that no query touches. Keeping it
in its own file keeps the working database small, lets the archive be detached
or rebuilt on demand, and costs nothing: it is regenerable from the event files
and its provenance is recorded in both.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

ARCHIVE_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
  version     INTEGER NOT NULL,
  applied_at  TEXT NOT NULL
);

-- One row per ingest run.
CREATE TABLE IF NOT EXISTS corpus (
  corpus_id     INTEGER PRIMARY KEY,
  ingested_at   TEXT NOT NULL,
  rsse_version  TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  notes         TEXT
);

-- Provenance. Retrosheet reissues corrected files, so the digest is what makes
-- a stale or mixed corpus detectable rather than silent (spec/01-CORPUS.md §4).
CREATE TABLE IF NOT EXISTS source_files (
  file_id       INTEGER PRIMARY KEY,
  corpus_id     INTEGER NOT NULL REFERENCES corpus,
  path          TEXT NOT NULL,
  season        INTEGER,
  sha256        TEXT NOT NULL,
  byte_length   INTEGER NOT NULL,
  mtime         TEXT NOT NULL,
  line_ending   TEXT NOT NULL CHECK (line_ending IN ('crlf','lf','mixed')),
  record_count  INTEGER NOT NULL DEFAULT 0,
  UNIQUE (corpus_id, path)
);

CREATE TABLE IF NOT EXISTS raw_records (
  record_id     INTEGER PRIMARY KEY,
  file_id       INTEGER NOT NULL REFERENCES source_files,
  line_no       INTEGER NOT NULL,
  game_id       TEXT,
  record_type   TEXT NOT NULL,
  raw_line      TEXT NOT NULL
);

-- A game's records are contiguous in record_id: files are read once, in order,
-- and record_id is monotonic. Storing the span once per game replaces an index
-- over every record with one row per game -- ~200k rows instead of ~30M, and a
-- game lookup becomes one seek plus a range scan on the primary key.
-- game_id is NOT unique: three ids repeat within a single file, so the key is
-- a surrogate and `occurrence` disambiguates. Rejecting the repeats would lose
-- data, and one of the three is not even a duplicate -- the two blocks have
-- different play counts (spec/01-CORPUS.md §5.4).
CREATE TABLE IF NOT EXISTS game_spans (
  game_key        INTEGER PRIMARY KEY,
  game_id         TEXT NOT NULL,
  occurrence      INTEGER NOT NULL DEFAULT 1,
  file_id         INTEGER NOT NULL REFERENCES source_files,
  first_record_id INTEGER NOT NULL,
  last_record_id  INTEGER NOT NULL,
  record_count    INTEGER NOT NULL,
  UNIQUE (game_id, occurrence)
);
"""

# Applied after bulk load: building indexes during insert is far slower.
#
# Deliberately minimal. Two candidate indexes were measured and rejected:
#
#   ux_raw_file_line (file_id, line_no) -- 13% of the database, and redundant:
#       source_files.UNIQUE(corpus_id, path) already prevents loading a file
#       twice, and each file is read once line by line, so the pair is unique
#       by construction. It guarded nothing and served no query.
#
#   ix_raw_game (game_id, record_id) -- 25% of the database, ~760 MB over the
#       full corpus. Replaced by game_spans, which is ~200k rows.
INDEXES = """
CREATE INDEX IF NOT EXISTS ix_spans_file ON game_spans (file_id);
CREATE INDEX IF NOT EXISTS ix_spans_game ON game_spans (game_id);
"""

#: The query database records which archive it was derived from, so a result
#: can cite its provenance without attaching a 2 GB file.
QUERY_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
  version     INTEGER NOT NULL,
  applied_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corpus_ref (
  corpus_id      INTEGER PRIMARY KEY,
  archive_path   TEXT NOT NULL,
  ingested_at    TEXT NOT NULL,
  rsse_version   TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  record_count   INTEGER NOT NULL,
  game_count     INTEGER NOT NULL,
  file_count     INTEGER NOT NULL
);
"""

LOAD_PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA cache_size = -262144",   # 256 MB
    "PRAGMA temp_store = MEMORY",
)


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create(conn: sqlite3.Connection) -> None:
    """Create the archive schema."""
    conn.executescript(ARCHIVE_DDL)
    row = conn.execute("SELECT max(version) FROM schema_version").fetchone()
    if row[0] is None:
        conn.execute(
            "INSERT INTO schema_version VALUES (?, datetime('now'))",
            (SCHEMA_VERSION,),
        )
    conn.commit()


def create_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(INDEXES)
    conn.commit()


def create_query_db(conn: sqlite3.Connection) -> None:
    """Create the query-database schema."""
    conn.executescript(QUERY_DDL)
    row = conn.execute("SELECT max(version) FROM schema_version").fetchone()
    if row[0] is None:
        conn.execute("INSERT INTO schema_version VALUES (?, datetime('now'))",
                     (SCHEMA_VERSION,))
    conn.commit()


def attach_archive(conn: sqlite3.Connection, archive_path: str) -> None:
    """Attach the archive read-only, for a re-parse.

    Normal queries never need this: everything queryable is promoted into a
    typed table in the query database.
    """
    conn.execute("ATTACH DATABASE ? AS archive", (f"file:{archive_path}?mode=ro",))
