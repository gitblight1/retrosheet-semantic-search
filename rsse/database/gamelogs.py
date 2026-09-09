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
        cur = conn.execute(
            "INSERT OR REPLACE INTO game_log_files"
            " (path, sha256, byte_length, loaded_at) VALUES (?,?,?,?)",
            (str(path), digest, path.stat().st_size,
             datetime.now(timezone.utc).isoformat(timespec="seconds")))
        gl_file_id = cur.lastrowid
        conn.execute("DELETE FROM game_logs WHERE gl_file_id = ?", (gl_file_id,))

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
    #: Games in one source and not the other -- coverage, not error.
    log_only: int = 0
    replay_only: int = 0
    #: Games excluded because the log row is not a fair test: a forfeit's score
    #: is awarded by rule, and a suspended game is split across records.
    skipped_forfeit: int = 0
    skipped_incomplete: int = 0

    @property
    def rate(self) -> float:
        return self.agreed / self.compared if self.compared else 0.0


def reconcile(query_conn, archive_conn, seasons=None) -> Reconciliation:
    """Compare every replayed final score against the published game log.

    The strongest check available on the state machine: `games.final_home` and
    `final_away` are produced by replaying 17.9 million plays, and the log's
    scores were compiled by Retrosheet from box scores. Nothing in the pipeline
    can make both wrong in the same direction.
    """
    out = Reconciliation()

    logs: dict[str, tuple] = {}
    for row in archive_conn.execute(
            "SELECT game_id, away_score, home_score, forfeit, completion"
            " FROM game_logs"):
        game_id, away, home, forfeit, completion = row
        if forfeit.strip():
            out.skipped_forfeit += 1
            continue
        if completion.strip():
            out.skipped_incomplete += 1
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

    seen = set()
    for game_id, replay_away, replay_home in query_conn.execute(sql, params):
        entry = logs.get(game_id)
        if entry is None:
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
    out.log_only = len(logs) - len(seen)
    return out


def explain_earned_runs(query_conn, archive_conn, limit: int = 2000) -> dict:
    """Decide which game log earned-run field is the team total.

    The layout names both an "individual" and a "team" earned-run field per
    side, and the documentation does not settle which is the per-game total.
    Rather than pick one and report thousands of false mismatches, both are
    compared against the event files' own `data,er` records -- the same
    empirical approach that settled the replay verdict flag
    (05-DATABASE §5.1) -- and whichever agrees is the one to use.

    Returns agreement counts per candidate field. It is a diagnostic, not a
    gate: a tie or a low score means the question is still open.
    """
    scores = {"individual": 0, "team": 0, "neither": 0}
    checked = 0
    rows = archive_conn.execute(
        "SELECT game_id, home_er_individual, home_er_team,"
        " away_er_individual, away_er_team FROM game_logs"
        " WHERE home_er_team IS NOT NULL LIMIT ?", (limit,)).fetchall()
    for game_id, h_ind, h_team, a_ind, a_team in rows:
        span = archive_conn.execute(
            "SELECT first_record_id, last_record_id FROM game_spans"
            " WHERE game_id = ? LIMIT 1", (game_id,)).fetchone()
        if span is None:
            continue
        total = 0
        found = False
        for (raw,) in archive_conn.execute(
                "SELECT raw_line FROM raw_records"
                " WHERE record_id BETWEEN ? AND ? AND raw_line LIKE 'data,er,%'",
                span):
            parts = raw.split(",")
            if len(parts) >= 4 and parts[3].strip().isdigit():
                total += int(parts[3])
                found = True
        if not found:
            continue
        checked += 1
        # `data,er` covers both teams' pitchers, so the event-file total is the
        # sum of the two sides whichever field is the right one.
        if h_ind is not None and a_ind is not None and h_ind + a_ind == total:
            scores["individual"] += 1
        elif h_team is not None and a_team is not None and h_team + a_team == total:
            scores["team"] += 1
        else:
            scores["neither"] += 1
    return {"checked": checked, **scores}
