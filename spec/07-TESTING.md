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
| no two `source_files` share a `sha256` | the same file ingested twice under two path spellings |
| `runs_on_play <= runners_on_base + 1` | runs credited to nobody |
| `runs_on_play ==` count of scoring advances | a run counted twice |
| reconstructed final score `==` game `info` score | state machine drift |
| derived earned-run bounds contain `data,er` | responsibility and error logic (§4.5) |
| every half-inning ends with 3 outs, or is the game's last | out accounting |
| `outs_before + outs_recorded <= 3` everywhere | over-counted outs from `X` with a negating error |
| no duplicate base occupancy in `bases_after` | advance resolution |
| plate appearances reconcile with roster-derived box scores | lineup, `sub`, `ladj` handling |

Score and earned-run reconciliation are the strongest checks available, because
they compare against numbers Retrosheet published independently of the event
strings. They will catch classes of bug no unit test is shaped to find. See
§4.3 for the score check and §4.5 for earned runs — which turned out to be
denser than either of these lines suggests, because Retrosheet adjudicates
every run individually and not merely every game.

The earned-run row says *contain*, not *equal*, and that is not a weakening.
9.16 defers several of its own clauses to the scorer, so the derivation
produces an interval — the runs it calls earned, and those it declines to call
either way ([03-STATE](03-STATE.md) §9.3). A published figure either falls
inside that interval or it does not, which is a falsifiable check that does not
require pretending to have resolved an ambiguity the rule leaves open.

The two **run** invariants are the cheap stand-in for them, and they are listed
because until the derived tables existed *nothing whatsoever checked a run
count*. The out-accounting invariant cannot see runs, score reconciliation
needs game logs the project does not hold, and the unit tests asserted whatever
the code produced — one of them asserted `runs_on_play == 3` on a play with a
single runner on base, which is arithmetically impossible, because it was
written against the observed value while a double-count was live. Every home
run written with an explicit `B-H` advance had been scoring one run too many
([03-STATE](03-STATE.md) §3.1).

The general lesson is worth stating, because this project has now hit it four
times. A gate detects the class of error it is shaped for and nothing else:
the round-trip gate proves nothing was discarded but not that it was filed
correctly ([02-GRAMMAR](02-GRAMMAR.md) §4); the out-accounting invariant proves
outs balance but says nothing about runs; a unit test asserting current
behaviour proves only that behaviour has not changed; and the archive's
partition invariants, below, proved the corpus was consistent while it held two
of everything. **Adding a gate for a quantity nobody had checked found a bug
every single time.**

#### A partition of a doubled corpus is still a partition

The archive checks are all *partition* checks: the spans tile the records, the
files sum to the records, no span overlaps its neighbour, no gaps. They are the
right checks, they were correct, and they were all green on an archive
containing the corpus twice.

`rsse ingest` resolves its default root to an absolute path; `rsse ingest
--path data/events` names the same 2,646 files relatively. `source_files` is
unique on `(corpus_id, path)`, and the two spellings are different strings, so
the second run inserted 2,646 new files, 31,115,272 new records and 203,285 new
spans — and every count above simply doubled. 62,230,544 records tiled exactly
by 406,570 spans, declared exactly by 5,292 files. Contiguous, non-overlapping,
complete. Nothing was wrong with the partition; there were just two corpora in
it.

`game_spans` is unique on `(game_id, occurrence)`, which did not stop it either.
`occurrence` exists for the three games the corpus genuinely records twice, and
it absorbed 203,282 duplicates as second occurrences without complaint — a
field doing exactly what it was built to do, on data that meant something else.

The check shaped for this class is not a partition check at all, which is the
point of including it:

    SELECT count(*) FROM (SELECT sha256 FROM source_files
     GROUP BY corpus_id, sha256 HAVING count(*) > 1)

Two files with identical content in one corpus. Each event file is one team's
season, so in a sound corpus this is zero; after the double ingest it was
2,646. The same test applied to `aux_files` would be wrong — sixteen `TEAM`
files are byte-identical to the previous season's, because no franchise moved
— and that asymmetry is the reason the gate is stated per table rather than as
a general rule about duplicate content.

The repair needed no re-ingest. The duplicate occupied contiguous rowid ranges
in all three tables, so three range deletes restored the archive exactly, with
the surviving 2,646 files re-hashed against disk first.

### 4.1 `rsse verify --derived`

The invariants above run against the archive and the replay. The derived tables
need their own gate, because the load is a separate opportunity to be wrong:
`verify` proves nothing was lost in ingest, `verify --derived` proves it was
filed correctly. Twenty checks, each a query that must return zero rows —
referential integrity across the six derived tables, out accounting including
continuity of `outs_before` against the previous play's `outs_after`, the two
run invariants, base-state agreement between `plays` and `runner_advances`, and
`NULL`-state discipline on unparsed rows.

It has grown to **37**: the original twenty, plus the earned-run, reference,
replay and hit-location checks, each group skipped with a printed note when the
table or column it reads is absent.

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
over every load — scores are plausible run totals, earned runs do not exceed
runs allowed, team ids are three characters — and these are properties of
baseball rather than of Retrosheet's file format, so a one-field shift fails
them en masse. Each carries a tolerance, because real data has genuine
oddities and a check with no tolerance would report those instead of a layout
error. A failing check means the offsets are wrong far more likely than the
data is, and `rsse gamelogs` says so and exits non-zero.

**A guard is only as good as the belief encoded in it.** On the first real
load, six checks passed at 0.000% and one failed: *out counts are a multiple
of three*, on 16,838 of 235,607 rows. The offsets were right; the belief was
wrong. A walk-off ends the home half early, so the game's total out count is
not a multiple of three — and 7.7% of games end that way. What settled it was
that **98.95% of the offending rows are home wins, against a 58.65% home-win
rate overall**, which is the walk-off signature and is not something a shifted
field could produce. The check now reads *a multiple of three unless the home
team won*.

The episode is the argument for investigating a firing guard rather than
trusting it or dismissing it. A check that fires on correct data is worse than
no check, because the next real failure gets waved through with it.

The **earned-run field was not chosen from the documentation.** The layout
names both an "individual" and a "team" earned-run figure per side and does not
settle which is the per-game total; picking wrong would report thousands of
false mismatches. Both are stored, and `rsse reconcile --explain-er` decided it
by comparing each against the event files' own `data,er` records — the same
approach that settled the replay verdict flag
([05-DATABASE](05-DATABASE.md) §5.1).

The answer is the **individual** field, 16–0, over a 2,011-game sample spread
across the whole date range. 1,995 of those games cannot tell the two apart —
the fields are equal in 99.8% of games — so the 16 that discriminate are the
whole evidence, and they are unanimous. Zero games disagreed with *both*
candidates, which is a second result: it confirms the earned-run offsets as
well as the choice between them.

### 4.4 Reconciliation result

| | |
|---|---|
| games compared | **200,876** |
| scores agree | **200,876 (100.0000%)** |
| scores disagree | **0** |

Every replayed final score matches the independently published one. This is the
first check in the project that compares against numbers not derived from the
event strings, and the state machine passes it exactly.

The residual counts are coverage, not error — but only once broken down. A
single "34,393 games in the logs only" reads as a defect:

| Games in the logs, not the corpus | |
|---|---|
| before the corpus begins (pre-1908) | 29,017 |
| postseason | 1,896 |
| all-star | 95 |
| **no event file exists** | **3,385** |

Only the last line is a coverage gap, and it is a real one: **3,385 Major
League games that Retrosheet's own logs list and the event files do not
cover**, concentrated in 1920–1955 and peaking in the war years — 289 missing
games in 1944 alone, 23% of the season. Below 1960 it is a live limitation on
any era comparison; from 1960 onward it is essentially nil.

The other direction is 2,193 games in the replay and not the logs, every one of
them Negro Leagues: Retrosheet's game logs are Major League only. 216 more have
a log row that was excluded as an unfair test (forfeit or suspended), counted
separately so the number does not imply the logs were silent about them.

**The field offsets are confirmed.** 235,607 rows from 160 files parsed with
zero malformed lines, every field landing where the map says — `gl2000.txt`
line 1 is Cubs 5, Mets 3 at `TOK01`, the 2000 opener in Tokyo — and six of
seven shape checks at 0.000%.

Retrosheet publishes a single combined archive, `gl1871_2025.zip`, so the fetch
is **one request** rather than 155. That matters: this project has already been
rate-limited off the server once (§4).

### 4.5 Earned runs, and the densest check in the project

Earned runs are derived from the play-by-play ([03-STATE](03-STATE.md) §9) and
then checked against **three** published figures, none of which the derivation
reads:

| source | what it adjudicates |
|---|---|
| `(UR)` / `(TUR)` advance flags | every individual run, 1908–2025 |
| `data,er` records | earned runs per pitcher per game |
| game log `er_individual` / `er_team` | both ledgers, per team per game |

The first of these is the find. `(UR)` appears in every season at 11.46% of
scoring advances — the published unearned-run rate for the span — so it is a
**complete per-run adjudication covering the whole corpus**, not a sparse
annotation. That is 1.8 million independent verdicts against the 203,285 game
totals the other two sources offer, and it is why the derivation must not read
it: spending a complete answer key as an input buys a derivation that can
never be checked.

#### Result

```
runs adjudicated     1,796,610   in 203,236 games
  settled by rule    1,509,054
  graded likely        108,165
  9.16 defers          179,391   (9.98%)

runs compared        1,616,079
  agree              1,599,106   (98.9497%)
  differ                16,973
TUR before 1969        1,140     -- notation not yet in use
```

| source | compared | in bound | exact | exact agree |
|---|---|---|---|---|
| `data,er` | 1,167,947 | 99.16% | 1,026,203 | **99.47%** |
| game log individual ER | 402,092 | 98.34% | 276,651 | **99.16%** |
| game log team ER | 402,092 | 98.43% | 276,651 | **99.22%** |

#### The grades are calibrated, which is the result that matters

| certainty | compared | agree |
|---|---|---|
| `derived` | 1,507,904 | **99.660%** |
| `likely` | 108,169 | **89.031%** |

A certainty vocabulary is worth nothing if it does not predict anything. These
do: where the derivation says the rule decided, it is right 99.66% of the
time; where it says one reading is merely indicated, 89.0%. **69% of all
disagreements fall in the `likely` bucket**, which holds 6.7% of the runs.

The deferrals are justified the same way. Each deferred class splits roughly
40/60 against what the scorers actually wrote — never 95/5. A class that came
back lopsided would mean the rule *had* decided it and the deferral was
evasion; none does.

#### The era trend says as much about the source as about the rule

| decade | compared | agree | deferred |
|---|---|---|---|
| 1900s | 13,361 | 95.45% | 20.7% |
| 1910s | 83,267 | 96.66% | 18.6% |
| 1920s | 98,471 | 98.15% | 13.0% |
| 1940s | 89,799 | 98.52% | 11.6% |
| 1960s | 114,926 | 99.18% | 10.6% |
| 1980s | 157,503 | 99.36% | 9.9% |
| 2000s | 213,763 | **99.52%** | 7.5% |
| 2010s | 197,813 | 99.45% | 7.2% |

Monotone, and steepest exactly where Retrosheet's files stop being
transcriptions of scoresheets and become reconstructions from published box
scores. In the early files the unearned-run totals had to be distributed
across individual runs to match a printed pitcher total, and the disagreements
concentrate there. The deferral rate falls for a different reason: fewer
errors are committed.

This is a claim about the source, so it is stated as a correlation and not as
an excuse. The 1900s figure is 95.45%, and some of that is the rule.

#### `(TUR)` did not exist before 1969

Three uses in 1911, then **none at all until 1969**. A derived `TUR` against a
recorded `UR` in a 1920 game is a distinction the source had no way to write
down, and counting it as a disagreement blames the rule for a gap in the
notation. 1,142 runs, reported as their own line and folded into neither
agreement nor disagreement.

This was checked before it was believed: **all 18** such cases in the first
sample were pre-1969, which is what a notation gap looks like and not what a
rule defect looks like.

#### A hypothesis that measured worse

9.16(b) makes a run unearned when the runner "would have been put out by
errorless play". The derivation counts only the outs Retrosheet actually
writes. Extending it to throwing errors on advances — `FC5.1-3(E5/TH)` is the
third baseman throwing at the runner taking third and missing, which is
plainly a chance not accepted — is the obvious next step.

It is wrong. Over 211,000 runs on a twelve-season spread:

| | agreement |
|---|---|
| as shipped | **98.8453%** |
| extended to error-aided advances | 98.8383% |

A narrower form, restricted to errors charged to the fielder covering the
destination base, gained 7 runs in 38,167 on a three-season sample — and lost
on twelve. The three-season gain was noise, and the rule is not in the code.
It is recorded here because the next person to read 9.16(b) will have the same
idea.

#### What this found underneath

The check found a defect in the **state machine**, two layers down:
[03-STATE](03-STATE.md) §2 rule 2 listed `FLE$` — an error on a foul fly — as
putting the batter on first. It does not; it prolongs the plate appearance,
which is the entire reason 9.16(a)(2)(i) exists. Of the **8,563** `FLE` plays
in the corpus, all 8,562 that have a following play are followed by the same
batter, and 6,187 of them had left a phantom runner on first for the rest of the half-inning:
**20,883 plays across 6,183 half-innings** with a runner in the base state who
was still holding a bat.

These figures are from the run *after* the `FLE$` correction below was
derived. Before it they were 98.9486% and 179,395 deferred: the correction
moved **34 verdicts** out of 1,796,610, improving agreement by 19 net
disagreements, and every one of them falls in a half-inning containing an
`FLE`. That the movement is confined there, and small, is the check on the
fix rather than a footnote to it.

No gate here could see it. No out was invented, so out accounting balanced.
The half-innings still ended with three. And a runner no advance in the file
ever names never scores, so score reconciliation stayed at **100.0000%**
throughout. The defect was reachable only from a question nobody had asked
yet — *did this batter's plate appearance end?* — which is the same lesson
this document has now recorded six times, arriving from a new direction.

### 4.6 Replay verdicts, and a prediction that was wrong twice over

`ReplayOverturned` is the one tag that cannot be derived from the event string
alone: whether a call was reversed is only in the linked `com` record. That
makes it checkable against a quantity it is not derived from — the comments
themselves — and the check found two defects that a re-derive had been
expected to fix and did not.

The prediction was that fixing the comment linker would take the tag from
2,320 to **2,370**: +17 reversals that had been landing on no play, and +33
from the two linkers no longer disagreeing. The rebuild produced **2,337**.

The +17 was right. The +33 was wrong *in construction*: relinking a comment
**moves** a verdict from one play to another, and a move does not change a
count. Only a verdict landing where none existed can raise it. An arithmetic
error of that shape survives review easily, because both terms are real
findings and only one of them is an addition.

Checking the tag against the comments is what settled it. 2,427 comments say a
call was reversed; they reach 2,422 distinct plays; 2,337 were tagged. The 85
split three ways, and two of the three were bugs:

| | plays | |
|---|---|---|
| a later *upheld* verdict overwrote the reversal | 33 | bug ([03-STATE](03-STATE.md) §6.7) |
| the verdict landed on an `NP` substitution | 44 | bug ([03-STATE](03-STATE.md) §6.7) |
| real play, but no `MREV`/`UREV` in the event string | 8 | not a bug |

Neither bug was caused by the linker work: none of the 33 involves a
forward-pointing comment. They had been there all along, invisible because
nothing compared the tag to the record it is derived from.

**The standing invariant**, two checks in `rsse verify --derived`. Every play
carrying a linked reversed verdict and a review modifier must carry the tag,
and no play may carry the tag without both. Both drive from `comments` — 5,102
replay rows — rather than from `plays`; the natural phrasing of the first tests
the modifier with a `LIKE` and scans all 17.9 million.
After the fixes that is 2,384 plays, with 38 left over — a linked reversal on a
play whose event string never said it was reviewed, concentrated in 2014–2017
and absent from 2018 on. Those 38 are a gap in Retrosheet's annotation, not in
the derivation, and they are the reason the invariant is stated as *both
conditions* rather than as a count.

### 4.7 A lifted column is a new class of thing to check

`plays.event_location` ([05-DATABASE](05-DATABASE.md) §3.2) is the first column
in the derived layer that holds a *copy* of something the same row already
carries. Every other column is either verbatim from the archive or computed by
a rule; this one is neither, and that is a failure mode nothing here had a
check shaped for.

The failure it invites is not a wrong reading. It is a **shift**: a positional
insert into a 39-column table writes 39 plausible values into 39 columns and
fails nothing, and the lifted column is the one that stops corresponding to its
own row without changing shape. That is the same lesson as the doubled corpus
(§4) one layer down — a check that validates each value in isolation cannot see
it, because each value is still a valid location.

So the two checks compare it to its source rather than to a vocabulary:

- **location not in its own modifier list** — the value must be a suffix of one
  of that play's emitted modifiers, which is what a `hit` modifier emits.
  Honestly weak for the short values (a column wrongly reading `8` on a play
  carrying `/E8` passes) and total for a column that has stopped corresponding
  to its row at all, which is the one it is for.
- **location is not zone-shaped** — a digit, then qualifiers. Straight off the
  grammar, and it catches an empty string, which is a missing location
  masquerading as a present one.

Both are skipped, with a note, on a database derived before the column existed.
That is the same guard the reference, earned-run and replay checks carry: a
check that cannot run should say so rather than pass.

**The shape check was inverted on its first run, and nothing here caught it.**
It was written `event_location GLOB '[!0-9]*'`. SQLite negates a GLOB character
class with `^`, not `!`, so `!` was read as an ordinary member of the class:
the check matched every string beginning with `!` or a digit — which is every
*valid* location — and reported all **4,999,462** located plays as malformed
the first time it met a database that had any.

The bug is a one-character syntax error. What it exposes is a hole in the
discipline of §4, which has always been about the *class of error a gate is
shaped for*: nothing here had ever asked whether a gate fires **at all**. Both
of these had been exercised only against data that passes, so an inverted check
and a working one were indistinguishable. Every check added from here gets a
pair of cases — a row it must catch and a row it must let through — and the
existing location gates now have six of them between them, including the
literal string `!7` that the broken version let by.

A gate that has never been seen to fail is not a gate. It is a query that
returns zero.

## 5. Performance suite

`rsse bench` runs twelve pinned queries against the full corpus, each with a
wall-clock budget. Warm cache — every query is run once to warm SQLite's page
cache and then timed over repeats, and the **median** is reported, since one
run competing with another process should not move a pinned number.

**The plan is pinned alongside the time.** Each benchmark records whether its
plan scans `plays`. A query can sit inside budget today and be one row count
away from scanning 17.9 million rows; recording only the number loses the
reason it was good.

| Benchmark | Measured | Budget | What it exercises |
|---|---|---|---|
| motivating: sql | 144 ms | 500 ms | the compiled query alone, 5 predicates |
| motivating: full | 920 ms | 3,000 ms | the same query with its mandatory reports |
| common tag count | 3,059 ms | 10,000 ms | 1.7M matching plays; the worst realistic case |
| rare tag count | <1 ms | 100 ms | rarity should be cheap |
| rare tag with rows | 303 ms | 1,000 ms | materialising results, not just counting |
| tag + season | 207 ms | 700 ms | tag join plus a `games` filter |
| two tags | 240 ms | 800 ms | two joins on `ix_playtags_tag` |
| force predicate | 156 ms | 500 ms | `runner_advances` on its partial index |
| putout sequence | 4 ms | 100 ms | equality on `ix_credseq_text` |
| contains_sequence | 1,341 ms | 5,000 ms | `LIKE '%64%'`, unindexable by construction |
| context only | 659 ms | 2,000 ms | no tag to drive the plan |
| pitcher lookup | 44 ms | 200 ms | the lineup timeline |

Budgets are roughly **three times** the measured median, floored at 100 ms.
Not the measured value: these timings move with disk contention, and a budget
set at the observed number fails on a busy afternoon and teaches everyone to
ignore it. Three times still catches what these exist for — the regressions
found below were 160x and 3,800x, not 20%.

Ingest and derive budgets are measured in [05-DATABASE](05-DATABASE.md) §7.

### 5.1 What pinning them found

The original budgets in this section — <100 ms for the motivating query,
<250 ms for a rare-tag count — were written before any data existed, like the
<2 GB database goal §7 of 05-DATABASE had to replace. Measuring them was
expected to end in revising them. It did not. **Four predicates were
accidentally quadratic.**

Every sub-table predicate compiled to a correlated `EXISTS`, which SQLite
evaluates once per candidate play, so the index on the inner table could only
ever be probed and never driven. Rewritten as `p.play_id IN (SELECT ...)`,
the subquery runs once and drives:

| | before | after |
|---|---|---|
| `.force_play(at="H")` | 24,975 ms | **156 ms** |
| `.putout_sequence([6,4,3])` | 16,797 ms | **4 ms** |
| `.contains_sequence([6,4])` | 21,650 ms | **1,341 ms** |
| `.pitcher(...)` | 45,826 ms | **44 ms** |

`.pitcher()` needed more than a rewrite, because it correlates on game and team
rather than only on play. The form that reads straight from the rule — "no
later lineup entry has taken effect yet" — walks the lineup twice for every
candidate play. A window function computes each holder's *interval* once per
game and the predicate becomes a range test. That the partition invariant of
§4.2 still holds is what says the rewrite is semantically identical rather
than merely faster.

The mandatory reports were also costing more than the queries they describe:
`.run()` on the motivating query took 4,615 ms against a 244 ms `.count()`,
because `ExcludedCounts` re-ran the query with filters relaxed and
`ForceReport` ran five more aggregates. Collapsing the certainty split into one
`GROUP BY`, reusing the grouped pass's own total instead of re-counting, and
fetching coverage in one query instead of 274 brought it to **920 ms** with
byte-identical output.

### 5.2 A benchmark catches what a unit test is not shaped for

The `EXISTS` → `IN` change was made once and did nothing. The wrapper changed
and the correlation clause stayed *inside* the subquery:

```sql
p.play_id IN (SELECT a.play_id FROM runner_advances a
              WHERE a.play_id = p.play_id AND ...)   -- still correlated
```

The query looked rewritten and ran at exactly the old speed. The regression
test written to guard the change asserted `"EXISTS" not in sql`, which passed:
it was shaped for *did the wrapper change*, and the defect was *is it still
correlated*.

The benchmark caught it on the next run, because wall-clock time is the one
property a query cannot satisfy by looking right. That is the argument for this
suite beyond regression-catching, and the test now asserts no `= p.play_id`
appears inside any `IN (SELECT ...)`.

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
