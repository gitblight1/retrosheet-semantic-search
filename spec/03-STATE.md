# 03 — Game State Reconstruction

The syntax tree from [02-GRAMMAR](02-GRAMMAR.md) does not say who was on base,
how many were out, or whether an out was a force. All of that must be
reconstructed by replaying each half-inning. This layer is where the motivating
query is actually answered, and it is entirely absent from the original specs.

Rule: **deterministic or flagged.** Every value below is either derived by a
stated rule or marked uncertain. Nothing is guessed.

## 1. State vector

Carried across plays within a half-inning:

```
outs           0..2 entering the play
bases          {1: runner|None, 2: runner|None, 3: runner|None}
runner         (player_id, responsible_pitcher_id, is_earned_eligible)
score          (home, away)          — carried across the whole game
lineup         per team: batting order → player, current fielding positions
due_up         index into the batting order
```

`bases_before` / `outs_before` are the vector entering the play;
`bases_after` / `outs_after` the vector leaving it. Both are persisted per play
so queries never need to replay.

Reset `outs` to 0 and `bases` to empty at each half-inning boundary, except for
the `radj` case (§6.4).

## 2. Batter-runner destination

Determining where the batter ended up is the first step, because the force rule
depends on it.

Precedence, highest first:

1. An **explicit** `B-%` or `BX%` advance in the advance section. This always
   wins.
2. The **implicit** destination of the basic event:

   | Event | Batter destination |
   |---|---|
   | `S$` | 1 |
   | `D$`, `DGR` | 2 |
   | `T$` | 3 |
   | `H`, `HR` | H (scores) |
   | `W`, `I`, `IW`, `HP`, `C` (interference) | 1 |
   | `E$`, `$E$`, `FC$` | 1 |
   | `K` with no fielders, no compound event, no `B-` advance | out on strikes |
   | `K$$` (e.g. `K23`) | out at the base the sequence retires him at |
   | out on ball (`63`, `54(B)`, `8/F78`) | out |
   | `FLE$`, `SB`, `CS`, `PO`, `POCS`, `WP`, `PB`, `BK`, `DI`, `OA`, `NP` | batter stays at the plate |
   **`FLE$` was in the first row of that table until the earned-run
   derivation went looking**, and it is the one entry the corpus refutes
   outright: **all 8,562 `FLE` plays are followed by another play with the
   same batter at bat.** A muffed foul fly prolongs the plate appearance
   rather than ending it — which is the entire reason OBR 9.16(a)(2)(i)
   exists — and placing the batter on first leaves a runner standing there
   who is still holding a bat.

   **6,187** of those plays had first base empty and put a phantom runner on
   it; the other 2,376 found first occupied and the placement was silently
   dropped, which is why the duplicate-occupancy invariant never fired. The
   corruption then runs to the end of the half-inning: **20,883 plays across
   6,183 half-innings** carry a base state with a runner in it who was still
   at the plate.

   Nothing in the project could see it. Outs still balanced, because no out
   was invented; the half-inning still ended with three; and a phantom runner
   is named by no advance in the file, so he never scores and the score
   reconciliation stayed at **100.0000%** ([07-TESTING](07-TESTING.md) §4.4)
   throughout. Every gate here was shaped for a different question — the
   recurring lesson of this project, and the first time it has cost the base
   state itself.

3. An **out-count impossibility** overrides rule 2 for a bare strikeout. If
   reading the batter as retired on strikes would make more than three outs in
   the half-inning, he cannot have been, so he reached first. See §4.5 step 1;
   this is the rule that makes `K.3XH(21)` readable at all.
4. If none applies, `parse_status = 'state_ambiguous'`.

**`K` needs care.** `K.1X2(26)` is a strikeout *and* a runner thrown out — the
batter is out on strikes, two outs on the play. `K.B-1;...` is a batter who
reached. The presence of an `X` advance says nothing about the batter. See
[04-ONTOLOGY](04-ONTOLOGY.md) §3.1.

**A fielded out does not always retire the batter.** `64(1)3` ends with a bare
`3` — the throw went on to first — so the batter is out. `64(1)/FO/G6` ends
with the runner designator, so only the runner from first was retired and the
batter reached. The rule: the batter is out if any group designates `B`, or if
the **last** group carries no designator. Reading every fielded out as a batter
out double-counts every force out in the corpus.

### 2.1 Outs recorded outside the advance section

Three sources of outs must all be counted, and a replay reading only the
advance section misses two of them:

| Source | Example | Effect |
|---|---|---|
| Advance marked `X` | `.3XH(21)` | one out, unless negated by an error |
| Runner designator in a putout group | `64(1)3` | the runner from first is out |
| Base-running event in the basic section | `CS2(26)`, `PO1(13)`, `POCS2(1361)` | the runner is out, unless negated |

Base-running events also move runners with no advance section at all: `SB2`
takes the runner from first to second, `SBH` scores from third, and `SB3;SB2`
moves two. A replay that only applies the advance section leaves those runners
where they were.

An explicit advance for a runner always overrides the implicit movement, so
`CS2(2E4).1-3` puts the runner on third rather than out at second.

## 3. Advance resolution

1. Start from `bases_before`.
2. Apply every explicit advance in the advance section. `X` moves the runner off
   the bases and adds an out — **unless** the credit sequence contains an `E`,
   which negates the out ([02-GRAMMAR](02-GRAMMAR.md) §5).
3. Apply the batter destination from §2. **The batter's movement is an advance
   like any other and always gets a row**, whether or not the scorer wrote
   one — `D9` and `S9.B-2` both put the batter on second, and storing only the
   explicit form makes the same physical fact present or absent depending on
   notation. In the 2000 season alone, 69,938 plays had a batter who reached
   with no advance row against 2,425 that had one, and every implicitly scored
   run — a home run above all — was credited to nobody.

   A batter who **ran and was retired** gets a row too, `B` → `1` with
   `is_force = 1`: the batter-runner is always forced at first (§4.1), and this
   is the commonest force play in baseball. A caught fly produces no row,
   because nobody ran.

   The row is written **once**. The advance section and the batter placement
   are two paths to the same movement, and both counting it is what produced
   the double-counted run above.
4. Runners not mentioned hold. `3-3` is an explicit hold and behaves the same.
5. Runners reaching `H` score. **Earned or unearned is derived, never read
   off the `(UR)` / `(TUR)` flags** — see §9, which replaces an earlier
   version of this rule that used the flags as an input. They cover the whole
   corpus, which makes them a complete answer key; spending them as an input
   would have bought a derivation that could never be checked.
6. Charge each run to the responsible pitcher, honouring any `presadj` record
   ([01-CORPUS](01-CORPUS.md) §3).

### 3.1 Consistency checks

These are validation, not inference. A failure sets
`parse_status = 'state_inconsistent'` and the play is excluded from default
results ([01-CORPUS](01-CORPUS.md) §5.5).

- `outs_before + outs_recorded <= 3`.
- **`runs_on_play <= runners_on_base + 1`.** A run needs a runner.
- **`runs_on_play == the number of advances marked `scored`.`** Every run is
  attributable to exactly one runner.
- No two runners occupy the same base in `bases_after`.
- A runner cannot advance to a base behind them, except via the `PASS` modifier.
- `outs_after == 3` implies the half-inning ends at this play.
- Reconstructed final score equals the score in the game's own `info` and
  linescore data.
- Reconstructed earned runs equal the game's `data,er` records.

The last two are the strongest available end-to-end check on the whole state
machine and MUST run over the entire corpus in CI ([07-TESTING](07-TESTING.md)
§4).

The two run checks were added late and immediately earned it. **Nothing had
ever checked a run count.** The out-accounting invariant of §8 cannot see runs;
score reconciliation needs game logs the project does not hold; and the unit
tests asserted whatever the code produced. So a double-count sat in the batter
placement undetected: an explicit `B-H` advance was counted once by the advance
loop and again when the batter was placed, which meant **every home run written
with an explicit `B-H` scored one run too many** — `HR/F7D+.B-H(UR)`, a solo
shot, scored two. Retrosheet uses that form and the bare `HR` form
interchangeably, so the error was scattered through the corpus rather than
confined to anything.

It surfaced only once the derived tables made `runs_on_play` comparable against
`bases_before` in SQL, and it is worth being explicit about why a *unit* test
could not have found it: one of them had asserted `runs_on_play == 3` on a play
with a single runner on base, a figure that is arithmetically impossible. The
test was written against the observed value while the bug was live.

## 4. Force plays — the derivation

**Retrosheet does not record this.** `/FO` is applied to batted-ball force outs
only. The out in `K.3XH(21)` carries no marker at all. Force status is derived
here, and `.force_play()` in the query API means exactly what this section
defines.

### 4.1 Definition

A runner is forced when the batter becomes a runner and every base behind that
runner is occupied, so the runner has no right to their current base.

Given `bases_before` and a live batter-runner:

- runner on 1 is forced iff the batter-runner is live;
- runner on 2 is forced iff 1 is occupied and the batter-runner is live;
- runner on 3 is forced iff 1 and 2 are occupied and the batter-runner is live;
- the batter-runner is himself always forced at first.

If the batter-runner is not live — a strikeout with the batter retired, a caught
fly, a pure base-running play — **no runner is forced**, however the bases are
occupied.

### 4.2 Did the batter become a runner?

The force test in §4.1 is keyed on the batter **becoming a runner**, which is
not the same as reaching safely. On `64(1)3/GDP/G6` the batter is retired at
first, yet the force at second is real, because he was running while the throw
was made. On `8(B)84(2)/LDP/L8` the liner was caught, the batter never left the
box, and the runner doubled off second was tagged rather than forced. Keying
the force on "the batter was safe" misclassifies every ground-ball double play.

The rule, in order:

1. Batter reached a base → became a runner.
2. Retired on strikes without reaching → did not.
3. Trajectory modifier present → `G`/`BG` means he ran; `F`/`P`/`L`/`BP`/`BL`
   means the ball was caught and he did not.
4. `/FO`, `/GDP`, `/GTP`, `/BGDP`, `/SH` imply he ran; `/SF`, `/FDP`, `/LDP`,
   `/LTP`, `/BPDP`, `/IF` imply he did not.
5. No trajectory and no such modifier: read the trajectory off the fielding,
   which is geometry rather than a tendency. In order:

   | Fielding | Reading | Certainty |
   |---|---|---|
   | the batter's putout group names **two or more** fielders (`63`, `43`, `23`) | a throw retired him, and a throw is only necessary if he ran | `likely` |
   | a **lone** fielder, **not** 1, 2 or 3 (`8`, `6`, `5`) | he caught it: retiring a batter at first requires the ball at first, and this fielder was not there | `derived` |
   | a lone 1, 2 or 3, but **another group on the play involved a throw** (`64(1)3`) | the `64` is a throw, so the ball was on the ground | `likely` |
   | a lone 1, 2 or 3 and no throw anywhere (`3`, `1`, `2`) | **not recoverable** | no force claimed; `batter_ran = 'unknown'` |
   | `99` | not recoverable | no force claimed; `batter_ran = 'unknown'` |

   The positional split is deductive, and the corpus agrees. Of bare
   single-fielder putouts that *do* carry a trajectory: **zero** of 20,494 by an
   outfielder are ground balls, and 6 of 13,465 by a second baseman, third
   baseman or shortstop. For the first baseman it is 53%, and for the pitcher
   45% — which is exactly why those two are the unresolvable cases and everyone
   else is not.

   The last two rows claim **nothing**: no force is derived for the batter or
   for any runner behind him. An unassisted putout by the first baseman or the
   pitcher is genuinely two different plays — a grounder beaten to the bag, or
   a ball caught in the air — and the event string does not distinguish them.

   The doubt is recorded in **`batter_ran = 'unknown'`** on the play, and
   `parse_status` stays `ok`. It is not a `parse_status` because only the force
   derivation depends on it, and a non-`ok` status removes the play from every
   default result ([06-QUERY](06-QUERY.md) §4): escalating would drop 2.97% of
   the corpus from queries about home runs, and skew every era comparison. A
   force query reports the count in its `CoverageReport`, where it is
   actionable, instead of it living in the unexplained gap between two totals.

   The rate is **entirely** a function of how much detail the scorer recorded,
   and it falls off a cliff:

   | season | `yes` | `no` | `unknown` | |
   |---|---|---|---|---|
   | 1920 | 57,119 | 36,995 | 7,848 | **7.70%** |
   | 1950 | 53,714 | 40,525 | 3,199 | 3.28% |
   | 1970 | 88,807 | 79,872 | 4,033 | 2.34% |
   | 1990 | 94,523 | 93,419 | 253 | 0.13% |
   | 2020 | 36,116 | 43,947 | 0 | **0.00%** |

   Baseball did not change; the notation did. Modern files carry a trajectory
   modifier on essentially every batted ball, so rule 5 is never reached. This
   is the shape of every coverage problem in this corpus, and the reason a
   count without a `CoverageReport` is not an answer.

### 4.2.1 What `is_force` does and does not claim

`is_force` is the force **situation** of §4.1: the runner had no right to the
base, because the batter became a runner and every base behind was occupied.

It is **not** a claim about how the out was executed. Whether the fielder
retired the runner by touching the base or by tagging him is **never recorded,
for any play, in any era** — a first baseman pulled off the bag by a throw
tags the batter several times a season, and the event string is identical. So
that limitation is uniform and does not vary with `force_certainty`: it applies
equally to `63` and to `63/G6`, and modern seasons where every play carries a
trajectory read 0.0% uncertain despite carrying exactly the same risk.

The two questions are separate and only one of them is derivable. Conflating
them makes `force_certainty` unreadable, because it would then be low for the
old corpus (where the trajectory is missing) and high for the new (where it is
not) while the tag/touch question is unchanged throughout. `force_certainty`
grades whether the batter ran. Nothing grades the touch.

### 4.3 When is the batter-runner live on an uncaught third strike?

For batted balls and walks: whenever §2 puts the batter anywhere other than
"out". For an uncaught third strike the rule is the rulebook's:

> On an uncaught third strike the batter becomes a runner **iff** first base is
> unoccupied, **or** there are two outs.

RSSE does not apply that rule to decide what happened — Retrosheet already
records the outcome. It applies it as a **check**: if the encoding says the
batter reached but the rule says he could not have, the play is flagged
`state_inconsistent`. Encoding is authoritative; the rule catches errors.

### 4.4 Ordering and the removal of the force

The force is off for trailing runners once a preceding forced runner is retired.
Force status is therefore evaluated **per out, in the order the outs occurred**,
not against the state at the start of the play.

Out order is known in two ways:

- **Explicit** — putout groups in the basic section are chronological:
  `64(1)3` retires the runner from first, then the batter.
- **Conventional** — advances are listed lead-runner first, which is a
  presentation order, not necessarily the chronological one.

Rules:

1. Outs in the basic section are ordered as written.
2. If exactly one out occurs in the advance section, order is unambiguous.
3. If two or more outs occur in the advance section and their relative order
   changes any runner's force status, set
   `force_certainty = 'ambiguous'` on the affected advances and record both
   candidate readings. Do not pick one.

`force_certainty` is one of `derived` (deterministic), `ambiguous`, or `n/a`
(no determination applies — a safe advance). Predicates match `derived` only,
unless the caller opts in ([06-QUERY](06-QUERY.md) §4).

**Every out carries a determination, including a negative one.** `ambiguous` is
reserved for an out whose force status actually *turns* on §4.2 rule 5. A
runner with an empty base behind them is unforced however the batter was
retired, so that out is `derived` with `is_force = 0` — a positive finding, not
an absence of one. Recording it as `n/a` would make a certain tag out
indistinguishable from an unexamined one, and the 2000 record
([07-TESTING](07-TESTING.md) §2.2) is the anchor for the whole force
derivation: its tag out has to be a determination.

### 4.5 Worked case — the motivating play

Bases loaded, two outs, swinging third strike gets away from the catcher, who
retrieves it and throws to the pitcher covering the plate.

```
bases_before   = {1: X, 2: Y, 3: Z}
outs_before    = 2
event          = K.3XH(21)          (variants in 04-ONTOLOGY §3.1)
```

Derivation:

1. §2 rule 3 — the `K` has no fielder digits. Out-count check: if the batter
   were out on strikes, `outs_before(2) + strikeout(1) + out at home(1) = 4`.
   Impossible. Therefore the batter-runner is live.

   This step is arithmetic, not a heuristic: it fires only where the
   alternative reading is *impossible*, and only where adopting it makes the
   play consistent. If both readings overflow, the original is kept so the
   inconsistency is reported rather than moved. It is scoped to a strikeout
   with nothing in the event describing the batter's fate — no fielder digits
   after the `K`, no `+`-joined event, no explicit `B` advance — because that
   is the only shape where §2 rule 2 gives an answer the out count can refute.

   It went unimplemented until the ontology was built, and nothing caught it,
   because no play of this shape occurs in 1908–2025: the corpus replay
   reported zero inconsistent plays. The gold corpus caught it, from the
   `equivalents` list of a play that has not been released yet.
2. §4.3 — two outs, so the batter was entitled to run. Consistent.
3. §4.1 — batter-runner live, 1 and 2 occupied, so the runner on **3 is forced
   at home**.
4. §4.4 — one out in the advance section. Order unambiguous;
   `force_certainty = 'derived'`.
5. `(21)` — assist to the catcher, putout to the pitcher. Putout sequence
   `[2, 1]`.
6. `outs_after = 3`, half-inning ends, no run scores on a force for the third
   out.

Tags emitted: `Strikeout`, `UncaughtThirdStrike`, `BatterReachedOnK`, `ForceOut`,
`ForceOutAtHome`, `BasesLoaded`, `TwoOuts`, `InningEnding`, plus the fielding
credits `CatcherAssist` and `PitcherPutout`.

Every step is a stated rule. Nothing here is a heuristic.

## 5. Fielding credits

Derived uniformly from every credit sequence in the play, whether it appears in
the basic section or an advance parameter, so that queries need not know where
the play was written down:

- last atom of a sequence → **putout**;
- earlier atoms → **assist**, one each, in order;
- `E$` → **error** charged to `$`, and the out is negated;
- `U` → unknown handler, no credit;
- `99`, and `9` padding in an extended double play → no credit.

Each credit is stored as its own row with its position in the sequence
([05-DATABASE](05-DATABASE.md) §3), and the whole sequence is also stored as a
normalized string so `[2,1]` can be matched by an indexed lookup rather than a
scan.

**Known limitation: an implicit assist is not credited.** Retrosheet writes a
6-4-3 double play as `64(1)3`. The trailing `3` is a sequence of one, so the
rule above credits the first baseman a putout and nobody an assist — yet the
second baseman clearly threw to him. The assist is implicit in the adjacency of
the groups, and RSSE does not infer it, because inferring it is a heuristic and
the alternative is worse than the gap: the project's purpose is semantic
retrieval, not computing official statistics. `.putout_by()` and
`.putout_sequence()` are unaffected, and anyone reconstructing fielding
statistics from `fielding_credits` needs to know this.

## 6. Non-play records during replay

### 6.1 `NP` and `sub`

`NP` is a marker only: no state change, no out, no advance. It always precedes a
`sub`. Apply the substitution to the lineup, then continue. A pinch hitter is
position 11 and a pinch runner 12; a pinch hitter or runner for the DH becomes
the DH with no further `sub` record.

### 6.2 `badj` / `padj`

Override the handedness that the roster implies, for the next plate appearance
only. Stored on the play, not on the player.

### 6.3 `ladj`

A team batted out of order. Set `due_up` to the stated batting-order position
rather than advancing it normally. Do not attempt to model the resulting appeal;
Retrosheet encodes the outcome in the plays themselves.

### 6.4 `radj`

An extra-inning half begins with a placed runner (2020+). Initialize `bases`
with that runner instead of empty. The runner is not charged to the pitcher for
earned-run purposes; `info,tiebreaker` states the placement base.

**`radj` is not reliably the first record of its half-inning.** It commonly
follows the leading `NP`/`sub` block, so by the time it arrives the replay has
already crossed the boundary:

```
play,10,0,biggc002,00,,NP     <- boundary crossed here
radj,shawt001,2               <- placed runner declared only now
play,10,0,biggc002,31,...,W
```

A replay that applies pending placements *only* at the boundary therefore
places nobody in that half-inning, and then leaks the runner into the next one.
The rule: apply the placement immediately if the current half-inning has not
yet had a state-changing play, and defer it to the boundary otherwise. `NP`
changes no state (§6.1), so applying it mid-`NP`-block is exact rather than an
approximation.

Getting this wrong is nearly invisible. The out accounting still balances —
a missing runner records no outs — so §8's invariant passes, and the only
symptom is a run scored from a base the state calls empty. That is what the run
invariants of §3.1 detect, and they found **91 plays** doing exactly it, every
one of them a 2020s extra-inning home run.

### 6.5 `htbf`

When the home team bats first, `start` records and plays for team `1` precede
those for team `0`. The replay MUST key half-innings off the `team` field of
each play **and the game's own `info,htbf`**, never off an assumption that the
visitor bats in the top half.

`info,htbf,true` appears in **51 games**. Deriving `half` from the team alone —
`bottom` if team 1, else `top` — gets every one of them backwards, and nothing
else in the replay notices, because the out and base accounting keys off the
team and never reads `half`. It is only wrong in the output.

### 6.6 `info,innings`

`scheduled_innings` comes from `info,innings`, and the key exists **only from
2020** — which is exactly when it started to matter. **518 games in the corpus
are scheduled for seven innings** (plus 6 at eight, 6 at six, 6 at five and one
at ten), so assuming nine misreports the eighth inning of every one of them as
regulation and `ExtraInnings` ([04-ONTOLOGY](04-ONTOLOGY.md) §7) with it.

Where the key is absent the value is stored as NULL rather than 9, so "we were
told nine" stays distinguishable from "we assumed nine". The replay still
applies 9 as its default, which is right for every season that does not say.

## 7. Context derived per play

Computed once at replay and persisted, since every one of these is a common
query filter:

| Field | Definition |
|---|---|
| `bases_state` | 3-bit code of `bases_before`, `000`–`111` |
| `runners_before` | the player ids on 1st, 2nd, 3rd — occupancy says *whether*, this says *who* |
| `batter_ran` | `yes` / `no` / `unknown` — whether the batter left the box (§4.2) |
| `bases_loaded` | `bases_state == '111'` |
| `outs_before` | 0, 1, 2 |
| `inning_ending` | `outs_after == 3` |
| `runs_on_play` | runners reaching H on this play |
| `score_diff_before` | batting team score minus fielding team score |
| `is_go_ahead` | the play puts the batting team ahead having not been |
| `is_walkoff` | home team, bottom half, final play of the game, takes the lead |
| `is_final_play` | last play of the game |
| `leverage_ctx` | (inning, half, outs, bases, score_diff) tuple for indexing |

`is_walkoff` requires knowing the game ended, so it is stamped in a second pass
after the game is fully replayed.

### 7.1 When the state itself cannot be trusted

Every field above is derived from the base-out state carried into the play. If
that state is wrong, the fields are wrong — and nothing about *this* play looks
wrong, so nothing flags it.

That happens for exactly one reason: **an earlier play in the same half-inning
could not be parsed.** Its effect was never applied, so every later play in
that half inherits a state that is missing it. The later plays parse cleanly
and are internally consistent; they are simply built on a false premise.

Such a play carries `parse_status = 'state_untrusted'`.

Three properties of the flag matter:

- **It is about the context, not the event.** `state_ambiguous` says this play
  is underdetermined; `state_untrusted` says this play is fine and its
  surroundings are not. Collapsing them would lose which is which.
- **It is assigned by the loader, not by `apply_play`.** The state machine sees
  one play at a time and has nothing to be suspicious of. Only something
  holding the whole half-inning can see that an unparsed play preceded this
  one, so the flag is set where the plays are assembled into rows.
- **It ranks just above `ok`.** A finding about the play itself is the more
  specific statement, so a play that is *also* `state_ambiguous` or
  `state_inconsistent` keeps that status.

The blast radius is bounded by the half-inning: state resets at the boundary,
so contamination never crosses it.

The unparsed play itself is a separate case. Its base-out state is not wrong,
it is **unknown** — the effects of the play were never computed. Those columns
are therefore `NULL`, and `plays.outs_before`, `outs_recorded`, `outs_after`,
`bases_before` and `bases_after` are nullable for this reason alone. A stored
`'000'` would be indistinguishable from bases genuinely empty, and would be
counted as such by every aggregate.

Corpus-wide this is 7 unparsed plays and 15 downstream, out of 17.9 million —
but they are precisely the plays a force query would answer wrongly and
confidently, which is the failure mode this project exists to avoid.

## 8. Validation status

The replay is implemented and run over the whole corpus. Final-score
reconciliation ([07-TESTING](07-TESTING.md) §4) needs Retrosheet's game logs,
which are a separate download and not yet held, so the strongest check
available from event files alone is the out accounting:

> **Every half-inning must end with exactly three outs**, except the last of a
> game, which can end early on a walk-off or not be played at all.

That single invariant exercises nearly everything above — batter destination,
advance resolution, error negation, designator outs and base-running outs all
surface as a miscounted inning.

| | |
|---|---|
| games replayed | 203,285 |
| plays | 17,891,790 |
| plays flagged `state_inconsistent` | **0** |
| games with a short half-inning | **3**, all explained by known source defects |
| unexplained short half-innings | **0** |
| plays flagged `data_contradicts_rules` | 6 |
| plays flagged `state_ambiguous` | **2** |
| plays with `batter_ran = 'unknown'` | 530,888 (2.97%) |
| runtime | ~15 min |

### 8.1 What the invariant caught

The first run failed **75 of 81 games**. Seven bugs, each localised by the same
check:

| Bug | Symptom | Fix |
|---|---|---|
| Every fielded out read as a batter out | force outs counted twice; innings closed at 4–5 outs | §2, last-group rule |
| `CS`/`PO`/`POCS` outs ignored | innings closed at 2 outs | §2.1 |
| `SB` never moved the runner | wrong base state | §2.1 |
| An error anywhere in an advance negated the out | `OA.1X3(E1)(35)` lost a real out | [02-GRAMMAR](02-GRAMMAR.md) §5 |
| A `+`-joined event overrode the batter's fate | `K+E2` put a phantom runner on first, corrupting the rest of the inning | §2 |
| A safe advance cancelled a recorded out | `CS2(25).1-2` dropped the out | §2.1 |
| A parameter with no credits counted as a putout | `BXH(TH)(E2/TH)(8E2)` kept an out that two errors had negated | [02-GRAMMAR](02-GRAMMAR.md) §5 |

Most were invisible to unit tests written from the documentation: its examples
are well-formed single-purpose plays, and every one of these bugs needed a play
combining two features. The corpus-wide invariant found them all.

The `K+E2` bug is the instructive one. It was a single wrong line — treating
the second event of a `+` chain as the batter's — and it accounted for 12 of
the 17 remaining failures *and* silently corrupted base state for the rest of
each affected inning. Retrosheet documents `K+event` and `W+event` with event
one of `SB%`, `CS%`, `OA`, `PO%`, `PB`, `WP`, `E$`: all base-running. Only the
first basic event describes the batter.

### 8.2 What remains, and why it is not a bug

**Three short half-innings**, in `MIN199606081`, `SDN199607050` and
`TOR199604130`. Each of those games contains one of the seven records that are
malformed at source ([02-GRAMMAR](02-GRAMMAR.md) §8.1). A play that cannot be
parsed cannot record its out, so the inning is one short. `rsse replay`
attributes these and exits zero; an unexplained short inning fails.

The same seven records have a second consequence, which the out-accounting
invariant cannot see: the plays *after* them run on a stale base state. Those
plays are flagged `state_untrusted` (§7.1) rather than left reading `ok`.

**Six plays flagged `data_contradicts_rules`**, every one of them pre-1947:

```
1909  K+PB.1-2;B-1
1911  K+PB.3-H(UR);2-H(UR);1-3;B-2
1913  K+PB.3-H(UR);1-3;B-1
1932  K+E2.1X2(236);B-1
1934  K.B-1(E2)
1946  K+E2.3-H(UR);2-3;1-2;B-1
```

Each records a batter reaching on an uncaught third strike with first base
occupied and fewer than two outs, which the modern rule does not allow. The
replays are self-consistent — the innings balance — so this is a statement
about the data, not a state-machine failure, and it gets its own status rather
than being counted as one. Whether the rule was applied differently in that era
or the accounts are imperfect is a question for Retrosheet.

**Two plays flagged `state_ambiguous`**, down from 9,347. The 9,347 were force
determinations that had fallen back on §4.2 rule 5, and flagging the play was
the wrong instrument for them twice over:

- Where rule 5 reads the force off a **throw**, the determination is now
  `force_certainty = 'likely'` on the advance. It is recorded, it is
  interrogable, and it stays in default results — which matters, because
  `likely` is how the pre-1970s corpus writes an ordinary ground out, so
  excluding it dropped about half the force outs at first.
- Where rule 5 cannot settle the question at all, the play now carries
  `batter_ran = 'unknown'` and claims no force. 530,888 plays, 2.97%.

`parse_status` is for a play that cannot be trusted. A play with one
underdetermined *field* is not that play, and using the status because it was
the nearest available flag spread a narrow doubt across every query — a count
of home runs would have come back 3% short, and 7.7% short in 1920.

The two that remain are genuine: an advance that places a runner the basic
section retires, and a batter destination the grammar leaves undetermined.


## 9. Earned runs — the derivation

Earned runs are the second quantity in this corpus that the rulebook defines
and the record does not explain. Retrosheet writes the *verdict* — `(UR)` on
an advance, `(TUR)` for the team-only case, `data,er` per pitcher — and never
the reasoning. This is the reasoning.

The rule is OBR 9.16, and its mechanism is a **reconstructed inning**: replay
the half-inning as though no error, passed ball, or obstruction had occurred,
and count the outs the defence *would* have made. Three of them end the
inning, and every run after that is unearned however cleanly it scored.

### 9.1 Always derive; the flags are the answer key

`(UR)` appears in **every season from 1908 to 2025**, on 205,967 of the
1,796,610 scoring advances — 11.46%, which is the published unearned-run rate
for this span. It is a complete annotation, not a sparse one, and its absence
on a scoring advance means *earned* rather than *unknown*.

That is precisely why it must not be an input. A complete, independent,
per-run adjudication is the densest check available anywhere in this project:
1.8 million individual verdicts against the 203,285 game totals that `data,er`
and the game logs offer. Using it to answer the question would have destroyed
the only thing that can tell us whether the answer is right.

So the derivation never reads it. It is carried through to
`earned_runs.recorded` untouched, and compared afterwards
([07-TESTING](07-TESTING.md) §4.5).

### 9.2 Two ledgers

9.16(g) denies a relief pitcher "the benefit of previous chances for outs not
accepted". A team's inning can therefore be over in the reconstruction while
the reliever's is not, and a run be **unearned for the team and earned for the
pitcher who allowed it**. That is the whole content of `(TUR)`, and it is why
this derives `earned_team` and `earned_pitcher` separately rather than one
flag:

| written | team | pitcher |
|---|---|---|
| *(no flag)* | earned | earned |
| `(UR)` | unearned | unearned |
| `(TUR)` | unearned | earned |

The two map onto the two figures published per side in the game logs —
`er_team` and `er_individual` — and `data,er` counts the pitcher ledger.

Each pitcher carries his own out count, opened when he arrives at
`min(reconstructed outs, actual outs)`: the reliever gets no benefit from a
chance missed before him, and no penalty for an out made on a runner who does
not exist in the reconstruction either.

### 9.3 Where the rule defers, so does the derivation

9.16 is not mechanical, and it says so in its own text: "in the scorer's
judgment", "benefit of the doubt shall be given to the pitcher". Those clauses
are not gaps to be filled by a plausible guess. Where one applies,
`earned_team` and `earned_pitcher` are **NULL** and `certainty` is
`ambiguous`.

This is the same discipline as `force_certainty` (§4.4) and the same one as
the zero-versus-unknown rule in [05-DATABASE](05-DATABASE.md): *an unearned
verdict has to be earned.* Defaulting an undetermined run to "unearned"
because the reconstruction could not walk it home would put roughly 10% of all
runs into a category the rule never placed them in.

| certainty | when | what it claims |
|---|---|---|
| `derived` | a categorical clause applies, or the reconstruction never diverged from the inning that was played | the rule decides, and this is its answer |
| `likely` | the run scored unaided but the reconstruction diverged elsewhere in the inning | an answer, graded |
| `ambiguous` | 9.16 defers to the scorer | **no claim** |
| `untrusted` | a play in the half-inning could not be parsed or its state is not trusted (§7.1) | no claim, for a different reason |

The deferral is justified by measurement, not by reading. Each deferred class
splits roughly 40/60 against what Retrosheet's scorers actually wrote — if any
of them had come back 95/5, the rule would have been deciding it after all and
the deferral would be evasion.

### 9.4 What the reconstruction does

Categorical, decided by the rule with no hypothetical consulted:

- **9.16(a)(2)(iii)** reached on an error (`E$`) — unearned, and the batter
  owes the reconstruction an out.
- **9.16(a)(2)(ii)** reached on interference or obstruction (`C`) — unearned,
  and owes **no** out: the batter was still at the plate. Folding this
  together with the clause above ends innings that should still be going.
- **9.16(a)(2)(i)** at bat prolonged by a muffed foul fly (`FLE$`) — the muff
  is the missed chance, counted there, and the at bat's eventual outcome owes
  nothing further.
- **9.16(a)(1)** reached on a fielder's choice retiring a runner who himself
  reached on an error — the batter inherits the taint and the out. This fires
  on the situation, not the notation: a force out at second is written
  `54(1)/FO`, a plain out group, and the rule does not turn on whether
  Retrosheet wrote `FC`.
- **9.16(b)** life prolonged by an error — an advance written `X` that only
  succeeded because of an error is an out in the reconstruction. The runner is
  recorded absent at the base he actually reached, not the one he left.
- Three reconstructed outs already recorded — unearned for the team.

Deferred to the scorer:

- The third reconstructed out falling on the very play a run scored. Whether
  it would have come before or after the run is not in the record.
- An advance aided by an error or a passed ball — 9.16(c) and (e), the clauses
  that contain the words.
- A run scored on a play that turned on an error anywhere, including one
  written in the basic event. `E6/G.3-H` is a run that scored because the
  shortstop booted the ball, and the advance section says nothing about it;
  `PO1(E1).2-3` is a pickoff throw that got away. Reading only each advance's
  own parameters calls both of these unaided.
- A runner the reconstruction has held short of home by an earlier error.

A **passed ball is aid; a wild pitch is not.** 9.16(a) lists the wild pitch
among the things that produce an earned run and pointedly omits the passed
ball, because one is the pitcher's own mistake and the other is the
catcher's.

### 9.5 Responsibility

Each runner carries the pitcher who was on the mound when he reached base, and
a run is charged there rather than to whoever threw the pitch it scored on
(9.16(f)). On a fielder's choice the charge follows the runner erased, so a
reliever is not handed a run for a baserunner he inherited and merely swapped.
A `presadj` record overrules all of it: Retrosheet stating who is responsible
beats working it out.

The **extra-inning placed runner** (§6.4) is outside the reconstruction from
the first pitch. No pitcher put him on second, so his run is nobody's to earn.

### 9.6 What was tried and rejected

Extending 9.16(b) past the outs Retrosheet actually writes — reading a
throwing error on an advance as a chance for an out that was not accepted, as
in `FC5.1-3(E5/TH)`, the third baseman throwing at the runner taking third and
missing — is the obvious next step, and it is wrong. Over 211,000 runs it made
agreement **worse**: the cases it fixes are outnumbered by the runners it
retires who were never going to be out. A narrower form restricted to the
fielder covering the destination base gained 7 runs in 38,000 on a
three-season sample and lost on twelve. See [07-TESTING](07-TESTING.md) §4.5.
