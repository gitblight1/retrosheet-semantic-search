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

SCHEMA_VERSION = 2

#: Every format `aux_files` accepts, and the same set the CHECK constraint
#: above spells out. Version 2 added `appearances` -- `allplayers.csv`, which
#: earlier versions deliberately left out.
AUX_KINDS = ("roster", "team", "teamlist", "park", "bio", "appearances")

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

-- Source records that belong to no game: rosters, team files, ballparks,
-- biographies. They cannot go in `raw_records`, because `raw_records` is
-- partitioned exactly by `game_spans` and `rsse verify` asserts both
-- `count(raw_records) == sum(game_spans.record_count)` and
-- `sum(source_files.record_count) == count(raw_records)`. A non-game record
-- has no span to belong to, so adding one would break the first check, and
-- giving its file a `source_files` row would break the second.
--
-- Hence a parallel pair rather than a `kind` column on the existing tables.
-- It costs six duplicated metadata columns and buys leaving both invariants
-- exactly as they are: `source_files` keeps meaning "a file of game records",
-- and the two new checks below are of the same shape rather than weakened
-- versions of the old ones (spec/05-DATABASE.md §1.2).
CREATE TABLE IF NOT EXISTS file_revisions (
  revision_id       INTEGER PRIMARY KEY,
  corpus_id         INTEGER NOT NULL REFERENCES corpus,
  path              TEXT NOT NULL,
  old_sha256        TEXT NOT NULL,
  new_sha256        TEXT NOT NULL,
  replaced_at       TEXT NOT NULL,
  old_record_count  INTEGER NOT NULL,
  new_record_count  INTEGER NOT NULL,
  games_replaced    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_revisions_path
  ON file_revisions (path, replaced_at);

CREATE TABLE IF NOT EXISTS aux_files (
  aux_file_id   INTEGER PRIMARY KEY,
  corpus_id     INTEGER NOT NULL REFERENCES corpus,
  path          TEXT NOT NULL,
  -- One kind per *format*, because that is what the reader dispatches on:
  -- `TEAM####` is four positional fields and `teams.csv` is six with a
  -- header, so calling both 'team' would put a branch in every consumer.
  -- Keep in step with `AUX_KINDS` below. A test asserts the two agree,
  -- because a CHECK and the constant the code validates against are exactly
  -- the pair that drifts.
  kind          TEXT NOT NULL
    CHECK (kind IN ('roster','team','teamlist','park','bio','appearances')),
  -- Rosters and team files are per-season; ballparks and biographies are not.
  season        INTEGER,
  sha256        TEXT NOT NULL,
  byte_length   INTEGER NOT NULL,
  mtime         TEXT NOT NULL,
  line_ending   TEXT NOT NULL CHECK (line_ending IN ('crlf','lf','mixed')),
  -- Whether the file's last line carries a terminator. Needed to rebuild the
  -- bytes exactly; without it a file that ends mid-line and one that does not
  -- reassemble identically, and the round-trip gate would pass on both.
  final_newline INTEGER NOT NULL DEFAULT 1,
  record_count  INTEGER NOT NULL DEFAULT 0,
  UNIQUE (corpus_id, path)
);

CREATE TABLE IF NOT EXISTS aux_records (
  aux_record_id INTEGER PRIMARY KEY,
  aux_file_id   INTEGER NOT NULL REFERENCES aux_files,
  line_no       INTEGER NOT NULL,
  -- Verbatim, terminator stripped. These files have no record type to key on
  -- the way an event file does: a `.ROS` line is positional and a `.csv` has
  -- a header row, so interpretation belongs in the derived layer and the
  -- archive keeps only the bytes.
  raw_line      TEXT NOT NULL
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
CREATE INDEX IF NOT EXISTS ix_aux_file ON aux_records (aux_file_id, line_no);
CREATE INDEX IF NOT EXISTS ix_aux_kind ON aux_files (kind);
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

#: The derived, typed tables that searches run against (spec/05-DATABASE.md
#: §2-§4). Rebuildable in full from the archive, so nothing here is a source of
#: truth and dropping it costs only time.
#:
#: `games` and `plays` are keyed on a **surrogate**, not on `game_id`: three
#: ids repeat in the corpus (spec/01-CORPUS.md §5.4), which is what stopped the
#: raw ingest, and declaring `game_id` unique here would have failed the same
#: way one layer later after a much longer run. `game_id` is carried alongside
#: the key because every query orders and reports by it.
DERIVED_DDL = """
CREATE TABLE IF NOT EXISTS games (
  game_key           INTEGER PRIMARY KEY,
  game_id            TEXT NOT NULL,
  occurrence         INTEGER NOT NULL DEFAULT 1,
  date               TEXT,                   -- ISO yyyy-mm-dd
  season             INTEGER,
  game_number        INTEGER,
  home_team          TEXT,
  away_team          TEXT,
  league             TEXT,
  site               TEXT,
  game_type          TEXT,
  scheduled_innings  INTEGER,
  use_dh             INTEGER,
  home_bats_first    INTEGER NOT NULL DEFAULT 0,
  pitch_detail       TEXT,
  tiebreaker_base    INTEGER,
  final_home         INTEGER,
  final_away         INTEGER,
  plays              INTEGER NOT NULL DEFAULT 0,
  parse_status       TEXT NOT NULL DEFAULT 'ok',
  -- The digest of the source file this game was derived from, carried over
  -- from `source_files.sha256`. It is what `derive --refresh` compares, and
  -- it has to be content rather than an identifier: `game_spans.game_key` and
  -- `raw_records.record_id` are both plain INTEGER PRIMARY KEYs, so deleting
  -- a file's rows and re-inserting them hands back the *same* numbers. A
  -- reissued file compared by key looks identical to the file it replaced.
  source_sha256      TEXT,
  UNIQUE (game_id, occurrence)
);

-- Lossless: every info record, including ones the model ignores.
CREATE TABLE IF NOT EXISTS game_info (
  game_key INTEGER NOT NULL REFERENCES games,
  key      TEXT NOT NULL,
  value    TEXT,
  seq      INTEGER NOT NULL,
  PRIMARY KEY (game_key, seq)
);

CREATE TABLE IF NOT EXISTS plays (
  play_id        INTEGER PRIMARY KEY,
  game_key       INTEGER NOT NULL REFERENCES games,
  game_id        TEXT NOT NULL,
  seq            INTEGER NOT NULL,
  -- raw_records.record_id, in the *archive*: a separate database, so no
  -- REFERENCES clause. Provenance -- every derived row names its source line.
  record_id      INTEGER,

  inning         INTEGER NOT NULL,
  half           TEXT NOT NULL CHECK (half IN ('top','bottom')),
  batting_team   INTEGER NOT NULL CHECK (batting_team IN (0,1)),
  batter_id      TEXT NOT NULL,

  count_balls    INTEGER,
  count_strikes  INTEGER,
  pitch_seq      TEXT,

  event_raw      TEXT NOT NULL,
  event_basic    TEXT NOT NULL,
  event_modifiers TEXT NOT NULL,
  event_advances TEXT NOT NULL,
  annotations    TEXT NOT NULL,

  -- The hit location, lifted out of the modifier list so it can be indexed.
  -- Retrosheet's zone string as written (`7`, `78D`, `56`, `9LS`), never
  -- normalised: the zone system is published only as a diagram and any
  -- rewriting here would be this project's reading of a picture.
  --
  -- NULL for the 72% of plays that carry no location, which includes almost
  -- the whole corpus before 1980 -- 2.8% of the 1910s against 60.4% of the
  -- 1990s. NULL is "the scorer did not record one", and it is the reason the
  -- index below is partial.
  --
  -- Redundant with `event_modifiers`, and deliberately so: it is a lifted
  -- copy, not a second source, and `rsse verify` asserts that every value
  -- here still appears in the modifier text it came from.
  event_location TEXT,

  -- Nullable on purpose. An unparsed play's effect cannot be applied, so its
  -- base-out state is unknown; NOT NULL here forced the loader to write zeros,
  -- and a stored '000' is indistinguishable from bases genuinely empty. NULL
  -- is the only honest value. See spec/03-STATE.md §7.
  outs_before    INTEGER,
  outs_recorded  INTEGER,
  outs_after     INTEGER,
  bases_before   TEXT,
  bases_after    TEXT,
  runner_1_before TEXT, runner_2_before TEXT, runner_3_before TEXT,

  batter_dest    TEXT,
  batter_is_out  INTEGER NOT NULL DEFAULT 0,
  batter_ran     TEXT NOT NULL DEFAULT 'unknown'
    CHECK (batter_ran IN ('yes','no','unknown')),
  runs_on_play   INTEGER NOT NULL DEFAULT 0,
  score_batting_before INTEGER NOT NULL DEFAULT 0,
  score_fielding_before INTEGER NOT NULL DEFAULT 0,

  is_inning_ending INTEGER NOT NULL DEFAULT 0,
  is_final_play    INTEGER NOT NULL DEFAULT 0,
  is_walkoff       INTEGER NOT NULL DEFAULT 0,
  is_go_ahead      INTEGER NOT NULL DEFAULT 0,

  parse_status   TEXT NOT NULL DEFAULT 'ok'
    CHECK (parse_status IN ('ok','parsed_untagged','unparsed',
                            'data_contradicts_rules',
                            'state_ambiguous','state_inconsistent',
                            'state_untrusted')),
  parse_error    TEXT,
  parser_version TEXT NOT NULL,

  UNIQUE (game_key, seq)
);

CREATE TABLE IF NOT EXISTS runner_advances (
  play_id        INTEGER NOT NULL REFERENCES plays,
  seq            INTEGER NOT NULL,
  runner_id      TEXT,
  origin         TEXT NOT NULL CHECK (origin IN ('B','1','2','3')),
  destination    TEXT NOT NULL CHECK (destination IN ('1','2','3','H')),
  is_explicit    INTEGER NOT NULL,
  marked_out     INTEGER NOT NULL,
  is_out         INTEGER NOT NULL,
  is_force       INTEGER NOT NULL DEFAULT 0,
  force_certainty TEXT NOT NULL DEFAULT 'derived'
    CHECK (force_certainty IN ('derived','likely','ambiguous','n/a')),
  scored         INTEGER NOT NULL DEFAULT 0,
  raw            TEXT NOT NULL,
  PRIMARY KEY (play_id, seq)
);

CREATE TABLE IF NOT EXISTS fielding_credits (
  play_id     INTEGER NOT NULL REFERENCES plays,
  scope       TEXT NOT NULL CHECK (scope IN ('basic','advance')),
  scope_seq   INTEGER NOT NULL,
  pos_in_seq  INTEGER NOT NULL,
  fielder     INTEGER,
  credit      TEXT NOT NULL CHECK (credit IN ('putout','assist','error','none')),
  PRIMARY KEY (play_id, scope, scope_seq, pos_in_seq)
);

CREATE TABLE IF NOT EXISTS credit_sequences (
  play_id    INTEGER NOT NULL REFERENCES plays,
  scope      TEXT NOT NULL,
  scope_seq  INTEGER NOT NULL,
  origin_seq INTEGER NOT NULL,
  seq_text   TEXT NOT NULL,
  has_error  INTEGER NOT NULL,
  records_out INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (play_id, scope, scope_seq)
);

CREATE TABLE IF NOT EXISTS tags (
  tag_id      INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  category    TEXT NOT NULL,
  version     TEXT NOT NULL,
  rule_hash   TEXT NOT NULL,
  deprecated_alias_of TEXT
);

CREATE TABLE IF NOT EXISTS play_tags (
  play_id    INTEGER NOT NULL REFERENCES plays,
  tag_id     INTEGER NOT NULL REFERENCES tags,
  confidence TEXT NOT NULL DEFAULT 'certain'
    CHECK (confidence IN ('certain','uncertain')),
  source     TEXT NOT NULL DEFAULT 'derived'
    CHECK (source IN ('derived','curated')),
  PRIMARY KEY (play_id, tag_id)
);

CREATE TABLE IF NOT EXISTS derive_runs (
  derive_id        INTEGER PRIMARY KEY,
  derived_at       TEXT NOT NULL,
  parser_version   TEXT NOT NULL,
  ontology_version TEXT NOT NULL,
  ontology_hash    TEXT NOT NULL,
  games            INTEGER NOT NULL,
  plays            INTEGER NOT NULL,
  notes            TEXT
);
"""

#: Applied after the bulk load, for the same reason the archive's are: building
#: an index during insert is far slower than building it once at the end.
#:
#: Unlike the archive (§1.1), the derived layer *is* the query surface, so
#: these are chosen against the predicates of spec/06-QUERY.md rather than
#: minimised.
DERIVED_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_games_date ON games (date);
CREATE INDEX IF NOT EXISTS ix_games_season ON games (season);
CREATE INDEX IF NOT EXISTS ix_games_id ON games (game_id);

CREATE INDEX IF NOT EXISTS ix_plays_ctx ON plays (bases_before, outs_before);
CREATE INDEX IF NOT EXISTS ix_plays_game ON plays (game_id, seq);
CREATE INDEX IF NOT EXISTS ix_plays_gamekey ON plays (game_key, seq);
CREATE INDEX IF NOT EXISTS ix_plays_batter ON plays (batter_id);
CREATE INDEX IF NOT EXISTS ix_plays_status ON plays (parse_status)
  WHERE parse_status <> 'ok';
CREATE INDEX IF NOT EXISTS ix_plays_batter_ran ON plays (batter_ran)
  WHERE batter_ran = 'unknown';

-- Partial because 72% of plays have no location and no location query wants
-- them: the index costs a quarter of what a full one would. `.hit_location()`
-- spells `event_location IS NOT NULL` into every predicate it builds so that
-- SQLite can see the partial index applies -- the term is redundant with the
-- equality test beside it, and without it the planner falls back to a scan.
CREATE INDEX IF NOT EXISTS ix_plays_location ON plays (event_location, play_id)
  WHERE event_location IS NOT NULL;

-- The force predicate of 06-QUERY §3.2, partial so it costs only the rows it
-- serves rather than one entry per advance.
CREATE INDEX IF NOT EXISTS ix_adv_force ON runner_advances
  (is_force, destination, play_id) WHERE is_force = 1;
CREATE INDEX IF NOT EXISTS ix_adv_out ON runner_advances
  (destination, play_id) WHERE is_out = 1;

CREATE INDEX IF NOT EXISTS ix_credit_fielder ON fielding_credits (fielder, credit);
CREATE INDEX IF NOT EXISTS ix_credseq_text ON credit_sequences (seq_text, play_id);

-- Tag-first: the driving index for nearly every query.
CREATE INDEX IF NOT EXISTS ix_playtags_tag ON play_tags (tag_id, play_id);
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


def migrate_archive(conn: sqlite3.Connection) -> list[str]:
    """Bring an existing archive up to `SCHEMA_VERSION`, returning what changed.

    `CREATE TABLE IF NOT EXISTS` is not a migration: an archive built under
    version 1 keeps version 1's CHECK constraint forever, and the first
    `allplayers.csv` to reach it fails with `CHECK constraint failed` in the
    middle of an ingest rather than at the start of one. Re-ingesting 31
    million records to widen one constraint is not a plan either.

    So the one thing version 2 changes is done here, by rebuilding a 3,435-row
    table. The order matters and is not the obvious one: `ALTER TABLE ... RENAME
    TO` rewrites every *other* table's `REFERENCES` clause to follow the rename
    (SQLite 3.25+), so renaming `aux_files` out of the way would silently point
    `aux_records` at `aux_files_old` and leave it there after the drop. Building
    the new table under its own name and renaming it *into* place has no such
    effect, because nothing references the temporary name.
    """
    done: list[str] = []
    row = conn.execute("SELECT sql FROM sqlite_master"
                       " WHERE type = 'table' AND name = 'aux_files'").fetchone()
    if row and "'appearances'" not in row[0]:
        columns = ",".join(r[1] for r in conn.execute(
            "PRAGMA table_info(aux_files)"))
        new_ddl = _statement(ARCHIVE_DDL, "aux_files").replace(
            "aux_files", "aux_files_v2", 1)
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN")
            conn.execute(new_ddl)
            conn.execute(f"INSERT INTO aux_files_v2 ({columns})"
                         f" SELECT {columns} FROM aux_files")
            conn.execute("DROP TABLE aux_files")
            conn.execute("ALTER TABLE aux_files_v2 RENAME TO aux_files")
            conn.execute("COMMIT")
            conn.executescript(INDEXES)
            # Proof rather than confidence: if the rename left `aux_records`
            # pointing at a table that no longer exists, every one of its rows
            # is a violation and this returns them.
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise RuntimeError(
                    f"aux_files migration left {len(broken)} dangling"
                    f" references, e.g. {broken[0]}")
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        done.append("aux_files: kind now accepts 'appearances'")
    if done:
        conn.execute("INSERT INTO schema_version VALUES (?, datetime('now'))",
                     (SCHEMA_VERSION,))
        conn.commit()
    return done


def _statement(script: str, table: str) -> str:
    """The `CREATE TABLE` for ``table``, taken from the DDL rather than retyped.

    A migration that carries its own copy of the target schema is a migration
    that produces a table subtly unlike the one a fresh database gets, and the
    difference shows up seasons later.

    Scanned by line rather than split on `;`, which is the version that was
    written first and which silently returned a statement truncated at the
    semicolon inside a `--` comment. The comments are part of what is being
    copied: they are what `sqlite_master` hands back to the next person who
    asks the table what it is for.
    """
    want = f"CREATE TABLE IF NOT EXISTS {table} ("
    lines = script.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(want):
            for j in range(i, len(lines)):
                if lines[j].strip() == ");":
                    return "\n".join(lines[i:j + 1])
            break
    raise KeyError(table)


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


def create_derived(conn: sqlite3.Connection) -> None:
    """Create the derived, typed tables in the query database."""
    conn.executescript(DERIVED_DDL)
    conn.commit()


def create_derived_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(DERIVED_INDEXES)
    conn.commit()


def drop_derived(conn: sqlite3.Connection) -> None:
    """Drop every derived table, so a rebuild starts clean.

    Safe by construction: none of these is a source of truth
    (spec/05-DATABASE.md, opening), so dropping them costs only the time to
    rebuild from the archive.
    """
    for table in ("play_tags", "tags", "credit_sequences", "fielding_credits",
                  "runner_advances", "plays", "game_info", "games",
                  "derive_runs"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()


def attach_archive(conn: sqlite3.Connection, archive_path: str) -> None:
    """Attach the archive read-only, for a re-parse.

    Normal queries never need this: everything queryable is promoted into a
    typed table in the query database.
    """
    conn.execute("ATTACH DATABASE ? AS archive", (f"file:{archive_path}?mode=ro",))
