"""AST for the Retrosheet event grammar.

Implements the node types of `spec/02-GRAMMAR.md` §2. Every node emits from its
structured fields, never from a retained source slice -- that is what makes the
round-trip gate (§6) a real test of the grammar rather than a tautology.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FIELDERS = "123456789U"
RUNNERS = "B123H"
ADV_BASES = "123H"

#: Advance parameters that are flags rather than fielding credits (§5).
#: `SB%` joins `WP`/`PB` as an event named in an advance rather than the basic
#: play, e.g. `BK.2-3(SB3)`.
ADV_FLAGS = frozenset({"UR", "TUR", "RBI", "NR", "NORBI", "WP", "PB"})
ADV_FLAG_STOLEN_BASE = tuple(f"SB{b}" for b in "123H")

#: Modifier codes carrying no argument (§4).
#:
#: `BF` and `S` are absent from Retrosheet's published list but occur in the
#: corpus, always on a strikeout: `BF` a foul bunt, `S` a swinging third strike
#: (the complement of the documented `C`). See spec/02-GRAMMAR.md §4.1.
#:
#: **Trajectories are deliberately not here.** `G`, `F`, `L`, `P`, `BG`, `BP`
#: and `BL` look like no-argument codes when they appear bare, but the EBNF
#: production is `trajectory [ location ]` -- the location is *optional*, and
#: `/G` and `/G6` state the same fact. Listing them here matched the bare form
#: as a named code and only the located form as a trajectory, so every
#: consumer reading `Modifier.trajectory` silently missed `/G`: ground outs
#: went untagged and the force derivation fell back on its one inference
#: (spec/03-STATE.md §4.2 rule 5) on a third of the corpus.
#:
#: The longer codes that *start* with a trajectory letter -- `FL`, `FO`, `FDP`,
#: `GDP`, `GTP`, `LDP`, `LTP`, `PASS`, `BGDP`, `BPDP` -- must stay, and are
#: matched here before the trajectory production is tried.
NAMED_MODIFIERS = frozenset(
    """AP BF BGDP BINT BOOT BPDP BR C COUB COUF COUR DP FDP FINT FL
    FO GDP GTP IF INT IPHR LDP LTP MREV NDP OBS PASS RINT S SF SH TP UINT
    UREV""".split()
)

#: Longest first: `B` (bunt, unspecified) must not shadow `BG`/`BP`/`BL`.
TRAJECTORIES = ("BG", "BP", "BL", "B", "G", "L", "P", "F")
LOC_QUALIFIERS = "LMRSDXFW"


class Node:
    def emit(self) -> str:  # pragma: no cover - interface
        raise NotImplementedError


# --------------------------------------------------------------------------
# modifiers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Modifier(Node):
    """A `/`-introduced modifier.

    ``kind`` is one of: named, error, throw, coverage, hit (trajectory and/or
    location).
    """

    kind: str
    code: str | None = None
    fielders: str | None = None
    base: str | None = None
    trajectory: str | None = None
    location: str | None = None
    #: For kind "coverage": ordered ``(marker, fielders)`` pairs, marker R or U.
    groups: tuple[tuple[str, str], ...] = ()

    @property
    def relay_fielders(self) -> str:
        """Fielders in ``R`` groups -- the documented relay throw."""
        return "".join(f for k, f in self.groups if k == "R")

    @property
    def u_group_fielders(self) -> str:
        """Fielders in ``U`` groups.

        Deliberately not given a semantic name: ``U`` is undocumented and its
        meaning is inferred but unconfirmed. See spec/02-GRAMMAR.md §4.1.
        """
        return "".join(f for k, f in self.groups if k == "U")

    def emit(self) -> str:
        if self.kind == "named":
            return self.code
        if self.kind == "error":
            return "E" + self.fielders
        if self.kind == "throw":
            return "TH" + (self.base or "")
        if self.kind == "coverage":
            return "".join(k + f for k, f in self.groups)
        if self.kind == "hit":
            return (self.trajectory or "") + (self.location or "")
        raise ValueError(f"unknown modifier kind {self.kind!r}")


# --------------------------------------------------------------------------
# fielding credits
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Credit(Node):
    """One atom of a credit sequence: a fielder, or an error charged to one."""

    fielder: str
    is_error: bool = False

    def emit(self) -> str:
        return ("E" if self.is_error else "") + self.fielder


@dataclass(frozen=True)
class CreditSequence(Node):
    """A fielding parameter: credits, optionally followed by modifiers.

    ``2E4`` is credits only; ``E5/TH`` is one error credit plus a throw
    modifier; ``TH`` alone is modifiers with no credits.
    """

    credits: tuple[Credit, ...] = ()
    modifiers: tuple[Modifier, ...] = ()

    @property
    def has_error(self) -> bool:
        return any(c.is_error for c in self.credits)

    def emit(self) -> str:
        head = "".join(c.emit() for c in self.credits)
        parts = ([head] if head else []) + [m.emit() for m in self.modifiers]
        return "/".join(parts)


@dataclass(frozen=True)
class Flag(Node):
    """A non-fielding advance parameter such as ``(UR)`` or ``(NR)``."""

    text: str

    def emit(self) -> str:
        return self.text


# --------------------------------------------------------------------------
# advances
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Advance(Node):
    origin: str
    dest: str
    marked_out: bool
    params: tuple[Flag | CreditSequence, ...] = ()

    @property
    def is_out(self) -> bool:
        """The ``X`` as written, minus any error that negates it (§5).

        An error negates the out only when no parameter records a clean putout.
        `BX2(7E4)` is one sequence containing an error, so the runner is safe;
        `1X3(E1)(35)` charges an error in one parameter *and* records the
        putout 3-5 in another, so the runner is out. Treating any error
        anywhere as negating loses the out and leaves the inning short.
        """
        if not self.marked_out:
            return False
        fielding = [p for p in self.params if isinstance(p, CreditSequence)]
        if not fielding:
            return True
        # A parameter rescues the out only if it names fielders and charges no
        # error. `(TH)` is a bare throw annotation with no credits, so in
        # `BXH(TH)(E2/TH)(8E2)` nothing records a putout and the runner scored.
        return any(p.credits and not p.has_error for p in fielding)

    def emit(self) -> str:
        op = "X" if self.marked_out else "-"
        return (
            self.origin
            + op
            + self.dest
            + "".join("(" + p.emit() + ")" for p in self.params)
        )


# --------------------------------------------------------------------------
# basic events
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OutGroup(Node):
    """``fielder_seq [ "(" runner ")" ]`` -- one segment of a putout sequence."""

    fielders: str
    runner: str | None = None

    def emit(self) -> str:
        return self.fielders + (f"({self.runner})" if self.runner else "")


class BasicEvent(Node):
    pass


@dataclass(frozen=True)
class Out(BasicEvent):
    """An out recorded by a fielder sequence, including double and triple plays."""

    groups: tuple[OutGroup, ...]

    @property
    def is_unknown(self) -> bool:
        """``99`` codes an unknown play and earns no fielding credit (§2.1)."""
        return any("99" in g.fielders for g in self.groups)

    def emit(self) -> str:
        return "".join(g.emit() for g in self.groups)


@dataclass(frozen=True)
class Hit(BasicEvent):
    kind: str  # S, D, T
    fielders: str = ""

    def emit(self) -> str:
        return self.kind + self.fielders


@dataclass(frozen=True)
class GroundRuleDouble(BasicEvent):
    """``DGR``, optionally naming a fielder.

    The documentation states no fielder is specified, but ``DGR7`` and ``DGR9``
    both occur in the corpus.
    """

    fielders: str = ""

    def emit(self) -> str:
        return "DGR" + self.fielders


@dataclass(frozen=True)
class HomeRun(BasicEvent):
    code: str  # H or HR, as written
    fielders: str = ""

    @property
    def inside_the_park(self) -> bool:
        return bool(self.fielders)

    def emit(self) -> str:
        return self.code + self.fielders


@dataclass(frozen=True)
class Strikeout(BasicEvent):
    groups: tuple[OutGroup, ...] = ()

    def emit(self) -> str:
        return "K" + "".join(g.emit() for g in self.groups)


@dataclass(frozen=True)
class Walk(BasicEvent):
    code: str  # W, I, IW

    @property
    def intentional(self) -> bool:
        return self.code in ("I", "IW")

    def emit(self) -> str:
        return self.code


@dataclass(frozen=True)
class HitByPitch(BasicEvent):
    def emit(self) -> str:
        return "HP"


@dataclass(frozen=True)
class ReachedOnError(BasicEvent):
    """``E$`` or ``$$E$`` -- leading fielders are assists (§2.1)."""

    fielder: str
    assists: str = ""

    def emit(self) -> str:
        return self.assists + "E" + self.fielder


@dataclass(frozen=True)
class FieldersChoice(BasicEvent):
    fielders: str = ""

    def emit(self) -> str:
        return "FC" + self.fielders


@dataclass(frozen=True)
class FoulFlyError(BasicEvent):
    fielders: str

    def emit(self) -> str:
        return "FLE" + self.fielders


@dataclass(frozen=True)
class Interference(BasicEvent):
    """Bare ``C``. The interfering fielder is carried in an ``E$`` modifier."""

    def emit(self) -> str:
        return "C"


@dataclass(frozen=True)
class NoPlay(BasicEvent):
    def emit(self) -> str:
        return "NP"


@dataclass(frozen=True)
class BaseRunning(BasicEvent):
    """SB / CS / PO / POCS / DI / OA / WP / PB / BK."""

    code: str
    base: str | None = None
    params: tuple[Flag | CreditSequence, ...] = ()

    def emit(self) -> str:
        return (
            self.code
            + (self.base or "")
            + "".join("(" + p.emit() + ")" for p in self.params)
        )


# --------------------------------------------------------------------------
# the event
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Event(Node):
    """A parsed event field.

    ``groups`` is the ``;``-separated list of ``+``-joined basic events, per the
    two distinct roles of ``;`` described in §1.
    """

    groups: tuple[tuple[BasicEvent, ...], ...]
    modifiers: tuple[Modifier, ...] = ()
    advances: tuple[Advance, ...] = ()

    @property
    def basics(self) -> tuple[BasicEvent, ...]:
        return tuple(e for g in self.groups for e in g)

    def modifier_codes(self) -> set[str]:
        return {m.code for m in self.modifiers if m.kind == "named"}

    @property
    def hit_location(self) -> str | None:
        """The zone the ball was hit to, or None if no modifier names one.

        Singular on purpose. A `hit` modifier may carry a trajectory with no
        location (`/L`), a location with no trajectory (`/78`), or both
        (`/L78`), and an event may carry several `hit` modifiers -- but across
        483,561 sampled plays not one carries two *located* ones, so "the
        first" and "the only" are the same thing here. If that ever stops
        being true this returns the first and the verify gate stays true, so
        the failure would be silent; it is asserted in the tests instead.
        """
        return next((m.location for m in self.modifiers
                     if m.kind == "hit" and m.location), None)

    def emit(self) -> str:
        out = ";".join("+".join(e.emit() for e in g) for g in self.groups)
        out += "".join("/" + m.emit() for m in self.modifiers)
        if self.advances:
            out += "." + ";".join(a.emit() for a in self.advances)
        return out
