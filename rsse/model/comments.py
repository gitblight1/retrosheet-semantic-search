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
    # Ids are stripped. Four `replay` records pad the player id with a
    # trailing space, and an id that does not compare equal to the batter it
    # names is an id that silently fails every link that uses it.
    return {"inning": int(f[1]), "player_id": f[2].strip() or None,
            "team": f[3].strip() or None, "umpire_id": f[4].strip() or None,
            "site": f[5].strip() or None, "call": f[6].strip() or None,
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


#: Where a `com` record's play is, relative to the record itself.
BEFORE = "before"
AFTER = "after"

#: `NP` -- "no play" -- is the event string of a record that exists only to
#: carry a substitution. It is always exactly this, in all 1,784,548 of them.
#:
#: **A replay comment's neighbours must be chosen with these skipped**, which
#: is a correctness requirement rather than tidiness. An `NP` names the batter
#: due up, who is normally the same player as the next real play's batter, so
#: `replay_target`'s test -- does the named player bat *after* but not *before*
#: -- reads `batter_before == player_id` and can never fire. 141 replay
#: comments landed on an `NP` that way. Of the 47 where the surrounding event
#: strings settle the question, the review modifier `MREV`/`UREV` is on the
#: play *after* the `NP` 47 times and on the play before it **none**; the other
#: 94 carry no modifier either side. An `NP` is not a play and cannot be the
#: subject of a review.
NO_PLAY = "NP"


def replay_target(player_id: str | None, batter_before: str | None,
                  batter_after: str | None) -> str:
    """Which adjacent play a structured `replay` comment describes.

    A `com` record normally describes the play **before** it, and 4,241 of the
    corpus's 5,102 replay records do. But 51 describe the play *after* — the
    umpires confer, the comment is written, and the resulting play follows —
    and linking those backwards attaches the verdict to a play that was never
    reviewed. 31 of them carry a reversal, so 31 `ReplayOverturned` tags were
    landing on the wrong play or not at all.

    The record names the player involved, which settles it without a guess:
    if he does not bat in the play before and does bat in the play after, the
    comment belongs to the one after. Anything else keeps the default,
    including the 804 records where the same batter is on both sides of the
    boundary and the 6 whose named player bats in neither.

    Those 6 are worth keeping in view: the field names *the player involved in
    the reviewed call*, which for a review of a runner's advance is the
    runner, not the batter. A name that matches nothing is not evidence that
    the link is wrong, so it changes nothing.
    """
    if (player_id and batter_before != player_id
            and batter_after == player_id):
        return AFTER
    return BEFORE
