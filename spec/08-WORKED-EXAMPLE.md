# 08 — Worked Example: the 2-1 Force Out at Home

This document is the acceptance test for the spec set. It traces the play that
motivated RSSE from raw bytes to a returned row, citing the clause that governs
each step. If a step here needs a rule that no spec states, the specs are still
insufficient and this file is where that shows up.

## The play

Comerica Park, 23 July 2026, Royals at Tigers, top of the first. Troy Melton
walks two around a single to load the bases with one out, strikes out Michael
Massey for the second out, then gets a swinging third strike on Isaac Collins —
but the pitch bounces. Catcher Dillon Dingler, back turned, does not know what
has happened. Melton positions himself at home plate. Dingler retrieves the ball
and throws to Melton covering, forcing the runner from third. Inning over.

Elias counted three 2-1 putouts on an uncaught third strike since 2021. Read
that number carefully — see §"The question is narrower than the Elias count"
below. It is not the number this query should return.

Three things make this the right test case. It is a **force play Retrosheet
never marks**; its only searchable trace is in the advance section rather than
the basic play; and it sits inside a broader class of superficially identical
plays from which the force derivation is the only thing that separates it. All
three were undefined in the original specs.

## Why there is a force at all

The force is not implied by the putout sequence, the uncaught third strike, or
the out being recorded at home. It requires the batter-runner to be live *and*
every base behind the retired runner to be occupied
([03-STATE](03-STATE.md) §4.1). Both conditions have to be checked, and each
fails in a real, observed way:

- **Batter not live.** With fewer than two outs and first base occupied, an
  uncaught third strike leaves the batter automatically out. No batter-runner,
  so no force at any base — a runner retired at home on such a play was tagged,
  not forced.
- **Bases behind not occupied.** With a runner on second and first base empty,
  the batter *is* live (first base unoccupied entitles him to run whatever the
  out count), but the runner on second is not forced, because nobody is forced
  into second behind him. A runner going home from second on that play is
  tagged.

Only bases loaded with two outs satisfies both. That conjunction is why the
play is rare, and it is the reason `.outs()` had to be disambiguated into
before/after ([06-QUERY](06-QUERY.md) §2): here the two differ, and the
before-value is load-bearing.

## Layer 0 — raw record

```
play,1,0,colli001,12,CBBFS,K.3XH(21)
```

Six fields after `play` ([01-CORPUS](01-CORPUS.md) §3.2): inning 1, team 0
(visitors; Tigers are home, so the Royals bat in the top half), batter, count
1-2, pitch sequence, event.

Written verbatim to `raw_records` with its file and line number
([05-DATABASE](05-DATABASE.md) §1). Every later table is rebuildable from this
row and nothing else.

## Layer 1 — grammar

`K.3XH(21)` decomposes per [02-GRAMMAR](02-GRAMMAR.md) §2:

```
event
├── basic_section
│   └── basic_group → basic_event → strikeout "K"      (no fielder_seq)
├── modifiers: none
└── advance_section
    └── advance
        ├── origin      "3"
        ├── operator    "X"
        ├── destination "H"
        └── adv_param   fielding_param → credit_seq "21"
```

No baseball knowledge is applied here. The parser does not yet know the batter
reached, that the out was a force, or that the inning ended.

Credit assignment ([03-STATE](03-STATE.md) §5): last atom is the putout, so
fielder 1 (pitcher) putout, fielder 2 (catcher) assist. The sequence is also
flattened to `seq_text = '21'` in `credit_sequences`
([05-DATABASE](05-DATABASE.md) §3.1) — this is what makes the query an index
seek.

Round-trip: `emit(parse("K.3XH(21)")) == "K.3XH(21)"`
([02-GRAMMAR](02-GRAMMAR.md) §6).

## Layer 2 — state

Entering the play: `outs_before = 2`, `bases_before = '111'`.

**Is the batter-runner live?** [03-STATE](03-STATE.md) §2 — the `K` has no
fielder digits, no compound event, and no explicit `B-` advance, which by the
table would mean out on strikes. The out-count check overrides: strikeout (1) +
out at home (1) added to `outs_before` (2) gives 4. Impossible. Therefore the
batter is live and the third out is the force.

That inference is deterministic, not a heuristic — it follows from the
arithmetic invariant `outs_before + outs_recorded <= 3`
([03-STATE](03-STATE.md) §3.1). If the file instead carries the explicit
`K.B-1;3XH(21)`, rule 1 of §2 supplies the same answer directly, and the two
encodings converge on identical state. That convergence is asserted by the
`equivalents` list in the gold file ([07-TESTING](07-TESTING.md) §2.1).

**Rulebook cross-check** ([03-STATE](03-STATE.md) §4.3): two outs, so the batter
was entitled to run. Consistent — no flag.

**Force derivation** ([03-STATE](03-STATE.md) §4.1): batter-runner live, first
and second occupied, therefore the runner on third is forced at home. One out in
the advance section, so ordering is unambiguous and
`force_certainty = 'derived'` (§4.4).

Leaving the play: `outs_after = 3`, `is_inning_ending = 1`, `runs_on_play = 0`
— no run scores on a force for the third out.

## Layer 3 — tags

Per [04-ONTOLOGY](04-ONTOLOGY.md):

| Tag | Rule |
|---|---|
| `Strikeout` | §2, basic event `K` |
| `UncaughtThirdStrike` | §3.1 trigger (c) — batter-runner live on a `K` |
| `BatterReachedOnK` | §3.1 — uncaught **and** batter live |
| `ForceOut` | §5 — derived, not from `/FO`, which is absent |
| `ForceOutAtHome` | §5 — destination `H` |
| `BasesLoaded`, `TwoOuts`, `InningEnding` | §7 context |

Not tagged, and each is a real trap: `TagOut` (§5 excludes force outs),
`CaughtStealing` (`CS` never appeared), `DoublePlay` (one out recorded).
These are the `forbidden_tags` in the gold file.

## Layer 4 — query

```python
Search() \
    .bases_loaded() \
    .outs(2) \
    .uncaught_third_strike() \
    .force_play(at="H") \
    .putout_sequence([2, 1]) \
    .run()
```

Compiles per [06-QUERY](06-QUERY.md) §1 to a tag-driven join, column filters on
`plays`, and `EXISTS` subqueries against `runner_advances` and
`credit_sequences`. Every clause resolves to an index; no scan of `plays`.

The result is a `ResultSet` ([06-QUERY](06-QUERY.md) §6) ordered
chronologically, carrying `coverage`, `excluded`, `corpus_version`, and
`ontology_version`.

**If it returns none**, the answer is *not* "never". It is "not in the N games
covered", with the coverage report stating exactly which seasons and leagues
were searched and how many plays were excluded as unparsed or uncertain
([01-CORPUS](01-CORPUS.md) §5.2). Retrosheet's own coverage thins going back in
time, and the honest form of the answer says so.

## The question is narrower than the Elias count

Elias's three-since-2021 figure counts **2-1 putouts on an uncaught third
strike**, and it *includes* the Tigers play. The other two were **tag** outs,
not forces: the bases were not loaded and the lead runner went for home anyway.
Matching the query above against the Elias figure would therefore be wrong, and
an implementer who treats a three-row result as validation has a passing test
over a broken force derivation.

The actual question is the narrower one: was the *force* version unique? That
is a different query, and the difference is precisely the derivation in
[03-STATE](03-STATE.md) §4.

This gives the sharpest available test of that derivation — two queries over the
same plays, where only the force predicate differs:

```python
base = Search().strikeout().putout_sequence([2, 1]) \
               .out_at("H") \
               .seasons(2021, 2026)

broad  = base.run()                          # expect 3 — reconciles with Elias
narrow = base.force_play(at="H").run()       # expect 1 — the Tigers play alone
```

**Note the predicate is `.strikeout()`, not `.uncaught_third_strike()`.** That
is not a simplification. Retrosheet does not record the uncaught third strike
when the batter is retired on strikes and no wild pitch, passed ball, or error
is charged, so the stronger predicate silently drops exactly the tag-out cases
this test depends on ([04-ONTOLOGY](04-ONTOLOGY.md) §3.1.1). Building the broad
query the intuitive way returns one row, matches nothing, and looks like the
force derivation is fine.

`broad` minus `narrow` should be exactly the tag-out cases, each with
`is_force = 0` on the advance. Three properties must hold, and each catches a
distinct class of bug:

| Assertion | Catches |
|---|---|
| `broad.total >= narrow.total` | force predicate not actually filtering |
| every row in `broad - narrow` carries `TagOut`, not `ForceOut` | a derivation that fires on the putout sequence or the uncaught third strike instead of on base state |
| the runner-from-second case has `bases_before` with 1 empty and `is_force = 0` | a force chain that ignores whether the bases *behind* the runner are occupied |

A derivation that keys on "2-1 putout at home following a `K`" passes the broad
query and fails all three. That is the failure mode this pair is designed to
expose, and it is exactly what the original spec's `.force_play()` — a method
name with no rule behind it — would have shipped.

Note the asymmetry in the API this exposes: the broad query needs a way to ask
for "an out at home, forced or not". `.force_play(at="H")` cannot express it,
so [06-QUERY](06-QUERY.md) §3 carries `.out_at(base)` for the unqualified form
and `include_tag_outs=` on `.force_play()`. Without one of those, the
discriminating test cannot be written at all.

## What this example proves about the specs

The play is reachable at four different levels of the stack, and only one of
them works:

1. **Event-string matching** — fails. `K.3XH(21)`, `K.B-1;3XH(21)`, and
   `K+WP.3XH(21);B-1` all describe it, and a regex for one misses the others.
2. **The `/FO` modifier** — fails. It is not present on this play, and never is
   on a strikeout force.
3. **The `DroppedThirdStrike` tag alone** — fails. It does not distinguish
   `K23`, where the batter was retired, from this play, where he reached.
4. **Matching the shape of the play** — `K`, an out at home, sequence 2-1 —
   fails, and fails *quietly*. It returns the whole Elias class, including the
   tag outs, so it looks like it works and even reconciles against a published
   figure. It is wrong about the only thing being asked.
5. **Derived state plus derived tags** — works, because force status is computed
   from base occupancy, out count, and batter-runner liveness rather than read
   off the string.

Options 1–4 are what the original specs, taken literally, would have produced.
That is the concrete sense in which they were insufficient — and option 4 is the
worst of them, because a spec with no derivation rule behind `.force_play()`
gives an implementer nothing to notice they are wrong with.
