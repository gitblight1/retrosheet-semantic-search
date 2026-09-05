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
