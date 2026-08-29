"""Annotation trivia handling for event strings.

Retrosheet permits `#`, `!`, `?`, `+` and `-` inside an event and states they
can be ignored. Principle 2.1 forbids discarding them, so they are lifted into
a side channel keyed by position in the *cleaned* string, leaving a clean input
for the parser while keeping the original byte-for-byte recoverable
(spec/02-GRAMMAR.md §3).

`+` and `-` are overloaded. `+` joins two basic events; `-` is the advance
operator. The disambiguation rules below are the ones §3 specifies, and they
are the likeliest source of a silent mis-parse, so they are isolated here and
tested directly.
"""

from __future__ import annotations

ALWAYS_TRIVIA = "#!?"

#: After one of these, a `+` cannot be joining an event to another.
_PLUS_TRIVIA_FOLLOWERS = "/.;)"

_BASES = "B123H"
_ADV_DEST = "123H"

Trivia = tuple[int, str]


def _next_significant(raw: str, i: int) -> str:
    """The next character after ``raw[i]`` that is not annotation trivia."""
    j = i + 1
    while j < len(raw) and raw[j] in ALWAYS_TRIVIA:
        j += 1
    return raw[j] if j < len(raw) else ""


def strip_trivia(raw: str) -> tuple[str, tuple[Trivia, ...]]:
    """Split ``raw`` into a parseable string and positioned annotation chars.

    Each trivia entry is ``(index, char)`` where ``index`` is the offset into
    the cleaned string at which the character should be reinserted.
    """
    clean: list[str] = []
    trivia: list[Trivia] = []

    # The advance section begins at the first '.', which is never trivia.
    dot = raw.find(".")
    in_advances_from = dot if dot >= 0 else len(raw)

    for i, ch in enumerate(raw):
        if ch in ALWAYS_TRIVIA:
            trivia.append((len(clean), ch))
            continue
        if ch == "+":
            # Look past any adjacent annotation characters: in `L78+#.2-H` the
            # `+` is a hard-hit marker, not an event joiner.
            nxt = _next_significant(raw, i)
            if nxt == "" or nxt in _PLUS_TRIVIA_FOLLOWERS:
                trivia.append((len(clean), ch))
                continue
        elif ch == "-":
            prev = clean[-1] if clean else ""
            nxt = _next_significant(raw, i)
            operator = i > in_advances_from and prev in _BASES and nxt in _ADV_DEST
            if not operator:
                trivia.append((len(clean), ch))
                continue
        clean.append(ch)

    return "".join(clean), tuple(trivia)


def reinsert_trivia(clean: str, trivia: tuple[Trivia, ...]) -> str:
    """Inverse of :func:`strip_trivia`."""
    out: list[str] = []
    pos = 0
    for index, ch in trivia:
        out.append(clean[pos:index])
        out.append(ch)
        pos = index
    out.append(clean[pos:])
    return "".join(out)
