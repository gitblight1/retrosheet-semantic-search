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
results ([01-CORPUS](01-CORPUS.md) §5.4).

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

### 4.2 When is the batter-runner live?

For batted balls and walks: whenever §2 puts the batter anywhere other than
"out". For an uncaught third strike the rule is the rulebook's:

> On an uncaught third strike the batter becomes a runner **iff** first base is
> unoccupied, **or** there are two outs.

RSSE does not apply that rule to decide what happened — Retrosheet already
records the outcome. It applies it as a **check**: if the encoding says the
batter reached but the rule says he could not have, the play is flagged
`state_inconsistent`. Encoding is authoritative; the rule catches errors.

### 4.3 Ordering and the removal of the force

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

### 4.4 Worked case — the motivating play

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
2. §4.2 — two outs, so the batter was entitled to run. Consistent.
3. §4.1 — batter-runner live, 1 and 2 occupied, so the runner on **3 is forced
   at home**.
4. §4.3 — one out in the advance section. Order unambiguous;
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
