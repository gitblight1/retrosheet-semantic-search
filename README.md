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

The specs are the contract: they are written to be built from without
consulting anything else, and `spec/02-GRAMMAR.md` is validated against the
corpus rather than against Retrosheet's documentation alone.

## Status

**Parser layer built and validated.** The corpus sweep — build step 1 — is
complete for the seasons on disk:

| | |
|---|---|
| seasons | 57 of 118 (1908–1961, plus 1965, 2000, 2023) |
| games | 73,131 |
| plays | **6,240,913** |
| parse failures | **0** |
| round-trip failures | **0** |
| sweep runtime | 3m30s |

Findings and the eleven grammar gaps the sweep closed are in
[02-GRAMMAR §9](spec/02-GRAMMAR.md).

```
rsse/parser/grammar.py    AST; every node emits from structured fields
rsse/parser/lexer.py      annotation trivia, and the +/- disambiguation
rsse/parser/parser.py     recursive descent over spec/02-GRAMMAR.md §2
rsse/parser/records.py    record and play-record reading
rsse/util/download.py     corpus acquisition, throttled
rsse/cli.py               rsse fetch | rsse sweep
tests/                    38 tests; tests/gold/ 4 gold plays
```

Not yet built: state machine, ontology, database, query API.

### Corpus download is incomplete

Seasons **1962–2025 are not downloaded**. A first fetch pass at one second
between requests pulled 54 archives and then the server stopped responding —
the whole of retrosheet.org became unreachable from this host, not just the
archives, so this is a block or a rate limit rather than a bad URL.

`rsse/util/download.py` now waits 5s between requests and backs off
exponentially on failure. Resume later with:

```
python3 -m rsse.cli fetch --since 1962
```

Then re-run the sweep. Expect it to find further grammar gaps in the unswept
seasons: the 1908–1961 range surfaced four that the 1965/2000/2023 sample did
not, so era coverage matters more than play count.

### Commands

```
python3 -m rsse.cli fetch --since 1962
python3 -m rsse.cli sweep --by-season --progress --report data/sweep-report.json
python3 -m unittest discover -s tests -t .
```

## Build order

1. **Corpus sweep first** — *done for 57 of 118 seasons, and to be re-run as
   the rest arrive.* Run the §2 grammar over every event string before writing
   any semantic code. Gaps are cheap to fix in the parser and expensive once
   the ontology depends on it, and the sweep has found eleven so far.
2. Raw layer and round-trip gate ([02-GRAMMAR](spec/02-GRAMMAR.md) §6) — the
   gate is implemented and green; the persisted raw layer is not built.
3. State machine, checked against final scores and `data,er`
   ([07-TESTING](spec/07-TESTING.md) §4) — the strongest available signal that
   the replay is right.
4. Ontology, database, query API.

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
