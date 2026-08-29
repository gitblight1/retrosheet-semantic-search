"""Recursive-descent parser for the Retrosheet event field.

Implements spec/02-GRAMMAR.md §2 exactly. This layer applies no baseball
knowledge: it produces a syntax tree and nothing else. Whether a strikeout was
uncaught, whether an out was forced, and where runners ended up are decided in
the state and ontology layers.

The parser never guesses. Anything the grammar rejects raises `ParseError`
carrying an offset, and the caller records the play with a non-ok parse_status
(§8) rather than half-interpreting it.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import grammar as G
from .lexer import Trivia, reinsert_trivia, strip_trivia


class ParseError(Exception):
    def __init__(self, message: str, offset: int, text: str) -> None:
        super().__init__(f"{message} at offset {offset} in {text!r}")
        self.message = message
        self.offset = offset
        self.text = text


@dataclass(frozen=True)
class ParsedEvent:
    """A parsed event plus everything needed to reproduce the source string."""

    raw: str
    event: G.Event
    trivia: tuple[Trivia, ...]

    def emit(self) -> str:
        return reinsert_trivia(self.event.emit(), self.trivia)

    def roundtrips(self) -> bool:
        return self.emit() == self.raw


class _Cursor:
    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0

    def eof(self) -> bool:
        return self.i >= len(self.text)

    def at(self, chars: str) -> bool:
        return not self.eof() and self.text[self.i] in chars

    def peek(self, n: int = 1) -> str:
        return self.text[self.i : self.i + n]

    def take(self, n: int = 1) -> str:
        if self.i + n > len(self.text):
            self.fail("unexpected end of event")
        out = self.text[self.i : self.i + n]
        self.i += n
        return out

    def accept(self, literal: str) -> bool:
        if self.text.startswith(literal, self.i):
            self.i += len(literal)
            return True
        return False

    def expect(self, literal: str) -> None:
        if not self.accept(literal):
            self.fail(f"expected {literal!r}")

    def fail(self, message: str) -> None:
        raise ParseError(message, self.i, self.text)


# --------------------------------------------------------------------------
# shared pieces
# --------------------------------------------------------------------------


def _fielders(c: _Cursor) -> str:
    start = c.i
    while c.at(G.FIELDERS):
        c.i += 1
    return c.text[start : c.i]


def _out_groups(c: _Cursor) -> tuple[G.OutGroup, ...]:
    """``fielder_seq { "(" runner ")" [ fielder_seq ] }`` -- e.g. ``64(1)3``."""
    groups: list[G.OutGroup] = []
    while True:
        fielders = _fielders(c)
        runner = None
        if c.at("("):
            c.take()
            if not c.at(G.RUNNERS):
                c.fail("expected runner designator")
            runner = c.take()
            c.expect(")")
        if not fielders and runner is None:
            break
        groups.append(G.OutGroup(fielders, runner))
        if not (c.at(G.FIELDERS) or c.at("(")):
            break
    return tuple(groups)


def _paren_body(c: _Cursor) -> str:
    c.expect("(")
    start = c.i
    while not c.eof() and c.peek() != ")":
        c.i += 1
    if c.eof():
        c.fail("unterminated parameter")
    body = c.text[start : c.i]
    c.expect(")")
    return body


def _parse_modifier_text(text: str, c: _Cursor) -> G.Modifier:
    if text in G.NAMED_MODIFIERS:
        return G.Modifier("named", code=text)
    if len(text) == 2 and text[0] == "E" and text[1] in G.FIELDERS:
        return G.Modifier("error", fielders=text[1])
    if text.startswith("TH"):
        rest = text[2:]
        if rest == "" or (len(rest) == 1 and rest in G.RUNNERS):
            return G.Modifier("throw", base=rest or None)
    if text.startswith("R") and len(text) > 1:
        rest = text[1:]
        if all(ch in "123456789" for ch in rest):
            return G.Modifier("relay", fielders=rest)
    trajectory = ""
    for traj in G.TRAJECTORIES:
        if text.startswith(traj):
            trajectory = traj
            break
    location = text[len(trajectory) :]
    if _is_location(location):
        return G.Modifier(
            "hit", trajectory=trajectory or None, location=location or None
        )
    raise ParseError(f"unknown modifier {text!r}", c.i, c.text)


def _is_location(text: str) -> bool:
    """``zone { loc_qualifier }`` -- kept structural; zone semantics are §2.1."""
    if text == "":
        return True
    i = 0
    while i < len(text) and text[i].isdigit() and i < 2:
        i += 1
    if i == 0:
        return False
    return all(ch in G.LOC_QUALIFIERS for ch in text[i:])


def _param(c: _Cursor) -> G.Flag | G.CreditSequence:
    body = _paren_body(c)
    if body in G.ADV_FLAGS:
        return G.Flag(body)
    parts = body.split("/")
    head, tail = parts[0], parts[1:]
    credits: list[G.Credit] = []
    j = 0
    while j < len(head):
        if head[j] == "E" and j + 1 < len(head) and head[j + 1] in G.FIELDERS:
            credits.append(G.Credit(head[j + 1], is_error=True))
            j += 2
        elif head[j] in G.FIELDERS:
            credits.append(G.Credit(head[j]))
            j += 1
        else:
            break
    if j != len(head):
        # Head is not a credit sequence; treat the whole body as modifiers.
        credits, tail = [], parts
    mods = tuple(_parse_modifier_text(t, c) for t in tail)
    return G.CreditSequence(tuple(credits), mods)


def _params(c: _Cursor) -> tuple[G.Flag | G.CreditSequence, ...]:
    out = []
    while c.at("("):
        out.append(_param(c))
    return tuple(out)


# --------------------------------------------------------------------------
# basic events
# --------------------------------------------------------------------------

_SIMPLE_RUNNING = ("DI", "OA", "WP", "PB", "BK")


def _basic_event(c: _Cursor) -> G.BasicEvent:
    if c.accept("NP"):
        return G.NoPlay()

    # Base-running events. Longest literal first (§2.1).
    for code in ("POCS", "PO", "CS", "SB"):
        if c.accept(code):
            base = c.take() if c.at(G.RUNNERS) else None
            return G.BaseRunning(code, base, _params(c))
    for code in _SIMPLE_RUNNING:
        if c.accept(code):
            return G.BaseRunning(code)

    # Batter events.
    if c.accept("DGR"):
        return G.GroundRuleDouble(_fielders(c))
    if c.accept("FLE"):
        return G.FoulFlyError(_fielders(c))
    if c.accept("FC"):
        return G.FieldersChoice(_fielders(c))
    if c.accept("HP"):  # before H/HR, or every hit batsman reads as a home run
        return G.HitByPitch()
    for code in ("HR", "H"):
        if c.accept(code):
            return G.HomeRun(code, _fielders(c))
    for code in ("IW", "I", "W"):
        if c.accept(code):
            return G.Walk(code)
    if c.accept("K"):
        return G.Strikeout(_out_groups(c))
    if c.at("SDT"):
        kind = c.take()
        return G.Hit(kind, _fielders(c))
    if c.accept("C"):
        return G.Interference()

    # E$ / $$E$ must be distinguished from a plain fielder sequence (§2.1).
    save = c.i
    assists = _fielders(c)
    if c.at("E"):
        c.take()
        if not c.at(G.FIELDERS):
            c.fail("expected fielder after E")
        return G.ReachedOnError(c.take(), assists)
    c.i = save

    groups = _out_groups(c)
    if groups:
        return G.Out(groups)
    c.fail("no basic event")


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------


def parse_clean(text: str) -> G.Event:
    """Parse a trivia-free event string."""
    c = _Cursor(text)

    groups: list[tuple[G.BasicEvent, ...]] = []
    current: list[G.BasicEvent] = [_basic_event(c)]
    while True:
        if c.accept("+"):
            current.append(_basic_event(c))
            continue
        if c.accept(";"):
            groups.append(tuple(current))
            current = [_basic_event(c)]
            continue
        break
    groups.append(tuple(current))

    modifiers: list[G.Modifier] = []
    while c.accept("/"):
        start = c.i
        while not c.eof() and not c.at("/."):
            c.i += 1
        modifiers.append(_parse_modifier_text(c.text[start : c.i], c))

    advances: list[G.Advance] = []
    if c.accept("."):
        while True:
            advances.append(_advance(c))
            if not c.accept(";"):
                break

    if not c.eof():
        c.fail("trailing input")
    return G.Event(tuple(groups), tuple(modifiers), tuple(advances))


def _advance(c: _Cursor) -> G.Advance:
    if not c.at(G.RUNNERS):
        c.fail("expected advance origin")
    origin = c.take()
    op = c.take()
    if op not in ("-", "X"):
        c.fail(f"expected advance operator, got {op!r}")
    if not c.at(G.ADV_BASES):
        c.fail("expected advance destination")
    dest = c.take()
    return G.Advance(origin, dest, op == "X", _params(c))


def parse(raw: str) -> ParsedEvent:
    """Parse a raw event field, preserving annotation trivia."""
    clean, trivia = strip_trivia(raw)
    return ParsedEvent(raw, parse_clean(clean), trivia)
