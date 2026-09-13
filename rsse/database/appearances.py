"""`appearances` -- `allplayers.csv`, a season at a time (spec/05-DATABASE.md §9).

Its own pass, and the reason is worth stating rather than assumed. Everything
`reference` builds answers *who someone is*: a name, a birthplace, a club, a
ballpark. This answers **how much someone played and where on the field**,
which is a statistic about a person rather than a fact identifying one. Every
id here is already in `people`, so folding it into `reference` would add a
table to a pass whose contract it does not share, and would make the one
question this table is good at -- what the corpus is missing -- look like part
of the identity layer.

Built from the archive's `aux_records`, like every other reference table, so a
reissued file cannot change it without an explicit re-ingest.

**The file is Negro Leagues, 1903-1962.** Not "all players", whatever its
name: 3,422 people over 49 seasons. That narrowness is the point. For those
seasons Retrosheet knows how many games a player played from sources the event
files do not contain, which makes this the only place in the project where the
corpus can be measured against an outside count of the same thing -- and
`rsse coverage` already reports 3,433 games with no event file. `coverage`
counts the games that are missing; this counts the *playing* that is missing,
and the two do not have to agree.

The comparison is a **diagnostic, not a gate**, for the same reason the park
date check is (`reference.park_date_check`): a disagreement here is a fact
about what survives, and a check that fires on 3,422 correct rows is a check
somebody deletes.

On the current corpus it reads 47,713 of 110,772 games, **43.1%** -- a sharper
statement of the Negro Leagues gap than a game count can make, because one
surviving game covers eighteen players and one lost game loses eighteen. 207
rows run the other way, the corpus holding more than the file counts, and they
are left counted rather than explained: they are concentrated in 1933-34 and
the excess is not only exhibition play, so the two sources are counting
different things and which definition the file uses is Retrosheet's to state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..model import reference as ref

APPEARANCE_DDL = """
CREATE TABLE IF NOT EXISTS appearances (
  person_id   TEXT NOT NULL,
  season      INTEGER NOT NULL,
  -- The club, and part of the key: 1,718 of 11,476 rows are a second club for
  -- a player already counted that season, and summing them into one row would
  -- lose which club the games were played for.
  team_id     TEXT NOT NULL,
  last        TEXT,
  first       TEXT,
  bats        TEXT,
  throws      TEXT,
  -- ISO, or NULL where the file writes `0` -- 1,494 rows, meaning Retrosheet
  -- has no date, not that the player debuted at the epoch.
  first_game  TEXT,
  last_game   TEXT,

  -- Games played, and **not** the sum of the position columns: a player who
  -- caught and then pinch-hit is one game and two positions.
  g           INTEGER NOT NULL,
  g_p         INTEGER NOT NULL,
  g_sp        INTEGER NOT NULL,
  g_rp        INTEGER NOT NULL,
  g_c         INTEGER NOT NULL,
  g_1b        INTEGER NOT NULL,
  g_2b        INTEGER NOT NULL,
  g_3b        INTEGER NOT NULL,
  g_ss        INTEGER NOT NULL,
  g_lf        INTEGER NOT NULL,
  g_cf        INTEGER NOT NULL,
  g_rf        INTEGER NOT NULL,
  -- Outfield without a side. Not `g_lf + g_cf + g_rf`: it disagrees with that
  -- sum in 207 rows, because a game in an unspecified outfield spot is
  -- recorded here and nowhere else.
  g_of        INTEGER NOT NULL,
  g_dh        INTEGER NOT NULL,
  g_ph        INTEGER NOT NULL,
  g_pr        INTEGER NOT NULL,

  -- Games in the *corpus* that name this person in this season for this club.
  -- The whole reason the table is worth having, and a measurement rather than
  -- a copy: `g` is what Retrosheet counted from box scores and newspapers,
  -- this is what the event files actually hold. NULL is not possible -- a
  -- person-season with no surviving game is a zero, and a zero here has to be
  -- earned.
  games_in_corpus INTEGER NOT NULL,
  PRIMARY KEY (person_id, season, team_id)
);
"""

APPEARANCE_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_appear_season ON appearances (season);
CREATE INDEX IF NOT EXISTS ix_appear_team ON appearances (team_id, season);
"""

#: Column order for the insert, matching the DDL above. Named rather than
#: positional for the reason `derived._PLAY_COLUMNS` is: a 26-column
#: positional insert is a standing invitation to a silent shift.
COLUMNS = ("person_id", "season", "team_id", "last", "first", "bats",
           "throws", "first_game", "last_game") + ref.APPEARANCE_COUNTS + (
           "games_in_corpus",)


@dataclass
class AppearanceStats:
    rows: int = 0
    people: int = 0
    seasons: tuple[int, int] = (0, 0)
    #: Person-seasons the file records and the corpus holds no game for.
    absent_from_corpus: int = 0
    #: Games the file counts, and games of those the corpus can show. Both
    #: summed over the same rows, so the ratio means something.
    games_stated: int = 0
    games_held: int = 0
    #: Rows whose corpus count *exceeds* the file's. Not an error to fix here
    #: -- it is one of the two sources being wrong, and which one is not this
    #: pass's to decide -- but it is a number that should be small and visible.
    more_in_corpus: int = 0
    #: Team ids in the file that `teams` has never heard of. Two of them, CUX
    #: and PHG, are 1903-04 clubs with no `TEAM####` file and no surviving
    #: game; `franchises` does know them.
    unknown_teams: list = field(default_factory=list)


def build(query_conn, archive_conn) -> AppearanceStats:
    """Build `appearances` from the archive, measuring against the corpus."""
    query_conn.executescript(APPEARANCE_DDL)
    query_conn.execute("DELETE FROM appearances")
    rows = ref.read_appearances([
        line for (line,) in archive_conn.execute(
            "SELECT r.raw_line FROM aux_records r"
            "  JOIN aux_files f USING (aux_file_id)"
            " WHERE f.kind = 'appearances' ORDER BY r.aux_record_id")])
    stats = AppearanceStats()
    if not rows:
        return stats

    lo = min(a.season for a in rows)
    hi = max(a.season for a in rows)
    held = _games_in_corpus(query_conn, lo, hi)
    out = []
    for a in rows:
        n = held.get((a.person_id, a.season, a.team_id), 0)
        out.append((a.person_id, a.season, a.team_id, a.last, a.first,
                    a.bats, a.throws, a.first_game, a.last_game,
                    *(a.counts[c] for c in ref.APPEARANCE_COUNTS), n))
        stats.games_stated += a.counts["g"]
        stats.games_held += n
        if n == 0:
            stats.absent_from_corpus += 1
        elif n > a.counts["g"]:
            stats.more_in_corpus += 1
    query_conn.executemany(
        "INSERT INTO appearances (" + ",".join(COLUMNS) + ") VALUES ("
        + ",".join("?" * len(COLUMNS)) + ")", out)
    query_conn.executescript(APPEARANCE_INDEXES)
    query_conn.commit()

    stats.rows = len(out)
    stats.people = len({a.person_id for a in rows})
    stats.seasons = (min(a.season for a in rows), max(a.season for a in rows))
    known = {r[0] for r in query_conn.execute("SELECT team_id FROM teams")}
    stats.unknown_teams = sorted({a.team_id for a in rows} - known)
    return stats


def _games_in_corpus(conn, lo: int, hi: int) -> dict[tuple[str, int, str], int]:
    """`(person, season, team) -> distinct games` from the lineups.

    Keyed on the *club the person appeared for*, which the lineup does not
    store: `lineup_entries.team` is 0 visitor / 1 home, so the club has to
    come from the game. That is what the `CASE` is, and getting it backwards
    would attribute every road game to the wrong side and still produce a
    plausible-looking count.

    Restricted to the seasons the file covers, which is why they are passed
    in rather than read back from `appearances`: the table has just been
    emptied and has not been filled yet, so asking it for its season range
    returns NULL and this returns an empty map. Every `games_in_corpus` would
    then be zero, every row would look absent from a corpus that holds it, and
    the diagnostic would be uniformly wrong in the direction that reads as a
    finding. Written the other way first.
    """
    return {(person, season, team): n for person, season, team, n
            in conn.execute(
                "SELECT l.player_id, g.season,"
                "       CASE l.team WHEN 1 THEN g.home_team ELSE g.away_team END,"
                "       COUNT(DISTINCT l.game_key)"
                "  FROM lineup_entries l JOIN games g USING (game_key)"
                " WHERE g.season BETWEEN ? AND ?"
                " GROUP BY 1, 2, 3", (lo, hi))}
