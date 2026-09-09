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
  -- Surrogate: game_id repeats three times in the corpus (01-CORPUS §5.4).
  game_key        INTEGER PRIMARY KEY,
  game_id         TEXT NOT NULL,
  occurrence      INTEGER NOT NULL DEFAULT 1,
  file_id         INTEGER NOT NULL REFERENCES source_files,
  first_record_id INTEGER NOT NULL,
  last_record_id  INTEGER NOT NULL,
  record_count    INTEGER NOT NULL,
  UNIQUE (game_id, occurrence)
);
CREATE INDEX ix_spans_file ON game_spans (file_id);
CREATE INDEX ix_spans_game ON game_spans (game_id);
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

`game_id` is **not** a primary key here, for exactly the reason §1 gave it up in
`game_spans`: three ids repeat in the corpus
([01-CORPUS](01-CORPUS.md) §5.4), so the archive holds 203,285 spans under
203,282 distinct ids. The first cut of this section declared
`game_id TEXT PRIMARY KEY` anyway — the derived layer would have failed on the
same UNIQUE constraint that stopped the ingest, one layer later and after a
much longer run. `games` is keyed on a surrogate with an `occurrence` column,
and `plays` carries `game_key` alongside `game_id`.

```sql
CREATE TABLE games (
  -- Surrogate, for the same reason game_spans has one: game_id is NOT unique.
  game_key           INTEGER PRIMARY KEY,
  game_id            TEXT NOT NULL,
  occurrence         INTEGER NOT NULL DEFAULT 1,
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
  parse_status       TEXT NOT NULL DEFAULT 'ok',
  UNIQUE (game_id, occurrence)
);
CREATE INDEX ix_games_date ON games (date);
CREATE INDEX ix_games_season_type ON games (season, game_type);
CREATE INDEX ix_games_id ON games (game_id);

-- lossless: every info record, including ones the model ignores
CREATE TABLE game_info (
  game_key INTEGER NOT NULL REFERENCES games,
  key      TEXT NOT NULL,
  value    TEXT,
  seq      INTEGER NOT NULL,
  PRIMARY KEY (game_key, seq)
);

CREATE TABLE lineup_entries (
  game_key     INTEGER NOT NULL REFERENCES games,
  -- A running index in file order, not "0 for every start record". With all
  -- starters sharing seq 0 the primary key depends on no game listing the
  -- same player twice at the same position, which is an assumption about
  -- 203,285 games rather than a key. Running order is unique by construction
  -- and preserves the same information.
  seq          INTEGER NOT NULL,
  is_sub       INTEGER NOT NULL,
  -- The play a substitute entered after; NULL for a starter. This is what
  -- makes the table a timeline rather than a mapping, and it is what
  -- `.pitcher()` and `.fielder()` read (06-QUERY §2).
  play_id      INTEGER REFERENCES plays,
  player_id    TEXT NOT NULL,
  player_name  TEXT NOT NULL,
  team         INTEGER NOT NULL CHECK (team IN (0,1)),
  batting_order INTEGER NOT NULL,      -- 0 = pitcher in a DH game
  position     INTEGER NOT NULL,       -- 10 DH, 11 PH, 12 PR
  PRIMARY KEY (game_key, seq)
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
  game_key       INTEGER NOT NULL REFERENCES games,
  -- Denormalised beside the key: every query orders and reports by it, and
  -- §6's preference for legible ids over surrogates applies to output even
  -- where correctness needs the surrogate.
  game_id        TEXT NOT NULL,
  seq            INTEGER NOT NULL,          -- play order within the game
  record_id      INTEGER NOT NULL,          -- raw_records.record_id, in the archive

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

  -- Nullable: NULL for an unparsed play, whose state was never computed.
  -- A stored '000' would read as bases genuinely empty (03-STATE §7.1).
  outs_before    INTEGER,
  outs_recorded  INTEGER,
  outs_after     INTEGER,
  bases_before   TEXT,                      -- '000'..'111', or NULL
  bases_after    TEXT,
  runner_1_before TEXT, runner_2_before TEXT, runner_3_before TEXT,

  batter_dest    TEXT,                      -- '1','2','3','H','out',NULL
  batter_is_out  INTEGER NOT NULL DEFAULT 0,
  batter_ran     TEXT NOT NULL DEFAULT 'unknown'
    CHECK (batter_ran IN ('yes','no','unknown')),
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
                            'state_ambiguous','state_inconsistent',
                            'state_untrusted')),
  parse_error    TEXT,
  parser_version TEXT NOT NULL,

  UNIQUE (game_key, seq)
);
CREATE INDEX ix_plays_ctx  ON plays (bases_before, outs_before);
CREATE INDEX ix_plays_batter_ran ON plays (batter_ran)
  WHERE batter_ran = 'unknown';
CREATE INDEX ix_plays_game ON plays (game_id, seq);
-- record_id is a reference into the *archive*, which is a separate database
-- (§7 option 2), so it carries no REFERENCES clause. It is provenance: every
-- derived row can name the verbatim line it came from.
CREATE INDEX ix_plays_status ON plays (parse_status) WHERE parse_status <> 'ok';
```

`batter_ran` records whether the batter left the box
([03-STATE](03-STATE.md) §4.2), and it is a **column rather than a
`parse_status`** on purpose. Only the force derivation turns on it, and
`parse_status <> 'ok'` excludes a play from every default result (§4 of
[06-QUERY](06-QUERY.md)). Escalating the unresolved case would drop 2.97% of
the corpus — 7.70% of 1920, 0.00% of 2020 — from queries that have nothing to
do with force plays, so a count of home runs would come back short for reasons
about ground balls, and an era comparison would skew.

`unknown` claims no force at all: no `runner_advances` row asserts one. The
number belongs in the `CoverageReport` of a *force* query, which is where a
researcher can act on it, rather than in the silent difference between two
totals. The partial index makes that count a seek.

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
    CHECK (force_certainty IN ('derived','likely','ambiguous','n/a')),
  scored         INTEGER NOT NULL DEFAULT 0,
  earned         INTEGER,
  rbi            INTEGER,
  raw            TEXT NOT NULL,
  PRIMARY KEY (play_id, seq)
);
CREATE INDEX ix_adv_force ON runner_advances (is_force, destination)
  WHERE is_force = 1;
```

`force_certainty` is four-valued, and the fourth was added on measurement
rather than up front. `n/a` is a safe advance, where force status does not
apply. The other three grade the *force determination*
([03-STATE](03-STATE.md) §4.2): `derived` when a trajectory or modifier states
whether the batter ran, `likely` when only the fielding implies it, and
`ambiguous` when nothing settles it.

The distinction is not cosmetic. `likely` covers about half of all force outs
at first — bare `63`-style putouts, which is how the pre-1970s corpus is
written — so folding it into `ambiguous` would exclude them from default
results, and folding it into `derived` would claim more than the record
supports. Tag confidence is deliberately *binary*
([04-ONTOLOGY](04-ONTOLOGY.md) §1.4), with `likely` counting as `certain`, so
this column is the only place the three-way distinction survives. A query that
needs the strict population reads it directly.

```sql
CREATE TABLE fielding_credits (
  play_id     INTEGER NOT NULL REFERENCES plays,
  scope       TEXT NOT NULL CHECK (scope IN ('basic','advance')),
  scope_seq   INTEGER NOT NULL,             -- as in credit_sequences: a
                                            -- running index, not the advance
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
  scope_seq  INTEGER NOT NULL,   -- running index within scope; see below
  origin_seq INTEGER NOT NULL,   -- which out group or advance; provenance only
  seq_text   TEXT NOT NULL,      -- '21', '8434', '2E4'; 'U' kept, '99' kept
  has_error  INTEGER NOT NULL,
  PRIMARY KEY (play_id, scope, scope_seq)
);
CREATE INDEX ix_credseq_text ON credit_sequences (seq_text);
```

`.putout_sequence([2,1])` becomes `seq_text = '21'`; the "contains" variant
becomes a `LIKE '%21%'` fallback and is documented as slower
([06-QUERY](06-QUERY.md) §3).

**`scope_seq` cannot be the advance index.** The first cut of this schema
described it as "which advance, or 0", and that key is not unique. A single
advance can carry more than one credit sequence:

```
S9.BXH(TH)(E2/TH)(8E2)
```

Three parameters on one advance, two of them with credits — `E2` and `8E2` —
both of which would be `(advance, 0)`. Under `PRIMARY KEY (play_id, scope,
scope_seq)` that is a constraint failure at load, or, with an upsert, one
sequence silently overwriting the other. `scope_seq` is therefore a **running
index within the scope**, and the advance or out-group index moves to
`origin_seq`, which is provenance rather than a key and may repeat.

This is the same play `runner_advances` uses to show that an error does not
always negate an out ([02-GRAMMAR](02-GRAMMAR.md) §5) — it is unusually good at
finding assumptions.

**One row per putout group, not one per basic event.** The same field also
needs to number out groups rather than basic events: in `64(1)3` the `64` retires the
runner from first and the trailing `3` retires the batter, which are two
throws, two putouts and two outs. Flattening them to `643` would lose that,
and would also make `.putout_sequence([6,4,3])` — a plausible way to ask for a
6-4-3 double play — match a shape that never occurs, since Retrosheet writes
that play as `64(1)3`. So `scope = 'basic'` numbers the out groups, and
`scope_seq` is the group index.

The consequence for the query API is real and belongs in
[06-QUERY](06-QUERY.md) §3.1: `.putout_sequence()` matches one *throw
sequence*, not the whole play. A researcher asking for the fielders involved in
a double play wants `.putout_by()` or a `fielding_credits` join.

Credits are collected uniformly from wherever Retrosheet wrote them — a basic
putout group, a base-running event's parameters, or an advance parameter —
because the same physical play moves between those sections depending on where
the out occurred (`K23` versus `K.3XH(21)`). Requiring a query to know which
section a play was written in would defeat the point of the layer.

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
-- Populated from the ontology registry, not written by hand:
-- rsse.semantic.derive.tag_rows() is the single source, so a tag cannot exist
-- in the database without a rule or in the ontology without a row. rule_hash
-- is per tag rather than per ontology, so a version bump that touched one
-- derivation is distinguishable from one that touched forty.

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
  game_key INTEGER NOT NULL REFERENCES games, seq INTEGER NOT NULL,
  play_id INTEGER REFERENCES plays,          -- the play *before* the comment
  record_id INTEGER NOT NULL,                -- archive raw_records.record_id
  -- `protest` is not here: no `com` record carries a structured protest
  -- sub-record. The 861 comments mentioning a protest are prose and stay
  -- `text`, because inventing a kind the data does not encode would make an
  -- absence look like a finding.
  kind TEXT NOT NULL CHECK (kind IN ('text','replay','ejection',
                                     'umpchange','suspend')),
  text TEXT NOT NULL, payload TEXT,          -- JSON for structured kinds
  marker INTEGER NOT NULL DEFAULT 0,         -- the body began with `$`
  PRIMARY KEY (game_key, seq)
);

-- Built by `rsse coverage --rebuild` from `games` and `plays`, not carried
-- through the load: it is a GROUP BY, so it costs seconds after a derive and
-- would otherwise be a second thing to keep in step with the data.
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

### 5.1 `com` records carry a second record format inside them

222,495 `com` records, and 88% are free text. The rest hold **structured
sub-records in the quoted body** — an undocumented format nested inside the
documented one:

```
com,"ej,mcgud101,M,sherj901,Call at 2B"
com,"replay,6,pench001,HOU,welkt901,HOU03,O,N,I,,H"
com,"umpchange,4,ump1b,hurst801"
com,"suspended,19131002,NYC14,fans in bleachers"
```

The two counts below differ on purpose, and the gap is the point of §5.1's
warning: the first column counts bodies whose text *starts* with the tag, the
second counts bodies that actually parse as one.

| Tag | Prefix matches | Parsed | Fields |
|---|---|---|---|
| `ej` | 18,157 | **18,129** | person, role (`M` manager / `P` player), umpire, reason |
| `replay` | 5,106 | **5,102** | inning, player, team, umpire, site, call, **`Y`/`N` reversed**, … |
| `umpchange` | 1,468 | **1,465** | inning, position, umpire (`(none)` for a vacancy) |
| `suspended` | 195 | **195** | date `yyyymmdd`, site, reason |

The 40 records in the gap are prose that happens to begin with the tag word.
A prefix test would have read every one of them as a structured record.

**The `Y`/`N` field is the only machine-readable record of a replay verdict,
and its meaning is inferred.** It was cross-checked before being relied on: of
the 5,102 well-formed records, `Y` sits next to a prose comment saying
"overturned" 2,374 times against 5 saying "upheld", and `N` next to "upheld"
2,532 times against 30 saying "overturned" — 98.6% agreement with an
independent human account of the same play. The 48% reversal rate also matches
the published MLB figure. The 35 disagreements are stored as written rather
than reconciled; they are a statement about the data.

**A tag prefix is not sufficient to identify one.** Four prose comments begin
`replay, ` — "replay, scoring two runs" — and a prefix test alone reads them as
structured records and fabricates a verdict. Every parser therefore also
requires the field count and the shape of the fields it depends on. This is the
one place in the pipeline where a misclassification would produce a confident
wrong answer rather than a missing one.

The `$` prefix on 66,000 comment bodies is recorded as `marker` and **not
interpreted**. It appears in every era and the pattern does not settle to a
single reading — play-level versus game-level, or the start of a comment split
across records, both fit some cases and not others. Named neutrally for the
same reason as the `U` coverage marker ([02-GRAMMAR](02-GRAMMAR.md) §4.1).

### 5.2 Build order

`comments` and `lineup_entries` are built by `rsse secondary`, a separate pass
over the archive, **not** by `derive`. Neither needs the state machine: a `com`
record attaches to the play before it, and `plays.record_id` already names each
play's archive record, so the link is a lookup. That takes minutes instead of
an hour and a half, and it means both tables can be added to an existing query
database without re-deriving 17.9 million plays.

The tag `ReplayOverturned` is the exception and does need a re-derive, because
it is a *tag*: `rsse/model/game.py` sets `PlayContext.replay_reversed` from a
linked structured `replay` comment during replay, and tags are derived there.

Because these tables are not derived, `derive --rebuild` — which replaces the
file — destroys them. It counts and names them before doing so rather than
letting them disappear quietly.

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

The derived layer is now **measured**, on the full 2000 season (2,429 games,
222,509 plays, all indexes built):

| | |
|---|---|
| `plays` | 222,509 |
| `runner_advances` | ~180,000 |
| `fielding_credits` | 147,254 |
| `credit_sequences` | 102,392 |
| `play_tags` | 893,211 |
| on disk | **567 bytes per play** |
| wall clock | ~1.0 min per season |

At 567 bytes per play the full corpus projects to **~10.1 GB** and roughly 80
minutes, which lands inside the estimate below rather than overturning it. The
original order-of-magnitude guess was:

| Table | Rows | Rough size |
|---|---|---|
| `plays` | 17.9M | 1.5–2 GB plus indexes |
| `runner_advances` | ~25M | ~1.5 GB |
| `fielding_credits`, `credit_sequences` | ~40M | ~2 GB |
| `play_tags` | ~50M | ~1 GB plus its driving index |

**A realistic total is 8–12 GB, not 2 GB**, and 10.1 GB measured is inside it. The goal was aspirational and is
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
