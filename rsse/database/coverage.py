"""The `coverage` table (spec/05-DATABASE.md §5).

Coverage is what makes an empty result set readable. Without it, no rows means
"never happened"; with it, no rows means "no such play in the N games from YYYY
to YYYY", which is the only claim the data supports
(spec/01-CORPUS.md §5.2).

Everything here is a GROUP BY over `games` and `plays`, so it is rebuilt from
the derived tables in seconds rather than carried through the load.
"""

from __future__ import annotations

COVERAGE_DDL = """
CREATE TABLE IF NOT EXISTS coverage (
  corpus_id INTEGER NOT NULL,
  season INTEGER NOT NULL, league TEXT NOT NULL,
  games INTEGER NOT NULL, teams INTEGER NOT NULL,
  first_date TEXT NOT NULL, last_date TEXT NOT NULL,
  games_with_pitches INTEGER NOT NULL,
  games_with_count_only INTEGER NOT NULL,
  games_without_pitch_data INTEGER NOT NULL,
  plays INTEGER NOT NULL, plays_unparsed INTEGER NOT NULL,
  plays_inconsistent INTEGER NOT NULL,
  -- Games Retrosheet's own game logs list for this season and league that the
  -- corpus has no event file for. NULL when the game logs have not been
  -- loaded, which is *not* the same as zero: "we know of no missing games" and
  -- "we have not looked" must not read alike.
  games_missing INTEGER,
  PRIMARY KEY (corpus_id, season, league)
);
"""

#: `league` is NOT NULL in the table but is derived from the file suffix, which
#: is absent for a handful of games. '??' keeps them countable rather than
#: dropping them from coverage entirely -- a game missing from the coverage
#: report is exactly the error this table exists to prevent.
_UNKNOWN_LEAGUE = "??"

_BUILD = """
INSERT INTO coverage (corpus_id, season, league, games, teams,
                      first_date, last_date, games_with_pitches,
                      games_with_count_only, games_without_pitch_data,
                      plays, plays_unparsed, plays_inconsistent)
SELECT
  :corpus_id,
  g.season,
  coalesce(g.league, :unknown),
  count(*),
  count(DISTINCT g.home_team),
  min(coalesce(g.date, '')),
  max(coalesce(g.date, '')),
  sum(CASE WHEN g.pitch_detail = 'pitches' THEN 1 ELSE 0 END),
  sum(CASE WHEN g.pitch_detail = 'count' THEN 1 ELSE 0 END),
  sum(CASE WHEN g.pitch_detail IS NULL OR g.pitch_detail
           NOT IN ('pitches','count') THEN 1 ELSE 0 END),
  sum(g.plays),
  0, 0
FROM games g
WHERE g.season IS NOT NULL
GROUP BY g.season, coalesce(g.league, :unknown)
"""

#: Play-level status counts are a second pass. Rolling them into the games
#: aggregate above would need a join from games to plays and turn a scan of
#: 203k rows into a scan of 17.9M.
_STATUS = """
UPDATE coverage SET
  plays_unparsed = coalesce((
    SELECT count(*) FROM plays p JOIN games g USING (game_key)
     WHERE g.season = coverage.season
       AND coalesce(g.league, :unknown) = coverage.league
       AND p.parse_status = 'unparsed'), 0),
  plays_inconsistent = coalesce((
    SELECT count(*) FROM plays p JOIN games g USING (game_key)
     WHERE g.season = coverage.season
       AND coalesce(g.league, :unknown) = coverage.league
       AND p.parse_status IN ('state_inconsistent','state_ambiguous',
                              'state_untrusted')), 0)
WHERE corpus_id = :corpus_id
"""


def build(conn, corpus_id: int = 1) -> int:
    """Rebuild the coverage table. Returns the number of (season, league) rows."""
    conn.executescript(COVERAGE_DDL)
    conn.execute("DELETE FROM coverage WHERE corpus_id = ?", (corpus_id,))
    conn.execute(_BUILD, {"corpus_id": corpus_id, "unknown": _UNKNOWN_LEAGUE})
    conn.execute(_STATUS, {"corpus_id": corpus_id, "unknown": _UNKNOWN_LEAGUE})
    conn.commit()
    return conn.execute("SELECT count(*) FROM coverage WHERE corpus_id = ?",
                        (corpus_id,)).fetchone()[0]


#: Added after `coverage` first shipped; `CREATE TABLE IF NOT EXISTS` will not
#: add it to an existing table (the same gap that bit `game_logs.series`).
_MIGRATIONS = (("games_missing", "INTEGER"),)


def _migrate(conn) -> None:
    present = {r[1] for r in conn.execute("PRAGMA table_info(coverage)")}
    if not present:
        return
    for column, decl in _MIGRATIONS:
        if column not in present:
            conn.execute(f"ALTER TABLE coverage ADD COLUMN {column} {decl}")
    conn.commit()


def fill_missing_games(conn, archive, corpus_id: int = 1) -> int:
    """Record, per season and league, the games the corpus has no file for.

    Counted against Retrosheet's own game logs, which is the only external
    list of what *should* exist. Postseason and all-star games are excluded by
    `series`: the corpus holds regular-season team files and never claimed to
    hold the others, so counting those as missing would turn a design boundary
    into a defect.

    Returns the total. Leaves `games_missing` NULL and returns -1 if the game
    logs are absent, because a coverage report that says "0 missing" when
    nothing was checked is worse than one that says nothing.
    """
    _migrate(conn)
    have = archive.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table'"
        " AND name='game_logs'").fetchone()[0]
    if not have:
        return -1
    series_sql = "series" if any(
        r[1] == "series" for r in archive.execute("PRAGMA table_info(game_logs)")
    ) else "'regular'"

    known = {r[0] for r in conn.execute("SELECT game_id FROM games")}
    first, last = conn.execute(
        "SELECT min(season), max(season) FROM games").fetchone()
    missing: dict[tuple, int] = {}
    #: (season, league) pairs the logs say anything at all about. A pair with
    #: no log rows is *unmeasured*, not complete -- Retrosheet's game logs
    #: cover the Major Leagues, so all 36 Negro Leagues rows would otherwise
    #: record `games_missing = 0` and read as "nothing is missing" when the
    #: truth is "we have no list to check against". That is the same mistake
    #: as storing 0 for an unloaded corpus, one level down.
    covered: set[tuple] = set()
    for game_id, date, league in archive.execute(
            f"SELECT game_id, date, home_league FROM game_logs"
            f" WHERE {series_sql} = 'regular'"):
        season = int(date[:4])
        if season < (first or 0) or season > (last or 0):
            continue
        key = (season, league or _UNKNOWN_LEAGUE)
        covered.add(key)
        if game_id in known:
            continue
        missing[key] = missing.get(key, 0) + 1

    conn.execute("UPDATE coverage SET games_missing = NULL WHERE corpus_id = ?",
                 (corpus_id,))
    for season, league in covered:
        conn.execute(
            "UPDATE coverage SET games_missing = ?"
            " WHERE corpus_id = ? AND season = ? AND league = ?",
            (missing.get((season, league), 0), corpus_id, season, league))
    conn.commit()
    return sum(missing.values())

