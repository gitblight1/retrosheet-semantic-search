# 07 — Testing

The original spec's testing philosophy is kept intact: every parser feature gets
an ordinary, an unusual, and a historical example; a bug gets a regression test
written **before** the fix. This document adds the file formats and the
corpus-wide gates, which were missing.

## 1. Layers

| Layer | Tested by |
|---|---|
| Lexer / grammar | unit tests per production; byte-exact round-trip (§3) |
| State machine | inning replay fixtures; box-score reconciliation (§4) |
| Ontology | one positive and one **negative** case per tag; corpus census |
| Database | schema migration tests, index-plan assertions; `rsse verify --derived` (§4.1) |
| Query | compiled-SQL assertions per predicate; end-to-end result assertions |

The negative case per tag is not optional. `K.1X2(26)` must not be tagged
`UncaughtThirdStrike` ([04-ONTOLOGY](04-ONTOLOGY.md) §3.1) — a rule that only
ever sees positives will happily over-fire.

**Status.** The ontology layer is built and this requirement is enforced
mechanically: [tests/test_ontology.py](../tests/test_ontology.py) carries both
kinds of case for all 84 tags, and a coverage test fails if a tag is registered
without them. The corpus-wide counterpart is `rsse tags`, which derives every
play and fails on two findings: a derivable tag that **never fires** (either
the rule is wrong or the encoding does not exist, and a suite full of positives
cannot tell you which), and a tag that fires on **over 90% of plays** (a rule
that broad is not selecting anything, whatever its name says).

`--seasons N` samples N seasons spread end to end across the corpus, which is
the cheap routine check. On a sample the never-fired finding is **advisory**: a
tag can be absent because it is rare rather than because its rule is wrong, and
`CourtesyFielder` and `PitcherInterference` each appear in one 12-season sample
and not another. The full-corpus run is the gate; the sample is the smoke test.
Sampling must span to the **most recent** season — new encodings live at the
end of the range, and a stride that stops short reports replay review and
placed runners as never firing.

## 2. Gold corpus

`tests/gold/`, one JSON file per play. This is the format the original spec
called for and left undefined.

```json
{
  "name": "dropped_third_force_home",
  "description": "Bases loaded, two outs. Uncaught third strike; catcher
                  retrieves the ball and throws to the pitcher covering
                  home for the force. Inning over.",
  "source": {
    "game_id": "DET202607230",
    "citation": "Retrosheet 2026DET.EVA",
    "note": "Detroit v Kansas City, 2026-07-23, first inning",
    "status": "player ids, count, and pitch sequence are placeholders until
               the 2026 event file is released; the event string, state, and
               expected tags are the assertion"
  },
  "input": {
    "records": [
      "play,1,0,colli001,12,CBBFS,K.3XH(21)"
    ],
    "state_before": {
      "inning": 1, "half": "top", "outs": 2,
      "bases": {"1": "runner_a", "2": "runner_b", "3": "runner_c"}
    }
  },
  "expected_parse": {
    "basic": {"type": "Strikeout", "fielders": []},
    "modifiers": [],
    "advances": [
      {"origin": "3", "destination": "H", "marked_out": true,
       "credits": [{"fielder": 2, "credit": "assist"},
                   {"fielder": 1, "credit": "putout"}]}
    ],
    "annotations": []
  },
  "expected_state": {
    "batter_dest": "1",
    "batter_is_out": false,
    "outs_recorded": 1,
    "outs_after": 3,
    "runs_on_play": 0,
    "advances": [
      {"origin": "3", "destination": "H", "is_out": true,
       "is_force": true, "force_certainty": "derived"}
    ]
  },
  "expected_tags": [
    "Strikeout", "UncaughtThirdStrike", "BatterReachedOnK",
    "ForceOut", "ForceOutAtHome", "BasesLoaded", "TwoOuts", "InningEnding"
  ],
  "forbidden_tags": ["CaughtStealing", "TagOut", "DoublePlay"],
  "expected_sql_fields": {
    "bases_before": "111", "outs_before": 2, "outs_after": 3,
    "is_inning_ending": 1
  },
  "roundtrip": "K.3XH(21)"
}
```

`forbidden_tags` is what makes a gold file a real assertion rather than a
snapshot. Every file MUST list at least one.

### 2.1 Encoding variants

A gold entry may carry an `equivalents` list of event strings that MUST produce
the same tag set. For the play above, at minimum:

```
K.3XH(21)
K.B-1;3XH(21)
K+WP.3XH(21);B-1
```

These differ in whether the batter advance is explicit and whether a wild pitch
was charged.

**The comparison excludes `WildPitch` and `PassedBall`.** The third encoding
charges a wild pitch and the others do not, so it must carry `WildPitch` and
they must not — an encoding that records something more is not the same as one
that records it wrongly. Everything else must agree exactly. Keeping the
exclusion list to those two tags is deliberate: widen it and the assertion
stops assuring anything.

**This assertion has already earned its place.** Run for the first time against
the built ontology, it failed on `K.3XH(21)` in two independent ways:

1. The out-count inference of [03-STATE](03-STATE.md) §4.5 step 1 was never
   implemented, so the bare form read the batter as retired on strikes, totalled
   four outs, and came back `state_inconsistent` — tagged `TagOut` where the
   other two encodings were tagged `ForceOut`. Wrong, and wrong in the
   direction that answers the motivating question incorrectly.
2. `UncaughtThirdStrike`'s trigger (c) was keyed on a *written* `B-%` advance,
   so even with the state fixed the bare form still carried a different tag set.

Neither was reachable from a corpus-wide invariant: no play of that shape
occurs in 1908–2025, and the corpus replay reported zero inconsistent plays.
A gold entry for a game that has not been released yet found both. A researcher asking "has this happened before" must find all of
them. This is the concrete reason the query API targets derived tags rather than
event-string patterns ([06-QUERY](06-QUERY.md) §3), and the reason the ontology
lists four independent triggers for `UncaughtThirdStrike`.

### 2.2 Near-miss pairs

`equivalents` proves the deriver does not *under*-fire. The opposite risk is a
rule that fires on the wrong plays, and the corpus must pin that down with plays
that look nearly identical and must be tagged differently.

The force out at home has real siblings. Elias counted three 2-1 putouts on an
uncaught third strike since 2021; the others were **tag** outs, where the bases
were not loaded and the lead runner went for home anyway — in one case from
second. Same putout sequence, same uncaught third strike, same base, different
answer.

Two mandatory near-misses, each defeating a different wrong rule:

| Gold file | Event and state | Must tag | Must **not** tag | Defeats |
|---|---|---|---|---|
| `strikeout_tag_home_2000` (**verified**) | `K/NDP.3XH(21)`, runners on 1 and 3, **one** out — real record, below | `Strikeout`, `TagOut` | `ForceOut`, `BatterReachedOnK`, `UncaughtThirdStrike`, `DoublePlay` | a rule that infers a force from the putout sequence, and one that credits a DP from two outs |
| `dropped_third_tag_home_from_second` | `K.2XH(21);B-1`, runner on 2 only, first base empty | `TagOut` | `ForceOut` | a force chain that ignores whether the bases *behind* the runner are occupied |

The first is a real record, taken from the released event file:

```
id,KCA200009270
...
play,3,1,ortih001,22,BCCBX,D7/L78S+
play,3,1,feblc001,12,FFBS,K
play,3,1,damoj001,22,BFFBX,S9/L34D.2-3
play,3,1,sancr001,12,CBFS,K/NDP.3XH(21)
```

Royals third, 27 September 2000 at Kauffman Stadium. Ortiz doubled and reached
third on Damon's single; Febles had struck out for the first out. Sanchez then
struck out and Ortiz was retired at the plate, catcher to pitcher.

`outs_before = 1`, `bases_before = '101'`. Out-count arithmetic
([03-STATE](03-STATE.md) §2): 1 + strikeout + out at home = 3, so the batter
**was** retired on strikes and never became a runner. No batter-runner, so no
force at any base — and second base was empty regardless. `is_force = 0`,
`TagOut`.

This entry is worth more than the other gold files combined, for three reasons:

- Its event string is **`K/NDP.3XH(21)`** — differing from the Tigers play only
  by the `/NDP` modifier. The advance, the putout sequence, and the base are
  identical. The two are separated by `outs_before` and `bases_before` alone.
  Nothing about the *string* distinguishes a force from a tag.
- `/NDP` is present precisely because two outs were recorded, and it suppresses
  the double play. A `DoublePlay` rule keyed on "two outs on the play" tags this
  wrongly; that is why [04-ONTOLOGY](04-ONTOLOGY.md) §5 carries the `/NDP`
  exception.
- It is verifiable **today**, against a released file, whereas the 2026 play is
  not. Until the 2026 season is published this is the anchor for the entire
  force derivation.

The second case is constructed rather than observed, and is marked as such until
a real instance is located. Both now exist as gold files, and both pass:
`dropped_third_tag_home_from_second` derives `TagOut` and not `ForceOut`,
because `forced_bases({2}, live)` is `{1}` — the runner on second has an empty
base behind them, and the batter-runner being live does not change that.

A note on the name `dropped_third_tag_home_batter_out` in §2.3: it refers to
the same record as `strikeout_tag_home_2000`, which is the file that exists.
Only two near-miss files are required, and there are two.

These two entries plus `dropped_third_force_home` are the minimal set that
distinguishes a real force derivation from a pattern match on `K` + `XH` +
`(21)`. The corresponding query-level assertion is in
[08-WORKED-EXAMPLE](08-WORKED-EXAMPLE.md).

### 2.3 Initial contents

Of the list below, five files exist:
`dropped_third_force_home`, `strikeout_tag_home_2000`,
`dropped_third_tag_home_from_second`, `dropped_third_putout_at_first` and
`strikeout_throw_out` — the two mandatory near-misses of §2.2 among them. Each
asserts its parse, its state, and its tags; `expected_sql_fields` is reported
as pending until the derived tables exist. The rest are outstanding, and the
historical ones (`pine_tar_game`, `harpers_obstruction`, `grand_slam_single`)
need a game id and a released file before they can assert anything.

Carried over from the original spec, plus the ones needed to cover the grammar's
sharp edges:

`dropped_third_force_home`, `dropped_third_tag_home_from_second`,
`dropped_third_tag_home_batter_out` (both below),
`dropped_third_putout_at_first` (`K23`, batter
retired — must **not** tag `BatterReachedOnK`), `strikeout_throw_out`
(`K.1X2(26)` — must not tag `UncaughtThirdStrike`), `hidden_ball_trick`
(curated, no derived tag), `unassisted_triple_play`, `four_strikeout_inning`,
`walkoff_balk`, `grand_slam_single`, `harpers_obstruction`, `pine_tar_game`,
`batting_out_of_turn`, `caught_stealing_negated_by_error` (`CS2(2E4)`),
`error_on_relay` (`BX2(7E4)`), `double_steal` (`SB3;SB2`), `catcher_interference`
(`C/E2`), `inside_the_park_hr`, `runner_passed_runner`, `placed_runner_scores`.

## 3. Round-trip gate

For every play in the corpus:

```
emit(parse(event_raw)) == event_raw
```

Whole corpus, every CI run, not a sample. This is the executable form of
principle 2.1, and it is the cheapest possible detector for a grammar that
quietly discards a construct.

Failures are reported grouped by the distinct shape of the offending substring,
so one missing production surfaces as one finding rather than 40,000.

## 4. Corpus-wide invariants

Run over the full corpus; each is a hard gate with a recorded baseline that may
only move downward.

| Invariant | Detects |
|---|---|
| `plays_unparsed == 0` | grammar gaps |
| round-trip failures `== 0` | information loss |
| `runs_on_play <= runners_on_base + 1` | runs credited to nobody |
| `runs_on_play ==` count of scoring advances | a run counted twice |
| reconstructed final score `==` game `info` score | state machine drift |
| reconstructed earned runs `==` `data,er` records | responsibility and error logic |
| every half-inning ends with 3 outs, or is the game's last | out accounting |
| `outs_before + outs_recorded <= 3` everywhere | over-counted outs from `X` with a negating error |
| no duplicate base occupancy in `bases_after` | advance resolution |
| plate appearances reconcile with roster-derived box scores | lineup, `sub`, `ladj` handling |

Score and earned-run reconciliation are the strongest checks available, because
they compare against numbers Retrosheet published independently of the event
strings. They will catch classes of bug no unit test is shaped to find. See
§4.3 for how they are implemented and what is still unverified.

The two **run** invariants are the cheap stand-in for them, and they are listed
because until the derived tables existed *nothing whatsoever checked a run
count*. The out-accounting invariant cannot see runs, score reconciliation
needs game logs the project does not hold, and the unit tests asserted whatever
the code produced — one of them asserted `runs_on_play == 3` on a play with a
single runner on base, which is arithmetically impossible, because it was
written against the observed value while a double-count was live. Every home
run written with an explicit `B-H` advance had been scoring one run too many
([03-STATE](03-STATE.md) §3.1).

The general lesson is worth stating, because this project has now hit it three
times. A gate detects the class of error it is shaped for and nothing else:
the round-trip gate proves nothing was discarded but not that it was filed
correctly ([02-GRAMMAR](02-GRAMMAR.md) §4); the out-accounting invariant proves
outs balance but says nothing about runs; and a unit test asserting current
behaviour proves only that behaviour has not changed. **Adding a gate for a
quantity nobody had checked found a bug every single time.**

### 4.1 `rsse verify --derived`

The invariants above run against the archive and the replay. The derived tables
need their own gate, because the load is a separate opportunity to be wrong:
`verify` proves nothing was lost in ingest, `verify --derived` proves it was
filed correctly. Twenty checks, each a query that must return zero rows —
referential integrity across the six derived tables, out accounting including
continuity of `outs_before` against the previous play's `outs_after`, the two
run invariants, base-state agreement between `plays` and `runner_advances`, and
`NULL`-state discipline on unparsed rows.

It runs after every `derive`. The first time it ran it failed three checks, and
all three traced to one omission: **a play's `parse_status` described its own
event string and said nothing about whether the state it inherited was
trustworthy** ([03-STATE](03-STATE.md) §7.1). Fifteen plays across 17.9 million
had been reading `ok` while sitting on a base state known to be wrong, and the
unparsed rows they descended from stored a fabricated `outs 0 / bases '000'`
because the columns were `NOT NULL`.

Two checks are filtered to exclude `state_untrusted` and `unparsed` plays,
since on those the state really is wrong and the check would fire correctly and
uselessly. **The excluded count is printed at the top of every run**, so the
exclusion is a visible number rather than a silent whitelist that could grow
without anyone noticing.

### 4.2 The query API's two invariants

Two assertions in [tests/test_query.py](../tests/test_query.py) carry more
weight than the rest, because each names a way the API can be wrong while
looking right.

**The trio must partition.** For every base, `.out_at(b)` must equal
`.force_play(at=b)` plus `.tag_out(at=b)`. The force/tag distinction is derived
([03-STATE](03-STATE.md) §4) and is therefore the part of the pipeline most
likely to be wrong; the partition is the only check available that does not
assume the derivation is correct. This is what [06-QUERY](06-QUERY.md) §3.2
means by an API that can express its own control case.

**Coverage must not depend on results.** A rare tag and a common one must
report the *same* `CoverageReport`. Coverage computed from matched rows says
"we looked exactly where we found something" — which reads as diligence and is
the failure the report exists to prevent. The first implementation did this,
and this assertion is what caught it.

A third is smaller but was a real bug: quality flags must be order-independent,
so `.strikeout().include_uncertain()` and `.include_uncertain().strikeout()`
must compile identically. A builder documented as immutable, whose answer
changes with call order, gives no signal that anything is wrong.

### 4.3 Game logs, and validating a positional format

Retrosheet's game logs are one 161-field CSV row per game, compiled separately
from the event files. That separation is the whole value: every other check in
this document compares the pipeline against itself, and internal consistency
cannot catch an error that is internally consistent.

`rsse gamelogs` loads them into the **archive** — they are source data, not
derived — in their own table rather than `raw_records`, which is partitioned
exactly by `game_spans` (§4) and must stay that way. `rsse reconcile` then
compares `games.final_home` / `final_away`, produced by replaying 17.9 million
plays, against the published score.

Three things the reconciliation must not treat as failures:

| | Why |
|---|---|
| a game in one source and not the other | the logs are Major League games; the corpus includes the Negro Leagues. Coverage, reported separately |
| a forfeit | the score is awarded by rule, not scored on the field |
| a suspended or later-completed game | the record is split, so neither half is a fair test |

**The field offsets cannot be validated by a fixture.** The format is
positional with no header row, so a wrong offset yields a plausible number
rather than an error — and a test file written from the offset table agrees
with that table however wrong it is. That is the same tautology as a unit test
asserting whatever the code currently produces.

What *can* be validated is that the values arriving in each field look like
the thing the field is supposed to hold. `check_layout` runs seven such tests
over every load — scores are plausible run totals, out counts are multiples of
three, earned runs do not exceed runs allowed, team ids are three characters —
and these are properties of baseball rather than of Retrosheet's file format,
so a one-field shift fails them en masse. Each carries a tolerance, because
real data has genuine oddities (a 19th-century game with no out count) and a
check with no tolerance would report those instead of a layout error. A failing
check means the offsets are wrong far more likely than the data is, and
`rsse gamelogs` says so and exits non-zero.

The **earned-run field is deliberately not chosen from the documentation.**
The layout names both an "individual" and a "team" earned-run figure per side
and does not settle which is the per-game total; picking wrong would report
thousands of false mismatches. Both are stored, and
`rsse reconcile --explain-er` decides it empirically by comparing each against
the event files' own `data,er` records — the same approach that settled the
replay verdict flag ([05-DATABASE](05-DATABASE.md) §5.1).

**Status: built, not yet run.** retrosheet.org was unreachable from the
development machine when this was written — DNS resolved, TCP to port 443 timed
out, other hosts connected in 0.1s — so no game log file has been loaded and no
score has been reconciled. Parsing, loading, the layout guard and the
reconciliation are implemented and tested against synthetic rows; the offsets
themselves are unconfirmed until a real file arrives. The download is also the
one part that cannot be tested here, so the loader takes files from a directory
and needs no network access at all.

## 5. Performance suite

Pinned queries with wall-clock budgets, run on a fixed corpus snapshot:

- the motivating query (bases loaded, 2 outs, uncaught third strike, force at
  home, sequence 2-1) — <100 ms
- single-tag lookups over the most common tags — <100 ms
- a full-corpus `.count()` on a rare tag — <250 ms
- `.contains_sequence()` on the unindexed path — budgeted separately and
  documented as slow
- full ingest — <10 min; database <2 GB; peak RSS <500 MB

## 6. Property tests

- Generate event strings from the EBNF; assert parse→emit is the identity.
- Generate random legal base/out states, apply each parsed play, assert the
  §4 invariants hold.
- Assert that force derivation depends only on `(bases_before, batter_live,
  out_order)` and nothing else — no dependence on the event's textual form.

## 7. Regression discipline

Unchanged from the original spec and restated because it is the rule most easily
skipped under time pressure: **when a parser bug is found, the failing case is
added to `tests/gold/` and observed to fail before any fix is written.** A fix
without a preceding red test is not accepted.
