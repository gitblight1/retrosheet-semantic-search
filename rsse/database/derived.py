"""Derived-table build (spec/05-DATABASE.md §2-§4).

Reads the **archive**, not the event files. That is the contract §1 sets: the
archive holds 100% of the input verbatim, every derived table is rebuildable
from it, and a re-derive therefore reproduces against exactly the bytes that
were ingested rather than against whatever is on disk today. Retrosheet
reissues corrected files, so those are not the same thing.

Nothing written here is a source of truth. `rsse derive --rebuild` drops the
lot and rebuilds, and that is a safe operation by construction.

The pipeline per game is the one the earlier layers already established:

    raw lines -> records -> replay (state) -> derive (tags) -> rows

so this module contains no baseball knowledge at all. Where it looked like it
needed some, that was a sign the fact belonged one layer down -- `htbf` and
`info,innings` both moved into the replay for exactly that reason.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ..model.game import GameReplay, half_for, replay_game
from ..model.state import ParseStatus, credit_sequences
from ..parser.records import Record, parse_line
from ..semantic import ontology as O
from ..semantic.derive import CuratedEntry, load_curated, tag_rows, tags_for_play
from . import schema

BATCH = 5_000

#: League by event-file extension (spec/01-CORPUS.md §2).
_LEAGUE_BY_SUFFIX = {"EVA": "AL", "EVN": "NL", "EVF": "FL", "EVR": "NGL"}


@dataclass
class DeriveStats:
    games: int = 0
    plays: int = 0
    unparsed: int = 0
    advances: int = 0
    credits: int = 0
    sequences: int = 0
    tags: int = 0
    curated: int = 0
    status: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        return (f"games {self.games:,}, plays {self.plays:,}, "
                f"advances {self.advances:,}, credits {self.credits:,}, "
                f"sequences {self.sequences:,}, tags {self.tags:,} "
                f"(curated {self.curated:,}), unparsed {self.unparsed:,}")


# ---------------------------------------------------------------------------
# reading games back out of the archive
# ---------------------------------------------------------------------------

#: SQLite's default parameter ceiling is 999; this leaves room for the rest.
_KEY_CHUNK = 500


def iter_archive_games(archive, seasons: list[int] | None = None,
                       limit: int | None = None,
                       game_keys: list[int] | None = None
                       ) -> Iterator[tuple[dict, list[Record], list[int]]]:
    """Yield ``(span, records, record_ids)`` for each game in the archive.

    A game's records are contiguous in ``record_id`` by construction (§1), so
    each game is one seek into `game_spans` plus a range scan on the integer
    primary key -- which is the whole reason that table exists instead of an
    index over 31 million rows.
    """
    sql = ("SELECT s.game_key, s.game_id, s.occurrence, s.file_id, "
           "       s.first_record_id, s.last_record_id, f.path, f.season, "
           "       f.sha256 "
           "  FROM game_spans s JOIN source_files f ON f.file_id = s.file_id")
    params: list = []
    where = []
    if seasons:
        where.append("f.season IN (%s)" % ",".join("?" * len(seasons)))
        params += list(seasons)
    if game_keys is not None:
        # Chunked rather than a temp table: the archive is opened read-only,
        # and a refresh touches tens of games, not the corpus.
        keys = list(game_keys)
        if not keys:
            return
        if len(keys) > _KEY_CHUNK:
            for start in range(0, len(keys), _KEY_CHUNK):
                yield from iter_archive_games(
                    archive, seasons, limit, keys[start:start + _KEY_CHUNK])
            return
        where.append("s.game_key IN (%s)" % ",".join("?" * len(keys)))
        params += keys
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY s.game_key"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)

    for span in archive.execute(sql, params).fetchall():
        (game_key, game_id, occurrence, file_id,
         first_id, last_id, path, season, sha256) = span
        rows = archive.execute(
            "SELECT record_id, line_no, raw_line FROM raw_records "
            " WHERE record_id BETWEEN ? AND ? ORDER BY record_id",
            (first_id, last_id),
        ).fetchall()
        records: list[Record] = []
        record_ids: list[int] = []
        for record_id, line_no, raw_line in rows:
            record = parse_line(line_no, raw_line)
            if record is None:
                continue
            records.append(record)
            record_ids.append(record_id)
        yield (
            {"game_key": game_key, "game_id": game_id, "occurrence": occurrence,
             "file_id": file_id, "path": path, "season": season,
             "sha256": sha256},
            records,
            record_ids,
        )


# ---------------------------------------------------------------------------
# the games row
# ---------------------------------------------------------------------------

def _iso_date(value: str | None) -> str | None:
    """`1990/04/09` -> `1990-04-09`, and leave anything unexpected alone."""
    if not value:
        return None
    parts = value.replace("-", "/").split("/")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    return value


def _int_or_none(value: str | None) -> int | None:
    return int(value) if value and value.lstrip("-").isdigit() else None


def game_row(span: dict, records: list[Record], replay: GameReplay) -> dict:
    """Build the `games` row from the game's `info` records plus the replay.

    The final score comes from the **replay**, not from `info`: event files
    carry no linescore, so the reconstructed total is the only score available.
    That makes it a derived value rather than a check on the derivation, and
    the real check needs Retrosheet's game logs
    ([07-TESTING](../../spec/07-TESTING.md) §4).
    """
    info = {}
    for rec in records:
        if rec.type == "info" and len(rec.fields) >= 1:
            info[rec.fields[0]] = rec.fields[1] if len(rec.fields) > 1 else None

    game_id = span["game_id"]
    date = _iso_date(info.get("date"))
    season = span["season"] or _int_or_none(game_id[3:7])
    suffix = Path(span["path"]).suffix.lstrip(".").upper()
    pitch_detail = (info.get("pitches") or "").lower() or None
    if pitch_detail not in ("pitches", "count", "none", None):
        pitch_detail = None

    return {
        "game_key": span["game_key"],
        "game_id": game_id,
        "occurrence": span["occurrence"],
        "date": date,
        "season": season,
        "game_number": _int_or_none(game_id[11:12]) if len(game_id) >= 12 else None,
        "home_team": info.get("hometeam"),
        "away_team": info.get("visteam"),
        "league": _LEAGUE_BY_SUFFIX.get(suffix),
        "site": info.get("site"),
        "game_type": info.get("gametype"),
        # NULL below 2020: `info,innings` does not exist in the older files, so
        # the replay's default of 9 is what applied. Recording NULL rather than
        # 9 keeps "we were told" distinct from "we assumed".
        "scheduled_innings": _int_or_none(info.get("innings")),
        "use_dh": 1 if (info.get("usedh") or "").lower() == "true" else 0,
        "home_bats_first": 1 if replay.home_bats_first else 0,
        "pitch_detail": pitch_detail,
        "tiebreaker_base": _int_or_none(info.get("tiebreaker")),
        "final_home": replay.runs.get(1, 0),
        "final_away": replay.runs.get(0, 0),
        "plays": len(replay.plays),
        "parse_status": "ok" if replay.ok else "suspect",
    }


# ---------------------------------------------------------------------------
# the per-play rows
# ---------------------------------------------------------------------------

#: Credit by position in a sequence (spec/03-STATE.md §5): the last atom is the
#: putout, earlier ones are assists, an `E$` is an error, `U`/`99` earn none.
def _credit_rows(play_id: int, seqs) -> Iterator[tuple]:
    for seq in seqs:
        last = len(seq.atoms) - 1
        for i, (fielder, is_error) in enumerate(seq.atoms):
            if is_error:
                credit = "error"
            elif fielder in ("U", "9?"):
                credit = "none"
            elif i == last and seq.putout is not None:
                credit = "putout"
            else:
                credit = "assist"
            yield (play_id, seq.scope, seq.scope_seq, i,
                   int(fielder) if fielder.isdigit() else None, credit)


def derive_game(span: dict, records: list[Record], record_ids: list[int],
                play_id_start: int, parser_version: str,
                curated: dict[tuple[str, int], CuratedEntry],
                tag_ids: dict[str, int], stats: DeriveStats) -> dict:
    """Replay and derive one game into rows for every derived table."""
    replay = replay_game(records, span["game_id"])
    by_line = dict(zip((r.line_no for r in records), record_ids))

    rows: dict[str, list[tuple]] = {
        "plays": [], "runner_advances": [], "fielding_credits": [],
        "credit_sequences": [], "play_tags": [],
    }
    play_id = play_id_start

    # Half-innings holding a play that could not be parsed. Its effect on the
    # base-out state was never applied, so every *later* play in that half
    # inherits a state known to be wrong -- and parses perfectly well on it.
    # Only the loader can see this: `apply_play` sees one play at a time and
    # has nothing to be suspicious of. See spec/03-STATE.md §7.
    untrusted: set[tuple[int, str]] = set()

    for play in replay.plays:
        play_id += 1
        outcome, context, parsed = play.outcome, play.context, play.parsed
        # `context` is None for an unparsed play, so the half is recomputed
        # here -- from `home_bats_first`, not from the team number, which is
        # backwards for the 51 `htbf` games (§6.5).
        half = (context.half if context is not None
                else half_for(play.team, replay.home_bats_first))
        if outcome is None or parsed is None:
            # Unparseable: recorded so the play is not silently absent, with
            # every derived column left NULL rather than invented. The
            # base-out columns are nullable precisely for this row: a stored
            # '000' would be indistinguishable from bases genuinely empty.
            stats.unparsed += 1
            untrusted.add((play.inning, half))
            rows["plays"].append(_check_row("plays", (
                play_id, span["game_key"], span["game_id"], play.seq,
                by_line.get(play.line_no), play.inning,
                half, play.team,
                play.batter_id, None, None, "", play.event, "", "[]", "[]", "[]",
                None, None, None, None, None, None, None, None,
                None, 0, "unknown", 0, 0, 0, 0, 0, 0, 0,
                "unparsed", play.error, parser_version,
            ), _PLAY_COLUMNS))
            continue

        status = outcome.parse_status
        if status == ParseStatus.OK and (play.inning, half) in untrusted:
            # Only an otherwise-clean play is relabelled: a finding about the
            # play itself is the more specific statement and outranks this.
            status = ParseStatus.UNTRUSTED

        event = parsed.event
        rows["plays"].append(_check_row("plays", (
            play_id, span["game_key"], span["game_id"], play.seq,
            by_line.get(play.line_no),
            play.inning, context.half, play.team, play.batter_id,
            play.balls, play.strikes, play.pitches,
            parsed.raw,
            ";".join("+".join(e.emit() for e in g) for g in event.groups),
            json.dumps([m.emit() for m in event.modifiers]),
            json.dumps([a.emit() for a in event.advances]),
            json.dumps([[i, c] for i, c in parsed.trivia]),
            outcome.outs_before, outcome.outs_recorded, outcome.outs_after,
            outcome.bases_before, outcome.bases_after,
            *outcome.runners_before,
            outcome.batter_dest, int(outcome.batter_is_out), outcome.batter_ran,
            outcome.runs_on_play,
            context.score_batting_before, context.score_fielding_before,
            int(outcome.is_inning_ending), int(context.is_final_play),
            int(context.is_walkoff), int(context.is_go_ahead),
            status,
            "; ".join(outcome.notes) or None, parser_version,
        ), _PLAY_COLUMNS))

        for seq, adv in enumerate(outcome.advances):
            rows["runner_advances"].append((
                play_id, seq,
                adv.runner.player_id if adv.runner else None,
                adv.origin, adv.dest, int(adv.is_explicit),
                int(adv.marked_out), int(adv.is_out), int(adv.is_force),
                adv.force_certainty, int(adv.scored), adv.raw,
            ))

        seqs = credit_sequences(event)
        for cs in seqs:
            rows["credit_sequences"].append((
                play_id, cs.scope, cs.scope_seq, cs.origin_seq,
                cs.seq_text, int(cs.has_error), int(cs.records_out),
            ))
        rows["fielding_credits"].extend(_credit_rows(play_id, seqs))

        for tag in tags_for_play(event, outcome, context, parsed.trivia,
                                 span["game_id"], play.seq, curated):
            rows["play_tags"].append(
                (play_id, tag_ids[tag.name], tag.confidence, tag.source))
            if tag.source == "curated":
                stats.curated += 1

    stats.games += 1
    stats.plays += len(replay.plays)
    stats.advances += len(rows["runner_advances"])
    stats.credits += len(rows["fielding_credits"])
    stats.sequences += len(rows["credit_sequences"])
    stats.tags += len(rows["play_tags"])
    for row in rows["plays"]:
        key = row[_PLAY_COLUMNS.index("parse_status")]
        stats.status[key] = stats.status.get(key, 0) + 1

    return {"rows": rows, "game": game_row(span, records, replay),
            "next_play_id": play_id}


# ---------------------------------------------------------------------------
# the driver
# ---------------------------------------------------------------------------

_INSERTS = {
    "games": ("INSERT INTO games (game_key, game_id, occurrence, date, season,"
              " game_number, home_team, away_team, league, site, game_type,"
              " scheduled_innings, use_dh, home_bats_first, pitch_detail,"
              " tiebreaker_base, final_home, final_away, plays, parse_status,"
              " source_sha256)"
              " VALUES (" + ",".join("?" * 21) + ")"),
    "game_info": "INSERT INTO game_info (game_key, key, value, seq) VALUES (?,?,?,?)",
    "plays": None,   # built from _PLAY_COLUMNS below
    "runner_advances": ("INSERT INTO runner_advances VALUES ("
                        + ",".join("?" * 12) + ")"),
    "fielding_credits": ("INSERT INTO fielding_credits VALUES ("
                         + ",".join("?" * 6) + ")"),
    "credit_sequences": ("INSERT INTO credit_sequences VALUES ("
                         + ",".join("?" * 7) + ")"),
    "play_tags": "INSERT INTO play_tags VALUES (?,?,?,?)",
}

#: Named rather than positional, and the row builder is asserted against it.
#: The first cut used `INSERT INTO plays VALUES (?...)` with a hand-counted 36
#: placeholders against 38 columns -- a positional insert into a 38-column
#: table is a standing invitation to that, and it fails at load time on a
#: long run rather than at import.
_PLAY_COLUMNS = (
    "play_id", "game_key", "game_id", "seq", "record_id",
    "inning", "half", "batting_team", "batter_id",
    "count_balls", "count_strikes", "pitch_seq",
    "event_raw", "event_basic", "event_modifiers", "event_advances",
    "annotations",
    "outs_before", "outs_recorded", "outs_after",
    "bases_before", "bases_after",
    "runner_1_before", "runner_2_before", "runner_3_before",
    "batter_dest", "batter_is_out", "batter_ran", "runs_on_play",
    "score_batting_before", "score_fielding_before",
    "is_inning_ending", "is_final_play", "is_walkoff", "is_go_ahead",
    "parse_status", "parse_error", "parser_version",
)

_INSERTS["plays"] = (
    "INSERT INTO plays (" + ",".join(_PLAY_COLUMNS) + ") VALUES ("
    + ",".join("?" * len(_PLAY_COLUMNS)) + ")"
)

_GAME_COLUMNS = ("game_key", "game_id", "occurrence", "date", "season",
                 "game_number", "home_team", "away_team", "league", "site",
                 "game_type", "scheduled_innings", "use_dh",
                 "home_bats_first", "pitch_detail", "tiebreaker_base",
                 "final_home", "final_away", "plays", "parse_status",
                 "source_sha256")


def _check_row(table: str, row: tuple, columns: tuple) -> tuple:
    """Fail loudly and immediately on a row that does not match its columns."""
    if len(row) != len(columns):
        raise AssertionError(
            f"{table}: built {len(row)} values for {len(columns)} columns")
    return row


def load_tags_table(conn) -> dict[str, int]:
    """Populate `tags` from the ontology registry and return name -> tag_id.

    The registry is the single source, so a tag cannot exist in the database
    without a rule or in the ontology without a row (spec/05-DATABASE.md §4).
    Re-running updates the version and rule hash in place rather than
    inserting a second row, so `play_tags` references survive a re-derive.
    """
    for row in tag_rows():
        conn.execute(
            "INSERT INTO tags (name, category, version, rule_hash,"
            " deprecated_alias_of) VALUES (?,?,?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET category=excluded.category,"
            " version=excluded.version, rule_hash=excluded.rule_hash,"
            " deprecated_alias_of=excluded.deprecated_alias_of",
            (row["name"], row["category"], row["version"], row["rule_hash"],
             row["deprecated_alias_of"]),
        )
    conn.commit()
    return {name: tag_id for name, tag_id
            in conn.execute("SELECT name, tag_id FROM tags")}


#: Every table holding rows that belong to one game, in the order they must be
#: deleted. The last three are not built by `derive` at all -- they belong to
#: `secondary` and `earned-runs` -- and they are here because `plays` is about
#: to be deleted underneath their `play_id`. Leaving them would be a dangling
#: reference nothing checks for, which is the same shape as the bug that let
#: `coverage` vanish unnoticed.
_GAME_TABLES_BY_PLAY = ("play_tags", "credit_sequences", "fielding_credits",
                        "runner_advances")
_GAME_TABLES_BY_KEY = ("earned_runs", "comments", "lineup_entries", "plays",
                       "game_info", "games")


def _has_table(conn, name: str) -> bool:
    return bool(conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,)).fetchone()[0])


class NoSourceDigests(Exception):
    """The derived database predates `games.source_sha256`."""


def stale_and_missing(conn, archive) -> tuple[list[int], list[int]]:
    """`(games to forget, games to build)`, by comparing content.

    The archive is the source of truth, so agreement is decided by comparing
    the two databases rather than by anything remembered between runs -- but
    it must be compared on **content**, not identity. `game_spans.game_key`
    and `raw_records.record_id` are plain `INTEGER PRIMARY KEY`s, so deleting
    a reissued file's rows and inserting its replacement hands back the very
    same integers: measured, a refreshed two-game file came back as keys 1 and
    2, exactly as before. Comparing key sets reported nothing stale and
    nothing missing, and would have left the derived layer serving the old
    vintage of a corrected file with every check green -- the precise outcome
    `CorpusChanged` exists to prevent.

    So a game agrees when its key *and* its source file's digest agree. A
    reissue changes the digest by definition, so its games appear in both
    lists and are rebuilt.
    """
    archive_games = {r[0]: r[1] for r in archive.execute(
        "SELECT s.game_key, f.sha256 FROM game_spans s"
        " JOIN source_files f ON f.file_id = s.file_id")}
    try:
        derived_games = {r[0]: r[1] for r in conn.execute(
            "SELECT game_key, source_sha256 FROM games")}
    except sqlite3.OperationalError as exc:
        raise NoSourceDigests(str(exc)) from None

    if derived_games and all(v is None for v in derived_games.values()):
        raise NoSourceDigests("games.source_sha256 is empty")

    stale = [k for k, sha in derived_games.items()
             if archive_games.get(k) != sha]
    missing = [k for k, sha in archive_games.items()
               if derived_games.get(k) != sha]
    return sorted(stale), sorted(missing)


def adopt_digests(conn, archive) -> int:
    """Record the archive's current digests as this database's vintage.

    A migration for databases built before `games.source_sha256`, and an
    assertion rather than a measurement: it says "these derived games came
    from the files the archive holds now", which nothing here can verify. It
    is true of a database built from this archive and not since re-ingested,
    and false -- silently -- of one built from an earlier vintage. That is why
    it needs an explicit flag rather than happening on demand.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(games)")}
    if "source_sha256" not in cols:
        conn.execute("ALTER TABLE games ADD COLUMN source_sha256 TEXT")
    rows = list(archive.execute(
        "SELECT f.sha256, s.game_key FROM game_spans s"
        " JOIN source_files f ON f.file_id = s.file_id"))
    conn.executemany("UPDATE games SET source_sha256 = ? WHERE game_key = ?",
                     rows)
    conn.commit()
    return conn.execute(
        "SELECT count(*) FROM games WHERE source_sha256 IS NOT NULL"
    ).fetchone()[0]


def forget_games(conn, keys: list[int]) -> dict[str, int]:
    """Delete every row belonging to `keys`. Returns rows removed per table."""
    removed: dict[str, int] = {}
    if not keys:
        return removed
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _stale (game_key INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM _stale")
    conn.executemany("INSERT INTO _stale VALUES (?)", [(k,) for k in keys])
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _stale_plays"
                 " (play_id INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM _stale_plays")
    conn.execute("INSERT INTO _stale_plays SELECT play_id FROM plays"
                 " WHERE game_key IN (SELECT game_key FROM _stale)")
    for table in _GAME_TABLES_BY_PLAY:
        if _has_table(conn, table):
            cur = conn.execute(f"DELETE FROM {table} WHERE play_id IN"
                               " (SELECT play_id FROM _stale_plays)")
            removed[table] = cur.rowcount
    for table in _GAME_TABLES_BY_KEY:
        if _has_table(conn, table):
            cur = conn.execute(f"DELETE FROM {table} WHERE game_key IN"
                               " (SELECT game_key FROM _stale)")
            removed[table] = cur.rowcount
    conn.commit()
    return removed


def build(conn, archive, parser_version: str, seasons: list[int] | None = None,
          limit: int | None = None, progress=None,
          curated_path: Path | None = None,
          game_keys: list[int] | None = None) -> DeriveStats:
    """Derive every game in the archive into the query database."""
    stats = DeriveStats()
    curated = load_curated(curated_path)
    tag_ids = load_tags_table(conn)

    pending: dict[str, list[tuple]] = {name: [] for name in _INSERTS}
    play_id = conn.execute("SELECT coalesce(max(play_id), 0) FROM plays"
                           ).fetchone()[0]
    seen_seasons: set[int | None] = set()

    def flush() -> None:
        for table, rows in pending.items():
            if rows:
                conn.executemany(_INSERTS[table], rows)
                rows.clear()
        conn.commit()

    for span, records, record_ids in iter_archive_games(archive, seasons, limit,
                                                        game_keys):
        if progress and span["season"] not in seen_seasons:
            seen_seasons.add(span["season"])
            progress(span["season"], stats)

        result = derive_game(span, records, record_ids, play_id,
                             parser_version, curated, tag_ids, stats)
        play_id = result["next_play_id"]

        game = result["game"]
        game["source_sha256"] = span["sha256"]
        pending["games"].append(tuple(game[c] for c in _GAME_COLUMNS))
        seq = 0
        for rec in records:
            if rec.type == "info" and rec.fields:
                pending["game_info"].append((
                    span["game_key"], rec.fields[0],
                    rec.fields[1] if len(rec.fields) > 1 else None, seq))
                seq += 1
        for table, rows in result["rows"].items():
            pending[table].extend(rows)

        if len(pending["plays"]) >= BATCH:
            flush()

    flush()
    conn.execute(
        "INSERT INTO derive_runs (derived_at, parser_version,"
        " ontology_version, ontology_hash, games, plays, notes)"
        " VALUES (?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"),
         parser_version, O.ONTOLOGY_VERSION, O.ontology_hash(),
         stats.games, stats.plays,
         f"seasons={seasons or 'all'} limit={limit or 'none'}"),
    )
    conn.commit()
    return stats
