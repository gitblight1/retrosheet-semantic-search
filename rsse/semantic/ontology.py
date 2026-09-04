"""The semantic ontology: one derivation rule per tag (spec/04-ONTOLOGY.md).

Tags are the query surface. The original specs listed tag *names*; this module
is where each name becomes a rule over the syntax tree
(spec/02-GRAMMAR.md) and the reconstructed game state (spec/03-STATE.md).

The organising principle of §1 is enforced structurally rather than by
convention:

* **A tag without a rule is not part of the ontology.** A tag exists only as a
  :class:`TagDef` in :data:`REGISTRY`, and a ``TagDef`` cannot be built without
  a callable. `.dropped_third()` cannot be implemented from a name, so a name
  alone cannot be registered.
* **Total and deterministic.** Every rule is a pure function of one
  :class:`PlayFacts`. Rules never read the clock, the database, or each other.
* **Versioned.** :func:`rule_hash` hashes each rule's own source, so changing a
  derivation changes its hash whether or not anyone remembers to say so, and
  :data:`ONTOLOGY_VERSION` is what a stored result cites.
* **Additive.** Every rule whose predicate holds contributes its tag. Tags are
  not mutually exclusive, and composition -- a walk-off sacrifice fly is
  `WalkOff` + `SacrificeFly` -- is why.

Implication and aliasing are declared on the ``TagDef`` rather than repeated
inside rules, so `GroundRuleDouble` need not restate what makes a `Double`.
Confidence is decided in :mod:`rsse.semantic.derive`, from the three stated
sources of ambiguity in §1.4 and nothing else.
"""

from __future__ import annotations

import hashlib
import inspect
import textwrap
from dataclasses import dataclass
from typing import Callable

from ..model.state import (OUT, CreditSeq, PlayContext, PlayOutcome,
                           ResolvedAdvance, credit_sequences)
from ..parser import grammar as G

#: Bumped whenever any rule changes. Results record the version they were
#: computed under (§1.3), so a re-derive under a new version is detectable
#: rather than silently mixed with the old one.
ONTOLOGY_VERSION = "1.0.0"

CERTAIN = "certain"
UNCERTAIN = "uncertain"


# ---------------------------------------------------------------------------
# facts: the vocabulary the rules are written against
# ---------------------------------------------------------------------------

class PlayFacts:
    """Everything a rule may look at, computed once per play.

    Rules stay one-liners because the awkward parts of the grammar are resolved
    here exactly once: which basic event describes the *batter*, which modifier
    codes are present, and where the credit sequences are. A rule that reached
    into the syntax tree itself would be re-deriving those, differently.
    """

    __slots__ = ("event", "outcome", "context", "trivia", "basics",
                 "first_basic", "trailing", "codes", "trajectory",
                 "advances", "seqs", "adv_flags", "running")

    def __init__(self, event: G.Event, outcome: PlayOutcome,
                 context: PlayContext | None = None,
                 trivia: tuple = ()) -> None:
        self.event = event
        self.outcome = outcome
        self.context = context or PlayContext()
        self.trivia = trivia

        self.basics = event.basics
        # Only the *first* basic event describes the batter (spec/03 §2): in
        # `K+E2` the error belongs to a runner's advance and the batter is
        # still out on strikes.
        self.first_basic = self.basics[0] if self.basics else None
        self.trailing = self.basics[1:]

        self.codes = frozenset(event.modifier_codes())
        hit = next((m for m in event.modifiers if m.kind == "hit"), None)
        self.trajectory = hit.trajectory if hit else None
        # The hit *location* is deliberately not lifted here: no tag keys on
        # it, and `.hit_location()` is a column filter on `plays`
        # (spec/06-QUERY.md §3), not a tag join.

        self.advances = outcome.advances
        self.seqs = credit_sequences(event)
        self.adv_flags = frozenset(
            p.text for a in event.advances for p in a.params
            if isinstance(p, G.Flag))
        self.running = tuple(b for b in self.basics
                             if isinstance(b, G.BaseRunning))

    # -- basic events -------------------------------------------------------

    def batter_event_is(self, *types) -> bool:
        """Whether the event describing the batter is one of ``types``."""
        return isinstance(self.first_basic, types)

    def hit_kind(self, kind: str) -> bool:
        return isinstance(self.first_basic, G.Hit) and self.first_basic.kind == kind

    # -- base-running events ------------------------------------------------

    def running_events(self, code: str) -> tuple[G.BaseRunning, ...]:
        return tuple(b for b in self.running if b.code == code)

    def has_running(self, code: str) -> bool:
        return bool(self.running_events(code))

    @staticmethod
    def negated(basic: G.BaseRunning) -> bool:
        """Whether an error in the event's own parameters negated its out."""
        return any(isinstance(p, G.CreditSequence) and p.has_error
                   for p in basic.params)

    def running_out_stands(self, code: str) -> bool:
        return any(not self.negated(b) for b in self.running_events(code))

    def running_out_negated(self, code: str) -> bool:
        return any(self.negated(b) for b in self.running_events(code))

    # -- modifiers ----------------------------------------------------------

    def error_modifier_on(self, fielder: str) -> bool:
        return any(m.kind == "error" and m.fielders == fielder
                   for m in self.event.modifiers)

    @property
    def has_throw_modifier(self) -> bool:
        """A `/TH` anywhere: a play modifier, or inside a credit sequence."""
        if any(m.kind == "throw" for m in self.event.modifiers):
            return True
        return any(m.kind == "throw"
                   for a in self.event.advances for p in a.params
                   if isinstance(p, G.CreditSequence) for m in p.modifiers)

    @property
    def relay(self) -> bool:
        return any(m.kind == "coverage" and m.relay_fielders
                   for m in self.event.modifiers)

    # -- outs and advances --------------------------------------------------

    @property
    def outs_on_play(self) -> int:
        return self.outcome.outs_recorded

    @property
    def batter_live(self) -> bool:
        """The batter-runner reached and is live (spec/03 §2)."""
        return self.outcome.batter_dest not in (None, OUT)

    def outs(self) -> list[ResolvedAdvance]:
        return [a for a in self.advances if a.is_out]

    def force_outs(self, at: str | None = None) -> list[ResolvedAdvance]:
        return [a for a in self.outs()
                if a.is_force and (at is None or a.dest == at)]

    def tag_outs(self, at: str | None = None) -> list[ResolvedAdvance]:
        return [a for a in self.outs()
                if not a.is_force and (at is None or a.dest == at)]

    @property
    def runner_out_in_advances(self) -> bool:
        """A runner -- not the batter -- retired in the advance section."""
        return any(a.is_out and a.is_explicit and a.origin != "B"
                   for a in self.advances)

    # -- credit sequences ---------------------------------------------------

    def any_seq(self, predicate: Callable[[CreditSeq], bool]) -> bool:
        return any(predicate(s) for s in self.seqs)

    # -- context ------------------------------------------------------------

    @property
    def runners_on(self) -> int:
        return self.outcome.bases_before.count("1")

    @property
    def base_occupied_before(self) -> tuple[bool, bool, bool]:
        b = self.outcome.bases_before
        return b[0] == "1", b[1] == "1", b[2] == "1"


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TagDef:
    name: str
    category: str
    #: The derivation. Required: this is what makes the tag part of the
    #: ontology rather than a name in a document (§1, opening).
    rule: Callable[[PlayFacts], bool]
    #: Section of spec/04-ONTOLOGY.md the rule implements.
    spec: str
    #: Tags that fire whenever this one does. Declared rather than restated in
    #: the rule, so `GroundRuleDouble` need not re-derive `Double`.
    implies: tuple[str, ...] = ()
    #: A deprecated name kept for compatibility; fires with its target.
    alias_of: str | None = None
    #: Never `certain`, whatever the rest of the play looks like.
    always_uncertain: bool = False
    #: Derived from the state *entering* the play, or from the shape of the
    #: game -- not from this play's own encoding. Such a tag is immune to the
    #: play-wide uncertainty of a `#` or a `99`: the base/out state a batter
    #: walked into was established by earlier plays, and a questionable
    #: record of what he then did says nothing about it.
    prior_state: bool = False
    #: True for tags no rule can derive: they exist so a curated annotation
    #: has a name to attach to (§8).
    curated_only: bool = False
    note: str = ""

    @property
    def rule_hash(self) -> str:
        return rule_hash(self.rule)


REGISTRY: dict[str, TagDef] = {}


def tag(name: str, category: str, spec: str, **kwargs):
    """Register a tag's derivation rule."""
    def register(fn: Callable[[PlayFacts], bool]) -> Callable:
        if name in REGISTRY:
            raise ValueError(f"tag {name!r} registered twice")
        REGISTRY[name] = TagDef(name, category, fn, spec, **kwargs)
        return fn
    return register


def rule_hash(fn: Callable) -> str:
    """A hash of the derivation itself (§1.3).

    Hashing the rule's source rather than a hand-maintained version string
    means a changed derivation changes its hash whether or not anyone
    remembered to bump anything. Truncated to 16 hex digits: this identifies a
    revision, it is not a security boundary.
    """
    src = textwrap.dedent(inspect.getsource(fn))
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]


def ontology_hash() -> str:
    """A single digest over every rule, for stamping a derive run."""
    h = hashlib.sha256(ONTOLOGY_VERSION.encode("utf-8"))
    for name in sorted(REGISTRY):
        h.update(name.encode("utf-8"))
        h.update(REGISTRY[name].rule_hash.encode("utf-8"))
    return h.hexdigest()[:16]


# ===========================================================================
# 2. Batting
# ===========================================================================

@tag("Single", "batting", "§2")
def _single(f: PlayFacts) -> bool:
    return f.hit_kind("S")


@tag("Double", "batting", "§2")
def _double(f: PlayFacts) -> bool:
    return f.hit_kind("D")


@tag("Triple", "batting", "§2")
def _triple(f: PlayFacts) -> bool:
    return f.hit_kind("T")


@tag("GroundRuleDouble", "batting", "§2", implies=("Double",))
def _ground_rule_double(f: PlayFacts) -> bool:
    return f.batter_event_is(G.GroundRuleDouble)


@tag("HomeRun", "batting", "§2")
def _home_run(f: PlayFacts) -> bool:
    return f.batter_event_is(G.HomeRun)


@tag("InsideTheParkHomeRun", "batting", "§2", implies=("HomeRun",))
def _inside_the_park(f: PlayFacts) -> bool:
    """A home run naming a fielder, or carrying `/IPHR`.

    A fielder on `H`/`HR` means someone handled the ball, which on a home run
    can only mean it stayed in the park.
    """
    named = f.batter_event_is(G.HomeRun) and bool(f.first_basic.fielders)
    return named or "IPHR" in f.codes


@tag("Walk", "batting", "§2")
def _walk(f: PlayFacts) -> bool:
    return f.batter_event_is(G.Walk)


@tag("IntentionalWalk", "batting", "§2", implies=("Walk",))
def _intentional_walk(f: PlayFacts) -> bool:
    return f.batter_event_is(G.Walk) and f.first_basic.intentional


@tag("HitByPitch", "batting", "§2")
def _hit_by_pitch(f: PlayFacts) -> bool:
    return f.batter_event_is(G.HitByPitch)


@tag("Strikeout", "batting", "§2")
def _strikeout(f: PlayFacts) -> bool:
    """The batter struck out.

    Keyed on the batter's own event, so `K+WP` and `K+E2` are strikeouts and
    `SB2` on a play whose batter did something else is not.
    """
    return f.batter_event_is(G.Strikeout)


@tag("ReachedOnError", "batting", "§2")
def _reached_on_error(f: PlayFacts) -> bool:
    return f.batter_event_is(G.ReachedOnError)


@tag("FieldersChoice", "batting", "§2")
def _fielders_choice(f: PlayFacts) -> bool:
    return f.batter_event_is(G.FieldersChoice)


@tag("SacrificeFly", "batting", "§2")
def _sacrifice_fly(f: PlayFacts) -> bool:
    return "SF" in f.codes


@tag("SacrificeHit", "batting", "§2")
def _sacrifice_hit(f: PlayFacts) -> bool:
    return "SH" in f.codes


@tag("GroundOut", "batting", "§2")
def _ground_out(f: PlayFacts) -> bool:
    return f.batter_event_is(G.Out) and f.trajectory in ("G", "BG")


@tag("FlyOut", "batting", "§2")
def _fly_out(f: PlayFacts) -> bool:
    return f.batter_event_is(G.Out) and f.trajectory == "F"


@tag("LineOut", "batting", "§2")
def _line_out(f: PlayFacts) -> bool:
    return f.batter_event_is(G.Out) and f.trajectory in ("L", "BL")


@tag("PopOut", "batting", "§2")
def _pop_out(f: PlayFacts) -> bool:
    return f.batter_event_is(G.Out) and f.trajectory in ("P", "BP")


@tag("Bunt", "batting", "§2",
     note="Includes the undocumented plain `/B` (spec/02-GRAMMAR.md §4.1), "
          "which generalises BG/BP/BL and is the only bunt marker in much of "
          "the pre-1961 corpus.")
def _bunt(f: PlayFacts) -> bool:
    return f.trajectory in ("BG", "BP", "BL", "B")


@tag("InfieldFly", "batting", "§2")
def _infield_fly(f: PlayFacts) -> bool:
    return "IF" in f.codes


# ===========================================================================
# 3. Strikeout family
# ===========================================================================

#: Events that, joined to a `K` with `+`, say the ball was not cleanly caught.
_MISCUE_CODES = ("WP", "PB")


@tag("UncaughtThirdStrike", "strikeout", "§3.1")
def _uncaught_third_strike(f: PlayFacts) -> bool:
    """The third strike was not cleanly caught.

    Retrosheet has no dropped-third-strike token, so this is the disjunction of
    the four things it *does* record (§3.1):

    a. fielder digits after the `K` -- `K23`, `K13`;
    b. a `+`-joined `WP`, `PB` or `E$` -- `K+WP`, `K+PB`, `K+E2`;
    c. a live batter-runner -- `K.B-1`;
    d. a `(WP)` or `(PB)` parameter on an advance -- `K.1-2(WP)`.

    Trigger (c) is keyed on the batter-runner being **live**, as the state
    machine determined it, not on a `B-%` advance being written. The two differ
    exactly on `K.3XH(21)`, the bare form of the motivating play, where the
    batter's advance is unwritten and derived from the out count
    (spec/03-STATE.md §4.5 step 1). Keying on the written form makes the three
    encodings of that one play carry different tags, which is the failure mode
    the gold corpus's `equivalents` list exists to catch
    (spec/07-TESTING.md §2.1).

    This is not the widening §3.1.1 forbids. A live batter-runner on a
    strikeout is the rulebook's own definition of an uncaught third strike --
    on a caught third strike the batter is out, with no exception -- so the tag
    still asserts only what the record supports.

    The list is exhaustive over what Retrosheet records, which is **not** the
    same as what happened (§3.1.1): if the batter is retired on strikes and the
    miscue draws no wild pitch, passed ball or error, the event string carries
    no trace of it and this must not fire. That is why the broad-class query is
    `Strikeout`, not this tag.

    A rule keyed on "`K` with an `X` advance" is wrong: in `K.1X2(26)` the `X`
    belongs to a runner caught stealing and the third strike was clean.
    """
    if not f.batter_event_is(G.Strikeout):
        return False
    if f.first_basic.groups:                                        # (a)
        return True
    for basic in f.trailing:                                        # (b)
        if isinstance(basic, G.ReachedOnError):
            return True
        if isinstance(basic, G.BaseRunning) and basic.code in _MISCUE_CODES:
            return True
    if f.batter_live:                                               # (c)
        return True
    return bool(f.adv_flags & frozenset(_MISCUE_CODES))             # (d)


@tag("BatterReachedOnK", "strikeout", "§3.1", implies=("UncaughtThirdStrike",))
def _batter_reached_on_k(f: PlayFacts) -> bool:
    """Uncaught, *and* the batter-runner is live.

    Two tags rather than one because the distinction is the query: `K23` is
    uncaught with the batter thrown out at first, and `K+WP.2-3` is uncaught
    with the batter still out because first base was occupied with fewer than
    two outs. Only `K.B-1` fires both.
    """
    return _uncaught_third_strike(f) and f.batter_live


@tag("DroppedThirdStrike", "strikeout", "§3.1",
     alias_of="UncaughtThirdStrike",
     note="Deprecated. Kept because the original spec and `.dropped_third()` "
          "use it; 'dropped' names one way the ball can get away.")
def _dropped_third_strike(f: PlayFacts) -> bool:
    return _uncaught_third_strike(f)


@tag("StrikeoutDoublePlay", "strikeout", "§3.2",
     note="`/NDP` suppresses this for the same reason it suppresses "
          "DoublePlay (§5): it states that no double play was credited, and "
          "it is present precisely because two outs were recorded.")
def _strikeout_double_play(f: PlayFacts) -> bool:
    if not f.batter_event_is(G.Strikeout) or "NDP" in f.codes:
        return False
    return "DP" in f.codes or f.outs_on_play >= 2


@tag("CalledThirdStrike", "strikeout", "§3.2")
def _called_third_strike(f: PlayFacts) -> bool:
    return "C" in f.codes


@tag("StrikeoutThrowOut", "strikeout", "§3.2")
def _strikeout_throw_out(f: PlayFacts) -> bool:
    return f.batter_event_is(G.Strikeout) and f.runner_out_in_advances


# ===========================================================================
# 4. Base running
# ===========================================================================

@tag("StolenBase", "baserunning", "§4",
     note="One tag per play, not per steal: `play_tags` is keyed "
          "(play_id, tag_id). `SB3;SB2` is one StolenBase plus DoubleSteal; "
          "which bases were taken is a `runner_advances` question.")
def _stolen_base(f: PlayFacts) -> bool:
    return f.has_running("SB")


@tag("DoubleSteal", "baserunning", "§4", implies=("StolenBase",))
def _double_steal(f: PlayFacts) -> bool:
    return len(f.running_events("SB")) >= 2


@tag("CaughtStealing", "baserunning", "§4")
def _caught_stealing(f: PlayFacts) -> bool:
    """`CS%` with the out **not** negated by an error in the credit sequence.

    `CS2(2E4)` is written as a caught stealing and is not one: the runner is
    safe. Tagging it `CaughtStealing` would count a steal as its opposite.
    """
    return f.running_out_stands("CS")


@tag("CaughtStealingSafeOnError", "baserunning", "§4")
def _caught_stealing_safe_on_error(f: PlayFacts) -> bool:
    return f.running_out_negated("CS")


@tag("Pickoff", "baserunning", "§4")
def _pickoff(f: PlayFacts) -> bool:
    return f.running_out_stands("PO")


@tag("PickoffError", "baserunning", "§4")
def _pickoff_error(f: PlayFacts) -> bool:
    return f.running_out_negated("PO")


@tag("PickoffCaughtStealing", "baserunning", "§4")
def _pickoff_caught_stealing(f: PlayFacts) -> bool:
    return f.has_running("POCS")


@tag("DefensiveIndifference", "baserunning", "§4")
def _defensive_indifference(f: PlayFacts) -> bool:
    return f.has_running("DI")


@tag("WildPitch", "baserunning", "§4")
def _wild_pitch(f: PlayFacts) -> bool:
    """A `WP` event, or a `(WP)` charged against an individual advance.

    Both encodings exist and mean the same thing happened, so a query for wild
    pitches must reach both -- which is the whole argument for querying tags
    rather than event strings.
    """
    return f.has_running("WP") or "WP" in f.adv_flags


@tag("PassedBall", "baserunning", "§4")
def _passed_ball(f: PlayFacts) -> bool:
    return f.has_running("PB") or "PB" in f.adv_flags


@tag("Balk", "baserunning", "§4")
def _balk(f: PlayFacts) -> bool:
    return f.has_running("BK")


@tag("OtherAdvance", "baserunning", "§4")
def _other_advance(f: PlayFacts) -> bool:
    return f.has_running("OA")


@tag("RunnerPassedRunner", "baserunning", "§4")
def _runner_passed_runner(f: PlayFacts) -> bool:
    return "PASS" in f.codes


@tag("RunnerHitByBattedBall", "baserunning", "§4")
def _runner_hit_by_batted_ball(f: PlayFacts) -> bool:
    return "BR" in f.codes


# ===========================================================================
# 5. Outs and force plays
# ===========================================================================

@tag("ForceOut", "outs", "§5",
     note="Deliberately not keyed on `/FO`, which marks batted-ball force "
          "outs only and so misses `K.3XH(21)` -- the play that motivated "
          "the project.")
def _force_out(f: PlayFacts) -> bool:
    """Any out whose runner is forced per spec/03-STATE.md §4.

    Force status is derived, never read: Retrosheet does not record it. The
    derivation is keyed on whether the batter *became a runner*, which is not
    the same as reaching safely -- on `64(1)3/GDP/G6` the batter is retired at
    first and the force at second is still real.
    """
    return bool(f.force_outs())


@tag("ForceOutAtHome", "outs", "§5", implies=("ForceOut",))
def _force_out_at_home(f: PlayFacts) -> bool:
    return bool(f.force_outs("H"))


@tag("ForceOutAtThird", "outs", "§5", implies=("ForceOut",))
def _force_out_at_third(f: PlayFacts) -> bool:
    return bool(f.force_outs("3"))


@tag("ForceOutAtSecond", "outs", "§5", implies=("ForceOut",))
def _force_out_at_second(f: PlayFacts) -> bool:
    return bool(f.force_outs("2"))


@tag("ForceOutAtFirst", "outs", "§5", implies=("ForceOut",),
     note="Fires on every batter retired at first while running, which is "
          "most ground outs -- the batter-runner is always forced at first "
          "(§4.1). Selectivity lives in the `At...` variants, not here.")
def _force_out_at_first(f: PlayFacts) -> bool:
    return bool(f.force_outs("1"))


@tag("TagOut", "outs", "§5")
def _tag_out(f: PlayFacts) -> bool:
    """An out that is not a force.

    Strikeouts and caught flies are excluded structurally rather than by a
    clause: neither produces a runner-advance row, because nobody was running.
    """
    return bool(f.tag_outs())


@tag("AppealOut", "outs", "§5")
def _appeal_out(f: PlayFacts) -> bool:
    return "AP" in f.codes


_DP_CODES = frozenset({"DP", "GDP", "LDP", "FDP", "BGDP", "BPDP"})
_TP_CODES = frozenset({"TP", "GTP", "LTP"})


@tag("DoublePlay", "outs", "§5")
def _double_play(f: PlayFacts) -> bool:
    """Two outs on the play, **unless** `/NDP` says none was credited.

    The exception is not a nicety. `K/NDP.3XH(21)` records two outs and is not
    a double play; a rule keyed on the out count alone tags it wrongly, and
    that record is the anchor for the whole force derivation
    (spec/07-TESTING.md §2.2).
    """
    if "NDP" in f.codes:
        return False
    return f.outs_on_play >= 2 or bool(f.codes & _DP_CODES)


@tag("TriplePlay", "outs", "§5", implies=("DoublePlay",))
def _triple_play(f: PlayFacts) -> bool:
    return f.outs_on_play >= 3 or bool(f.codes & _TP_CODES)


@tag("UnassistedOut", "outs", "§5",
     note="Literally 'a putout with an empty assist list', so it fires on "
          "every caught fly as well as on the unassisted double play. The "
          "interesting queries pair it with DoublePlay or TriplePlay.")
def _unassisted_out(f: PlayFacts) -> bool:
    return f.any_seq(lambda s: s.records_out and s.putout is not None
                     and not s.assists)


@tag("Rundown", "outs", "§5")
def _rundown(f: PlayFacts) -> bool:
    """Three or more credit atoms with a fielder handling the ball twice.

    The repeat is what distinguishes a rundown from a long relay: in `1X2(3625)`
    nobody repeats, while `2(3)6252` has the shortstop and catcher trading
    throws.
    """
    return f.any_seq(lambda s: len(s.credited) >= 3 and s.repeats_a_fielder)


@tag("RelayThrow", "outs", "§5")
def _relay_throw(f: PlayFacts) -> bool:
    return f.relay


# ===========================================================================
# 6. Special
# ===========================================================================

@tag("CatcherInterference", "special", "§6")
def _catcher_interference(f: PlayFacts) -> bool:
    return f.batter_event_is(G.Interference) and f.error_modifier_on("2")


@tag("PitcherInterference", "special", "§6")
def _pitcher_interference(f: PlayFacts) -> bool:
    return f.batter_event_is(G.Interference) and f.error_modifier_on("1")


@tag("FirstBaseInterference", "special", "§6")
def _first_base_interference(f: PlayFacts) -> bool:
    return f.batter_event_is(G.Interference) and f.error_modifier_on("3")


@tag("BatterInterference", "special", "§6")
def _batter_interference(f: PlayFacts) -> bool:
    return "BINT" in f.codes


@tag("RunnerInterference", "special", "§6")
def _runner_interference(f: PlayFacts) -> bool:
    return "RINT" in f.codes


@tag("UmpireInterference", "special", "§6")
def _umpire_interference(f: PlayFacts) -> bool:
    return "UINT" in f.codes


@tag("FanInterference", "special", "§6")
def _fan_interference(f: PlayFacts) -> bool:
    return "FINT" in f.codes


@tag("Obstruction", "special", "§6")
def _obstruction(f: PlayFacts) -> bool:
    return "OBS" in f.codes


@tag("BattingOutOfTurn", "special", "§6")
def _batting_out_of_turn(f: PlayFacts) -> bool:
    """`/BOOT`, or a preceding `ladj`.

    The `ladj` case comes from the context rather than the event: the record
    that says the lineup was out of order is a different record.
    """
    return "BOOT" in f.codes or f.context.after_ladj


@tag("CourtesyRunner", "special", "§6")
def _courtesy_runner(f: PlayFacts) -> bool:
    return "COUR" in f.codes


@tag("CourtesyBatter", "special", "§6")
def _courtesy_batter(f: PlayFacts) -> bool:
    return "COUB" in f.codes


@tag("CourtesyFielder", "special", "§6")
def _courtesy_fielder(f: PlayFacts) -> bool:
    return "COUF" in f.codes


@tag("FoulFlyError", "special", "§6")
def _foul_fly_error(f: PlayFacts) -> bool:
    return f.batter_event_is(G.FoulFlyError)


@tag("ErrorOnThrow", "special", "§6")
def _error_on_throw(f: PlayFacts) -> bool:
    return f.any_seq(lambda s: s.has_error) and f.has_throw_modifier


@tag("UnknownPlay", "special", "§6", always_uncertain=True)
def _unknown_play(f: PlayFacts) -> bool:
    """`99` -- the scorer recorded that the play is not known.

    Always uncertain: the tag records the absence of information, so nothing
    derived from the play can be certain.
    """
    return any(isinstance(b, G.Out) and b.is_unknown for b in f.basics)


@tag("ReplayReviewed", "special", "§6")
def _replay_reviewed(f: PlayFacts) -> bool:
    return bool(f.codes & {"UREV", "MREV"})


@tag("ReplayOverturned", "special", "§6", implies=("ReplayReviewed",))
def _replay_overturned(f: PlayFacts) -> bool:
    """Reviewed, and the linked `com` record says the call was reversed.

    Whether it was reversed is not in the event string, so this cannot fire
    unless the comment was linked to the play. An unlinked review is
    `ReplayReviewed` and nothing more -- which is correct: we do not know.
    """
    return _replay_reviewed(f) and f.context.replay_reversed is True


@tag("PlacedRunner", "special", "§6")
def _placed_runner(f: PlayFacts) -> bool:
    """A runner who started the half-inning on second under the 2020+ rule."""
    return f.context.has_placed_runner or any(
        a.runner is not None and a.runner.placed for a in f.advances)


@tag("HiddenBallTrick", "special", "§8", curated_only=True,
     note="No Retrosheet encoding exists: it appears in prose `com` records "
          "only. The original spec listed it beside derivable tags, which is "
          "not implementable. It is here so a curated annotation has a name.")
def _hidden_ball_trick(f: PlayFacts) -> bool:
    return False


# ===========================================================================
# 7. Context
# ===========================================================================

@tag("BasesLoaded", "context", "§7", prior_state=True)
def _bases_loaded(f: PlayFacts) -> bool:
    return f.outcome.bases_before == "111"


@tag("BasesEmpty", "context", "§7", prior_state=True)
def _bases_empty(f: PlayFacts) -> bool:
    return f.outcome.bases_before == "000"


@tag("RunnerOnThird", "context", "§7", prior_state=True)
def _runner_on_third(f: PlayFacts) -> bool:
    return f.base_occupied_before[2]


@tag("ScoringPosition", "context", "§7", prior_state=True)
def _scoring_position(f: PlayFacts) -> bool:
    _first, second, third = f.base_occupied_before
    return second or third


@tag("NoOuts", "context", "§7", prior_state=True)
def _no_outs(f: PlayFacts) -> bool:
    return f.outcome.outs_before == 0


@tag("OneOut", "context", "§7", prior_state=True)
def _one_out(f: PlayFacts) -> bool:
    return f.outcome.outs_before == 1


@tag("TwoOuts", "context", "§7", prior_state=True)
def _two_outs(f: PlayFacts) -> bool:
    return f.outcome.outs_before == 2


@tag("InningEnding", "context", "§7")
def _inning_ending(f: PlayFacts) -> bool:
    return f.outcome.is_inning_ending


@tag("GoAheadRun", "context", "§7")
def _go_ahead_run(f: PlayFacts) -> bool:
    return f.context.is_go_ahead


@tag("WalkOff", "context", "§7",
     note="Composes rather than specialising: a walk-off sacrifice fly is "
          "WalkOff + SacrificeFly. Composition is why tags are additive.")
def _walk_off(f: PlayFacts) -> bool:
    return f.context.is_walkoff


@tag("FinalPlay", "context", "§7", prior_state=True)
def _final_play(f: PlayFacts) -> bool:
    return f.context.is_final_play


@tag("ExtraInnings", "context", "§7", prior_state=True)
def _extra_innings(f: PlayFacts) -> bool:
    return f.context.extra_innings


@tag("LateAndClose", "context", "§7", prior_state=True,
     note="The original specs named this tag without defining it. The rule "
          "below is the definition; it is stated here so a result can be "
          "audited rather than guessed at.")
def _late_and_close(f: PlayFacts) -> bool:
    """Seventh inning or later, with the game within reach of the batting team.

    Stated as a rule, since 'close' has no single conventional meaning:
    the batting team is tied, ahead by one, or trailing by no more than the
    number of runners on base plus one -- that is, the tying run is on base or
    at the plate.
    """
    if f.context.inning < 7:
        return False
    diff = f.context.score_diff_before
    if diff > 1:
        return False
    return diff >= 0 or -diff <= f.runners_on + 1


# ---------------------------------------------------------------------------
# derived views over the registry
# ---------------------------------------------------------------------------

def categories() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, td in REGISTRY.items():
        out.setdefault(td.category, []).append(name)
    return {k: sorted(v) for k, v in sorted(out.items())}


def derivable() -> list[TagDef]:
    """Tags a rule can produce -- everything but the curated-only names."""
    return [td for td in REGISTRY.values() if not td.curated_only]


def aliases() -> dict[str, str]:
    return {n: td.alias_of for n, td in REGISTRY.items() if td.alias_of}
