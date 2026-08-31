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
   | `E$`, `$E$`, `FC$`, `FLE$` | 1 |
   | `K` with no fielders, no compound event, no `B-` advance | out on strikes |
   | `K$$` (e.g. `K23`) | out at the base the sequence retires him at |
   | out on ball (`63`, `54(B)`, `8/F78`) | out |
   | `SB`, `CS`, `PO`, `POCS`, `WP`, `PB`, `BK`, `DI`, `OA`, `NP` | batter stays at the plate |
3. If neither applies, `parse_status = 'state_ambiguous'`.

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
3. Apply the batter destination from §2.
4. Runners not mentioned hold. `3-3` is an explicit hold and behaves the same.
5. Runners reaching `H` score. Determine earned/unearned from `(UR)` / `(TUR)`
   flags when present; otherwise apply the standard rules against the
   reconstructed inning and record the result as derived.
6. Charge each run to the responsible pitcher, honouring any `presadj` record
   ([01-CORPUS](01-CORPUS.md) §3).

### 3.1 Consistency checks

These are validation, not inference. A failure sets
`parse_status = 'state_inconsistent'` and the play is excluded from default
results ([01-CORPUS](01-CORPUS.md) §5.5).

- `outs_before + outs_recorded <= 3`.
- No two runners occupy the same base in `bases_after`.
- A runner cannot advance to a base behind them, except via the `PASS` modifier.
- `outs_after == 3` implies the half-inning ends at this play.
- Reconstructed final score equals the score in the game's own `info` and
  linescore data.
- Reconstructed earned runs equal the game's `data,er` records.

The last two are the strongest available end-to-end check on the whole state
machine and MUST run over the entire corpus in CI ([07-TESTING](07-TESTING.md)
§4).

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
5. No trajectory and no such modifier: a putout **with an assist** is a throw,
   which the batter had to be running to make necessary; a lone fielder caught
   the ball. This is the one inference in the chain, so any force it decides is
   marked `force_certainty = 'ambiguous'` rather than `derived`.

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

`force_certainty` is one of `derived` (deterministic) or `ambiguous`. Predicates
match `derived` only, unless the caller opts in
([06-QUERY](06-QUERY.md) §4).

### 4.5 Worked case — the motivating play

Bases loaded, two outs, swinging third strike gets away from the catcher, who
retrieves it and throws to the pitcher covering the plate.

```
bases_before   = {1: X, 2: Y, 3: Z}
outs_before    = 2
event          = K.3XH(21)          (variants in 04-ONTOLOGY §3.1)
```

Derivation:

1. §2 — the `K` has no fielder digits. Out-count check: if the batter were out
   on strikes, `outs_before(2) + strikeout(1) + out at home(1) = 4`. Impossible.
   Therefore the batter-runner is live.
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

### 6.5 `htbf`

When the home team bats first, `start` records and plays for team `1` precede
those for team `0`. The replay MUST key half-innings off the `team` field of
each play, never off an assumption that the visitor bats in the top half.

## 7. Context derived per play

Computed once at replay and persisted, since every one of these is a common
query filter:

| Field | Definition |
|---|---|
| `bases_state` | 3-bit code of `bases_before`, `000`–`111` |
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
| plays flagged `state_ambiguous` | 9,347 (0.05%) |
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

**9,347 plays flagged `state_ambiguous`** are expected. Those are force
determinations that fell back on §4.2 rule 5 — a fielded out with no trajectory
modifier — where certainty is recorded rather than guessed. They are excluded
from default query results by [06-QUERY](06-QUERY.md) §4.
