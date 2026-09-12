"""Game state and inning replay (spec/03-STATE.md).

The syntax tree says nothing about who was on base, how many were out, or
whether an out was forced. All of that is reconstructed here by replaying each
half-inning.

The governing rule is spec/03-STATE.md's: **deterministic or flagged**. Every
value is either derived by a stated rule or marked uncertain. Nothing is
guessed, and an inconsistency is recorded rather than smoothed over.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..parser import grammar as G

BASES = ("1", "2", "3")


class ParseStatus:
    OK = "ok"
    AMBIGUOUS = "state_ambiguous"
    INCONSISTENT = "state_inconsistent"
    #: The replay is self-consistent but the play contradicts the rulebook --
    #: a statement about the data, not about the state machine.
    CONTRADICTS_RULES = "data_contradicts_rules"
    #: The play parsed cleanly, but it inherited a base-out state known to be
    #: wrong: an earlier play in the same half-inning could not be parsed, so
    #: its effect was never applied. A statement about the *context*, not about
    #: this play's event string -- which is why it needs its own value rather
    #: than reusing AMBIGUOUS. Assigned by the loader, which can see the whole
    #: half-inning, not by `apply_play`, which sees one play. See §7.
    UNTRUSTED = "state_untrusted"


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
    #: derived | likely | ambiguous | n/a -- see §4.4 and :func:`certainty`.
    force_certainty: str = "n/a"
    scored: bool = False
    runner: Runner | None = None
    raw: str = ""


@dataclass
class PlayOutcome:
    """Everything the state machine derives for one play."""

    bases_before: str
    outs_before: int
    bases_after: str = "000"
    #: Player ids on 1st, 2nd, 3rd entering the play. `bases_before` gives
    #: occupancy; these give who, which `plays.runner_N_before` needs and an
    #: advance row can only supply for runners that moved.
    runners_before: tuple[str | None, str | None, str | None] = (None, None, None)
    outs_after: int = 0
    outs_recorded: int = 0
    batter_dest: str | None = None
    batter_is_out: bool = False
    #: Whether the batter left the box: 'yes', 'no', or 'unknown' (§4.2 rule 5).
    #:
    #: A queryable fact rather than a `parse_status`, deliberately. The force
    #: derivation is the only thing that turns on it, so escalating the whole
    #: play would exclude it from every default query -- 3% of the corpus, and
    #: 7.7% of 1920 -- and a count of home runs would come back short for
    #: reasons about ground balls. `unknown` claims no force and says so here,
    #: where a force query's CoverageReport can report it.
    batter_ran: str = "unknown"
    runs_on_play: int = 0
    advances: list[ResolvedAdvance] = field(default_factory=list)
    parse_status: str = ParseStatus.OK
    notes: list[str] = field(default_factory=list)

    @property
    def is_inning_ending(self) -> bool:
        return self.outs_after >= 3

    #: Weakest to strongest; a stronger status is never downgraded.
    #: `state_untrusted` ranks just above `ok`: it says nothing is wrong with
    #: this play, only with the state it inherited, so any finding about the
    #: play itself outranks it.
    _RANK = {"ok": 0, "state_untrusted": 1, "data_contradicts_rules": 2,
             "state_ambiguous": 3, "state_inconsistent": 4}

    def flag(self, status: str, note: str) -> None:
        if self._RANK[status] > self._RANK[self.parse_status]:
            self.parse_status = status
        self.notes.append(note)


@dataclass(frozen=True)
class PlayContext:
    """Game context around a play (§7).

    Everything here is either carried by the replay or stamped in the
    second pass over a finished game. It is separated from
    :class:`PlayOutcome` because the outcome is a pure function of
    ``(state, event)`` while the context needs the rest of the game --
    the score, the schedule length, and whether the game ended here.

    Defaults describe a leadoff plate appearance in a scoreless regulation
    game, so a rule can be exercised in isolation without inventing a game.
    """

    inning: int = 1
    half: str = "top"
    team: int = 0                      # 0 visitor, 1 home, per Retrosheet
    scheduled_innings: int = 9
    score_batting_before: int = 0
    score_fielding_before: int = 0
    is_go_ahead: bool = False
    is_walkoff: bool = False
    is_final_play: bool = False
    #: A runner placed to start an extra inning (radj) is on base for this play.
    has_placed_runner: bool = False
    #: An `ladj` record preceded this play: the team batted out of order (§6.3).
    after_ladj: bool = False
    #: From the linked `com` record of a reviewed play: True if reversed,
    #: False if upheld, None if no review or the comment was not linked.
    replay_reversed: bool | None = None

    @property
    def score_diff_before(self) -> int:
        """Batting team's runs minus the fielding team's, entering the play."""
        return self.score_batting_before - self.score_fielding_before

    @property
    def extra_innings(self) -> bool:
        return self.inning > self.scheduled_innings

    def leverage_ctx(self, outcome: "PlayOutcome") -> tuple:
        """The indexing tuple of §7: (inning, half, outs, bases, score_diff)."""
        return (self.inning, self.half, outcome.outs_before,
                outcome.bases_before, self.score_diff_before)


# ---------------------------------------------------------------------------
# fielding credits (§5)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CreditSeq:
    """One credit sequence, wherever it was written down (§5).

    Retrosheet places the same physical play in the basic section or in an
    advance parameter depending on where the out occurred -- `K23` versus
    `K.3XH(21)` -- so sequences are collected uniformly and carry their origin
    in ``scope`` rather than being reachable only from one of the two.

    ``atoms`` are ``(fielder, is_error)`` in written order. Per §5 the last
    atom is the putout and the earlier ones are assists, one each; an ``E$``
    charges an error and negates the out; ``U`` and ``99`` earn no credit.
    """

    scope: str                          # 'basic' | 'advance'
    #: Running index within ``scope``, unique per play. This is a *key*, and
    #: it cannot be the advance index: `S9.BXH(TH)(E2/TH)(8E2)` puts two
    #: credit sequences on one advance, so numbering by advance collides on
    #: `credit_sequences`' primary key (spec/05-DATABASE.md §3.1).
    scope_seq: int
    #: Which out group or advance this came from. Provenance, not a key --
    #: two sequences may share it.
    origin_seq: int = 0
    atoms: tuple[tuple[str, bool], ...] = ()
    seq_text: str = ""                  # as written: '21', '2E4', 'U9'
    #: True when the sequence records an out (a basic-section putout group, or
    #: an advance marked X). A relay modifier `R$` explicitly makes no out.
    records_out: bool = False

    @property
    def has_error(self) -> bool:
        return any(is_error for _f, is_error in self.atoms)

    @property
    def credited(self) -> tuple[tuple[str, bool], ...]:
        """Atoms that earn a credit: ``U`` and ``99`` padding earn none (§5)."""
        return tuple((f, e) for f, e in self.atoms if f not in ("U", "9?"))

    @property
    def putout(self) -> str | None:
        """The last credited atom, unless it is an error (§5)."""
        clean = [f for f, e in self.credited if not e]
        return clean[-1] if clean and not self.atoms[-1][1] else None

    @property
    def assists(self) -> tuple[str, ...]:
        clean = [f for f, e in self.credited if not e]
        return tuple(clean[:-1]) if self.putout is not None else tuple(clean)

    @property
    def repeats_a_fielder(self) -> bool:
        credited = [f for f, _e in self.credited]
        return len(credited) != len(set(credited))


def _atoms(fielders: str) -> tuple[tuple[str, bool], ...]:
    """Split a bare fielder run like ``643`` or ``99`` into credit atoms.

    ``99`` codes an unknown play (§2.1) and is kept as a single non-crediting
    atom rather than read as two nines, so it cannot be mistaken for a right
    fielder handling the ball twice.
    """
    if "99" in fielders:
        return (("9?", False),) if fielders == "99" else tuple(
            (c, False) for c in fielders)
    return tuple((c, False) for c in fielders)


def credit_sequences(event: G.Event) -> list[CreditSeq]:
    """Every credit sequence in a play, basic section and advances alike (§5).

    One sequence per putout group, not one per basic event: in `64(1)3` the
    `64` retires the runner from first and the trailing `3` retires the batter,
    which are two throws and two putouts. Flattening them into `643` would
    lose the fact that two outs were made.
    """
    seqs: list[CreditSeq] = []
    counters = {"basic": 0, "advance": 0}

    def add(scope: str, origin_seq: int, atoms, text: str, out: bool) -> None:
        if not atoms:
            return
        seqs.append(CreditSeq(scope, counters[scope], origin_seq,
                              tuple(atoms), text, out))
        counters[scope] += 1

    group_index = 0
    for basic in event.basics:
        if isinstance(basic, (G.Out, G.Strikeout)):
            for group in basic.groups:
                add("basic", group_index, _atoms(group.fielders),
                    group.fielders, True)
                group_index += 1
        elif isinstance(basic, G.ReachedOnError):
            # `$$E$`: the leading fielders are assists, the last is charged.
            atoms = list(_atoms(basic.assists)) + [(basic.fielder, True)]
            add("basic", group_index, atoms,
                basic.assists + "E" + basic.fielder, False)
            group_index += 1
        elif isinstance(basic, G.FoulFlyError):
            add("basic", group_index, [(basic.fielders[-1], True)],
                "E" + basic.fielders[-1], False)
            group_index += 1
        elif isinstance(basic, G.BaseRunning):
            for param in basic.params:
                if isinstance(param, G.CreditSequence) and param.credits:
                    atoms = [(c.fielder, c.is_error) for c in param.credits]
                    text = "".join(c.emit() for c in param.credits)
                    add("basic", group_index, atoms, text, not param.has_error)
            group_index += 1

    for adv_seq, adv in enumerate(event.advances):
        for param in adv.params:
            if isinstance(param, G.CreditSequence) and param.credits:
                atoms = [(c.fielder, c.is_error) for c in param.credits]
                text = "".join(c.emit() for c in param.credits)
                add("advance", adv_seq, atoms, text, adv.is_out)

    return seqs


# ---------------------------------------------------------------------------
# batter destination (§2)
# ---------------------------------------------------------------------------

#: Implicit destination by basic event, used when no explicit B advance exists.
_HIT_DEST = {"S": "1", "D": "2", "T": "3"}
#: `FLE$` is deliberately **not** here, though §2 rule 2 listed it until the
#: earned-run derivation went looking. See `batter_destination`.
_REACH_FIRST = (G.Walk, G.HitByPitch, G.ReachedOnError, G.FieldersChoice,
                G.Interference)
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
    if isinstance(basic, G.FoulFlyError):
        # A muffed foul fly does not end the plate appearance, it prolongs it
        # -- which is the whole reason OBR 9.16(a)(2)(i) exists. §2 rule 2
        # listed `FLE$` as putting the batter on first, and the corpus is
        # unanimous against it: of the 8,563 `FLE` plays in the corpus, the
        # 8,562 that have a following play are **every one** followed by the
        # same batter at bat -- the odd one is the last play of its game, not
        # a counter-example. Placing him
        # on first leaves a phantom runner there for the rest of the
        # half-inning, and no invariant in the project could see it -- outs
        # still balanced, and a runner nobody's advance ever mentions never
        # scores, so the score reconciliation stayed at 100%.
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

#: Force certainty, as stored on a resolved advance (§4.4).
FORCE_DERIVED = "derived"
FORCE_LIKELY = "likely"
FORCE_AMBIGUOUS = "ambiguous"
FORCE_NA = "n/a"

#: Not a stored certainty. The batter-ran question could not be settled at all,
#: so no force is claimed and the play records the doubt instead (§4.2 rule 5).
UNRESOLVED = "unresolved"

#: The only fielders who can retire a batter at first **without a throw**: the
#: first baseman, who is there, and the pitcher and catcher, who can cover the
#: bag or cut the batter off on his way. An unassisted putout by anyone else is
#: necessarily a catch -- a shortstop who fields a grounder has to throw it.
#: The corpus agrees deductively: of bare single-fielder putouts that do carry
#: a trajectory, **zero** of 20,494 by an outfielder are ground balls, and 6 of
#: 13,465 by a 2B/3B/SS. For the first baseman it is 53%.
_UNASSISTED_AT_FIRST = frozenset("123")


def batter_became_runner(event: G.Event, batter_dest: str | None
                         ) -> tuple[bool, str]:
    """Whether the batter left the box, and how well determined that is.

    This is *not* the same as reaching safely, and the difference decides every
    force play. On `64(1)3/GDP/G6` the batter is retired at first, yet the force
    at second is real, because he was running while the throw was made. On
    `8(B)84(2)/LDP/L8` the liner was caught, the batter never ran, and the
    runner doubled off second was tagged rather than forced.

    Returns ``(became_runner, certainty)`` where certainty is
    :data:`FORCE_DERIVED`, :data:`FORCE_LIKELY`, or :data:`UNRESOLVED`.
    """
    if batter_dest is None:
        return False, FORCE_DERIVED
    if batter_dest != OUT:
        return True, FORCE_DERIVED

    # Retired on strikes without reaching: never became a runner.
    if any(isinstance(b, G.Strikeout) for b in event.basics):
        return False, FORCE_DERIVED

    trajectory = next((m.trajectory for m in event.modifiers
                       if m.kind == "hit" and m.trajectory), None)
    if trajectory in _CAUGHT:
        return False, FORCE_DERIVED
    if trajectory in _ON_GROUND:
        return True, FORCE_DERIVED
    codes = event.modifier_codes()
    if codes & {"FO", "GDP", "GTP", "BGDP", "SH"}:
        return True, FORCE_DERIVED
    if codes & {"SF", "FDP", "LDP", "LTP", "BPDP", "IF"}:
        return False, FORCE_DERIVED

    # Rule 5. Nothing in the string states the trajectory, so read it off the
    # fielding, which is geometry rather than statistics.
    group = _batter_out_group(event)
    if group is None:
        return False, FORCE_DERIVED
    if "99" in group.fielders:
        # An unknown play states nothing about the batter either.
        return False, UNRESOLVED
    if len(group.fielders) > 1:
        # A throw retired him, and a throw is only necessary if he ran. Likely
        # rather than derived: the notation cannot rule out a caught ball
        # relayed for some other reason.
        return True, FORCE_LIKELY
    if group.fielders not in _UNASSISTED_AT_FIRST:
        # Deductive: retiring a batter at first requires the ball at first, and
        # this fielder was not there. He caught it.
        return False, FORCE_DERIVED
    if _has_a_throw(event, group):
        # `64(1)3` -- the batter's own putout is unassisted, but the `64` is a
        # throw, so the ball was on the ground.
        return True, FORCE_LIKELY
    # An unassisted putout by the pitcher, catcher or first baseman. Either he
    # fielded a grounder and beat the batter to the bag, or he caught the ball
    # in the air, and the record does not say which. Claim no force.
    return False, UNRESOLVED


def _batter_out_group(event: G.Event) -> G.OutGroup | None:
    """The putout group that retired the batter, per §2's last-group rule."""
    for basic in event.basics:
        if not isinstance(basic, G.Out):
            continue
        for group in basic.groups:
            if group.runner == "B":
                return group
        if basic.groups and basic.groups[-1].runner is None:
            return basic.groups[-1]
    return None


def _has_a_throw(event: G.Event, exclude: G.OutGroup) -> bool:
    """Whether any *other* putout group on the play involved a throw."""
    return any(
        group is not exclude and len(group.fielders) > 1
        for basic in event.basics if isinstance(basic, G.Out)
        for group in basic.groups
    )


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

def apply_play(state: HalfInningState, event: G.Event, *,
               batter_id: str | None = None,
               batter_reached_on_strikes: bool = False
               ) -> tuple[HalfInningState, PlayOutcome]:
    """Apply one parsed event to the state, returning the new state and outcome.

    ``batter_id`` names the batter, so that a batter who reaches goes onto the
    bases as himself. Without it every runner is anonymous the moment he stops
    being the batter, and `runner_advances.runner_id` and
    `plays.runner_N_before` ([05-DATABASE](../../spec/05-DATABASE.md) §3) can
    never be filled -- so "who was forced" and "who scored" become
    unanswerable while the base *state* looks perfectly correct.

    ``batter_reached_on_strikes`` is set only by the retry described in §4.5
    step 1 and is not part of the public contract; callers pass one event and
    one state.
    """
    before = state.copy()
    out = PlayOutcome(
        bases_before=before.code(), outs_before=before.outs,
        runners_before=tuple(
            before.bases[b].player_id if before.bases[b] else None
            for b in BASES),
    )

    if any(isinstance(b, G.NoPlay) for b in event.basics) and len(event.basics) == 1:
        # `NP` is a marker only (§6.1). Nothing happened, so the batter did not
        # run -- said explicitly, because leaving `batter_ran` at its default
        # would file every substitution marker under 'unknown' and swamp the
        # one population the field exists to count.
        out.bases_after, out.outs_after = before.code(), before.outs
        out.batter_ran = "no"
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
    elif batter_reached_on_strikes:
        # §4.5 step 1: the out count proves he cannot have been retired on
        # strikes, so he reached first. Set below, after the first resolution.
        batter_dest = "1"
    else:
        batter_dest = implicit_dest
    batter_live = batter_dest not in (None, OUT)
    out.batter_dest = batter_dest
    out.batter_is_out = batter_dest == OUT

    # -- 2. force status, from the state entering the play (§4.1).
    # Keyed on whether the batter *became* a runner, not on whether he was safe.
    became_runner, batter_certainty = batter_became_runner(event, batter_dest)
    if batter_certainty == UNRESOLVED:
        out.batter_ran = "unknown"
    else:
        out.batter_ran = "yes" if became_runner else "no"
    forced_if_live = forced_bases(before, True)
    forced = forced_if_live if became_runner else set()

    def certainty(origin: str) -> str:
        """How well determined the force status of ``origin`` is (§4.4).

        Three cases, and the first is the common one. For a runner with an
        empty base behind them the answer is "not forced" however the batter
        was retired, so that is `derived`, not `n/a`: it is a deterministic
        finding, and recording it as "not applicable" would make a certain tag
        out indistinguishable from an unexamined one.

        Otherwise the answer turns on whether the batter ran, and inherits how
        well *that* was determined -- `derived` from a trajectory or a
        modifier, `likely` from the fielding (§4.2 rule 5). When it could not
        be determined at all, no force is claimed and the advance says
        `ambiguous` rather than asserting the negative.
        """
        if origin not in forced_if_live:
            return FORCE_DERIVED
        if batter_certainty == UNRESOLVED:
            return FORCE_AMBIGUOUS
        return batter_certainty

    # -- 3. resolve advances
    after = HalfInningState(outs=before.outs, bases={"1": None, "2": None, "3": None})
    moved: set[str] = set()
    outs_recorded = 0
    runs = 0

    for adv in event.advances:
        # The batter is not on a base yet, so he is not in `before.bases` --
        # but he has an identity, and an explicit `B-1` must carry it just as
        # the implicit form below does. Leaving it None made the same physical
        # fact identified or anonymous depending on whether the scorer wrote
        # the advance: `K.3XH(21);2-3;1-2;B-1` named all three runners and not
        # the batter.
        runner = (Runner(batter_id) if adv.origin == "B"
                  else before.bases.get(adv.origin))
        resolved = ResolvedAdvance(
            origin=adv.origin, dest=adv.dest, marked_out=adv.marked_out,
            is_out=adv.is_out, is_explicit=True, runner=runner, raw=adv.emit(),
        )
        if adv.origin != "B":
            moved.add(adv.origin)
        if adv.is_out:
            resolved.is_force = adv.origin in forced
            resolved.force_certainty = certainty(adv.origin)
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
            is_explicit=False, is_force=(is_out and origin in forced),
            force_certainty=(certainty(origin) if is_out else FORCE_NA),
            scored=(not is_out and dest == "H"),
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
            force_certainty=certainty(origin),
            runner=before.bases.get(origin), raw=f"({origin})",
        ))
        outs_recorded += 1

    # -- 4. runners not mentioned hold (§3 rule 4)
    for base in BASES:
        if base not in moved and before.bases[base] is not None:
            after.bases[base] = before.bases[base]

    # -- 5. the batter, if not placed by an explicit advance
    if batter_dest == OUT and b_adv is None:
        outs_recorded += 1
        if became_runner:
            # He ran and was retired, so he was retired at first, where the
            # batter-runner is always forced (§4.1). Recording it as an advance
            # is what lets `ForceOutAtFirst` and `runner_advances` see the
            # commonest force play in baseball; a caught fly reaches this
            # branch with `became_runner` false and correctly yields no row.
            out.advances.append(ResolvedAdvance(
                origin="B", dest="1", marked_out=True, is_out=True,
                is_explicit=False, is_force=True,
                force_certainty=batter_certainty,
                runner=Runner(batter_id), raw="(B)",
            ))
    elif batter_live and b_adv is None:
        # The batter's movement is an advance like any other, and it gets a row
        # whether or not the scorer wrote one. `D9` and `S9.B-2` put the batter
        # on second by different routes; storing only the explicit form makes
        # the same physical fact present or absent depending on notation, and
        # leaves every implicit run -- a home run above all -- scored by
        # nobody, so `runs_on_play` disagreed with the advances on 5,725 plays
        # of the 2000 season alone.
        if batter_dest == "H":
            runs += 1
        else:
            after.bases[batter_dest] = Runner(batter_id)
        out.advances.append(ResolvedAdvance(
            origin="B", dest=batter_dest, marked_out=False, is_out=False,
            is_explicit=False, force_certainty=FORCE_NA,
            scored=(batter_dest == "H"), runner=Runner(batter_id),
            raw=f"B-{batter_dest}",
        ))
    elif b_adv is not None and batter_live:
        # The advance loop above already counted this movement and, if it ended
        # at the plate, its run. Counting it again here double-counted the
        # batter on every home run written with an explicit `B-H` -- a solo
        # shot recorded as `HR/F7D+.B-H(UR)` scored two. Only the placement is
        # left to do, because the loop deliberately does not place the batter.
        if batter_dest != "H":
            after.bases[batter_dest] = Runner(batter_id)

    after.outs = before.outs + outs_recorded
    out.outs_recorded = outs_recorded
    out.outs_after = after.outs
    out.runs_on_play = runs
    out.bases_after = after.code()

    if not batter_reached_on_strikes and _overflows_on_a_bare_strikeout(
            out, event, b_adv):
        # §4.5 step 1. `K.3XH(21)` -- the bare form of the motivating play --
        # names no fielder after the `K`, joins no `WP`/`PB`/`E$`, and gives
        # the batter no explicit advance, so §2 rule 2 reads him as retired on
        # strikes. With two outs and a runner retired at home that totals
        # four, which is impossible; therefore the batter-runner was live.
        #
        # This is arithmetic, not a heuristic: it fires only when the
        # alternative reading is *impossible*, and only when adopting it makes
        # the play consistent. If the retry is inconsistent too, the original
        # reading is kept so the inconsistency is reported rather than moved.
        retry_after, retry = apply_play(state, event, batter_id=batter_id,
                                        batter_reached_on_strikes=True)
        if retry.outs_after <= 3:
            retry.notes.append(
                "batter-runner inferred live: retiring him on strikes would "
                f"make {retry.outs_after + 1} outs (§4.5 step 1)")
            return retry_after, retry

    if batter_certainty == UNRESOLVED and out.batter_is_out:
        # §4.2 rule 5, the case the fielding cannot settle: an unassisted
        # putout by the pitcher, catcher or first baseman, or an unknown play.
        # Either he fielded a grounder and beat the batter to the bag, or he
        # caught the ball in the air. No force is claimed on the batter or on
        # any runner behind him, and `batter_ran` says why -- a note, not a
        # `parse_status`, so the play stays in default results for every
        # question that does not depend on it.
        out.notes.append(
            "whether the batter ran is not recoverable: an unassisted putout "
            "by the pitcher, catcher or first baseman with no trajectory "
            "given, so no force is derived")

    _check(out, before, after, event, batter_live)
    return after, out


def _overflows_on_a_bare_strikeout(out: PlayOutcome, event: G.Event,
                                   b_adv: G.Advance | None) -> bool:
    """Whether §4.5 step 1's out-count inference applies.

    Restricted to the one case where the reading is forced: a strikeout with
    nothing in the event describing the batter's fate, whose out count is
    impossible. Anything else that overflows is a genuine inconsistency.
    """
    if out.outs_after <= 3 or b_adv is not None:
        return False
    if not out.batter_is_out:
        return False
    basic = event.groups[0][0] if event.groups and event.groups[0] else None
    return isinstance(basic, G.Strikeout) and not basic.groups


def _check(out: PlayOutcome, before: HalfInningState, after: HalfInningState,
           event: G.Event, batter_live: bool) -> None:
    """Consistency checks (§3.1). Validation only -- never inference."""
    if out.outs_after > 3:
        out.flag(ParseStatus.INCONSISTENT,
                 f"{before.outs} outs before + {out.outs_recorded} recorded = "
                 f"{out.outs_after}")

    # A run needs a runner. Nothing checked this until the derived tables made
    # it expressible, and it found the batter being counted twice on every home
    # run written with an explicit `B-H` -- a solo shot scoring two. The
    # out-accounting invariant cannot see runs, and score reconciliation needs
    # game logs the project does not hold, so this is the only run check
    # available from event files alone.
    runners_on = before.code().count("1")
    if out.runs_on_play > runners_on + 1:
        out.flag(ParseStatus.INCONSISTENT,
                 f"{out.runs_on_play} runs with {runners_on} on base and one "
                 "batter")
    scored = sum(1 for a in out.advances if a.scored)
    if out.runs_on_play != scored:
        out.flag(ParseStatus.INCONSISTENT,
                 f"{out.runs_on_play} runs but {scored} scoring advances")
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
