# Retrosheet Semantic Search Engine (RSSE)

RSSE is a semantic query engine over Retrosheet event files. It preserves raw
data while exposing parsed and semantic representations for historical baseball
research, so questions can be asked in baseball terms rather than Retrosheet
event syntax.

The motivating question: *bases loaded, two outs, uncaught third strike, catcher
throws to the pitcher covering home for the force — has that happened before?*
Retrosheet records neither "dropped third strike" nor "force play" as such, so
both must be derived. See [spec/08-WORKED-EXAMPLE.md](spec/08-WORKED-EXAMPLE.md).

## Specifications

Normative, in reading order:

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
non-goals bounding the project). Those are scope commitments rather than
build contracts, and they currently live only in the untracked build log.

The specs are the contract: they are written to be built from without
consulting anything else, and `spec/02-GRAMMAR.md` is validated against the
corpus rather than against Retrosheet's documentation alone.

## Status

**Parser layer built and validated against the complete corpus.** The corpus
sweep — build step 1 — is done:

| | |
|---|---|
| seasons | 118 (1908–2025, complete) |
| games | 203,282 |
| plays | **17,891,790** |
| parse failures | **7**, all [documented source defects](tests/known-source-defects.json) |
| unexplained failures | **0** |
| round-trip failures | **0** |
| sweep runtime | ~10 min |

The twelve grammar gaps the sweep closed, and what they imply, are in
[02-GRAMMAR §9](spec/02-GRAMMAR.md). One finding is left open: the `U` modifier,
which occurs in only four seasons and is undocumented — RSSE parses and
preserves it but deliberately asserts no meaning for it
([02-GRAMMAR §4.1](spec/02-GRAMMAR.md)).

```
rsse/parser/grammar.py    AST; every node emits from structured fields
rsse/parser/lexer.py      annotation trivia, and the +/- disambiguation
rsse/parser/parser.py     recursive descent over spec/02-GRAMMAR.md §2
rsse/parser/records.py    record and play-record reading
rsse/util/download.py     corpus acquisition, throttled
rsse/cli.py               rsse fetch | rsse sweep
rsse/database/schema.py   archive + query DDL
rsse/database/load.py     ingest, provenance, round-trip gate
rsse/model/state.py       half-inning state, force derivation, credits
rsse/model/game.py        whole-game replay, per-play context
rsse/semantic/ontology.py 84 tags, one derivation rule each
rsse/semantic/derive.py   implication, confidence, curated tags
rsse/database/derived.py  archive -> typed tables
tests/                    162 tests; tests/gold/ 5 gold plays
```

**Raw layer built and loaded** (build step 2). 31,115,272 records from 2,646
files, byte-exact, with provenance and per-game spans; all integrity invariants
hold (`rsse verify`, <1s). Two findings from the load are written up in the
specs: Retrosheet game ids are **not unique**
([01-CORPUS §5.4](spec/01-CORPUS.md)), and the measured size means the original
<2 GB goal has to be replaced ([05-DATABASE §7](spec/05-DATABASE.md)).

**State machine built and validated** (build step 3). Inning replay
reconstructs bases, outs, runs and — the point of the exercise — **force plays,
which Retrosheet never records**. Across all 203,285 games: **zero
state-inconsistent plays**, and the only three short half-innings are each
explained by a known source defect. Seven bugs the invariant caught, and the
six pre-1947 plays that contradict the rulebook, are written up in
[03-STATE §8](spec/03-STATE.md).

**Ontology built** (build step 4). 84 tags, each with a derivation rule over
the parse tree and the replayed state, in
[rsse/semantic/](rsse/semantic/). A tag exists only as a registered rule, so a
name cannot be added without a derivation; a tag added without both a positive
**and** a negative test case fails the suite. `rsse tags` derives the corpus
and reports a census, failing on any tag that never fires.

Building it found two defects in the layers below, both on the project's own
motivating play and both invisible to every corpus-wide invariant, because no
play of that shape occurs in 1908–2025:

- the out-count inference of [03-STATE §4.5](spec/03-STATE.md) was never
  implemented, so the bare form `K.3XH(21)` totalled four outs and came back
  tagged `TagOut` where its equivalents were tagged `ForceOut`;
- the batter's own out at first — the commonest force play in baseball — was
  producing no runner-advance row at all.

Both were caught by the gold corpus's `equivalents` assertion
([07-TESTING §2.1](spec/07-TESTING.md)), from an entry for a game that has not
been released yet.

The corpus census over a 12-season era-spread sample (1908–2025, 20,796 games,
1,821,286 plays): 7,050,409 tag rows, 0 plays skipped, 0.9% uncertain, no tag
firing on more than 54% of plays.

Running it found a **parser** bug three layers down. `G`, `F`, `L`, `P`, `BG`,
`BP` and `BL` were classified as no-argument modifier codes, so `/G6` parsed as
a trajectory and bare `/G` did not — and every consumer reading
`Modifier.trajectory` missed the bare form. `LineOut` had been under-firing by
**145%**, `GroundOut` by 51%, and the force derivation was falling back on its
one inference across a third of the corpus. The round-trip gate passes either
way, since a bare `G` emits as `G` from either node type: it proves nothing was
discarded, not that anything was filed correctly.

That in turn led to reworking the force certainty scale — see
[03-STATE §4.2](spec/03-STATE.md), which now separates *was there a force
situation* (derivable) from *was the out executed by a touch or a tag* (never
recorded, in any era).

**Derived tables built** (build step 5). `rsse derive` promotes the archive
into the typed tables searches run against — `games`, `plays`,
`runner_advances`, `fielding_credits`, `credit_sequences`, `tags`, `play_tags`
— and the [06-QUERY §1](spec/06-QUERY.md) SQL runs against them. Measured on
the full 2000 season: 222,509 plays, **567 bytes per play**, ~1.0 min/season,
projecting to ~10.1 GB for the corpus. The full-corpus build has not been run.

Building it found five defects in the layers below, the worst of which is that
**every home run written with an explicit `B-H` advance scored one run too
many** — a solo shot recorded as `HR/F7D+.B-H(UR)` scored two. Nothing had ever
checked a run count: the out-accounting invariant cannot see runs, score
reconciliation needs game logs we do not hold, and a unit test had asserted
`runs_on_play == 3` on a play with one runner on base, which is arithmetically
impossible. Adding the two run invariants of
[03-STATE §3.1](spec/03-STATE.md) then found 91 more plays, all traced to
`radj` not being the first record of its half-inning — so half the 2020+
extra-inning half-innings had no placed runner at all.

Not yet built: query API, and the §2 tables the motivating query does not need
(`lineup_entries`, `players`, `teams`, `parks`, `comments`, `coverage`).

### Commands

```
python3 -m rsse.cli fetch                   # download the corpus (~118 seasons)
python3 -m rsse.cli sweep --by-season --progress --report data/sweep-report.json
python3 -m rsse.cli ingest --progress          # build the raw layer (~11 min)
python3 -m rsse.cli verify                     # raw-layer integrity checks
python3 -m rsse.cli replay --progress          # replay every game (~15 min)
python3 -m rsse.cli derive --season 2000        # derived tables, one season (~1 min)
python3 -m rsse.cli derive --progress          # full derived layer (~80 min, ~10 GB)
python3 -m rsse.cli tags --seasons 12          # era-spread census (~20 min)
python3 -m rsse.cli tags --progress            # full corpus; hours, and the real gate
python3 -m unittest discover -s tests -t .
```

`sweep` exits non-zero on any round-trip failure or any parse failure not listed
in [tests/known-source-defects.json](tests/known-source-defects.json).

## Build order

1. **Corpus sweep first** — *done, all 118 seasons.* Run the §2 grammar over
   every event string before writing any semantic code. Gaps are cheap to fix
   in the parser and expensive once the ontology depends on it; the sweep found
   twelve.
2. **Raw layer and round-trip gate** ([02-GRAMMAR](spec/02-GRAMMAR.md) §6) —
   *done.* 31,115,272 records, byte-exact, 0 round-trip failures over the whole
   corpus on every run.
3. **State machine**, checked against the out-accounting invariant — *done,
   0 state-inconsistent plays.* Final-score and `data,er` reconciliation
   ([07-TESTING](spec/07-TESTING.md) §4) is the stronger check and still needs
   Retrosheet's game logs, which are a separate download.
4. **Ontology** — *done, 84 tags.* Tags with derivation rules
   ([04-ONTOLOGY](spec/04-ONTOLOGY.md)), validated per tag by a positive and a
   negative case and corpus-wide by `rsse tags`.
5. **Derived tables** ([05-DATABASE](spec/05-DATABASE.md) §2–4) — *done,
   validated on a full season.* `rsse derive`, reading the archive rather than
   the event files so a rebuild reproduces the ingested bytes.
6. Query API ([06-QUERY](spec/06-QUERY.md)), then the motivating query.

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

### Development

RSSE was designed and directed by its author and written with substantial
assistance from an AI coding agent (Claude). The project's conception, the
research question behind it, the design decisions, and the review and
correction of the generated work are the author's.

This is disclosed because the copyright status of machine-generated material is
unsettled and jurisdiction-dependent, and downstream users may reasonably want
to know. It has no practical effect on your rights under the Apache License:
you may use, modify, and redistribute this work on those terms regardless.
