# Retrosheet Semantic Search Engine (RSSE)

Ask questions about baseball history in **baseball terms**, over every play
Retrosheet has published — 17,891,790 plays across 203,285 games, 1908 to 2025.

Retrosheet's event files are precise and complete, and they are written in a
dense notation that records what happened without naming it. `K.3XH(21)` is a
strikeout, an uncaught third strike, a throw from the catcher to the pitcher,
and a force out at home — and the file says none of those things. RSSE derives
them, keeps the original bytes, and lets you search on the derived facts.

```
rsse query --bases-loaded --outs 2 --dropped-third \
           --force-play-at H --putout-sequence 2,1
```

```
1908-09-14 WS1190809142   b5 2 out, bases 111   K.3XH(21);2-3;1-2;B-1

1 matching plays
searched: 202,650 games, 17,840,457 plays, 1908-2025, leagues AL/FL/NGL/NL
force certainty: 1 derived, 0 likely, 0 ambiguous; 0 batter_ran unknown
```

Once in 118 seasons. Philadelphia at Washington, 14 September 1908, bottom of
the fifth: bases loaded, two out, the batter strikes out, the ball gets away,
the catcher throws to the pitcher covering the plate, and the runner from third
is forced at home to end the inning.

Four lines below it in the source file, Retrosheet's own scorer left a note:

```
com,"Catcher threw to pitcher at home on dropped third strike"
```

Every tag on that play was derived from the event string alone. The comment is
an independent human account of the same play, and it agrees.

## Quick start

Python 3.11+, no third-party dependencies.

```bash
git clone <this repo> && cd retrosheet
python3 -m rsse.cli fetch                 # download the corpus  (~862 MB)
python3 -m rsse.cli ingest --progress     # byte-exact archive   (~11 min, 1.9 GB)
python3 -m rsse.cli derive --progress     # queryable tables     (~90 min, 11.6 GB)
python3 -m rsse.cli secondary --progress  # comments + lineups   (~10 min)
python3 -m rsse.cli coverage --rebuild    # coverage table       (~10 s)
```

Then ask it something:

```bash
python3 -m rsse.cli query --strikeout --force-play-at H --seasons 1996,1997
python3 -m rsse.cli query --triple-play --format json
python3 -m rsse.cli coverage --season-range 1908,1920
```

`fetch` is throttled and resumable. `derive` reads the **archive**, never the
event files, so a rebuild reproduces exactly the bytes that were ingested —
Retrosheet reissues corrected files, and a rebuild that silently picked up new
data would not be a rebuild.

If you only want a season or two, every build step takes `--season`:

```bash
python3 -m rsse.cli derive --season 2000    # ~1 min instead of ~90
```

## Querying

The CLI covers the common cases; the Python API covers all of them. Every
predicate has one defined SQL contract — see
[06-QUERY](spec/06-QUERY.md).

```python
from rsse.query import Search, connect

conn = connect("data/database/rsse.db")

result = (Search()
          .bases_loaded()
          .outs(2)                       # before the play; .outs_after() for the other
          .dropped_third()
          .force_play(at="H")
          .putout_sequence([2, 1])       # one throw: catcher to pitcher
          .run(conn))

for play in result:
    print(play.date, play.game_id, play.event_raw, play.tags)

print(result.coverage.describe())         # what was searched
print(result.excluded.describe())         # what the default filters removed
print(result.force.describe())            # how sure the force derivation is
```

`Search` is immutable — every method returns a new query — and nothing runs
until `.run()`, `.count()` or `.explain()`.

### Three things worth knowing before you trust a result

**A count never comes back bare.** Every result carries a `CoverageReport`
saying what was actually searched, and it cannot be suppressed. `0 rows` with
coverage reads *"no such play in the 202,650 games from 1908 to 2025"*; without
it, it reads *"never happened"*, which the data does not support. Coverage is
computed from the query's own scope, never from the rows it matched.

**Derived facts say how sure they are.** A force play is an inference — the
files never record one. Where the notation settles it, `force_certainty` is
`derived`; where a throw only implies it, `likely`; where nothing settles it,
the play claims no force and says so via `batter_ran = 'unknown'` (2.97% of the
corpus, 7.70% of 1920, 0.00% of 2020). All of it is reported rather than
quietly filtered, because an era comparison built on a silent 7% gap looks fine
and is wrong.

**What is not recorded is not offered.** There is no predicate for *was the out
made by touching the base or tagging the runner* — no era of Retrosheet records
it, and a predicate implying otherwise would be the most misleading thing in
the API. `.force_play()`, `.tag_out()` and `.out_at()` are what exist:
`.out_at()` is the union, the other two partition it, and running a query with
and without the derived predicate is how you check the derivation.

`.explain()` returns the SQL and the query plan. It is public API on purpose: a
researcher must be able to audit what a "never happened" answer actually asked.

## What it holds

Two databases, deliberately separate.

**The archive** is the source of truth: 31,115,272 records from 2,646 files,
stored byte for byte with provenance and per-game spans, 1.9 GB. Every event
string in it has been parsed and re-emitted identically — the round-trip gate
([02-GRAMMAR §6](spec/02-GRAMMAR.md)) — so nothing was silently dropped on the
way in.

**The query database** is derived from the archive and holds nothing that is a
source of truth, which is what makes it safe to rebuild:

| Table | Rows | What it is |
|---|---|---|
| `games` | 203,285 | one row per game, with the `info` header flattened |
| `plays` | 17,891,790 | one row per play: base-out state, score, the raw event string |
| `runner_advances` | 14,482,091 | every runner movement, with `is_force` and its certainty |
| `fielding_credits` | 12,983,019 | putouts, assists and errors by position |
| `credit_sequences` | 9,057,478 | one throw sequence each — `64(1)3` is two of them, not one |
| `play_tags` | 69,076,275 | 84 semantic tags, each with a derivation rule |
| `comments` | 222,495 | scorer notes, including 5,102 replay verdicts and 18,129 ejections |
| `lineup_entries` | 5,537,381 | starters and substitutions, as a timeline |
| `coverage` | 274 | what the corpus covers, per season and league |

Total 11.6 GB. `rsse verify --derived` runs 21 integrity checks over it,
all passing.

### Coverage caveats you should know about

- **No postseason.** Every file is a regular-season team file; there are no
  `.EVE` files in the corpus. `--only-postseason` reaches the tiebreakers and
  Negro Leagues championship games recorded inside team files, and never a
  World Series.
- **Pitch sequences are modern.** Most games before the 1990s record no pitch
  data at all. `CoverageReport` states the split for whatever you searched.
- **7 plays cannot be parsed** and 15 more inherit a base-out state known to be
  wrong because of them; those carry `state_untrusted` and are excluded by
  default ([03-STATE §7.1](spec/03-STATE.md)).
- **3,385 Major League games have no event file.** Retrosheet's own game logs
  list them and the corpus cannot cover them. They are concentrated in
  1920–1955 and peak in the war years — 289 games missing from 1944, 23% of the
  season. From 1960 onward the gap is essentially nil, but for a pre-1960 era
  question it is a real limit on what any count here can mean.
- **Retrosheet data is subject to correction** and carries no guarantee of
  accuracy.

## Status

Everything in the build order is built and validated against the full corpus.

| Layer | State |
|---|---|
| Corpus | 118 seasons, 1908–2025, 862 MB, 2,646 files |
| Parser | 17,891,790 plays; **0** unexplained parse failures, **0** round-trip failures |
| Archive | 31,115,272 records, 1.94 GB, all raw invariants hold |
| State machine | 203,285 games, **0** state-inconsistent plays |
| Ontology | 84 tags, one derivation rule each, a positive **and** a negative case each |
| Derived tables | 17,891,790 plays, 11.6 GB, **21/21** integrity checks |
| Comments / lineups | 222,495 comments, 5,537,381 lineup entries, 0 unreadable |
| Query API | `Search`, coverage and force reporting, `query` / `explain` / `coverage` |
| Tests | 246, all passing |

**Every replayed score matches the published one.** `rsse reconcile` compares
`games.final_home` / `final_away` — reconstructed by replaying 17,891,790 plays
— against Retrosheet's separately compiled game logs, the only numbers here
that do not come from the event files:

| | |
|---|---|
| games compared | **200,876** |
| scores agree | **200,876 (100.0000%)** |
| scores disagree | **0** |

Not built: `players`, `teams` and `parks`. The roster and team files are on
disk but have never been ingested, and reading them straight from the file tree
would break the rule that derived tables come from the archive. Doing it
properly needs a new archive table for records that belong to no game, since
`raw_records` is partitioned exactly by `game_spans` and `rsse verify` asserts
it. `parks` also needs a Retrosheet download the project does not hold.

### Commands

```
python3 -m rsse.cli fetch                      # download the corpus
python3 -m rsse.cli sweep --by-season --progress   # parse every event string (~10 min)
python3 -m rsse.cli ingest --progress          # build the archive (~11 min)
python3 -m rsse.cli verify                     # raw-layer integrity
python3 -m rsse.cli replay --progress          # replay every game (~15 min)
python3 -m rsse.cli tags --seasons 12          # era-spread tag census (~20 min)
python3 -m rsse.cli derive --progress          # derived tables (~90 min)
python3 -m rsse.cli verify --derived           # derived-table integrity (21 checks)
python3 -m rsse.cli secondary --progress       # comments + lineup_entries (~10 min)
python3 -m rsse.cli coverage --rebuild         # coverage table
python3 -m rsse.cli gamelogs --fetch           # game logs, for score reconciliation
python3 -m rsse.cli reconcile --explain-er     # replayed scores vs published ones
python3 -m rsse.cli query ...                  # search; --format table|json|csv
python3 -m rsse.cli explain ...                # the SQL and plan for a search
python3 -m unittest discover -s tests -t .
```

`sweep` exits non-zero on any round-trip failure or any parse failure not
listed in [tests/known-source-defects.json](tests/known-source-defects.json).
`derive --rebuild` replaces the query database; it warns before removing
anything `derive` itself does not rebuild.

### Layout

```
rsse/parser/     lexer, recursive-descent parser, AST that emits from its fields
rsse/model/      half-inning replay, force derivation, `com` and game-log records
rsse/semantic/   84 tags, implication, confidence, curated tags
rsse/database/   archive DDL and ingest; derived, secondary and coverage builds
rsse/query/      Search builder, SQL compiler, coverage and force reporting
tests/           246 tests; tests/gold/ holds 5 plays asserted through every layer
```

## Design notes

Four ideas do most of the work, and each was arrived at the hard way. The full
account of what building this found — every bug and what it implies — is in
`BUILD-LOG.md`.

**The raw data is never the derived data.** Two databases, and the derived one
holds no source of truth. That is what makes `--rebuild` safe and what makes a
correction traceable.

**Deterministic or flagged.** Every derived value is either produced by a
stated rule or marked uncertain. Nothing is guessed, and an inconsistency is
recorded rather than smoothed over — which is why 15 plays out of 17.9 million
carry a status saying their base state cannot be trusted, instead of looking
like every other row.

**A gate detects the class of error it is shaped for, and nothing else.** The
round-trip gate proves nothing was discarded, not that it was filed correctly.
The out-accounting invariant proves outs balance and says nothing about runs —
and when a run invariant was finally added, it immediately found that every
home run written with an explicit `B-H` advance had been scoring twice. Almost
every time a quantity nobody had checked acquired a check, it found a bug. The
exception is the biggest check of all: score reconciliation against the game
logs came back clean on all 200,876 games, which is the one result here that is
worth more for having been predicted to fail.

**A tag is a rule, not a name.** A tag cannot be registered without a
derivation, and it cannot ship without a positive *and* a negative test case: a
rule that only ever sees positives will happily over-fire.

## Specifications

The specs are the contract — written to be built from without consulting
anything else, and [02-GRAMMAR](spec/02-GRAMMAR.md) is validated against the
corpus rather than against Retrosheet's documentation alone.

| | |
|---|---|
| [01-CORPUS](spec/01-CORPUS.md) | Input files, record types, ingest, coverage and provenance |
| [02-GRAMMAR](spec/02-GRAMMAR.md) | Complete EBNF for the event and pitch fields; round-trip requirement |
| [03-STATE](spec/03-STATE.md) | Inning replay: bases, outs, score, and the force-play derivation |
| [04-ONTOLOGY](spec/04-ONTOLOGY.md) | Semantic tags, each with a derivation rule |
| [05-DATABASE](spec/05-DATABASE.md) | SQLite DDL and index plan |
| [06-QUERY](spec/06-QUERY.md) | Builder API, predicate semantics, result contract |
| [07-TESTING](spec/07-TESTING.md) | Gold corpus format and corpus-wide gates |
| [08-WORKED-EXAMPLE](spec/08-WORKED-EXAMPLE.md) | The motivating play through every layer |

Two documents the original discussion called for are **not** in the set: a
roadmap, and a record of the future extensions and non-goals it listed
(Statcast and Baseball Savant integration, WPA/RE24, custom ontologies; and the
non-goals bounding the project). Those are scope commitments rather than build
contracts, and they currently live only in the untracked build log.

## Licensing and attribution

The code and specifications in this repository are licensed under the
[Apache License 2.0](LICENSE). See [NOTICE](NOTICE), which redistributors must
carry forward.

Three distinct sets of rights meet in this project, and they are not the same:

**This project's code and specs** — Apache-2.0, as above.

**Retrosheet's data** — not covered by that license. Retrosheet permits any
use, including commercial, on one condition, which applies to you as soon as
you redistribute the data or anything built from it:

> The information used here was obtained free of charge from and is copyrighted
> by Retrosheet. Interested parties may contact Retrosheet at
> "www.retrosheet.org".

Note this reaches into the repository itself: `tests/gold/` embeds real event
records, so the obligation is not limited to a downloaded `data/` directory.
Retrosheet makes no guarantee of accuracy, and data is subject to correction.

**Retrosheet's documentation** — the format description at
<https://www.retrosheet.org/eventfile.htm> is copyrighted prose. The grammar in
[02-GRAMMAR](spec/02-GRAMMAR.md) encodes the *format*, which is factual, and
restates rather than reproduces their text.

## Development

RSSE was designed and directed by its author and written with substantial
assistance from an AI coding agent (Claude). The project's conception, the
research question behind it, the design decisions, and the review and
correction of the generated work are the author's.

This is disclosed because the copyright status of machine-generated material is
unsettled and jurisdiction-dependent, and downstream users may reasonably want
to know. It has no practical effect on your rights under the Apache License:
you may use, modify, and redistribute this work on those terms regardless.
