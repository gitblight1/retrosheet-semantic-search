# 05 — Database Schema

SQLite. The DDL below is normative; migrations are numbered and forward-only.

Design constraints: the raw layer is byte-exact and never revised; every derived
table is rebuildable from `raw_records`; and the indexes are chosen so the
motivating query is a bounded lookup rather than a corpus scan.

## 1. Provenance and raw layer

```sql
CREATE TABLE corpus (
  corpus_id     INTEGER PRIMARY KEY,
  ingested_at   TEXT NOT NULL,
  rsse_version  TEXT NOT NULL,
  notes         TEXT
);

CREATE TABLE source_files (
  file_id       INTEGER PRIMARY KEY,
  corpus_id     INTEGER NOT NULL REFERENCES corpus,
  path          TEXT NOT NULL,
  sha256        TEXT NOT NULL,
  byte_length   INTEGER NOT NULL,
  mtime         TEXT NOT NULL,
  line_ending   TEXT NOT NULL CHECK (line_ending IN ('crlf','lf','mixed')),
  UNIQUE (corpus_id, path)
);

-- 100% of the input, verbatim. Nothing else may be a source of truth.
CREATE TABLE raw_records (
  record_id     INTEGER PRIMARY KEY,
  file_id       INTEGER NOT NULL REFERENCES source_files,
  line_no       INTEGER NOT NULL,
  game_id       TEXT,
  record_type   TEXT NOT NULL,
  raw_line      TEXT NOT NULL
);

-- A game's records are contiguous in record_id: each file is read once, in
-- order, and record_id is monotonic. One row per game replaces an index over
-- every record.
CREATE TABLE game_spans (
  game_id         TEXT PRIMARY KEY,
  file_id         INTEGER NOT NULL REFERENCES source_files,
  first_record_id INTEGER NOT NULL,
  last_record_id  INTEGER NOT NULL,
  record_count    INTEGER NOT NULL
);
CREATE INDEX ix_spans_file ON game_spans (file_id);
```

### 1.1 Why the raw layer carries almost no indexes

Measured on the 2000 season, the first cut of this schema spent **38% of the
database on two indexes**, and neither survived scrutiny:

| Index | Share | Verdict |
|---|---|---|
| `UNIQUE (file_id, line_no)` | 13% | **Redundant.** `source_files UNIQUE (corpus_id, path)` already prevents loading a file twice, and each file is read once line by line, so the pair is unique by construction. It guarded nothing and served no query. |
| `ix_raw_game (game_id, …)` | 25% | **Replaced.** ~760 MB over the full corpus, to support a lookup that `game_spans` answers with ~200k rows. |

A game lookup is now a `game_spans` seek plus a range scan on the integer
primary key:

```sql
SELECT * FROM raw_records
 WHERE record_id BETWEEN (SELECT first_record_id FROM game_spans WHERE game_id = ?)
                     AND (SELECT last_record_id  FROM game_spans WHERE game_id = ?);
-- SEARCH raw_records USING INTEGER PRIMARY KEY (rowid>? AND rowid<?)
```

Removing both took the raw layer from **99.8 to 54.0 bytes per record**.

The general rule this establishes: **the raw layer is an archive, not a query
surface.** Everything worth querying is promoted into a typed table by a later
stage. Indexes here are paid for on ~30 million rows and should be added only
against a query that cannot be served from a promoted table.

The contiguity `game_spans` relies on is an invariant of the loader, not an
assumption about the data, and it is asserted directly: the spans must
partition `raw_records` exactly, must not overlap, and a span lookup must
return the same rows as a `game_id` scan.

## 2. Games and lineups

```sql
CREATE TABLE games (
  game_id            TEXT PRIMARY KEY,
  file_id            INTEGER NOT NULL REFERENCES source_files,
  date               TEXT NOT NULL,          -- ISO yyyy-mm-dd
  season             INTEGER NOT NULL,
  game_number        INTEGER NOT NULL,
  home_team          TEXT NOT NULL,
  away_team          TEXT NOT NULL,
  league             TEXT,
  site               TEXT REFERENCES parks,
  game_type          TEXT,
  scheduled_innings  INTEGER,
  use_dh             INTEGER,
  home_bats_first    INTEGER NOT NULL DEFAULT 0,
  pitch_detail       TEXT CHECK (pitch_detail IN ('pitches','count','none')),
  tiebreaker_base    INTEGER,
  final_home         INTEGER,
  final_away         INTEGER,
  parse_status       TEXT NOT NULL DEFAULT 'ok'
);
CREATE INDEX ix_games_date ON games (date);
CREATE INDEX ix_games_season_type ON games (season, game_type);

-- lossless: every info record, including ones the model ignores
CREATE TABLE game_info (
  game_id  TEXT NOT NULL REFERENCES games,
  key      TEXT NOT NULL,
  value    TEXT,
  seq      INTEGER NOT NULL,
  PRIMARY KEY (game_id, seq)
);

CREATE TABLE lineup_entries (
  game_id      TEXT NOT NULL REFERENCES games,
  seq          INTEGER NOT NULL,       -- 0 for start records, then sub order
  is_sub       INTEGER NOT NULL,
  player_id    TEXT NOT NULL,
  player_name  TEXT NOT NULL,
  team         INTEGER NOT NULL CHECK (team IN (0,1)),
  batting_order INTEGER NOT NULL,      -- 0 = pitcher in a DH game
  position     INTEGER NOT NULL,       -- 10 DH, 11 PH, 12 PR
  PRIMARY KEY (game_id, seq, player_id, position)
);

CREATE TABLE players (
  player_id TEXT PRIMARY KEY, last_name TEXT, first_name TEXT,
  bats TEXT, throws TEXT
);
CREATE TABLE teams (season INTEGER, team_id TEXT, league TEXT, city TEXT,
  nickname TEXT, PRIMARY KEY (season, team_id));
CREATE TABLE parks (park_id TEXT PRIMARY KEY, name TEXT, city TEXT,
  state TEXT, opened TEXT, closed TEXT);
```

## 3. Plays

```sql
CREATE TABLE plays (
  play_id        INTEGER PRIMARY KEY,
  game_id        TEXT NOT NULL REFERENCES games,
  seq            INTEGER NOT NULL,          -- play order within the game
  record_id      INTEGER NOT NULL REFERENCES raw_records,

  inning         INTEGER NOT NULL,
  half           TEXT NOT NULL CHECK (half IN ('top','bottom')),
  batting_team   INTEGER NOT NULL CHECK (batting_team IN (0,1)),
  batter_id      TEXT NOT NULL,
  pitcher_id     TEXT,
  catcher_id     TEXT,
  bats           TEXT,                      -- after any badj
  throws         TEXT,                      -- after any padj

  count_balls    INTEGER,                   -- NULL when '??'
  count_strikes  INTEGER,
  pitch_seq      TEXT,

  event_raw      TEXT NOT NULL,             -- byte-exact, round-trip source
  event_basic    TEXT NOT NULL,
  event_modifiers TEXT NOT NULL,            -- JSON array
  event_advances TEXT NOT NULL,             -- JSON array
  annotations    TEXT NOT NULL,             -- JSON [[offset,char],...]

  outs_before    INTEGER NOT NULL,
  outs_recorded  INTEGER NOT NULL,
  outs_after     INTEGER NOT NULL,
  bases_before   TEXT NOT NULL,             -- '000'..'111'
  bases_after    TEXT NOT NULL,
  runner_1_before TEXT, runner_2_before TEXT, runner_3_before TEXT,

  batter_dest    TEXT,                      -- '1','2','3','H','out',NULL
  batter_is_out  INTEGER NOT NULL DEFAULT 0,
  runs_on_play   INTEGER NOT NULL DEFAULT 0,
  rbi_on_play    INTEGER NOT NULL DEFAULT 0,
  score_home_before INTEGER NOT NULL,
  score_away_before INTEGER NOT NULL,

  is_inning_ending INTEGER NOT NULL DEFAULT 0,
  is_final_play    INTEGER NOT NULL DEFAULT 0,
  is_walkoff       INTEGER NOT NULL DEFAULT 0,
  is_go_ahead      INTEGER NOT NULL DEFAULT 0,

  parse_status   TEXT NOT NULL DEFAULT 'ok'
    CHECK (parse_status IN ('ok','parsed_untagged','unparsed',
                            'data_contradicts_rules',
                            'state_ambiguous','state_inconsistent')),
  parse_error    TEXT,
  parser_version TEXT NOT NULL,

  UNIQUE (game_id, seq)
);
CREATE INDEX ix_plays_ctx  ON plays (bases_before, outs_before);
CREATE INDEX ix_plays_game ON plays (game_id, seq);
CREATE INDEX ix_plays_status ON plays (parse_status) WHERE parse_status <> 'ok';
```

```sql
CREATE TABLE runner_advances (
  play_id        INTEGER NOT NULL REFERENCES plays,
  seq            INTEGER NOT NULL,
  runner_id      TEXT,
  origin         TEXT NOT NULL CHECK (origin IN ('B','1','2','3')),
  destination    TEXT NOT NULL CHECK (destination IN ('1','2','3','H')),
  is_explicit    INTEGER NOT NULL,
  marked_out     INTEGER NOT NULL,          -- the 'X' as written
  is_out         INTEGER NOT NULL,          -- after error negation
  is_force       INTEGER NOT NULL DEFAULT 0,
  force_certainty TEXT NOT NULL DEFAULT 'derived'
    CHECK (force_certainty IN ('derived','ambiguous','n/a')),
  scored         INTEGER NOT NULL DEFAULT 0,
  earned         INTEGER,
  rbi            INTEGER,
  raw            TEXT NOT NULL,
  PRIMARY KEY (play_id, seq)
);
CREATE INDEX ix_adv_force ON runner_advances (is_force, destination)
  WHERE is_force = 1;
```

```sql
CREATE TABLE fielding_credits (
  play_id     INTEGER NOT NULL REFERENCES plays,
  scope       TEXT NOT NULL CHECK (scope IN ('basic','advance')),
  scope_seq   INTEGER NOT NULL,             -- which advance, or 0
  pos_in_seq  INTEGER NOT NULL,
  fielder     INTEGER,                      -- NULL for 'U'
  player_id   TEXT,
  credit      TEXT NOT NULL CHECK (credit IN ('putout','assist','error','none')),
  PRIMARY KEY (play_id, scope, scope_seq, pos_in_seq)
);
CREATE INDEX ix_credit_fielder ON fielding_credits (fielder, credit);
```

### 3.1 Normalized sequences — the fast path

Matching a putout sequence like `[2,1]` by reassembling `fielding_credits` at
query time is a join per play. Instead each credit sequence is also stored
flattened, so the predicate becomes an index seek:

```sql
CREATE TABLE credit_sequences (
  play_id    INTEGER NOT NULL REFERENCES plays,
  scope      TEXT NOT NULL,
  scope_seq  INTEGER NOT NULL,
  seq_text   TEXT NOT NULL,      -- '21', '8434', '2E4'; 'U' kept, '99' kept
  has_error  INTEGER NOT NULL,
  PRIMARY KEY (play_id, scope, scope_seq)
);
CREATE INDEX ix_credseq_text ON credit_sequences (seq_text);
```

`.putout_sequence([2,1])` becomes `seq_text = '21'`; the "contains" variant
becomes a `LIKE '%21%'` fallback and is documented as slower
([06-QUERY](06-QUERY.md) §3).

## 4. Tags

```sql
CREATE TABLE tags (
  tag_id      INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  category    TEXT NOT NULL,
  version     TEXT NOT NULL,
  rule_hash   TEXT NOT NULL,      -- hash of the derivation, per 04 §1.3
  deprecated_alias_of TEXT
);

CREATE TABLE play_tags (
  play_id    INTEGER NOT NULL REFERENCES plays,
  tag_id     INTEGER NOT NULL REFERENCES tags,
  confidence TEXT NOT NULL DEFAULT 'certain'
    CHECK (confidence IN ('certain','uncertain')),
  source     TEXT NOT NULL DEFAULT 'derived'
    CHECK (source IN ('derived','curated')),
  PRIMARY KEY (play_id, tag_id)
);
-- tag-first: the driving index for nearly every query
CREATE INDEX ix_playtags_tag ON play_tags (tag_id, play_id);
```

`play_tags` rows with `source='derived'` are deleted and rebuilt on every
re-derive; `source='curated'` rows are preserved ([04-ONTOLOGY](04-ONTOLOGY.md)
§8).

## 5. Comments, replays, coverage

```sql
CREATE TABLE comments (
  game_id TEXT NOT NULL REFERENCES games, seq INTEGER NOT NULL,
  play_id INTEGER REFERENCES plays,
  kind TEXT NOT NULL CHECK (kind IN ('text','replay','ejection','umpchange',
                                     'protest','suspend')),
  text TEXT NOT NULL, payload TEXT,          -- JSON for structured kinds
  PRIMARY KEY (game_id, seq)
);

CREATE TABLE coverage (
  corpus_id INTEGER NOT NULL REFERENCES corpus,
  season INTEGER NOT NULL, league TEXT NOT NULL,
  games INTEGER NOT NULL, teams INTEGER NOT NULL,
  first_date TEXT NOT NULL, last_date TEXT NOT NULL,
  games_with_pitches INTEGER NOT NULL,
  games_with_count_only INTEGER NOT NULL,
  games_without_pitch_data INTEGER NOT NULL,
  plays INTEGER NOT NULL, plays_unparsed INTEGER NOT NULL,
  plays_inconsistent INTEGER NOT NULL,
  PRIMARY KEY (corpus_id, season, league)
);
```

## 6. Operational notes

- `PRAGMA journal_mode = WAL`, `synchronous = NORMAL`, and a large `cache_size`
  during load; `ANALYZE` after.
- Load in one transaction per source file. On failure, roll back that file
  whole so the corpus never contains a partial game.
- Text ids (`player_id`, `team_id`) are stored directly rather than interned.
  Integer surrogates would shrink the file but obscure every query.
- Integrity checks must be single ordered passes. The natural formulation of
  the span-overlap check is a self-join of `game_spans` on range overlap; with
  203k spans and no usable index that is ~40 billion comparisons and does not
  finish. Scanning spans in `first_record_id` order settles overlap, gaps and
  coverage together, in under a second.

## 7. Measured size, and the target that must change

The original performance goals — **database < 2 GB**, **initial import < 10
minutes** — were set before any data existed. Both are now measured, and the
raw layer alone consumes essentially the whole budget:

| | |
|---|---|
| source event files | 862 MB of text, 2,646 files |
| raw-layer records | 31,115,272 |
| raw layer on disk | **1.94 GB** (62.4 bytes/record) |
| ingest wall clock | **10m50s** |

That is with both candidate indexes already removed (§1.1); the first cut was
3.0 GB projected. There is little left to trim without giving up either the
verbatim archive or the legibility of text ids.

The derived layer has not been built, and it is the larger half. Order-of-
magnitude, against 17.9M plays:

| Table | Rows | Rough size |
|---|---|---|
| `plays` | 17.9M | 1.5–2 GB plus indexes |
| `runner_advances` | ~25M | ~1.5 GB |
| `fielding_credits`, `credit_sequences` | ~40M | ~2 GB |
| `play_tags` | ~50M | ~1 GB plus its driving index |

**A realistic total is 8–12 GB, not 2 GB.** The goal was aspirational and is
not reachable by tuning. It has to be replaced with a measured one, and the
choice is a design decision rather than a detail:

1. **Raise the target** to ~12 GB and keep one database. Simplest; costs
   nothing but the assumption that this runs on a small disk.
2. **Split the archive from the query database.** The raw layer is an archive
   that no query touches — everything queryable is promoted into a typed table.
   Holding it in a separate file keeps the working database near 6 GB, lets the
   archive be detached or rebuilt on demand, and preserves reproducibility
   because it is regenerable from the event files with a recorded digest.
3. **Compress the archive**, one blob per game. Event text compresses roughly
   5:1, taking the raw layer under 500 MB, at the cost of row-level addressing
   into it.

Option 2 is the recommendation: it follows the distinction §1.1 already
established between archive and query surface, and it is the only one that
costs nothing in either information or legibility. **This is not yet decided
and nothing has been restructured** — the measurement is recorded here so the
choice is made deliberately rather than discovered when a disk fills.

The 10-minute import goal is likewise already spent on the raw layer. A
realistic figure for the full pipeline is 30–45 minutes, and the property worth
preserving is that ingest is resumable per file and re-parses do not re-read
the source files.
