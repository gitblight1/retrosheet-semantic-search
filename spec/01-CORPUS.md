# 01 — Corpus, Input Format, and Coverage

Normative. Defines what RSSE reads. Source of truth for the format is
<https://www.retrosheet.org/eventfile.htm>; this document restates the parts the
implementation depends on and fixes the decisions Retrosheet leaves open.

## 1. Attribution (required)

Retrosheet permits any use of the data, including commercial, on one condition.
This notice MUST appear in the README, in `rsse --version`, and in any exported
result set:

> The information used here was obtained free of charge from and is copyrighted
> by Retrosheet. Interested parties may contact Retrosheet at
> "www.retrosheet.org".

Retrosheet makes no accuracy guarantee and data is subject to correction. See
§5.3 for the consequence.

## 2. Files

### 2.1 Event files

Name: `YYYYTTT.EVX` — four-digit year, three-character Retrosheet team code,
and an extension identifying the league:

| Ext | League |
|---|---|
| `.EVA` | American |
| `.EVN` | National |
| `.EVF` | Federal |
| `.EVR` | Negro Leagues |

Each file holds one team's **home** games in chronological order. ASCII, one
record per line, terminated by CRLF. The loader MUST accept LF-only files
(some mirrors normalize) and MUST record which it saw, because line endings are
part of byte-exact round-trip (§6 of [02-GRAMMAR](02-GRAMMAR.md)).

### 2.2 Companion files

| File | Contents | Required |
|---|---|---|
| `TEAMYYYY` | team code → league, city, nickname for that season | yes |
| `*.ROS` | per-team roster: player id, name, bats, throws, team, position | yes |
| `parkcode.txt` | ballpark id → name, city, dates | yes |

Rosters supply the batting/throwing handedness that `badj` and `padj` records
override. Without them those records cannot be interpreted, so roster load is a
hard prerequisite, not an enrichment.

### 2.3 Player ids

Eight characters: first four letters of the last name (dash-padded if shorter),
first initial of the common name, three digits. Numbers from `001` are players
appearing in or after 1983; from `101`, players who finished before 1983.
Example: `joner002` is Ruppert Jones.

Ids are opaque keys. RSSE MUST NOT parse meaning out of them.

## 3. Record types

Every record is `type,field,field,...` with the type starting in column 1. The
type is not a field. Fields containing commas are double-quoted; **a comma
inside a quoted field is not a separator**, and a naive `line.split(",")` is
therefore a defect.

| Type | Purpose |
|---|---|
| `id` | 12-char game id: `TTTYYYYMMDDN`. `N` is 0 single, 1 first, 2 second game. Starts a game; ends the previous one. |
| `version` | File-management only. Retain verbatim, ignore semantically. |
| `info` | One `info,type,data` per fact. 30–40 per game. See §3.1. |
| `start` | Starting lineup: `player_id,"name",team,batting_order,position`. |
| `sub` | Substitution, same five fields. Always preceded by a `play` with event `NP`. |
| `play` | A game event. See §3.2. |
| `badj` | `badj,batter_id,hand` — batter bats from the unexpected side. |
| `padj` | `padj,pitcher_id,hand` — pitcher throws with the unexpected hand. |
| `ladj` | `ladj,batting_team,batting_order_position` — team batted out of order. |
| `radj` | `radj,runner_id,base` — extra-inning placed runner (2020+). |
| `presadj` | `presadj,pitcher_id,base` — forces inherited-runner responsibility. |
| `data` | `data,er,pitcher_id,n` — earned runs. Follows the last play. |
| `com` | Free comment, second field quoted. Also carries structured payloads (§3.3). |

Record order within a game: `id`, `version`, `info`*, `start`*, then a stream of
`play` / `sub` / `com` / adjustment records, then `data`*. The loader MUST NOT
assume `info` records appear in any particular order.

### 3.1 `info` records the model consumes

`visteam`, `hometeam`, `date` (`yyyy/mm/dd`), `number`, `site`, `starttime`,
`daynight`, `innings` (scheduled length; 7 for 2020 doubleheaders), `tiebreaker`,
`usedh`, `pitches` (`pitches` | `count` | `none`), `htbf`, `oscorer`,
`umphome`/`ump1b`/`ump2b`/`ump3b`/`umplf`/`umprf`, `fieldcond`, `precip`, `sky`,
`temp`, `winddir`, `windspeed`, `timeofgame`, `attendance`, `wp`, `lp`, `save`,
`gwrbi`, `gametype`.

Sentinels: `temp` 0, `attendance` 0, and `timeofgame` 0 mean unknown, not zero.
`windspeed` -1 means unknown. Umpire `(none)` means unassigned. These MUST map
to SQL `NULL`, and the raw value MUST still be retained in `game_info`.

`gametype` (2023+) is one of `regular`, `exhibition`, `preseason`, `allstar`,
`playoff`, `worldseries`, `lcs`, `divisionseries`, `wildcard`, `championship`.
`playoff` means a tiebreaker game and is a subset of `regular`; `preseason` is a
subset of `exhibition`. Query defaults exclude `exhibition` and `preseason`
(see [06-QUERY](06-QUERY.md) §5).

`howentered` is obsolete and MUST be ignored semantically.

`usedh` false and the 2022+ "Ohtani rule" interact: a starting pitcher may also
be the DH, appearing in **two** `start` records (position 1 and position 10).
Lineup construction MUST tolerate this rather than treating it as a duplicate.

### 3.2 The `play` record

```
play,inning,team,player_id,count,pitches,event
```

1. `inning` — integer from 1.
2. `team` — `0` visiting, `1` home.
3. `player_id` — the batter at the plate. On base-running events that do not
   involve the batter (`SB2`, `WP`, `BK`, …) this is **still the batter**, not
   the runner. Attributing such a play to the listed player is a defect.
4. `count` — two digits, balls then strikes; `??` when unknown (most pre-1988
   games).
5. `pitches` — variable length, may be empty. Alphabet in
   [02-GRAMMAR](02-GRAMMAR.md) §7.
6. `event` — the play description. [02-GRAMMAR](02-GRAMMAR.md).

A plate appearance may span several `play` records when a running event
interrupts it (steal, wild pitch, balk). The pitch sequence is broken by a `.`
at the interruption and resumes on the next record for the same batter.

### 3.3 Structured `com` payloads

`com` is normally prose, but four structured forms exist and MUST be parsed into
typed rows rather than left as text:

- `com,"replay,inning,batter,batter_team,umpire,park,reason,reversed,initiator,team,type"`
  — follows any play tagged `/UREV` or `/MREV`.
- `com,"ej,ejectee,job_code,umpire_id,reason"` — job code `P`/`M`/`C`/`T`/`N`.
- `com,"umpchange,inning,position,umpire_id"` — `(none)` means vacated.
- `com,"Protest=Code"` (`P`/`V`/`H`/`X`/`Y`) and
  `com,"Suspend=YYYYMMDD,ParkID,Vis,Home,Outs"`.

A comment beginning with `$` is displayed in Retrosheet's own narrative; retain
the marker.

## 4. Ingest pipeline

```
.EV* bytes
  → raw_records      one row per line, verbatim, with file + line number
  → games/lineups    id, info, start, sub, adjustments
  → parsed plays     grammar layer, no baseball knowledge
  → game state       inning replay: bases, outs, score, force status
  → semantic tags    ontology
```

Each stage depends only on the one above. Raw records are written first and are
never revised by a later stage; a re-parse rebuilds every downstream table from
`raw_records` without re-reading the source files.

**Immutability.** Source files are opened read-only. The loader records for each
file: **resolved** path, byte length, SHA-256, mtime, and line-ending style. A
re-ingest whose digest differs from the stored one MUST fail loudly rather than
silently mixing corpus vintages — Retrosheet reissues corrected files, and a
half-updated corpus produces answers that are wrong in an undetectable way.
`ingest --refresh` is the way to consent to the replacement (§5.3); nothing
else may proceed past the digest.

The path is resolved rather than stored as typed because the uniqueness
constraint is on the string: two spellings of one file are two files, and that
once loaded a second complete copy of the corpus (spec/05-DATABASE.md §1.1).

## 5. Coverage and provenance

RSSE's headline question is "has this ever happened before?" A zero-row result is
a claim about the world, and it is only as good as the corpus behind it.

### 5.1 Coverage is derived, never hard-coded

The `coverage` table is computed from the loaded data at ingest, not written by
hand. Per (season, league) it records: games loaded, distinct teams, first and
last game date, count of games with `pitches=pitches` / `count` / `none`, and
count of games flagged deduced or reconstructed.

### 5.2 Every negative result carries a coverage statement

A search returning zero rows MUST return, alongside the empty result set, the
season range, game count, and league set actually searched. The API surface for
this is fixed in [06-QUERY](06-QUERY.md) §6. Rendering "0 results" without it is
a spec violation, because the honest answer to the motivating question is
"not in the N games covered", never "never".

### 5.3 Corrections and refresh

Retrosheet data is explicitly subject to revision, and corrected files are
reissued in place. The corpus digest (§4) plus the ingest timestamp are stored
on every file, and results carry the corpus version, so a past answer can be
re-checked against a later corpus.

**What exists.** Every source file is recorded with its SHA-256 at ingest.
Re-running `ingest` skips any file whose digest is unchanged, and *fails* on
one whose digest differs rather than mixing vintages silently. So local change
detection is already per-file and already cheap — a re-ingest over an unchanged
corpus reads and hashes, but writes nothing.

**Upstream change detection.** `rsse fetch --refresh` sends the stored `ETag`
and `Last-Modified` as `If-None-Match`/`If-Modified-Since`; a `304` ends the
request with no body transferred. The validators live in a sidecar
`.fetch-state.json` beside the season archives rather than in the corpus
database, because `fetch` runs before any database exists and what it is
tracking is the state of a directory of zip files.

It is **not** the default. Checking all 118 seasons is 118 requests against a
volunteer-run nonprofit that stopped answering this project once already
(BUILD-LOG §3.3), so a present archive is still taken as current unless the
question is asked explicitly. Two smaller consequences of the same principle:
a 4xx other than 408/429 now fails after one request instead of being retried
four times with exponential backoff, and the digest of the body — not the
server's validators — decides whether anything actually changed, so a server
that declines to answer `304` costs bytes but disturbs nothing downstream.

**Refreshing only what changed.** `rsse ingest --refresh` replaces exactly the
reissued file's records, spans and `source_files` row. The freed `record_id`s
are not reclaimed and the replacement records are appended, which leaves a hole
in `record_id` space — deliberate, and the reason `rsse verify` counts gaps but
does not fail on them. The invariant that matters is that the spans tile the
records that exist and do not overlap, not that the integers run consecutively;
renumbering to close the hole would rewrite every row after the deleted file to
buy nothing.

**Refreshing the derived layer.** `rsse derive --refresh` rebuilds only the
games the two databases disagree about. Agreement is decided on **content**:
each derived game records the `sha256` of the file it came from
(`games.source_sha256`), and a game agrees when its key and that digest both
match the archive.

This has to be content and not identity, which is not obvious and was got wrong
first. `game_spans.game_key` and `raw_records.record_id` are plain `INTEGER
PRIMARY KEY`s, so deleting a reissued file's rows frees those rowids and the
replacement takes them straight back — measured, a refreshed two-game file came
back as keys 1 and 2, exactly as before. A reconciler comparing key sets
reported nothing stale and nothing missing, and would have left the derived
layer serving the superseded vintage with every check green: the precise
outcome the digest refusal exists to prevent.

**A record of what changed.** Every refresh writes a `file_revisions` row —
path, old digest, new digest, timestamp, record counts either side, games
replaced — inside the same transaction as the replacement records, so the
corpus can never hold one without the other. `rsse verify` prints them
whenever there are any. A corpus that has been refreshed is not the corpus that
was first ingested, and that is exactly the fact a published answer may need to
be re-checked against.

### 5.4 Retrosheet game ids are not unique

The `id` record is documented as identifying the date, home team, and game
number, which reads as a unique key. It is not one. Three ids appear twice, each
time **within a single file**, all in Negro League (`.EVR`) files:

| Game id | File | Blocks |
|---|---|---|
| `CI2194308030` | `1943NGL.EVR` | 68 plays and 68 plays |
| `NY6194505230` | `1945NGL.EVR` | 90 plays and 90 plays |
| `PRG193512012` | `1935NGL.EVR` | 81 plays and **40 plays** |

The first two look like duplicated entries. The third is not a duplicate at all
— the two blocks differ in length, so at least one is a partial account and
discarding either would lose data.

Consequences, all of them binding:

1. **`game_id` cannot be a primary key.** `game_spans` keys on a surrogate and
   numbers repeats with an `occurrence` column; `games` must do the same. A
   schema that assumes uniqueness fails the ingest outright, which is how this
   was found.
2. **Repeats are kept, never dropped.** Principle 2.1 admits no exception for
   inconvenient records, and the third case shows the two blocks are not
   interchangeable.
3. **They must not be silently double-counted.** This is the sharp edge: a
   duplicated game inflates every play it contains, and the project's central
   question is a count. Any query answering "how many times has this happened"
   must either deduplicate by `(game_id, occurrence = 1)` or report the repeat,
   and the ingest reports repeats explicitly so the decision is never made by
   accident.

Resolving whether these are true duplicates is a question for Retrosheet, not
for RSSE to guess at.

### 5.5 Data quality is queryable

Plays whose event string failed to parse, or whose state replay was
inconsistent, are retained with a `parse_status` other than `ok`
([05-DATABASE](05-DATABASE.md) §3). They are excluded from result sets by
default and counted in the coverage statement, so a query can never quietly
miss a play by failing to understand it.
