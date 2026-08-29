# 04 — Semantic Ontology

Tags are the query surface. The original specs listed tag names; this document
gives each one a **derivation rule** over the parse tree
([02-GRAMMAR](02-GRAMMAR.md)) and game state ([03-STATE](03-STATE.md)).

A tag without a derivation rule is not part of the ontology. That is the whole
point of this layer: `.dropped_third()` cannot be implemented from a name.

## 1. Rules for tags

1. **Total and deterministic.** Same inputs, same tags, always.
2. **Derived, never stored upstream.** Tags are a pure function of the parsed
   play plus state. Dropping and rebuilding `play_tags` changes nothing.
3. **Versioned.** Each tag carries an `ontology_version` and a hash of its
   derivation. Changing a rule requires a version bump, and results record the
   version they were computed under.
4. **Confidence.** A tag is `certain` or `uncertain`. `uncertain` arises only
   from a stated ambiguity — `force_certainty = 'ambiguous'`
   ([03-STATE](03-STATE.md) §4.3), a `#` annotation, or a `99` unknown play.
   Default queries return `certain` only.
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
| `Bunt` | trajectory `BG`, `BP`, or `BL` |
| `InfieldFly` | `/IF` |

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
| c | an explicit `B-%` advance exists | `K.B-1` |
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
| `StrikeoutDoublePlay` | `Strikeout` with `/DP` or two outs on the play |
| `CalledThirdStrike` | `/C` modifier |
| `StrikeoutThrowOut` | `Strikeout` and a runner retired in the advance section |

## 4. Base running

| Tag | Rule |
|---|---|
| `StolenBase` | basic event `SB%`; one tag per steal in a `SB3;SB2` list |
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

## 5. Outs and force plays

| Tag | Rule |
|---|---|
| `ForceOut` | any out whose runner is forced per [03-STATE](03-STATE.md) §4 |
| `ForceOutAtHome` | `ForceOut` with destination `H` |
| `ForceOutAtSecond` / `AtThird` / `AtFirst` | by destination |
| `TagOut` | an out that is not a `ForceOut` and not a strikeout or caught fly |
| `AppealOut` | `/AP` |
| `DoublePlay` | two outs on the play, or `/DP` `/GDP` `/LDP` `/FDP` `/BGDP` `/BPDP` — **unless `/NDP` is present**, which suppresses it |
| `TriplePlay` | three outs on the play, or `/TP` `/GTP` `/LTP` |
| `UnassistedOut` | a putout with an empty assist list |
| `Rundown` | three or more credit atoms with at least one fielder repeated |
| `RelayThrow` | `/R$` modifier |

`ForceOut` deliberately does **not** key on the `/FO` modifier. `/FO` marks only
batted-ball force outs; a rule keyed on it misses `K.3XH(21)` entirely, which is
the play that motivated this project.

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

## 8. Curated tags

Some plays are interesting for reasons no grammar can see — a hidden ball trick,
the pine tar game, an unassisted triple play whose significance is historical.

Curated tags live in a version-controlled file keyed by `(game_id, play_seq)`,
are loaded into the same `play_tags` table with `source = 'curated'`, and are
distinguishable from derived tags in every result. They are never produced by
the deriver and are never overwritten by a re-derive.

This keeps principle 2.2 intact: the parser stays free of heuristics, and human
judgement is quarantined in a place where it can be reviewed.
