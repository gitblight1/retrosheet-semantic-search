"""Retrosheet game logs: one published summary row per game.

These are **not** event data and they are not derived from it. Retrosheet
compiles the game logs separately, which is exactly what makes them useful
here: every check on the replay so far compares the pipeline against itself
(spec/07-TESTING.md §4), and internal consistency cannot catch an error that
is internally consistent. A final score published independently of the event
file can.

The format is a 161-field CSV, one line per game, stable across the whole
series. Field positions are recorded in ``FIELDS`` below rather than as magic
indexes at the point of use: the layout is positional with no header row, so a
wrong offset produces a plausible number rather than an error.

Two things here are deliberately *not* asserted from the documentation:

* which earned-run field is the team total. The layout names both "individual"
  and "team" earned runs per side, and reconciling against the wrong one would
  report thousands of false mismatches. Both are parsed and stored; which one
  agrees with the event files' `data,er` records is settled empirically by
  `rsse reconcile --explain-er`.
* whether the corpus and the logs cover the same games. They do not -- the
  logs are Major League games and the corpus includes the Negro Leagues -- so
  coverage is measured rather than assumed.
"""

from __future__ import annotations

from dataclasses import dataclass

#: 0-indexed positions of the fields this project reads, from Retrosheet's
#: published layout (which numbers them from 1). Only the fields that are used
#: are named; the rest of the 161 are kept in `raw` and nothing else.
FIELDS = {
    "date": 0,             # yyyymmdd
    "game_number": 1,      # "0" single, "1"/"2" doubleheader, "A"/"B" separate
    "day_of_week": 2,
    "away_team": 3,
    "away_league": 4,
    "home_team": 6,
    "home_league": 7,
    "away_score": 9,
    "home_score": 10,
    "outs": 11,            # length of game in outs -- both sides, all innings
    "day_night": 12,
    "completion": 13,      # non-empty if the game was completed later
    "forfeit": 14,
    "protest": 15,
    "park_id": 16,
    "attendance": 17,
    "duration_minutes": 18,
    # Pitching blocks: visitor 0-indexed 38..42, home 66..70, each
    # (pitchers used, individual ER, team ER, wild pitches, balks).
    "away_pitchers_used": 38,
    "away_er_individual": 39,
    "away_er_team": 40,
    "home_pitchers_used": 66,
    "home_er_individual": 67,
    "home_er_team": 68,
}

#: The published layout has this many fields. Lines that disagree are reported
#: rather than parsed on a best-effort basis: a short line silently shifts
#: every field after the gap.
EXPECTED_FIELDS = 161


class GameLogError(ValueError):
    pass


@dataclass(frozen=True)
class GameLog:
    """One game log line.

    ``raw`` is the line exactly as published, so nothing parsed here can lose
    information and the reconciliation can always be re-derived.
    """

    date: str                  # ISO yyyy-mm-dd
    game_number: str
    away_team: str
    home_team: str
    away_league: str
    home_league: str
    away_score: int
    home_score: int
    outs: int | None
    park_id: str | None
    completion: str
    forfeit: str
    away_er_individual: int | None
    away_er_team: int | None
    home_er_individual: int | None
    home_er_team: int | None
    raw: str

    @property
    def retrosheet_game_id(self) -> str:
        """The event-file game id this row describes.

        Retrosheet builds it as home team + date + game number, and the game
        log's `game_number` is the same digit -- except that separate-admission
        doubleheaders are lettered `A`/`B` in the logs and numbered in the
        event files, so those are mapped rather than passed through.
        """
        number = {"A": "1", "B": "2"}.get(self.game_number, self.game_number)
        if not number.isdigit():
            number = "0"
        return f"{self.home_team}{self.date.replace('-', '')}{number}"

    @property
    def total_runs(self) -> int:
        return self.away_score + self.home_score

    @property
    def is_complete(self) -> bool:
        """Whether the row describes a game played to a normal finish.

        A forfeit has a score awarded by rule rather than scored on the field,
        and a game completed on a later date is split across records, so
        neither is a fair test of the replay.
        """
        return not self.forfeit.strip() and not self.completion.strip()


def _int_or_none(text: str) -> int | None:
    text = text.strip().strip('"')
    return int(text) if text.lstrip("-").isdigit() else None


def _unquote(text: str) -> str:
    return text.strip().strip('"')


def parse_line(line: str) -> GameLog:
    """Parse one game log line. Raises `GameLogError` on a malformed row.

    Split with `csv`, not `str.split(',')`: manager and umpire names are
    quoted free text and several contain commas ("Harry Wright, Jr.").
    """
    import csv
    import io

    fields = next(csv.reader(io.StringIO(line)))
    if len(fields) != EXPECTED_FIELDS:
        raise GameLogError(
            f"expected {EXPECTED_FIELDS} fields, got {len(fields)}")

    def get(name: str) -> str:
        return _unquote(fields[FIELDS[name]])

    date = get("date")
    if len(date) != 8 or not date.isdigit():
        raise GameLogError(f"bad date {date!r}")
    away_score = _int_or_none(fields[FIELDS["away_score"]])
    home_score = _int_or_none(fields[FIELDS["home_score"]])
    if away_score is None or home_score is None:
        raise GameLogError("missing score")

    return GameLog(
        date=f"{date[:4]}-{date[4:6]}-{date[6:]}",
        game_number=get("game_number") or "0",
        away_team=get("away_team"),
        home_team=get("home_team"),
        away_league=get("away_league"),
        home_league=get("home_league"),
        away_score=away_score,
        home_score=home_score,
        outs=_int_or_none(fields[FIELDS["outs"]]),
        park_id=get("park_id") or None,
        completion=get("completion"),
        forfeit=get("forfeit"),
        away_er_individual=_int_or_none(fields[FIELDS["away_er_individual"]]),
        away_er_team=_int_or_none(fields[FIELDS["away_er_team"]]),
        home_er_individual=_int_or_none(fields[FIELDS["home_er_individual"]]),
        home_er_team=_int_or_none(fields[FIELDS["home_er_team"]]),
        raw=line.rstrip("\r\n"),
    )


# ---------------------------------------------------------------------------
# layout validation
# ---------------------------------------------------------------------------

#: Shape tests on parsed rows, used to check the field offsets above against
#: real data.
#:
#: This exists because the offsets cannot be validated by a fixture. A test
#: file written from `FIELDS` would agree with `FIELDS` however wrong it was --
#: the same tautology as a unit test asserting whatever the code produces
#: (spec/07-TESTING.md §4). What *can* be checked is that the values landing in
#: each field look like the thing that field is supposed to hold: a one-field
#: shift puts a league code where a score belongs, an attendance where an out
#: count belongs, and these predicates fail en masse rather than subtly.
#:
#: Each entry is (description, predicate over a GameLog, how much tolerance).
#: `tolerance` is the fraction of rows allowed to fail: real data has genuine
#: oddities -- a 19th-century game with no out count, a tie called for
#: darkness -- and a check with no tolerance would flag those instead of a
#: layout error.
LAYOUT_CHECKS = (
    ("scores are plausible run totals",
     lambda g: 0 <= g.away_score <= 60 and 0 <= g.home_score <= 60, 0.001),
    ("team ids are three characters",
     lambda g: len(g.home_team) == 3 and len(g.away_team) == 3, 0.001),
    ("league ids are two characters or empty",
     lambda g: len(g.home_league) in (0, 2) and len(g.away_league) in (0, 2),
     0.01),
    # A walk-off ends the home half early, so the game's total out count is
    # *not* a multiple of three -- 7.7% of games, and this check originally
    # flagged every one of them as a layout error. What settled it was that
    # 98.95% of the offenders are home wins against a 58.65% home-win rate
    # overall, which is the walk-off signature and not something a shifted
    # field could produce. The belief about baseball was wrong, not the offset.
    ("out counts are a multiple of three, unless the home team won",
     lambda g: (g.outs is None or g.outs % 3 == 0
                or g.home_score > g.away_score), 0.005),
    ("out counts are a plausible game length",
     lambda g: g.outs is None or 6 <= g.outs <= 200, 0.02),
    ("park ids start with three letters",
     lambda g: not g.park_id or g.park_id[:3].isalpha(), 0.01),
    ("earned runs do not exceed runs allowed",
     lambda g: (g.home_er_team is None or g.home_er_team <= g.away_score)
               and (g.away_er_team is None or g.away_er_team <= g.home_score),
     0.02),
)


@dataclass(frozen=True)
class LayoutFinding:
    description: str
    failed: int
    checked: int
    tolerance: float

    @property
    def rate(self) -> float:
        return self.failed / self.checked if self.checked else 0.0

    @property
    def ok(self) -> bool:
        return self.rate <= self.tolerance


def check_layout(rows) -> list[LayoutFinding]:
    """Run `LAYOUT_CHECKS` over parsed rows.

    Returns one finding per check. A failing finding means the field offsets
    are wrong far more likely than that the data is: these are properties of
    baseball, not of Retrosheet's file format.
    """
    rows = list(rows)
    findings = []
    for description, predicate, tolerance in LAYOUT_CHECKS:
        failed = 0
        checked = 0
        for row in rows:
            checked += 1
            try:
                if not predicate(row):
                    failed += 1
            except Exception:      # a type error is itself a failed shape test
                failed += 1
        findings.append(LayoutFinding(description, failed, checked, tolerance))
    return findings

