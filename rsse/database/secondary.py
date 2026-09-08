"""`comments` and `lineup_entries` (spec/05-DATABASE.md §2, §5).

A second pass over the archive rather than part of `derive`, for one reason:
neither table needs the state machine. A `com` record attaches to the play
before it, and `plays.record_id` already names each play's archive record, so
the link is a lookup rather than a replay. That makes this minutes instead of
an hour and a half, and it means these tables can be added to an existing
query database without re-deriving 17.9 million plays.

`derive` still sets `PlayContext.replay_reversed` from a linked structured
`replay` comment (`rsse/model/game.py`), because `ReplayOverturned` is a *tag*
and tags are derived there. So the tag needs a re-derive; these tables do not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..model.comments import classify

SECONDARY_DDL = """
CREATE TABLE IF NOT EXISTS comments (
  game_key INTEGER NOT NULL,
  seq      INTEGER NOT NULL,
  play_id  INTEGER,                          -- NULL before the first play
  record_id INTEGER NOT NULL,                -- archive raw_records.record_id
  kind     TEXT NOT NULL CHECK (kind IN ('text','replay','ejection',
                                         'umpchange','suspend')),
  text     TEXT NOT NULL,
  payload  TEXT,                             -- JSON for structured kinds
  marker   INTEGER NOT NULL DEFAULT 0,       -- body began with `$`
  PRIMARY KEY (game_key, seq)
);

CREATE TABLE IF NOT EXISTS lineup_entries (
  game_key      INTEGER NOT NULL,
  seq           INTEGER NOT NULL,
  is_sub        INTEGER NOT NULL,
  play_id       INTEGER,                     -- the play a sub entered after
  player_id     TEXT NOT NULL,
  player_name   TEXT NOT NULL,
  team          INTEGER NOT NULL CHECK (team IN (0,1)),
  batting_order INTEGER NOT NULL,
  position      INTEGER NOT NULL,
  PRIMARY KEY (game_key, seq)
);
"""

SECONDARY_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_comments_play ON comments (play_id)
  WHERE play_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_comments_kind ON comments (kind)
  WHERE kind <> 'text';
CREATE INDEX IF NOT EXISTS ix_lineup_player ON lineup_entries (player_id);
CREATE INDEX IF NOT EXISTS ix_lineup_pos ON lineup_entries
  (game_key, team, position);
"""


@dataclass
class SecondaryStats:
    games: int = 0
    comments: int = 0
    lineup_entries: int = 0
    replay_verdicts: int = 0
    malformed: list[str] = field(default_factory=list)
    kinds: dict = field(default_factory=dict)


def _lineup_fields(fields: tuple[str, ...]) -> tuple | None:
    """Split a `start`/`sub` record's fields.

    Read from both ends, not left to right: the name is quoted free text and a
    comma inside it shifts every field after it. `Griffey, Ken Jr.` would
    otherwise put a name fragment where the position belongs, and
    `int(...)` would raise on a row that is perfectly well formed.
    """
    if len(fields) < 6:
        return None
    player_id = fields[1]
    team, order, position = fields[-3], fields[-2], fields[-1]
    name = ",".join(fields[2:-3]).strip().strip('"').strip("'")
    if not (team.isdigit() and order.lstrip("-").isdigit()
            and position.lstrip("-").isdigit()):
        return None
    return player_id, name, int(team), int(order), int(position)


def build(conn, archive, progress=None) -> SecondaryStats:
    """Build `comments` and `lineup_entries` from the archive."""
    conn.executescript(SECONDARY_DDL)
    conn.execute("DELETE FROM comments")
    conn.execute("DELETE FROM lineup_entries")
    stats = SecondaryStats()
    seen_seasons: set = set()

    # Only games the query database actually holds. Iterating every archive
    # game instead would scan 203,285 spans to serve a `--season 2000` derive,
    # and would file comments against games that have no plays -- rows a
    # `comments`-to-`plays` join could never reach and nothing would flag.
    wanted = {r[0] for r in conn.execute("SELECT game_key FROM games")}
    spans = [row for row in archive.execute(
        "SELECT s.game_key, s.first_record_id, s.last_record_id, f.season"
        "  FROM game_spans s JOIN source_files f ON f.file_id = s.file_id"
        " ORDER BY s.game_key") if row[0] in wanted]

    comment_rows: list[tuple] = []
    lineup_rows: list[tuple] = []

    for game_key, first_id, last_id, season in spans:
        if progress and season not in seen_seasons:
            seen_seasons.add(season)
            progress(season, stats)

        # record_id -> play_id for this game. Plays are the only records the
        # derived layer kept an archive pointer for, which is exactly what
        # makes this pass possible.
        by_record = dict(conn.execute(
            "SELECT record_id, play_id FROM plays WHERE game_key = ?"
            " AND record_id IS NOT NULL", (game_key,)))

        last_play_id = None
        c_seq = l_seq = 0
        for record_id, raw in archive.execute(
                "SELECT record_id, raw_line FROM raw_records"
                " WHERE record_id BETWEEN ? AND ? ORDER BY record_id",
                (first_id, last_id)):
            kind = raw.split(",", 1)[0] if "," in raw else raw
            if kind == "play":
                last_play_id = by_record.get(record_id, last_play_id)
                continue
            if kind == "com":
                comment = classify(raw)
                comment_rows.append((
                    game_key, c_seq, last_play_id, record_id, comment.kind,
                    comment.text,
                    json.dumps(comment.payload) if comment.payload else None,
                    int(comment.marker)))
                c_seq += 1
                stats.comments += 1
                stats.kinds[comment.kind] = stats.kinds.get(comment.kind, 0) + 1
                if comment.replay_reversed is not None:
                    stats.replay_verdicts += 1
                continue
            if kind in ("start", "sub"):
                parsed = _lineup_fields(tuple(raw.rstrip("\r\n").split(",")))
                if parsed is None:
                    # Recorded, not skipped: a lineup record this pass cannot
                    # read is a gap in `.pitcher()`, and a silent one would
                    # look like the player simply did not play.
                    stats.malformed.append(raw)
                    continue
                player_id, name, team, order, position = parsed
                lineup_rows.append((
                    game_key, l_seq, int(kind == "sub"),
                    last_play_id if kind == "sub" else None,
                    player_id, name, team, order, position))
                l_seq += 1
                stats.lineup_entries += 1

        stats.games += 1
        if len(comment_rows) + len(lineup_rows) > 200_000:
            _flush(conn, comment_rows, lineup_rows)

    _flush(conn, comment_rows, lineup_rows)
    conn.executescript(SECONDARY_INDEXES)
    conn.commit()
    return stats


_COMMENT_INSERT = ("INSERT OR REPLACE INTO comments (game_key, seq, play_id,"
                   " record_id, kind, text, payload, marker)"
                   " VALUES (?,?,?,?,?,?,?,?)")
_LINEUP_INSERT = ("INSERT OR REPLACE INTO lineup_entries (game_key, seq,"
                  " is_sub, play_id, player_id, player_name, team,"
                  " batting_order, position) VALUES (?,?,?,?,?,?,?,?,?)")


def _flush(conn, comment_rows: list, lineup_rows: list) -> None:
    if comment_rows:
        conn.executemany(_COMMENT_INSERT, comment_rows)
        comment_rows.clear()
    if lineup_rows:
        conn.executemany(_LINEUP_INSERT, lineup_rows)
        lineup_rows.clear()
    conn.commit()
