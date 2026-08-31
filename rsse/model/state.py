"""Game state and inning replay (spec/03-STATE.md).

The syntax tree says nothing about who was on base, how many were out, or
whether an out was forced. All of that is reconstructed here by replaying each
half-inning.

The governing rule is spec/03-STATE.md's: **deterministic or flagged**. Every
value is either derived by a stated rule or marked uncertain. Nothing is
guessed, and an inconsistency is recorded rather than smoothed over.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..parser import grammar as G

BASES = ("1", "2", "3")


class ParseStatus:
    OK = "ok"
    AMBIGUOUS = "state_ambiguous"
    INCONSISTENT = "state_inconsistent"
    #: The replay is self-consistent but the play contradicts the rulebook --
    #: a statement about the data, not about the state machine.
    CONTRADICTS_RULES = "data_contradicts_rules"


@dataclass(frozen=True)
class Runner:
    player_id: str | None = None
    #: True for a runner placed at second to start an extra inning (radj).
    placed: bool = False


@dataclass
class HalfInningState:
    """The vector carried across plays within a half-inning (§1)."""

    outs: int = 0
    bases: dict[str, Runner | None] = field(
        default_factory=lambda: {"1": None, "2": None, "3": None})

    def code(self) -> str:
        """3-bit occupancy, first base first: '000'..'111' (§7)."""
        return "".join("1" if self.bases[b] else "0" for b in BASES)

    def occupied(self, base: str) -> bool:
        return self.bases.get(base) is not None

    def copy(self) -> "HalfInningState":
        return HalfInningState(self.outs, dict(self.bases))


@dataclass
class ResolvedAdvance:
    """One runner movement after error negation and force derivation."""

    origin: str
    dest: str
    marked_out: bool
    is_out: bool
    is_explicit: bool
    is_force: bool = False
    force_certainty: str = "n/a"   # derived | ambiguous | n/a
    scored: bool = False
    runner: Runner | None = None
    raw: str = ""


@dataclass
class PlayOutcome:
    """Everything the state machine derives for one play."""

    bases_before: str
    outs_before: int
    bases_after: str = "000"
    outs_after: int = 0
    outs_recorded: int = 0
    batter_dest: str | None = None
    batter_is_out: bool = False
    runs_on_play: int = 0
    advances: list[ResolvedAdvance] = field(default_factory=list)
    parse_status: str = ParseStatus.OK
    notes: list[str] = field(default_factory=list)

    @property
    def is_inning_ending(self) -> bool:
        return self.outs_after >= 3

    #: Weakest to strongest; a stronger status is never downgraded.
    _RANK = {"ok": 0, "data_contradicts_rules": 1, "state_ambiguous": 2,
             "state_inconsistent": 3}

    def flag(self, status: str, note: str) -> None:
        if self._RANK[status] > self._RANK[self.parse_status]:
            self.parse_status = status
        self.notes.append(note)


# ---------------------------------------------------------------------------
# batter destination (§2)
# ---------------------------------------------------------------------------

#: Implicit destination by basic event, used when no explicit B advance exists.
_HIT_DEST = {"S": "1", "D": "2", "T": "3"}
_REACH_FIRST = (G.Walk, G.HitByPitch, G.ReachedOnError, G.FieldersChoice,
                G.FoulFlyError, G.Interference)
#: Events that leave the batter at the plate: the play did not involve them.
_NOT_BATTER = ("SB", "CS", "PO", "POCS", "DI", "OA", "WP", "PB", "BK")

OUT = "out"


def batter_destination(event: G.Event) -> str | None:
    """Where the batter ended up, from the event alone (§2 rule 2).

    Returns a base, ``OUT``, or None when the play did not involve the batter.
    An explicit ``B`` advance overrides this and is applied by the caller.

    Only the **first** basic event describes the batter. In a `+`-joined chain
    the remainder are base-running events -- Retrosheet documents `K+event` and
    `W+event` with event one of `SB%`, `CS%`, `OA`, `PO%`, `PB`, `WP`, `E$`. So
    in `K+E2` the error is charged for a runner's advance and the batter is
    still out on strikes; letting the `E2` set the destination puts a phantom
    runner on first and corrupts the rest of the inning.
    """
    if not event.groups or not event.groups[0]:
        return None
    basic = event.groups[0][0]
    if isinstance(basic, G.NoPlay):
        return None
    if isinstance(basic, G.BaseRunning) and basic.code in _NOT_BATTER:
        return None
    if isinstance(basic, G.Hit):
        return _HIT_DEST.get(basic.kind)
    if isinstance(basic, G.GroundRuleDouble):
        return "2"
    if isinstance(basic, G.HomeRun):
        return "H"
    if isinstance(basic, _REACH_FIRST):
        return "1"
    if isinstance(basic, G.Strikeout):
        return OUT
    if isinstance(basic, G.Out):
        return OUT if _out_retires_batter(basic) else "1"
    return None


def _out_retires_batter(basic: G.Out) -> bool:
    """Whether a fielded out retired the batter rather than only a runner.

    `64(1)3` ends with a bare `3`: the throw went on to first and the batter is
    out. `64(1)/FO/G6` ends with the designator, so only the runner from first
    was retired and the batter reached. Reading every `Out` as a batter out
    double-counts every force out in the corpus.
    """
    if any(g.runner == "B" for g in basic.groups):
        return True
    return basic.groups[-1].runner is None if basic.groups else True


# ---------------------------------------------------------------------------
# force derivation (§4)
# ---------------------------------------------------------------------------

def forced_bases(bases_before: HalfInningState, batter_live: bool) -> set[str]:
    """Bases whose runner is forced, given the state before the play (§4.1).

    A runner is forced when the batter becomes a runner and every base behind
    them is occupied. If the batter-runner is not live, nothing is forced
    however the bases are occupied -- which is the whole distinction between
    the 2026 force at home and the 2000 tag at home, two plays whose event
    strings differ only by a modifier.
    """
    if not batter_live:
        return set()
    forced = {"1"}
    if bases_before.occupied("1"):
        forced.add("2")
        if bases_before.occupied("2"):
            forced.add("3")
    return forced


def batter_may_run_on_uncaught_third(bases_before: HalfInningState,
                                     outs_before: int) -> bool:
    """The rulebook condition (§4.2).

    Used only as a *check* on what Retrosheet recorded, never to decide what
    happened: the encoding is authoritative and this catches contradictions.
    """
    return not bases_before.occupied("1") or outs_before == 2


# ---------------------------------------------------------------------------
# did the batter become a runner? (§4.2)
# ---------------------------------------------------------------------------

NEXT_BASE = {"1": "2", "2": "3", "3": "H"}

#: Trajectories on which the ball was caught: the batter never left for first.
_CAUGHT = ("F", "P", "L", "BP", "BL")
#: Trajectories on which the ball was fielded off the ground: the batter ran.
_ON_GROUND = ("G", "BG")


def batter_became_runner(event: G.Event, batter_dest: str | None
                         ) -> tuple[bool, bool]:
    """Whether the batter left the box, and whether that is certain.

    This is *not* the same as reaching safely, and the difference decides every
    force play. On `64(1)3/GDP/G6` the batter is retired at first, yet the force
    at second is real, because he was running while the throw was made. On
    `8(B)84(2)/LDP/L8` the liner was caught, the batter never ran, and the
    runner doubled off second was tagged rather than forced.

    Returns ``(became_runner, certain)``.
    """
    if batter_dest is None:
        return False, True
    if batter_dest != OUT:
        return True, True

    # Retired on strikes without reaching: never became a runner.
    if any(isinstance(b, G.Strikeout) for b in event.basics):
        return False, True

    trajectory = next((m.trajectory for m in event.modifiers
                       if m.kind == "hit" and m.trajectory), None)
    if trajectory in _CAUGHT:
        return False, True
    if trajectory in _ON_GROUND:
        return True, True
    codes = event.modifier_codes()
    if codes & {"FO", "GDP", "GTP", "BGDP", "SH"}:
        return True, True
    if codes & {"SF", "FDP", "LDP", "LTP", "BPDP", "IF"}:
        return False, True

    # No trajectory given. A putout with an assist is a throw, which the batter
    # had to be running to make necessary; a lone fielder caught the ball.
    for basic in event.basics:
        if isinstance(basic, G.Out):
            assisted = any(len(g.fielders) > 1 for g in basic.groups)
            return assisted, False
    return False, False


#: Origin base for a base-running event, by code and target base.
_STEAL_ORIGIN = {"2": "1", "3": "2", "H": "3"}


def base_running_effects(event: G.Event) -> list[tuple[str, str, bool]]:
    """Runner movements implied by the basic section, as (origin, dest, is_out).

    Retrosheet puts these in the event rather than the advance section, so a
    replay that only reads advances loses every caught stealing and pickoff
    out, and never moves a runner on `SB2`.
    """
    effects = []
    for basic in event.basics:
        if not isinstance(basic, G.BaseRunning):
            continue
        base = basic.base
        negated = any(isinstance(p, G.CreditSequence) and p.has_error
                      for p in basic.params)
        if basic.code == "SB" and base:
            origin = _STEAL_ORIGIN.get(base)
            if origin:
                effects.append((origin, base, False))
        elif basic.code in ("CS", "POCS") and base:
            origin = _STEAL_ORIGIN.get(base)
            if origin:
                effects.append((origin, base, not negated))
        elif basic.code == "PO" and base in BASES:
            effects.append((base, base, not negated))
    return effects


def runner_designator_outs(event: G.Event) -> list[str]:
    """Runners retired in the basic section, e.g. the `1` of `64(1)3` (§2).

    `B` designators are excluded: the batter is accounted for by
    :func:`batter_destination`, and counting both would double the out.
    """
    outs = []
    for basic in event.basics:
        groups = getattr(basic, "groups", ())
        for group in groups:
            if group.runner and group.runner in BASES:
                outs.append(group.runner)
    return outs


# ---------------------------------------------------------------------------
# replay (§3)
# ---------------------------------------------------------------------------

def apply_play(state: HalfInningState, event: G.Event) -> tuple[HalfInningState,
                                                                PlayOutcome]:
    """Apply one parsed event to the state, returning the new state and outcome."""
    before = state.copy()
    out = PlayOutcome(bases_before=before.code(), outs_before=before.outs)

    if any(isinstance(b, G.NoPlay) for b in event.basics) and len(event.basics) == 1:
        out.bases_after, out.outs_after = before.code(), before.outs
        return before, out

    explicit = {a.origin: a for a in event.advances}
    implicit_dest = batter_destination(event)

    # Outs recorded by the basic section. These are authoritative: `CS2(25).1-2`
    # records a caught stealing with no error, so the runner is out however the
    # advance reads. Only an error in the event negates it.
    running = base_running_effects(event)
    event_outs = {origin for origin, _dest, is_out in running if is_out}

    # -- 1. batter destination: an explicit B advance always wins (§2 rule 1)
    b_adv = explicit.get("B")
    if b_adv is not None:
        batter_dest = OUT if b_adv.is_out else b_adv.dest
    else:
        batter_dest = implicit_dest
    batter_live = batter_dest not in (None, OUT)
    out.batter_dest = batter_dest
    out.batter_is_out = batter_dest == OUT

    # -- 2. force status, from the state entering the play (§4.1).
    # Keyed on whether the batter *became* a runner, not on whether he was safe.
    became_runner, certain = batter_became_runner(event, batter_dest)
    forced = forced_bases(before, became_runner)

    # -- 3. resolve advances
    after = HalfInningState(outs=before.outs, bases={"1": None, "2": None, "3": None})
    moved: set[str] = set()
    outs_recorded = 0
    runs = 0

    for adv in event.advances:
        runner = None if adv.origin == "B" else before.bases.get(adv.origin)
        resolved = ResolvedAdvance(
            origin=adv.origin, dest=adv.dest, marked_out=adv.marked_out,
            is_out=adv.is_out, is_explicit=True, runner=runner, raw=adv.emit(),
        )
        if adv.origin != "B":
            moved.add(adv.origin)
            if adv.origin in forced and adv.is_out:
                resolved.is_force = True
                resolved.force_certainty = "derived" if certain else "ambiguous"
        if adv.origin in event_outs and not adv.is_out:
            # The event says the runner was retired; do not place them.
            out.flag(ParseStatus.AMBIGUOUS,
                     f"advance {adv.emit()} places a runner the event retires")
            out.advances.append(resolved)
            continue
        if adv.is_out:
            outs_recorded += 1
        elif adv.dest == "H":
            runs += 1
            resolved.scored = True
        elif adv.origin != "B":
            after.bases[adv.dest] = runner
        out.advances.append(resolved)

    # -- 3a. runner movements implied by the basic section (SB/CS/PO/POCS)
    for origin, dest, is_out in running:
        adv = explicit.get(origin)
        if adv is not None and (adv.is_out or not is_out):
            continue      # already counted, or the advance places a safe runner
        if origin in moved and adv is None:
            continue
        moved.add(origin)
        runner = before.bases.get(origin)
        if is_out:
            outs_recorded += 1
        elif dest == "H":
            runs += 1
        elif dest in after.bases:
            after.bases[dest] = runner
        out.advances.append(ResolvedAdvance(
            origin=origin, dest=dest, marked_out=is_out, is_out=is_out,
            is_explicit=False, scored=(not is_out and dest == "H"),
            runner=runner, raw=f"{origin}{'X' if is_out else '-'}{dest}",
        ))

    # -- 3b. runners retired in the basic section (`64(1)3`)
    for origin in runner_designator_outs(event):
        if origin in moved:
            continue
        moved.add(origin)
        is_force = origin in forced
        # Retrosheet does not state where a designated runner was retired.
        # A forced runner is retired at the base ahead; an unforced one was
        # doubled off the base they held.
        dest = NEXT_BASE[origin] if is_force else origin
        out.advances.append(ResolvedAdvance(
            origin=origin, dest=dest, marked_out=True, is_out=True,
            is_explicit=False, is_force=is_force,
            force_certainty=("derived" if certain else "ambiguous"),
            runner=before.bases.get(origin), raw=f"({origin})",
        ))
        outs_recorded += 1
        if not certain:
            out.flag(ParseStatus.AMBIGUOUS,
                     f"force status of the runner from {origin} depends on "
                     "whether the batter ran, which the event does not state")

    # -- 4. runners not mentioned hold (§3 rule 4)
    for base in BASES:
        if base not in moved and before.bases[base] is not None:
            after.bases[base] = before.bases[base]

    # -- 5. the batter, if not placed by an explicit advance
    if batter_dest == OUT and b_adv is None:
        outs_recorded += 1
    elif batter_live and b_adv is None:
        if batter_dest == "H":
            runs += 1
        else:
            after.bases[batter_dest] = Runner()
    elif b_adv is not None and batter_live:
        if batter_dest == "H":
            runs += 1
        else:
            after.bases[batter_dest] = Runner()

    after.outs = before.outs + outs_recorded
    out.outs_recorded = outs_recorded
    out.outs_after = after.outs
    out.runs_on_play = runs
    out.bases_after = after.code()

    _check(out, before, after, event, batter_live)
    return after, out


def _check(out: PlayOutcome, before: HalfInningState, after: HalfInningState,
           event: G.Event, batter_live: bool) -> None:
    """Consistency checks (§3.1). Validation only -- never inference."""
    if out.outs_after > 3:
        out.flag(ParseStatus.INCONSISTENT,
                 f"{before.outs} outs before + {out.outs_recorded} recorded = "
                 f"{out.outs_after}")
    if out.batter_dest is None and any(
            isinstance(b, (G.Hit, G.Strikeout, G.Out, G.Walk)) for b in event.basics):
        out.flag(ParseStatus.AMBIGUOUS, "batter destination undetermined")

    strikeout = any(isinstance(b, G.Strikeout) for b in event.basics)
    if strikeout and batter_live and not batter_may_run_on_uncaught_third(
            before, before.outs):
        # The encoding is authoritative (§4.3); this records that the account
        # cannot be right under the modern rule, without altering the replay.
        out.flag(ParseStatus.CONTRADICTS_RULES,
                 "batter reached on an uncaught third strike with first base "
                 "occupied and fewer than two outs")
