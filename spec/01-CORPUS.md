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
file: absolute path, byte length, SHA-256, mtime, and line-ending style. A
re-ingest whose digest differs from the stored one MUST fail loudly rather than
silently mixing corpus vintages — Retrosheet reissues corrected files, and a
half-updated corpus produces answers that are wrong in an undetectable way.

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

### 5.3 Corrections

Retrosheet data is explicitly subject to revision. The corpus digest (§4) plus
the ingest timestamp are stored on every game, and results carry the corpus
version, so a past answer can be re-checked against a later corpus.

### 5.4 Data quality is queryable

Plays whose event string failed to parse, or whose state replay was
inconsistent, are retained with a `parse_status` other than `ok`
([05-DATABASE](05-DATABASE.md) §3). They are excluded from result sets by
default and counted in the coverage statement, so a query can never quietly
miss a play by failing to understand it.
