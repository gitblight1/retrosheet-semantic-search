"""`people`, `roster_entries`, `teams`, `franchises`, `parks`
(spec/05-DATABASE.md §8).

Built from the **archive's** `aux_records`, never from the file tree, so a
Retrosheet correction cannot change a derived table without an explicit
re-ingest. Its own pass, like `secondary` and `earned-runs`: nothing here
needs the state machine, so it runs in seconds and can be rebuilt without
touching 17.9 million plays.

Two decisions shape every table here.

**Verbatim, not normalised.** Retrosheet spells the majors `A`/`N` in 88
seasons and `AL`/`NL` in 1920-1949, with no season using both. Both reach
`teams.league` unchanged. It is Retrosheet's notation to reconcile, and
rewriting it here would make the derived table disagree with the archive it
came from for no gain the query layer cannot get with an `IN`.

**Every observed id gets a row, with NULL where nothing is known.** Four
people appear in the corpus with no roster line and no biography, and seven
ballparks host games that `ballparks.csv` has never heard of. Omitting them
would make a person nobody recorded indistinguishable from a broken join;
a row with null attributes says which, and `people.source` says where the
row came from.
"""

from __future__ import annotations

import collections
import re
from dataclasses import dataclass, field

from ..model import reference as ref

REFERENCE_DDL = """
CREATE TABLE IF NOT EXISTS people (
  person_id      TEXT PRIMARY KEY,
  last           TEXT,
  first          TEXT,
  nickname       TEXT,
  birthdate      TEXT,
  birth_city     TEXT,
  birth_state    TEXT,
  birth_country  TEXT,
  play_debut     TEXT,
  play_last      TEXT,
  mgr_debut      TEXT,
  mgr_last       TEXT,
  coach_debut    TEXT,
  coach_last     TEXT,
  ump_debut      TEXT,
  ump_last       TEXT,
  deathdate      TEXT,
  -- Where the row came from, so a person with no biography is distinguishable
  -- from one nobody has looked up. 'observed' means the corpus names them and
  -- no reference file does -- four people, every one Negro Leagues, one of
  -- them recorded with no first name at all.
  source         TEXT NOT NULL CHECK (source IN ('bio','roster','observed'))
);

CREATE TABLE IF NOT EXISTS roster_entries (
  person_id      TEXT NOT NULL,
  season         INTEGER NOT NULL,
  -- The team whose roster file this line is in. Not the team the *line*
  -- names: 85 lines disagree with their own file, and keying on the field
  -- collides (`PH51933.ROS` carries a line reading `NY5`, which then repeats
  -- the row already in `NY51933.ROS`). The file is the stronger statement --
  -- a roster file is that club's roster -- and keying on it is unique across
  -- all 121,600 lines.
  team_id        TEXT NOT NULL,
  -- What the line said, when it differed. NULL when the two agree, so the
  -- disagreements stay countable instead of being quietly resolved.
  stated_team_id TEXT,
  last           TEXT,
  first          TEXT,
  bats           TEXT,
  throws         TEXT,
  position       TEXT,
  PRIMARY KEY (person_id, season, team_id)
);

CREATE TABLE IF NOT EXISTS teams (
  team_id        TEXT NOT NULL,
  season         INTEGER NOT NULL,
  -- Verbatim. `A` and `AL` are the same league spelled two ways in different
  -- eras; 344 rows have no league at all, which is a fact about independent
  -- and barnstorming clubs rather than a missing value.
  league         TEXT,
  city           TEXT,
  nickname       TEXT,
  PRIMARY KEY (team_id, season)
);

CREATE TABLE IF NOT EXISTS franchises (
  team_id        TEXT PRIMARY KEY,
  league         TEXT,
  city           TEXT,
  nickname       TEXT,
  first_season   INTEGER,
  last_season    INTEGER
);

CREATE TABLE IF NOT EXISTS parks (
  park_id        TEXT PRIMARY KEY,
  name           TEXT,
  aka            TEXT,
  city           TEXT,
  state          TEXT,
  -- As written, `MM/DD/YYYY`, or NULL. Most parks carry no dates; a park with
  -- no known opening is not one that opened at the epoch.
  start_date     TEXT,
  end_date       TEXT,
  -- The same two dates as ISO, so they can be compared to `games.date`.
  -- Derived, not verbatim: the file writes `4/20/1912`, unpadded, and naive
  -- string slicing of that produced a check that fired on 106,537 correct
  -- games. The archive keeps the original; this is what queries should use.
  start_iso      TEXT,
  end_iso        TEXT,
  league         TEXT,
  notes          TEXT,
  source         TEXT NOT NULL CHECK (source IN ('file','observed'))
);
"""

REFERENCE_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_roster_person ON roster_entries (person_id);
CREATE INDEX IF NOT EXISTS ix_roster_team ON roster_entries (team_id, season);
-- `.position_played()` drives its subquery off this rather than scanning
-- 121,600 lines: the three columns are the whole of what it selects, so the
-- index answers the query without touching the table.
CREATE INDEX IF NOT EXISTS ix_roster_position
  ON roster_entries (position, season, person_id);
CREATE INDEX IF NOT EXISTS ix_teams_season ON teams (season);
CREATE INDEX IF NOT EXISTS ix_people_name ON people (last, first);
"""

_ROSTER_NAME = re.compile(r"([A-Z0-9]{3})(\d{4})\.ROS$", re.I)


@dataclass
class ReferenceStats:
    people: int = 0
    roster_entries: int = 0
    teams: int = 0
    franchises: int = 0
    parks: int = 0
    #: Rows created only because the corpus names the id, with no reference
    #: file behind them. A coverage number, reported rather than hidden.
    people_observed_only: int = 0
    parks_observed_only: int = 0
    #: Roster lines whose team field disagrees with the file they are in.
    team_field_conflicts: int = 0
    #: Games played at a park outside the date range `ballparks.csv` states
    #: for it. A **diagnostic, not a gate** -- see `park_date_check`.
    games_outside_park_dates: int = 0
    games_outside_park_dates_ngl: int = 0
    games_park_date_comparable: int = 0
    by_source: collections.Counter = field(default_factory=collections.Counter)


def _aux(archive_conn, kind: str) -> list[tuple[str, int | None, str]]:
    """`(path, season, raw_line)` for one kind, in archive order."""
    return list(archive_conn.execute(
        "SELECT f.path, f.season, r.raw_line"
        "  FROM aux_records r JOIN aux_files f USING (aux_file_id)"
        " WHERE f.kind = ? ORDER BY r.aux_record_id", (kind,)))


def build(query_conn, archive_conn) -> ReferenceStats:
    """Build every reference table from the archive."""
    query_conn.executescript(REFERENCE_DDL)
    stats = ReferenceStats()
    for table in ("people", "roster_entries", "teams", "franchises", "parks"):
        query_conn.execute(f"DELETE FROM {table}")

    # --- people, from biographies first so the richest source wins ---
    people: dict[str, tuple] = {}
    bio = ref.read_people([l for _p, _s, l in _aux(archive_conn, "bio")])
    for person in bio:
        people[person.person_id] = (
            person.person_id, person.last, person.first, person.nickname,
            person.birthdate, person.birth_city, person.birth_state,
            person.birth_country, person.play_debut, person.play_last,
            person.mgr_debut, person.mgr_last, person.coach_debut,
            person.coach_last, person.ump_debut, person.ump_last,
            person.deathdate, "bio")

    # --- rosters ---
    roster_rows = []
    for path, season, line in _aux(archive_conn, "roster"):
        match = _ROSTER_NAME.search(path)
        entry = ref.parse_roster_line(line)
        team_id = match.group(1).upper() if match else entry.team_id
        stated = entry.team_id if entry.team_id != team_id else None
        if stated:
            stats.team_field_conflicts += 1
        roster_rows.append((entry.player_id, season, team_id, stated,
                            entry.last, entry.first, entry.bats,
                            entry.throws, entry.position))
        if entry.player_id not in people:
            # A roster names them and no biography does. Their name is known
            # and nothing else is, which is what the row should say.
            people[entry.player_id] = (
                entry.player_id, entry.last, entry.first, *([None] * 14),
                "roster")

    # --- teams and franchises ---
    team_rows = [(*_team(line), season)
                 for _path, season, line in _aux(archive_conn, "team")]
    franchises = ref.read_franchises(
        [l for _p, _s, l in _aux(archive_conn, "teamlist")])

    # --- parks ---
    parks = ref.read_parks([l for _p, _s, l in _aux(archive_conn, "park")])
    park_rows = [(p.park_id, p.name, p.aka, p.city, p.state, p.start,
                  p.end, _iso(p.start), _iso(p.end), p.league, p.notes,
                  "file") for p in parks]

    # --- every id the corpus names, whether or not a file explains it ---
    observed_people = {r[0] for r in query_conn.execute(
        "SELECT DISTINCT player_id FROM lineup_entries")}
    observed_people |= {r[0] for r in query_conn.execute(
        "SELECT DISTINCT batter_id FROM plays")}
    for person_id in sorted(observed_people - set(people)):
        people[person_id] = (person_id, *([None] * 16), "observed")
        stats.people_observed_only += 1

    known_parks = {p[0] for p in park_rows}
    for (site,) in query_conn.execute(
            "SELECT DISTINCT site FROM games"
            " WHERE site IS NOT NULL AND site <> ''"):
        if site not in known_parks:
            park_rows.append((site, *([None] * 10), "observed"))
            stats.parks_observed_only += 1

    query_conn.executemany(
        "INSERT INTO people VALUES (" + ",".join("?" * 18) + ")",
        list(people.values()))
    query_conn.executemany(
        "INSERT INTO roster_entries VALUES (?,?,?,?,?,?,?,?,?)", roster_rows)
    query_conn.executemany(
        "INSERT INTO teams (team_id, league, city, nickname, season)"
        " VALUES (?,?,?,?,?)", team_rows)
    query_conn.executemany(
        "INSERT INTO franchises VALUES (?,?,?,?,?,?)",
        [(f.team_id, f.league, f.city, f.nickname, f.first_season,
          f.last_season) for f in franchises])
    query_conn.executemany(
        "INSERT INTO parks VALUES (" + ",".join("?" * 12) + ")", park_rows)
    query_conn.executescript(REFERENCE_INDEXES)
    query_conn.commit()

    stats.people = len(people)
    stats.roster_entries = len(roster_rows)
    stats.teams = len(team_rows)
    stats.franchises = len(franchises)
    stats.parks = len(park_rows)
    stats.by_source = collections.Counter(v[-1] for v in people.values())
    park_date_check(query_conn, stats)
    return stats


def park_date_check(query_conn, stats: ReferenceStats) -> None:
    """Compare each game's date to its park's stated range.

    Proposed as a free external gate, and **measured before being believed**.
    It is not one. `ballparks.csv` dates a park by its *Major League*
    occupancy, not its lifetime, so Kansas City's Municipal Stadium reads
    1955-1972 while the corpus holds 198 Negro Leagues games there from 1924.
    409 of 121,489 comparable games fall outside their park's range and
    **380 of them (93%) are Negro Leagues**.

    Shipping it as a check would have fired on 409 correct games, which is the
    failure mode a firing guard always has ([07-TESTING](../../spec/07-TESTING.md)
    §4.3): the next real one gets waved through with it. So it is counted and
    reported, and the count is the finding.
    """
    row = query_conn.execute("""
        SELECT count(*),
               sum(g.date < p.start_iso OR g.date > p.end_iso),
               sum((g.date < p.start_iso OR g.date > p.end_iso)
                   AND g.league = 'NGL')
          FROM games g JOIN parks p ON p.park_id = g.site
         WHERE g.date IS NOT NULL
           AND p.start_iso IS NOT NULL AND p.end_iso IS NOT NULL""").fetchone()
    stats.games_park_date_comparable = row[0] or 0
    stats.games_outside_park_dates = row[1] or 0
    stats.games_outside_park_dates_ngl = row[2] or 0


def _iso(value: str | None) -> str | None:
    """`M/D/YYYY` -> `YYYY-MM-DD`. None for anything that is not a date.

    The month and day are not zero-padded in the file, which is exactly why
    this is a function and not a `substr` expression in SQL.
    """
    if not value:
        return None
    parts = value.split("/")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    month, day, year = parts
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _team(line: str) -> tuple:
    entry = ref.parse_team_line(line)
    return (entry.team_id, entry.league, entry.city, entry.nickname)
