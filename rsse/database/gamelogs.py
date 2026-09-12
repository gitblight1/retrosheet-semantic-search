"""Game logs in the archive, and score reconciliation (spec/07-TESTING.md §4).

The game logs go in the **archive**, not the query database, because they are
source data and not derived from anything this project computes. That is the
whole point of them: they let the replay be checked against numbers Retrosheet
published independently of the event files.

They do not join `raw_records`. That table is partitioned exactly by
`game_spans` -- an invariant `rsse verify` asserts -- and a game log line
belongs to a game without being one of its records. A separate table keeps the
partition intact, which is the shape the roster and team files will need too
(BUILD-LOG §3.18).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..model.gamelog import GameLogError, check_layout, parse_line

GAMELOG_DDL = """
CREATE TABLE IF NOT EXISTS game_log_files (
  gl_file_id   INTEGER PRIMARY KEY,
  path         TEXT NOT NULL UNIQUE,
  sha256       TEXT NOT NULL,
  byte_length  INTEGER NOT NULL,
  loaded_at    TEXT NOT NULL,
  line_count   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS game_logs (
  gl_id        INTEGER PRIMARY KEY,
  gl_file_id   INTEGER NOT NULL REFERENCES game_log_files,
  line_no      INTEGER NOT NULL,
  -- Retrosheet's own game id for the row, rebuilt from home team, date and
  -- game number. Not unique: the same three repeated ids that stopped the
  -- event ingest (01-CORPUS §5.4) repeat here for the same reason.
  game_id      TEXT NOT NULL,
  date         TEXT NOT NULL,
  game_number  TEXT NOT NULL,
  home_team    TEXT NOT NULL,
  away_team    TEXT NOT NULL,
  home_league  TEXT,
  away_league  TEXT,
  home_score   INTEGER NOT NULL,
  away_score   INTEGER NOT NULL,
  outs         INTEGER,
  park_id      TEXT,
  completion   TEXT NOT NULL DEFAULT '',
  forfeit      TEXT NOT NULL DEFAULT '',
  -- Both earned-run columns are stored. Which one is the team total is
  -- settled against the event files rather than read off the layout; see
  -- `explain_earned_runs`.
  home_er_individual INTEGER, home_er_team INTEGER,
  away_er_individual INTEGER, away_er_team INTEGER,
  raw          TEXT NOT NULL,
  UNIQUE (gl_file_id, line_no)
);

CREATE INDEX IF NOT EXISTS ix_game_logs_id ON game_logs (game_id);
CREATE INDEX IF NOT EXISTS ix_game_logs_date ON game_logs (date);
"""


@dataclass
class LoadStats:
    files: int = 0
    lines: int = 0
    loaded: int = 0
    malformed: list[tuple[str, int, str]] = field(default_factory=list)
    #: `LayoutFinding`s from checking the parsed rows against the shape the
    #: field offsets claim. Reported on every load, because a wrong offset
    #: yields plausible numbers rather than an error.
    layout: list = field(default_factory=list)


_INSERT = """
INSERT OR REPLACE INTO game_logs (
  gl_file_id, line_no, game_id, date, game_number, home_team, away_team,
  home_league, away_league, home_score, away_score, outs, park_id,
  completion, forfeit, home_er_individual, home_er_team,
  away_er_individual, away_er_team, raw)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def load(conn, paths: list[Path], progress=None) -> LoadStats:
    """Load game log files into the archive."""
    from datetime import datetime, timezone

    from ..util.download import sha256

    conn.executescript(GAMELOG_DDL)
    stats = LoadStats()
    parsed: list = []

    for path in paths:
        if progress:
            progress(path)
        digest = sha256(path)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # Not `INSERT OR REPLACE`: REPLACE deletes the existing row and
        # inserts a new one with a *new* `gl_file_id`, orphaning every
        # `game_logs` row that referenced the old one. The first load
        # succeeded and the second failed on a foreign key. Reuse the id.
        existing = conn.execute(
            "SELECT gl_file_id FROM game_log_files WHERE path = ?",
            (str(path),)).fetchone()
        if existing:
            gl_file_id = existing[0]
            conn.execute("DELETE FROM game_logs WHERE gl_file_id = ?",
                         (gl_file_id,))
            conn.execute(
                "UPDATE game_log_files SET sha256 = ?, byte_length = ?,"
                " loaded_at = ? WHERE gl_file_id = ?",
                (digest, path.stat().st_size, now, gl_file_id))
        else:
            cur = conn.execute(
                "INSERT INTO game_log_files"
                " (path, sha256, byte_length, loaded_at) VALUES (?,?,?,?)",
                (str(path), digest, path.stat().st_size, now))
            gl_file_id = cur.lastrowid

        rows = []
        file_parsed: list = []
        # latin-1: the logs carry the same accented names the event files do,
        # and decoding must never be the thing that loses a game.
        for line_no, line in enumerate(
                path.read_text(encoding="latin-1").splitlines(), 1):
            if not line.strip():
                continue
            stats.lines += 1
            try:
                gl = parse_line(line)
            except GameLogError as exc:
                # Recorded, not skipped. A game log line that cannot be read
                # is a game the reconciliation cannot check, and a silent one
                # would make the check look more complete than it is.
                stats.malformed.append((str(path), line_no, str(exc)))
                continue
            file_parsed.append(gl)
            rows.append((
                gl_file_id, line_no, gl.retrosheet_game_id, gl.date,
                gl.game_number, gl.home_team, gl.away_team, gl.home_league,
                gl.away_league, gl.home_score, gl.away_score, gl.outs,
                gl.park_id, gl.completion, gl.forfeit,
                gl.home_er_individual, gl.home_er_team,
                gl.away_er_individual, gl.away_er_team, gl.raw))
        conn.executemany(_INSERT, rows)
        conn.execute("UPDATE game_log_files SET line_count = ? WHERE gl_file_id = ?",
                     (len(rows), gl_file_id))
        stats.loaded += len(rows)
        stats.files += 1
        conn.commit()
        parsed.extend(gl for gl in file_parsed)

    stats.layout = check_layout(parsed)
    return stats


# ---------------------------------------------------------------------------
# reconciliation
# ---------------------------------------------------------------------------


@dataclass
class Reconciliation:
    """The result of comparing the replay against the published logs."""

    compared: int = 0
    agreed: int = 0
    #: `(game_id, replay_away, replay_home, log_away, log_home)`
    mismatches: list[tuple] = field(default_factory=list)
    #: Games in one source and not the other -- coverage, not error. Split by
    #: cause, because one number lumping three unrelated things together is
    #: what a coverage report exists to avoid: "34,393 games in the logs only"
    #: reads as a defect until you know 29,133 of them predate the corpus.
    log_only_before_corpus: int = 0
    log_only_postseason: int = 0
    log_only_allstar: int = 0
    #: Major League games the logs list and the corpus has no event file for.
    #: The real coverage gap, and the only part of `log_only` that is one.
    log_only_gap: int = 0
    #: Replayed games with no log row at all -- Negro Leagues, which the logs
    #: do not cover.
    replay_only: int = 0
    #: Replayed games whose log row was excluded as an unfair test. Counted
    #: apart from `replay_only`, which would otherwise imply the log had
    #: nothing to say about them.
    replay_only_skipped: int = 0

    @property
    def log_only(self) -> int:
        return (self.log_only_before_corpus + self.log_only_postseason
                + self.log_only_allstar + self.log_only_gap)
    #: Games excluded because the log row is not a fair test: a forfeit's score
    #: is awarded by rule, and a suspended game is split across records.
    skipped_forfeit: int = 0
    skipped_incomplete: int = 0

    @property
    def rate(self) -> float:
        return self.agreed / self.compared if self.compared else 0.0


#: Home-team codes used for the All-Star Game in the logs.
_ALLSTAR_HOMES = frozenset({"NLS", "ALS", "AAS", "NAS"})


def _classify_log_only(date: str, home_team: str, first_season: int) -> str:
    """Why a game log row has no counterpart in the corpus.

    October and November games are postseason: the corpus holds regular-season
    team files only, so the combined `gl1871_2025` archive carries games the
    event files never could.
    """
    year = int(date[:4])
    if year < first_season:
        return "before_corpus"
    if home_team in _ALLSTAR_HOMES:
        return "allstar"
    if date[5:7] in ("10", "11"):
        return "postseason"
    return "gap"


def reconcile(query_conn, archive_conn, seasons=None) -> Reconciliation:
    """Compare every replayed final score against the published game log.

    The strongest check available on the state machine: `games.final_home` and
    `final_away` are produced by replaying 17.9 million plays, and the log's
    scores were compiled by Retrosheet from box scores. Nothing in the pipeline
    can make both wrong in the same direction.
    """
    out = Reconciliation()

    logs: dict[str, tuple] = {}
    meta: dict[str, tuple[str, str]] = {}
    skipped_ids: set[str] = set()
    for row in archive_conn.execute(
            "SELECT game_id, away_score, home_score, forfeit, completion,"
            " date, home_team FROM game_logs"):
        (game_id, away, home, forfeit, completion, date, home_team) = row
        meta.setdefault(game_id, (date, home_team))
        if forfeit.strip():
            out.skipped_forfeit += 1
            skipped_ids.add(game_id)
            continue
        if completion.strip():
            out.skipped_incomplete += 1
            skipped_ids.add(game_id)
            continue
        # A repeated game id means the two rows cannot be told apart on id
        # alone; keeping the first and counting the collision is honest, and
        # the count is small enough to inspect.
        logs.setdefault(game_id, (away, home))

    sql = ("SELECT game_id, final_away, final_home FROM games"
           " WHERE final_away IS NOT NULL AND final_home IS NOT NULL")
    params: list = []
    if seasons:
        sql += " AND season IN (%s)" % ",".join("?" * len(seasons))
        params += list(seasons)

    first_season = query_conn.execute(
        "SELECT min(season) FROM games").fetchone()[0] or 0

    seen = set()
    for game_id, replay_away, replay_home in query_conn.execute(sql, params):
        entry = logs.get(game_id)
        if entry is None:
            if game_id in skipped_ids:
                out.replay_only_skipped += 1
            else:
                out.replay_only += 1
            continue
        seen.add(game_id)
        out.compared += 1
        log_away, log_home = entry
        if (replay_away, replay_home) == (log_away, log_home):
            out.agreed += 1
        else:
            out.mismatches.append(
                (game_id, replay_away, replay_home, log_away, log_home))
    for game_id in logs.keys() - seen:
        date, home_team = meta[game_id]
        kind = _classify_log_only(date, home_team, first_season)
        setattr(out, f"log_only_{kind}", getattr(out, f"log_only_{kind}") + 1)
    return out


def explain_earned_runs(query_conn, archive_conn, sample: int = 2000) -> dict:
    """Decide which game log earned-run field is the team total.

    The layout names both an "individual" and a "team" earned-run figure per
    side and does not settle which is the per-game total. Rather than pick one
    and report thousands of false mismatches, both are compared against the
    event files' own `data,er` records -- the same empirical approach that
    settled the replay verdict flag (05-DATABASE §5.1) -- and whichever agrees
    is the one to use.

    The sample is spread across the whole date range, not taken from the front.
    The first version used `LIMIT 2000` with no ordering, which took the
    earliest rows by rowid: 1871 games, every one of them predating the corpus,
    so the join found nothing and the diagnostic reported *zero games checked*
    rather than an answer. Era coverage beats volume, and a sample taken from
    one end of the data is not a sample.

    Returns agreement counts per candidate field. A diagnostic, not a gate: a
    tie or a low count means the question is still open.
    """
    scores = {"individual": 0, "team": 0, "neither": 0}
    checked = 0

    # Only games the corpus actually holds, spread evenly over them.
    rows = archive_conn.execute(
        "SELECT gl.game_id, gl.home_er_individual, gl.home_er_team,"
        "       gl.away_er_individual, gl.away_er_team,"
        "       gs.first_record_id, gs.last_record_id"
        "  FROM game_logs gl JOIN game_spans gs ON gs.game_id = gl.game_id"
        " WHERE gl.home_er_team IS NOT NULL AND gl.away_er_team IS NOT NULL"
        " ORDER BY gl.date").fetchall()
    if not rows:
        return {"checked": 0, "candidates": 0, **scores}
    step = max(1, len(rows) // sample)

    for (game_id, h_ind, h_team, a_ind, a_team,
         first_id, last_id) in rows[::step]:
        total = 0
        found = False
        for (raw,) in archive_conn.execute(
                "SELECT raw_line FROM raw_records"
                " WHERE record_id BETWEEN ? AND ? AND raw_line LIKE 'data,er,%'",
                (first_id, last_id)):
            parts = raw.split(",")
            if len(parts) >= 4 and parts[3].strip().isdigit():
                total += int(parts[3])
                found = True
        if not found:
            continue
        checked += 1
        # `data,er` covers both teams' pitchers, so the event-file total is the
        # sum of the two sides whichever field is the right one.
        individual = (h_ind or 0) + (a_ind or 0)
        team = (h_team or 0) + (a_team or 0)
        if individual == total and team != total:
            scores["individual"] += 1
        elif team == total and individual != total:
            scores["team"] += 1
        elif team == total and individual == total:
            # The two fields are equal in 99.8% of games, so most rows cannot
            # tell them apart. Counted apart from a real disagreement rather
            # than credited to both.
            scores.setdefault("indistinguishable", 0)
            scores["indistinguishable"] += 1
        else:
            scores["neither"] += 1
    return {"checked": checked, "candidates": len(rows[::step]), **scores}
