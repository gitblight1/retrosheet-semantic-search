# 04 — Semantic Ontology

Tags are the query surface. The original specs listed tag names; this document
gives each one a **derivation rule** over the parse tree
([02-GRAMMAR](02-GRAMMAR.md)) and game state ([03-STATE](03-STATE.md)).

A tag without a derivation rule is not part of the ontology. That is the whole
point of this layer: `.dropped_third()` cannot be implemented from a name.

**Status: built.** 84 tags, in
[rsse/semantic/ontology.py](../rsse/semantic/ontology.py) (the rules) and
[derive.py](../rsse/semantic/derive.py) (implication, confidence, curated
tags). The rule requiring a rule is enforced structurally: a tag exists only
as a registered `TagDef`, and a `TagDef` cannot be constructed without a
callable, so a name alone cannot be registered. `rsse tags` derives the whole
corpus and reports a census, which is the ontology's corpus-wide gate; the
per-tag positive **and negative** cases required by
[07-TESTING](07-TESTING.md) §1 are in
[tests/test_ontology.py](../tests/test_ontology.py) and a tag added without
both fails the suite.

## 1. Rules for tags

1. **Total and deterministic.** Same inputs, same tags, always.
2. **Derived, never stored upstream.** Tags are a pure function of the parsed
   play plus state. Dropping and rebuilding `play_tags` changes nothing.
3. **Versioned.** Each tag carries an `ontology_version` and a hash of its
   derivation. Changing a rule requires a version bump, and results record the
   version they were computed under.
4. **Confidence.** A tag is `certain` or `uncertain`. `uncertain` arises only
   from a stated ambiguity — `force_certainty = 'ambiguous'`
   ([03-STATE](03-STATE.md) §4.4), a `#` annotation, or a `99` unknown play.
   Default queries return `certain` only.

   A `#` or a `99` makes every tag *about that play* uncertain: Retrosheet is
   saying the record itself is questionable, which is not a statement about one
   reading of it.

   It does **not** reach the context tags of §7 that describe the state
   entering the play — `BasesLoaded`, `TwoOuts`, `RunnerOnThird`,
   `ScoringPosition`, `ExtraInnings`, `LateAndClose`, `FinalPlay`. Earlier
   plays established that state; a questionable record of what the batter then
   did says nothing about it. `InningEnding`, `GoAheadRun` and `WalkOff` *are*
   affected, because they depend on this play's own outcome. Applying
   play-wide uncertainty to all of them marked ~35,000 `BasesEmpty` tags
   uncertain in a 12-season sample, every one of them for no reason, and
   `uncertain` tags are excluded from default results
   ([06-QUERY](06-QUERY.md) §4).

   Tag confidence is deliberately **binary**, so the four-valued
   `runner_advances.force_certainty` ([05-DATABASE](05-DATABASE.md) §3) does
   not map onto it one-for-one: `derived` and `likely` are both `certain` here,
   and only `ambiguous` is `uncertain`. `likely` is how the pre-1970s corpus
   records an ordinary ground out ([03-STATE](03-STATE.md) §4.2 rule 5), so
   calling it `uncertain` would drop about half the force outs at first out of
   default results. A query that needs the strict population reads the column.

   `force_certainty` is narrower still, and applies only to the force family
   and `TagOut`: a tag there is uncertain only when it actually
   *turns* on the one inference in the chain ([03-STATE](03-STATE.md) §4.2
   rule 5). A runner with an empty base behind them is unforced however the
   batter was retired, so that `TagOut` is `certain`.
5. **Additive.** A play carries every tag whose rule fires. Tags are not
   mutually exclusive.

## 2. Batting

| Tag | Rule |
|---|---|
| `Single` / `Double` / `Triple` | basic event `S` / `D` / `T` |
| `GroundRuleDouble` | `DGR`; also implies `Double` |
| `HomeRun` | `H` or `HR` |
| `InsideTheParkHomeRun` | `H`/`HR` with a fielder given, or `/IPHR` |
| `Walk` | `W` |
| `IntentionalWalk` | `I` or `IW`; also implies `Walk` |
| `HitByPitch` | `HP` |
| `Strikeout` | basic event `K` |
| `ReachedOnError` | `E$` or `$$E$` as the basic event |
| `FieldersChoice` | `FC$` |
| `SacrificeFly` | `/SF` |
| `SacrificeHit` | `/SH` |
| `GroundOut` / `FlyOut` / `LineOut` / `PopOut` | out on ball with `/G` / `/F` / `/L` / `/P` |
| `Bunt` | trajectory `BG`, `BP`, `BL`, or plain `B` |
| `InfieldFly` | `/IF` |

`Bunt` includes the undocumented plain `/B`
([02-GRAMMAR](02-GRAMMAR.md) §4.1), which was omitted from the first draft of
this table. It generalises `BG`/`BP`/`BL` and is the *only* bunt marker across
much of the pre-1961 corpus, so excluding it would have made `Bunt` silently
under-fire on the older half of the data — the same era-coverage failure the
grammar validation ran into.

## 3. Strikeout family

The most important group, and the one the earlier specs left underdetermined.

### 3.1 Uncaught third strike

Retrosheet has **no** dropped-third-strike token. Four encodings exist, and they
do not mean the same thing.

`UncaughtThirdStrike` — the ball was not cleanly caught. Fires when basic event
is `K` and any of:

| # | Condition | Example |
|---|---|---|
| a | fielder digits follow the `K` | `K23`, `K13` |
| b | compounded with `WP`, `PB`, or `E$` | `K+WP`, `K+PB`, `K+E2` |
| c | the batter-runner is live per [03-STATE](03-STATE.md) §2 | `K.B-1`, `K.3XH(21)` |
| d | an advance carries a `(WP)` or `(PB)` parameter | `K.1-2(WP)` |

`BatterReachedOnK` — `UncaughtThirdStrike` **and** the batter-runner is live per
[03-STATE](03-STATE.md) §2.

The distinction matters and is the reason two tags exist rather than one:

- `K23` — uncaught, batter thrown out at first. `UncaughtThirdStrike` fires,
  `BatterReachedOnK` does not.
- `K+WP.2-3` — uncaught, wild pitch, runner advances, batter still out (first
  base was occupied with fewer than two outs). `UncaughtThirdStrike` only.
- `K.B-1` — uncaught, batter safe. Both fire.
- `K.1X2(26)` — clean third strike; the `X` belongs to a runner caught stealing.
  **Neither** fires. A rule keyed on "`K` with an `X` advance" is wrong.

`DroppedThirdStrike` is retained as a deprecated alias of
`UncaughtThirdStrike`; the builder method `.dropped_third()` maps to it. New
code uses the precise names.

**Trigger (c) is keyed on the derived state, not on the written form.** The
first draft of this table read "an explicit `B-%` advance exists", and that is
wrong for the one play this project exists to find. `K.3XH(21)` — the bare form
of the motivating play — gives the batter no advance at all; that he reached is
derived from the out count ([03-STATE](03-STATE.md) §4.5 step 1). Keyed on the
written form, the three encodings of that single play carry *different* tag
sets, and a researcher asking the question would find one and miss two. The
gold corpus's `equivalents` list ([07-TESTING](07-TESTING.md) §2.1) exists
precisely to catch this, and it did.

This is **not** the widening §3.1.1 forbids. A live batter-runner on a
strikeout is the rulebook's own definition of an uncaught third strike — on a
caught third strike the batter is out, with no exception — so the tag still
asserts only what the record supports. §3.1.1 forbids inferring the *miscue*
from the shape of the fielding; this infers nothing, it reads a state the
state machine already determined by arithmetic.

### 3.1.1 When an uncaught third strike is not recoverable

The four triggers above are exhaustive over what Retrosheet records — which is
not the same as what happened. If the batter is retired on strikes and the
miscue draws no wild pitch, passed ball, or error, **the event string contains
no trace of the ball having gotten away**, and no rule can recover it.

This is not hypothetical. The real record for the 2000 tag-out case
([08-WORKED-EXAMPLE](08-WORKED-EXAMPLE.md)) is:

```
play,3,1,sancr001,12,CBFS,K/NDP.3XH(21)
```

The catcher threw to the pitcher covering home, which all but requires that the
ball got away from him — yet the string is indistinguishable from a clean
strikeout with a runner retired at the plate. `UncaughtThirdStrike` does **not**
fire, and must not: the tag asserts something the data does not support.

Two consequences, both binding:

1. **Do not widen the triggers to close this gap.** A rule inferring an uncaught
   third strike from a catcher-assisted putout away from the plate would be a
   heuristic, violating principle 2.2, and would misfire on genuine steals of
   home and appeal plays.
2. **Queries for the broad class must key on `Strikeout`, not
   `UncaughtThirdStrike`.** Any search meant to reproduce an externally counted
   population — the Elias figure, for instance — has to use the weaker predicate,
   because the stronger one silently drops exactly these plays.

Where the distinction genuinely matters and the data cannot supply it, the
curated tag set (§8) is the correct escape hatch: a human records what the
event string cannot.

### 3.2 Related

| Tag | Rule |
|---|---|
| `StrikeoutDoublePlay` | `Strikeout` with `/DP` or two outs on the play — **unless `/NDP` is present** |
| `CalledThirdStrike` | `/C` modifier |
| `StrikeoutThrowOut` | `Strikeout` and a runner retired in the advance section |

The `/NDP` exception applies here for the same reason it applies to
`DoublePlay` (§5), and was missing from the first draft of this table. `/NDP`
states that no double play was credited, and it appears precisely *because*
two outs were recorded — so a rule keyed on the out count needs the exception
wherever it counts outs, not only in one of the two places. `K/NDP.3XH(21)`
records two outs and is neither a double play nor a strikeout double play.

## 4. Base running

| Tag | Rule |
|---|---|
| `StolenBase` | basic event `SB%` |
| `DoubleSteal` | two or more `SB` in one play |
| `CaughtStealing` | `CS%` **with the out not negated** by an `E` in the credit sequence |
| `CaughtStealingSafeOnError` | `CS%` whose credit sequence contains `E` (e.g. `CS2(2E4)`) |
| `Pickoff` | `PO%` with the out standing |
| `PickoffError` | `PO%` negated by an error (e.g. `PO1(E3)`) |
| `PickoffCaughtStealing` | `POCS%` |
| `DefensiveIndifference` | `DI` |
| `WildPitch` | `WP` basic event, or a `(WP)` advance parameter |
| `PassedBall` | `PB` basic event, or a `(PB)` advance parameter |
| `Balk` | `BK` |
| `OtherAdvance` | `OA` |
| `RunnerPassedRunner` | `/PASS` |
| `RunnerHitByBattedBall` | `/BR` |

**One `StolenBase` per play, not per steal.** The first draft said "one tag per
steal in a `SB3;SB2` list", which `play_tags` cannot represent: its primary key
is `(play_id, tag_id)` ([05-DATABASE](05-DATABASE.md) §4), so a play either
carries a tag or does not. `SB3;SB2` is one `StolenBase` plus `DoubleSteal`,
and *which* bases were taken is a `runner_advances` question — the right place
for it, since that table is per movement by construction.

## 5. Outs and force plays

| Tag | Rule |
|---|---|
| `ForceOut` | any out whose runner is forced per [03-STATE](03-STATE.md) §4 |
| `ForceOutAtHome` | `ForceOut` with destination `H` |
| `ForceOutAtSecond` / `AtThird` / `AtFirst` | by destination |
| `TagOut` | an out that is not a `ForceOut` and not a strikeout or caught fly |
| `AppealOut` | `/AP` |
| `DoublePlay` | two outs on the play, or `/DP` `/GDP` `/LDP` `/FDP` `/BGDP` `/BPDP` — **unless `/NDP` is present**, which suppresses it |
| `TriplePlay` | three outs on the play, or `/TP` `/GTP` `/LTP`; also implies `DoublePlay` |
| `UnassistedOut` | a putout with an empty assist list |
| `Rundown` | three or more credit atoms with at least one fielder repeated |
| `RelayThrow` | `/R$` modifier |

`ForceOut` deliberately does **not** key on the `/FO` modifier. `/FO` marks only
batted-ball force outs; a rule keyed on it misses `K.3XH(21)` entirely, which is
the play that motivated this project.

A `TriplePlay` is also a `DoublePlay`, since three outs satisfies "two outs on
the play". That follows from the rule rather than being an exception to it, and
is stated as an implication so it is visible instead of emergent from an
inequality. A query wanting double plays and not triple plays writes
`.double_play().none_of(triple_play=True)`.

`UnassistedOut` means literally what it says — a putout with an empty assist
list — so it fires on every caught fly ball as well as on the unassisted double
play. That is correct and is not the same as useful: the interesting queries
pair it with `DoublePlay` or `TriplePlay`. The same is true of
`ForceOutAtFirst`, which fires on most ground outs, because the batter-runner
is always forced at first (§4.1). Neither is selective on its own, and neither
should be made selective by narrowing its rule — selectivity is the query's
job, and a tag that quietly excluded the common case would make
`.force_play()` disagree with §4.1.

Recording the batter's own out at first as a runner advance is what lets
`ForceOutAtFirst` and `runner_advances` see the commonest force play in
baseball; before this layer was built, the state machine emitted no advance row
for it at all ([03-STATE](03-STATE.md) §3).

## 6. Special

| Tag | Rule |
|---|---|
| `CatcherInterference` | `C` with `/E2` |
| `PitcherInterference` | `C` with `/E1` |
| `FirstBaseInterference` | `C` with `/E3` |
| `BatterInterference` | `/BINT` |
| `RunnerInterference` | `/RINT` |
| `UmpireInterference` | `/UINT` |
| `FanInterference` | `/FINT` |
| `Obstruction` | `/OBS` |
| `BattingOutOfTurn` | `/BOOT`, or a preceding `ladj` |
| `CourtesyRunner` / `CourtesyBatter` / `CourtesyFielder` | `/COUR` / `/COUB` / `/COUF` |
| `FoulFlyError` | `FLE$` |
| `ErrorOnThrow` | credit sequence containing `E` together with `/TH` |
| `UnknownPlay` | `99` — always `uncertain` |
| `ReplayReviewed` | `/UREV` or `/MREV` |
| `ReplayOverturned` | `ReplayReviewed` and the linked `com` says reversed `Y` |
| `PlacedRunner` | a runner originating from a `radj` |

`HiddenBallTrick` has **no** Retrosheet encoding. It is recorded in prose `com`
records only. It is therefore not a derived tag; it belongs to the curated tag
set (§8), and a query for it searches curated annotations, not the event
grammar. The original spec listed it beside derivable tags, which is not
implementable.

## 7. Context

Copied from the persisted state fields in [03-STATE](03-STATE.md) §7 so that
context is a tag join like any other predicate:

`BasesLoaded`, `BasesEmpty`, `RunnerOnThird`, `ScoringPosition`, `NoOuts`,
`OneOut`, `TwoOuts`, `InningEnding`, `GoAheadRun`, `WalkOff`, `FinalPlay`,
`ExtraInnings`, `LateAndClose`.

`WalkOff` composes: a walk-off sacrifice fly is `WalkOff` + `SacrificeFly`, not
a separate tag. Composition is why tags are additive.

Two of these needed definitions the earlier specs did not give:

| Tag | Rule |
|---|---|
| `ExtraInnings` | `inning > games.scheduled_innings` (9 unless stated) |
| `LateAndClose` | `inning >= 7` **and** the batting team is tied, ahead by one, or trailing by no more than `runners_on + 1` — that is, the tying run is on base or at the plate |

`LateAndClose` was a name with no rule, and "close" has no single conventional
meaning, so the rule above **is** the definition rather than an approximation
of one. It is stated here so a result can be audited instead of guessed at; if
it is the wrong definition, that is a version bump, not a mystery.

`WalkOff` is stamped in a second pass, since it needs to know the game ended.
Once the final play is known a walk-off is just a go-ahead run on it: the team
batting on the last play is by definition the team that bats last, so no test
on the half is made — and none may be, because the home team bats in the *top*
half of an `htbf` game ([03-STATE](03-STATE.md) §6.5).

## 8. Curated tags

Some plays are interesting for reasons no grammar can see — a hidden ball trick,
the pine tar game, an unassisted triple play whose significance is historical.

Curated tags live in a version-controlled file keyed by `(game_id, play_seq)` —
[rsse/semantic/curated_tags.json](../rsse/semantic/curated_tags.json) — are
loaded into the same `play_tags` table with `source = 'curated'`, and are
distinguishable from derived tags in every result. They are never produced by
the deriver and are never overwritten by a re-derive.

Every name in that file **must already be a registered tag**, and the loader
raises if it is not. §8 quarantines human judgement about *which plays
qualify*, not about what the vocabulary is: without the check, a typo would
invent a tag silently and no query would ever match it. Each entry also carries
a citation, because a curated tag is an assertion someone has to be able to
check.

A tag that no rule can derive is registered with `curated_only`, so it has a
name to attach to and a test asserting the deriver never produces it.
`HiddenBallTrick` is the one such tag today.

`rsse.semantic.derive.tags_for_play()` is what a `play_tags` load writes, and
it exists so this section's two guarantees are executable rather than
described. A curated tag survives a re-derive because derivation is a pure
function of the play — re-running it can only reproduce the derived set, and
the curated entries are merged in afterwards from the file.

**On a collision, the curated row wins.** `play_tags` is keyed
`(play_id, tag_id)` and holds one row, so a name that is both derived and
curated must resolve one way. It resolves to the curated one, because the
collision is not a mistake: §3.1.1 names it as the intended escape hatch. The
2000 record is an uncaught third strike the event string cannot show, so a
curator may assert `UncaughtThirdStrike` there. Letting the derived row win
would silently discard exactly the judgement this section exists to preserve.

**The shipped file is empty**, and that is a content gap rather than a
mechanism gap. Every entry must carry a citation, and no curated play has been
researched to that standard yet — `hidden_ball_trick`
([07-TESTING](07-TESTING.md) §2.3) is the obvious first one and needs a game id
and a `com` record to cite. The path itself is exercised by tests.

This keeps principle 2.2 intact: the parser stays free of heuristics, and human
judgement is quarantined in a place where it can be reviewed.

## 9. Vocabulary reconciliation

The original discussion listed tag names before any of them had rules. Six were
renamed once they did, and two turned out not to be tags at all. The mapping is
recorded because the discussion is the document of record and its vocabulary
has to remain traceable:

| Original name | Now | Why |
|---|---|---|
| `Steal` | `StolenBase` | matches the `SB` event and the `.stolen_base()` predicate |
| `AdvanceOnWildPitch` | `WildPitch` | the tag marks the *pitch*, which is what Retrosheet records; whether anyone advanced is a `runner_advances` question, and `WP` occurs with no advance at all |
| `AdvanceOnPassedBall` | `PassedBall` | as above |
| `Unassisted` | `UnassistedOut` | it qualifies an out, and the bare adjective read as a property of the play |
| `ForcePlay` | `ForceOut` | a force *play* is the situation; the tag marks an out. The query method stays `.force_play()`, which reads better in a predicate chain |
| `DroppedThirdStrike` | `UncaughtThirdStrike` | "dropped" names one way the ball can get away; `K23` and `K+WP` are not drops. Kept as a live deprecated alias (§3.1), unlike the five above, because [06-QUERY](06-QUERY.md) commits to `.dropped_third()` |

Only `DroppedThirdStrike` is retained as an alias in the registry. The other
five are renames, not aliases: no code uses the old names, and an alias costs a
duplicate `play_tags` row on every matching play — for `ForcePlay` that would
be millions of rows to support a name that has never been called.

**`PitcherPutout` and `CatcherAssist` are not tags.** The discussion listed
them beside `ForcePlay` and `InningEnding` as stage-4 output. They are fielding
*credits* ([03-STATE](03-STATE.md) §5), stored per fielder per position in the
sequence, and that is strictly more queryable than a tag: `.putout_by(1,
assist_by=[2])` and `.putout_sequence([2, 1])` both fall out of it, where a
`PitcherPutout` tag could express neither. A tag per position per credit type
would also be 18 tags carrying no more information than one table.

**`Interference` did not survive as a single tag.** Retrosheet distinguishes
who interfered — `C/E2` is the catcher, `C/E1` the pitcher, `/BINT` the batter,
`/RINT` a runner, `/UINT` the umpire, `/FINT` a fan — and collapsing those
loses the only thing that makes an interference call interesting. Seven tags,
not one, and a query wanting the union writes `.any_of(...)`.

### 9.1 Extensibility

"Custom ontologies" and "user-defined tags" were listed as future extensions,
and the registry is shaped for them: a tag is a `TagDef` in a dict, so another
module can register its own without touching this one, and `ontology_hash()`
covers whatever is registered at derive time — so a result computed under an
extended ontology is distinguishable from one that was not. The curated set
(§8) is the other half: a user-defined tag that no rule can compute already has
a home.

Two rules bind any extension, and both are enforced rather than requested: a
tag needs a callable to exist at all, and it needs a positive **and** a
negative test case or the suite fails ([07-TESTING](07-TESTING.md) §1).
