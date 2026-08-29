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
| Ontology | one positive and one **negative** case per tag |
| Database | schema migration tests, index-plan assertions |
| Query | golden SQL per predicate; end-to-end result assertions |

The negative case per tag is not optional. `K.1X2(26)` must not be tagged
`UncaughtThirdStrike` ([04-ONTOLOGY](04-ONTOLOGY.md) §3.1) — a rule that only
ever sees positives will happily over-fire.

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
was charged. A researcher asking "has this happened before" must find all of
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
a real instance is located.

These two entries plus `dropped_third_force_home` are the minimal set that
distinguishes a real force derivation from a pattern match on `K` + `XH` +
`(21)`. The corresponding query-level assertion is in
[08-WORKED-EXAMPLE](08-WORKED-EXAMPLE.md).

### 2.3 Initial contents

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
| reconstructed final score `==` game `info` score | state machine drift |
| reconstructed earned runs `==` `data,er` records | responsibility and error logic |
| every half-inning ends with 3 outs, or is the game's last | out accounting |
| `outs_before + outs_recorded <= 3` everywhere | over-counted outs from `X` with a negating error |
| no duplicate base occupancy in `bases_after` | advance resolution |
| plate appearances reconcile with roster-derived box scores | lineup, `sub`, `ladj` handling |

Score and earned-run reconciliation are the strongest checks available, because
they compare against numbers Retrosheet published independently of the event
strings. They will catch classes of bug no unit test is shaped to find.

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
