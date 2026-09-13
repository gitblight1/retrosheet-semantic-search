"""Readers for the reference files (spec/05-DATABASE.md §1.2, §8).

Five formats, all of them Retrosheet's and none of them documented in the
event-file spec: `.ROS` rosters, `TEAM####` team files, and three headed CSVs
for parks, franchises and biographies.

They are read from the **archive**, not the file tree, so a correction cannot
change a derived table without an explicit re-ingest. Every reader here takes
the stored `raw_line` and nothing else.

**The layout is checked, not assumed.** The lesson from the game logs
([07-TESTING](../../spec/07-TESTING.md) §4.3) applies unchanged: a positional
format that loses a field shifts every field after it, and offsets verified
against themselves verify nothing. So each format asserts its shape against
facts that would be false if the file had shifted -- a header that must match
exactly, a field count that must be uniform, value domains measured from the
corpus -- and a blank is preserved as a blank rather than guessed at.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass


class LayoutError(ValueError):
    """A reference file does not have the shape this reader expects."""


def _rows(lines: list[str]) -> list[list[str]]:
    """Parse with `csv`, not `str.split`.

    `ballparks.csv` quotes a `NOTES` field containing commas, and splitting on
    the comma turns one park into two and shifts nothing else -- the failure
    that is hardest to see, because the row count stays plausible.
    """
    return list(csv.reader(io.StringIO("\n".join(lines))))


# ---------------------------------------------------------------------------
# positional formats
# ---------------------------------------------------------------------------

#: Every one of the corpus's 121,600 roster lines has exactly these 7 fields,
#: and every player id is 8 characters. Uniform enough to be a hard gate.
ROSTER_FIELDS = 7
PLAYER_ID_LENGTH = 8

#: Measured across the corpus, not taken from documentation. `B` appears in
#: `throws` only 9 times -- the genuinely ambidextrous -- and would look like
#: a defect to anyone who had not counted. Blank is in the domain because
#: 7 lines have no `bats` and 3 have no `throws`: unknown, and recorded as
#: such rather than defaulted to the common value.
BATS = frozenset({"L", "R", "B", "?", ""})
THROWS = frozenset({"L", "R", "B", "?", ""})
#: 5,661 roster lines carry no position at all.
POSITIONS = frozenset({"P", "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF",
                       "OF", "DH", "PH", "PR", "?", ""})

TEAM_FIELDS = 4


@dataclass(frozen=True)
class RosterEntry:
    """One line of a `.ROS` file: a person in a team's season."""

    player_id: str
    last: str
    first: str
    bats: str | None
    throws: str | None
    team_id: str
    position: str | None


@dataclass(frozen=True)
class TeamEntry:
    """One line of a `TEAM####` file: a team as it stood that season."""

    team_id: str
    #: Stored exactly as written. Retrosheet spells the majors `A`/`N` in 88
    #: seasons and `AL`/`NL` in 1920-1949, with no season using both. That is
    #: its notation to reconcile, not this project's, so both reach the
    #: database unchanged (spec/05-DATABASE.md §8.2).
    league: str | None
    city: str
    nickname: str


def _blank_to_none(value: str) -> str | None:
    value = value.strip()
    return value or None


def parse_roster_line(line: str, line_no: int = 0) -> RosterEntry:
    parts = [p.strip() for p in line.split(",")]
    if len(parts) != ROSTER_FIELDS:
        raise LayoutError(
            f"line {line_no}: {len(parts)} fields, expected {ROSTER_FIELDS}")
    if len(parts[0]) != PLAYER_ID_LENGTH:
        raise LayoutError(
            f"line {line_no}: player id {parts[0]!r} is not "
            f"{PLAYER_ID_LENGTH} characters")
    if parts[3] not in BATS:
        raise LayoutError(f"line {line_no}: bats {parts[3]!r} not a known value")
    if parts[4] not in THROWS:
        raise LayoutError(f"line {line_no}: throws {parts[4]!r} not a known value")
    if parts[6] not in POSITIONS:
        raise LayoutError(f"line {line_no}: position {parts[6]!r} unknown")
    return RosterEntry(parts[0], parts[1], parts[2], _blank_to_none(parts[3]),
                       _blank_to_none(parts[4]), parts[5],
                       _blank_to_none(parts[6]))


def parse_team_line(line: str, line_no: int = 0) -> TeamEntry:
    parts = [p.strip() for p in line.split(",")]
    if len(parts) != TEAM_FIELDS:
        raise LayoutError(
            f"line {line_no}: {len(parts)} fields, expected {TEAM_FIELDS}")
    if not parts[0]:
        raise LayoutError(f"line {line_no}: no team id")
    # 344 of 3,493 rows have an empty league. That is a fact about the club --
    # independent and barnstorming teams had no affiliation -- and not a gap,
    # so it is preserved as NULL rather than filled in.
    return TeamEntry(parts[0], _blank_to_none(parts[1]), parts[2], parts[3])


# ---------------------------------------------------------------------------
# headed CSVs
# ---------------------------------------------------------------------------

#: Exact headers. These files carry their own column names, so the strongest
#: available layout check is that the names are the ones expected -- a column
#: inserted upstream changes the header before it changes anything else, and
#: is caught here instead of silently shifting every value to its right.
PARK_HEADER = ("PARKID", "NAME", "AKA", "CITY", "STATE", "START", "END",
               "LEAGUE", "NOTES")
TEAMLIST_HEADER = ("TEAM", "LEAGUE", "CITY", "NICKNAME", "FIRST", "LAST")
BIO_HEADER_HEAD = ("PLAYERID", "LAST", "FIRST", "NICKNAME", "BIRTHDATE",
                   "BIRTH.CITY", "BIRTH.STATE", "BIRTH.COUNTRY", "PLAY.DEBUT",
                   "PLAY.LASTGAME", "MGR.DEBUT", "MGR.LASTGAME",
                   "COACH.DEBUT", "COACH.LASTGAME", "UMP.DEBUT",
                   "UMP.LASTGAME", "DEATHDATE")
BIO_COLUMNS = 33


@dataclass(frozen=True)
class Park:
    park_id: str
    name: str
    aka: str | None
    city: str
    state: str | None
    #: As written, `MM/DD/YYYY` or blank. Kept as text: 371 of 656 parks have
    #: no dates at all, and a park with no known opening is not a park that
    #: opened on an epoch.
    start: str | None
    end: str | None
    league: str | None
    notes: str | None


@dataclass(frozen=True)
class Franchise:
    """One row of `teams.csv` -- a club across its whole life."""

    team_id: str
    league: str | None
    city: str
    nickname: str
    first_season: int | None
    last_season: int | None


@dataclass(frozen=True)
class Person:
    """One row of `biofile.csv`.

    Not "player": 2,369 of these have umpire dates and 1,006 have manager
    dates, and 471 of the umpires the corpus names never appear in a lineup at
    all. Filtering this file to people who batted would lose every one of
    them, and they are reachable from `comments` (spec/05-DATABASE.md §8.1).
    """

    person_id: str
    last: str
    first: str
    nickname: str | None
    birthdate: str | None
    birth_city: str | None
    birth_state: str | None
    birth_country: str | None
    play_debut: str | None
    play_last: str | None
    mgr_debut: str | None
    mgr_last: str | None
    coach_debut: str | None
    coach_last: str | None
    ump_debut: str | None
    ump_last: str | None
    deathdate: str | None
    #: Everything past the columns named above, verbatim and positional. The
    #: file has 33 columns and this project models 17 of them; discarding the
    #: rest would make the archive's completeness pointless one layer up.
    rest: tuple[str, ...] = ()


def _check_header(row: list[str], expected: tuple[str, ...], what: str) -> None:
    actual = tuple(c.strip() for c in row[:len(expected)])
    if actual != expected:
        raise LayoutError(
            f"{what}: header is {actual}, expected {expected}")


def read_parks(lines: list[str]) -> list[Park]:
    rows = _rows(lines)
    if not rows:
        return []
    _check_header(rows[0], PARK_HEADER, "ballparks.csv")
    out = []
    for n, row in enumerate(rows[1:], 2):
        if len(row) != len(PARK_HEADER):
            raise LayoutError(f"ballparks.csv line {n}: {len(row)} columns")
        f = [c.strip() for c in row]
        out.append(Park(f[0], f[1], f[2] or None, f[3], f[4] or None,
                        f[5] or None, f[6] or None, f[7] or None,
                        f[8] or None))
    return out


def read_franchises(lines: list[str]) -> list[Franchise]:
    rows = _rows(lines)
    if not rows:
        return []
    _check_header(rows[0], TEAMLIST_HEADER, "teams.csv")
    out = []
    for n, row in enumerate(rows[1:], 2):
        if len(row) != len(TEAMLIST_HEADER):
            raise LayoutError(f"teams.csv line {n}: {len(row)} columns")
        f = [c.strip() for c in row]
        out.append(Franchise(f[0], f[1] or None, f[2], f[3],
                             int(f[4]) if f[4].isdigit() else None,
                             int(f[5]) if f[5].isdigit() else None))
    return out


def read_people(lines: list[str]) -> list[Person]:
    rows = _rows(lines)
    if not rows:
        return []
    _check_header(rows[0], BIO_HEADER_HEAD, "biofile.csv")
    if len(rows[0]) != BIO_COLUMNS:
        raise LayoutError(
            f"biofile.csv: {len(rows[0])} columns, expected {BIO_COLUMNS}")
    out = []
    for n, row in enumerate(rows[1:], 2):
        if len(row) != BIO_COLUMNS:
            raise LayoutError(f"biofile.csv line {n}: {len(row)} columns")
        f = [c.strip() or None for c in row]
        out.append(Person(row[0].strip(), row[1].strip(), row[2].strip(),
                          *f[3:17], tuple(row[17:])))
    return out


# ---------------------------------------------------------------------------
# appearances (`allplayers.csv`)
# ---------------------------------------------------------------------------

#: 25 columns, header lower-case where every other reference file shouts. The
#: exact spelling is the gate: this is a positional file behind a header, and
#: `g_lf` arriving where `g_cf` is expected shifts every count after it.
APPEARANCE_HEADER = (
    "id", "last", "first", "bat", "throw", "team", "g", "g_p", "g_sp", "g_rp",
    "g_c", "g_1b", "g_2b", "g_3b", "g_ss", "g_lf", "g_cf", "g_rf", "g_of",
    "g_dh", "g_ph", "g_pr", "first_g", "last_g", "season")

#: The sixteen count columns, in file order, and the name each becomes in the
#: derived table. `g` is games played and is **not** the sum of the rest: a
#: player who caught and pinch-hit in the same game is one game and two
#: positions.
APPEARANCE_COUNTS = ("g", "g_p", "g_sp", "g_rp", "g_c", "g_1b", "g_2b", "g_3b",
                     "g_ss", "g_lf", "g_cf", "g_rf", "g_of", "g_dh", "g_ph",
                     "g_pr")

#: `0` in `first_g`/`last_g` -- 1,494 of 11,476 rows. Not a date, and not
#: 1 January of year zero either: Retrosheet has no game date for that
#: player-season. It becomes NULL, for the same reason `bases_before` does on
#: an unparsed play.
NO_DATE = "0"


@dataclass(frozen=True)
class Appearance:
    """One row of `allplayers.csv`: a person's season with one club.

    **Not the whole corpus.** The file covers 1903-1962 and 3,422 people, all
    of them Negro Leagues -- every id in it is already in a roster or in
    `biofile.csv`, which is why it adds no *person* and was left out of the
    reference pass (spec/05-DATABASE.md §1.2). What it adds is where those
    people played, which the event files can only answer for the games that
    survive.

    A person may hold several of these in one season: 1,718 of the 11,476 rows
    are a second club for a player already counted, and the file is keyed on
    the three of `(id, season, team)` together.
    """

    person_id: str
    last: str
    first: str
    bats: str | None
    throws: str | None
    team_id: str
    counts: dict[str, int]
    first_game: str | None
    last_game: str | None
    season: int


def read_appearances(lines: list[str]) -> list[Appearance]:
    """Read `allplayers.csv`, checking its shape before believing a field.

    The counts are checked against each other where the file lets them be:
    `g_p` is `g_sp + g_rp` in all 11,476 rows, so that one is a hard gate. The
    outfield is **not**: `g_of` disagrees with `g_lf + g_cf + g_rf` in 207
    rows, because a game in an unspecified outfield spot is recorded as
    outfield and nothing more. Asserting the sum there would fail on correct
    data, which is the failure mode that gets a check deleted rather than
    fixed.
    """
    rows = _rows(lines)
    if not rows:
        return []
    _check_header(rows[0], APPEARANCE_HEADER, "allplayers.csv")
    idx = {name: i for i, name in enumerate(APPEARANCE_HEADER)}
    out = []
    for n, row in enumerate(rows[1:], 2):
        if len(row) != len(APPEARANCE_HEADER):
            raise LayoutError(f"allplayers.csv line {n}: {len(row)} columns")
        f = [c.strip() for c in row]
        if len(f[0]) != PLAYER_ID_LENGTH:
            raise LayoutError(
                f"allplayers.csv line {n}: player id {f[0]!r}")
        counts = {}
        for name in APPEARANCE_COUNTS:
            value = f[idx[name]]
            if not value.isdigit():
                raise LayoutError(
                    f"allplayers.csv line {n}: {name} is {value!r}")
            counts[name] = int(value)
        if counts["g_p"] != counts["g_sp"] + counts["g_rp"]:
            raise LayoutError(
                f"allplayers.csv line {n}: g_p {counts['g_p']} is not"
                f" g_sp {counts['g_sp']} + g_rp {counts['g_rp']}")
        if f[idx["bat"]] not in BATS or f[idx["throw"]] not in THROWS:
            raise LayoutError(
                f"allplayers.csv line {n}: bats/throws"
                f" {f[idx['bat']]!r}/{f[idx['throw']]!r}")
        if not f[idx["season"]].isdigit():
            raise LayoutError(
                f"allplayers.csv line {n}: season {f[idx['season']]!r}")
        out.append(Appearance(
            f[0], f[1], f[2], f[idx["bat"]] or None, f[idx["throw"]] or None,
            f[idx["team"]], counts,
            _game_date(f[idx["first_g"]]), _game_date(f[idx["last_g"]]),
            int(f[idx["season"]])))
    return out


def _game_date(value: str) -> str | None:
    """`YYYYMMDD` as ISO, or None for the `0` that means "no date known"."""
    if value == NO_DATE or not value:
        return None
    if len(value) != 8 or not value.isdigit():
        raise LayoutError(f"allplayers.csv: game date {value!r}")
    return f"{value[:4]}-{value[4:6]}-{value[6:]}"
