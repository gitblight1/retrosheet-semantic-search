"""`com` records: classification and structured payloads (spec/05-DATABASE.md §5).

A `com` record looks like free text, and 88% of them are. The rest carry
**structured sub-records** in the quoted body -- a second, undocumented record
format nested inside the first:

    com,"ej,mcgud101,M,sherj901,Call at 2B"
    com,"replay,6,pench001,HOU,welkt901,HOU03,O,N,I,,H"
    com,"umpchange,4,ump1b,hurst801"
    com,"suspended,19131002,NYC14,fans in bleachers"

These matter because they are the only machine-readable record of things the
event string cannot express: who was ejected and why, whether a replay review
reversed the call, and which umpire took which position mid-game.

**A tag prefix is not enough to identify one.** Four prose comments begin
"replay, ..." ("replay, scoring two runs") and would be misread as structured
records by a prefix test alone, so every parser here also requires the field
count and the field shapes it depends on. Getting this wrong would not fail
loudly; it would silently invent a replay verdict.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Kinds, matching `comments.kind` in the schema.
TEXT = "text"
EJECTION = "ejection"
REPLAY = "replay"
UMPCHANGE = "umpchange"
SUSPEND = "suspend"

#: `Y`/`N` in field 8 of a structured `replay` record: whether the call on the
#: field was reversed.
#:
#: Inferred, not documented, and cross-checked before being relied on. Of the
#: 5,102 well-formed structured records, `Y` co-occurs with a nearby prose
#: comment saying "overturned" 2,374 times against 5 saying "upheld", and `N`
#: with "upheld" 2,532 times against 30 saying "overturned" -- 98.6% agreement
#: with an independent human account of the same play. The ~48% reversal rate
#: also matches the published MLB figure. The 35 disagreements are recorded in
#: the payload as written rather than reconciled; they are a statement about
#: the data.
_REVERSED = {"Y": True, "N": False}


@dataclass(frozen=True)
class Comment:
    """One `com` record, classified.

    ``text`` is always the body exactly as written, so nothing here can lose
    information: the payload is an *additional* reading of it, never a
    replacement.
    """

    kind: str
    text: str
    payload: dict | None = None
    #: The body began with `$`. Retrosheet's meaning for this marker is not
    #: documented and is not inferred here -- 66,000 of 222,495 comments carry
    #: it, in every era, and the pattern does not settle to a single reading.
    #: Recorded so a later answer can use it; deliberately not given a
    #: semantic name (cf. the `U` coverage marker, spec/02-GRAMMAR.md §4.1).
    marker: bool = False

    @property
    def replay_reversed(self) -> bool | None:
        """True reversed, False upheld, None if this is not a replay record."""
        if self.kind != REPLAY or not self.payload:
            return None
        return self.payload.get("reversed")


def body_of(raw: str) -> str:
    """The quoted body of a `com` record, unquoted.

    Retrosheet quotes the body with `"` and occasionally `'`, and a few
    records are unquoted entirely.
    """
    body = raw.split(",", 1)[1] if "," in raw else raw
    body = body.strip()
    for quote in ('"', "'"):
        if len(body) >= 2 and body.startswith(quote) and body.endswith(quote):
            return body[1:-1]
    return body


def _fields(body: str, tag: str) -> list[str] | None:
    parts = body.split(",")
    return parts if parts and parts[0] == tag else None


def _ejection(body: str) -> dict | None:
    """`ej,<person>,<role>,<umpire>,<reason>` -- role M manager, P player."""
    f = _fields(body, "ej")
    if f is None or len(f) < 4:
        return None
    if not f[1] or " " in f[1]:            # a player id, never a phrase
        return None
    return {"person_id": f[1], "role": f[2] or None,
            "umpire_id": f[3] or None,
            "reason": ",".join(f[4:]).strip() or None}


def _replay(body: str) -> dict | None:
    """`replay,<inning>,<player>,<team>,<umpire>,<site>,<call>,<Y|N>,...`

    The `Y`/`N` requirement is what separates this from a prose comment
    beginning "replay, ...": those have neither the field count nor the flag.
    """
    f = _fields(body, "replay")
    if f is None or len(f) < 8:
        return None
    if f[7] not in _REVERSED or not f[1].isdigit():
        return None
    return {"inning": int(f[1]), "player_id": f[2] or None,
            "team": f[3] or None, "umpire_id": f[4] or None,
            "site": f[5] or None, "call": f[6] or None,
            "reversed": _REVERSED[f[7]],
            # Two further coded fields whose meanings are not established.
            # Carried verbatim rather than named or dropped.
            "unknown_fields": [x for x in f[8:]]}


def _umpchange(body: str) -> dict | None:
    """`umpchange,<inning>,<position>,<umpire>` -- `(none)` for a vacancy."""
    f = _fields(body, "umpchange")
    if f is None or len(f) < 4 or not f[1].isdigit():
        return None
    ump = f[3].strip()
    return {"inning": int(f[1]), "position": f[2] or None,
            "umpire_id": None if ump.lower() in ("(none)", "", "none") else ump}


def _suspend(body: str) -> dict | None:
    """`suspended,<yyyymmdd>,<site>,<reason>`."""
    f = _fields(body, "suspended")
    if f is None or len(f) < 3 or not (f[1].isdigit() and len(f[1]) == 8):
        return None
    return {"date": f"{f[1][:4]}-{f[1][4:6]}-{f[1][6:]}",
            "site": f[2] or None, "reason": ",".join(f[3:]).strip() or None}


#: Order does not matter -- each parser is keyed on its own tag -- but the
#: table makes the set of structured kinds one visible thing.
_PARSERS = ((EJECTION, _ejection), (REPLAY, _replay),
            (UMPCHANGE, _umpchange), (SUSPEND, _suspend))


def classify(raw: str) -> Comment:
    """Classify one `com` record's raw line."""
    body = body_of(raw)
    marker = body.startswith("$")
    if marker:
        body = body[1:]
    for kind, parser in _PARSERS:
        payload = parser(body)
        if payload is not None:
            return Comment(kind, body, payload, marker)
    return Comment(TEXT, body, None, marker)
