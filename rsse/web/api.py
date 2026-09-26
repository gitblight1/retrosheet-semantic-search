"""The web UI's endpoints, as plain functions (spec/09-WEB.md §8).

Each takes an open read-only connection and returns a JSON-able dict, or raises
`ApiError`. There is no HTTP in this module, so it is tested without a socket
and could be mounted by a different server unchanged (§10.1).

Every page degrades rather than failing when an optional table is absent: the
section that needs it is replaced by `missing(...)`, which names the command
that builds it. The archive, which holds `game_logs` and the raw source lines,
is attached as the schema `arc` when it exists and is otherwise simply absent.
"""

from __future__ import annotations

import io
import json
import time

from ..query import QueryError
from ..query import params as qp
from ..query.export import result_dict, write_csv
from ..query.search import DEFAULT_GAME_TYPES, Search
from ..semantic.ontology import ONTOLOGY_VERSION, REGISTRY

#: Page sizes the UI offers. Larger exports go through `rsse query`, whose
#: exact command line the export dialog shows (§4).
PAGE_SIZES = (25, 100, 500)

#: What builds each optional table, for the note that replaces a section.
BUILT_BY = {
    "lineup_entries": "rsse secondary",
    "comments": "rsse secondary",
    "people": "rsse reference",
    "roster_entries": "rsse reference",
    "teams": "rsse reference",
    "franchises": "rsse reference",
    "parks": "rsse reference",
    "appearances": "rsse appearances",
    "earned_runs": "rsse earned-runs",
    "coverage": "rsse coverage --rebuild",
    "arc.game_logs": "rsse gamelogs (into the archive, which must be present)",
    "arc.raw_records": "rsse ingest (the archive must be present)",
}

OPTIONAL_TABLES = tuple(BUILT_BY)


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra

    def as_dict(self) -> dict:
        return {"error": self.message, "status": self.status, **self.extra}


# -- plumbing ---------------------------------------------------------------

def has(conn, table: str) -> bool:
    schema, _, name = table.rpartition(".")
    schema = schema or "main"
    try:
        return bool(conn.execute(
            f"SELECT count(*) FROM {schema}.sqlite_master"
            " WHERE type = 'table' AND name = ?", (name,)).fetchone()[0])
    except Exception:           # schema not attached
        return False


def missing(table: str) -> dict:
    return {"missing_table": table, "build": BUILT_BY.get(table, "")}


def _rows(conn, sql: str, params=()) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur]


def _row(conn, sql: str, params=()) -> dict | None:
    rows = _rows(conn, sql, params)
    return rows[0] if rows else None


def _names(conn, ids) -> dict:
    """`person_id -> name`, in one query, or `{}` without `people`."""
    if not has(conn, "people"):
        return {}
    from ..query.names import names_for
    return names_for(conn, ids)


def _int(value, what: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"{what} must be a whole number, not {value!r}")


# -- schema -----------------------------------------------------------------

def schema(conn) -> dict:
    """What the form is built from (§2), and which tables are present."""
    tags = []
    known = {r[0] for r in conn.execute("SELECT name FROM tags")}
    for name, t in sorted(REGISTRY.items(), key=lambda kv: (kv[1].category,
                                                           kv[0])):
        tags.append({"name": name, "category": t.category, "spec": t.spec,
                     "note": t.note, "alias_of": t.alias_of,
                     "curated_only": t.curated_only,
                     "in_database": name in known})
    return {
        "params": [p.as_dict() for p in qp.QUERY_PARAMS],
        "orders": list(qp.ORDERS),
        "page_sizes": list(PAGE_SIZES),
        "tags": tags,
        "tables": {t: has(conn, t) for t in OPTIONAL_TABLES},
        "ontology_version": ONTOLOGY_VERSION,
    }


def tags(conn) -> dict:
    return {"tags": schema(conn)["tags"]}


# -- query ------------------------------------------------------------------

def _search(body: dict) -> tuple[Search, int, int]:
    params = body.get("params") or {}
    if not isinstance(params, dict):
        raise ApiError(400, "params must be an object")
    limit = _int(body.get("limit", PAGE_SIZES[0]), "limit")
    if limit not in PAGE_SIZES:
        raise ApiError(400, f"limit must be one of {PAGE_SIZES}; larger"
                            " exports are done with `rsse query`")
    offset = _int(body.get("offset", 0), "offset")
    try:
        s = qp.search_from_params(params)
        s = qp.apply_page(s, order=body.get("order"), limit=limit,
                          offset=offset)
    except QueryError as exc:
        raise ApiError(400, str(exc)) from None
    return s, limit, offset


def _query_error(exc: QueryError) -> ApiError:
    if len(exc.candidates) > 1:
        return ApiError(409, str(exc), candidates=[
            {"kind": k, "id": i, "label": label}
            for k, i, label in exc.candidates])
    return ApiError(400, str(exc))


def query(conn, body: dict, attribution: str) -> dict:
    """§4. The `rsse query --format json` document, plus paging."""
    s, limit, offset = _search(body)
    started = time.monotonic()
    try:
        result = s.run(conn)
    except QueryError as exc:
        raise _query_error(exc) from None
    out = result_dict(result, attribution)
    # The CLI's own sentences, so the page and `rsse query` word the
    # disclosure identically rather than the page re-deriving it (§4).
    out["describe"] = {
        "coverage": result.coverage.describe(),
        "excluded": result.excluded.describe(),
        "force": result.force.describe() if result.force else None,
    }
    out["params"] = qp.normalize(body.get("params") or {})
    out["limit"], out["offset"] = limit, offset
    out["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    out["command"] = command_line(out["params"], limit, body.get("order"))
    return out


def explain(conn, body: dict) -> dict:
    """§5. Does not run the query."""
    s, _limit, _offset = _search(body)
    try:
        info = s.explain(conn)
    except QueryError as exc:
        raise _query_error(exc) from None
    return {"sql": info["sql"], "params": list(info["params"]),
            "plan": [list(r) for r in info["plan"]],
            "warnings": info["warnings"]}


def command_line(params: dict, limit: int | None = None,
                 order: str | None = None) -> str:
    """The `rsse query` invocation equivalent to ``params``.

    Exact because both build through the same table (§2). Shown by the export
    dialog for anything larger than a page.
    """
    import shlex
    parts = ["rsse", "query"]
    for p in qp.QUERY_PARAMS:
        if p.name not in params:
            continue
        v = params[p.name]
        if p.kind == "switch":
            parts.append(p.flag)
        elif p.kind == "list":
            for item in (v if isinstance(v, (list, tuple)) else [v]):
                parts += [p.flag, str(item)]
        else:
            if isinstance(v, (list, tuple)):
                v = ",".join(str(x) for x in v)
            parts += [p.flag, str(v)]
    if limit:
        parts += ["--limit", str(limit)]
    if order and order != "chronological":
        parts.append(f"# order={order} is available in the UI only")
    return " ".join(shlex.quote(x) if not x.startswith("# ") else x
                    for x in parts)


def export(conn, body: dict, fmt: str, attribution: str) -> tuple[str, bytes]:
    """§4. The current page, through the CLI's own writer."""
    s, _limit, _offset = _search(body)
    try:
        result = s.run(conn)
    except QueryError as exc:
        raise _query_error(exc) from None
    if fmt == "csv":
        buf = io.StringIO()
        write_csv(result, buf, attribution)
        return "text/csv; charset=utf-8", buf.getvalue().encode()
    if fmt == "json":
        return ("application/json",
                json.dumps(result_dict(result, attribution), indent=2,
                           default=str).encode())
    raise ApiError(400, "format must be csv or json")


# -- names (§3.5) -----------------------------------------------------------

def _prefix_range(q: str) -> tuple[str, str]:
    """`[q, q_next)`, the range every string starting with ``q`` falls in."""
    return q, q[:-1] + chr(ord(q[-1]) + 1)


def names(conn, q: str, per_kind: int = 20) -> dict:
    """Prefix search over people, teams and parks.

    The surname arm is a range test so that it uses `ix_people_name`: `LIKE`
    is case-insensitive and the index is not, so `LIKE 'Rob%'` scans. The
    index is case-sensitive too, so the typed text is tried as written and
    with its first letter capitalised, which covers what people type.
    """
    q = " ".join((q or "").split())
    out = {"q": q, "people": [], "teams": [], "parks": []}
    if len(q) < 2:
        return out
    if has(conn, "people"):
        first, _, last = q.rpartition(" ")
        variants = sorted({last, last[:1].upper() + last[1:]})
        clauses, params = [], []
        for v in variants:
            lo, hi = _prefix_range(v)
            clauses.append("(last >= ? AND last < ?)")
            params += [lo, hi]
        rows = _rows(conn, (
            "SELECT person_id AS id, coalesce(nickname, first) AS first, last,"
            " first AS legal_first, birthdate, play_debut, play_last, source"
            " FROM people WHERE (" + " OR ".join(clauses) + ")"
            " ORDER BY last, coalesce(nickname, first) LIMIT 400"), params)
        if first:
            f = first.lower()
            rows = [r for r in rows
                    if (r["first"] or "").lower().startswith(f)
                    or (r["legal_first"] or "").lower().startswith(f)]
        for r in rows[:per_kind]:
            years = "-".join(x[-4:] for x in (r["play_debut"], r["play_last"])
                             if x)
            label = " ".join(x for x in (r["first"], r["last"]) if x)
            detail = ", ".join(x for x in (
                f"b. {r['birthdate'][-4:]}" if r["birthdate"] else "",
                f"played {years}" if years else "") if x)
            out["people"].append({"id": r["id"], "label": label or r["id"],
                                  "detail": detail})
    if has(conn, "teams"):
        like = q.lower() + "%"
        out["teams"] = [
            {"id": r["team_id"], "label": r["label"],
             "detail": f"{r['lo']}-{r['hi']}"}
            for r in _rows(conn, (
                "SELECT team_id, min(trim(coalesce(city,'') || ' '"
                " || coalesce(nickname,''))) AS label,"
                " min(season) AS lo, max(season) AS hi FROM teams"
                " WHERE lower(coalesce(city,'')) LIKE ?"
                "    OR lower(coalesce(nickname,'')) LIKE ?"
                "    OR lower(trim(coalesce(city,'') || ' '"
                "             || coalesce(nickname,''))) LIKE ?"
                "    OR lower(team_id) = ?"
                " GROUP BY team_id ORDER BY lo LIMIT ?"),
                (like, like, like, q.lower(), per_kind))]
    if has(conn, "parks"):
        like = q.lower() + "%"
        out["parks"] = [
            {"id": r["park_id"], "label": r["name"] or r["park_id"],
             "detail": ", ".join(x for x in (r["city"], r["years"]) if x)}
            for r in _rows(conn, (
                "SELECT park_id, name, city,"
                " substr(start_iso,1,4) || coalesce('-' || substr(end_iso,1,4),"
                " '') AS years FROM parks"
                " WHERE lower(coalesce(name,'')) LIKE ?"
                "    OR lower(coalesce(aka,'')) LIKE ?"
                "    OR lower(park_id) = ?"
                " ORDER BY name LIMIT ?"), (like, like, q.lower(), per_kind))]
    return out


# -- play (§3.1) ------------------------------------------------------------

_PLAY_COLS = """p.play_id, p.game_key, p.game_id, p.seq, p.record_id, p.inning,
 p.half, p.batting_team, p.batter_id, p.count_balls, p.count_strikes,
 p.pitch_seq, p.event_raw, p.event_basic, p.event_modifiers, p.event_advances,
 p.annotations, p.event_location, p.outs_before, p.outs_recorded, p.outs_after,
 p.bases_before, p.bases_after, p.runner_1_before, p.runner_2_before,
 p.runner_3_before, p.batter_dest, p.batter_is_out, p.batter_ran,
 p.runs_on_play, p.score_batting_before, p.score_fielding_before,
 p.is_inning_ending, p.is_final_play, p.is_walkoff, p.is_go_ahead,
 p.parse_status, p.parse_error, p.parser_version"""


def _lineup_at(conn, game_key: int, play_id: int) -> dict:
    """Who held each position, and each batting slot, at ``play_id``.

    The timeline rule of spec/06-QUERY.md §2.1, as `.fielder()` applies it: a
    starter holds from the first play, a sub from the play *after* the one it
    follows, and the holder is the last entry that had taken effect by then.
    """
    entries = _rows(conn, (
        "SELECT seq, is_sub, play_id, player_id, player_name, team,"
        " batting_order, position FROM lineup_entries"
        " WHERE game_key = ? AND coalesce(play_id, 0) < ? ORDER BY seq"),
        (game_key, play_id))
    out = {}
    for team in (0, 1):
        by_pos, by_slot = {}, {}
        for e in entries:
            if e["team"] != team:
                continue
            if 1 <= e["position"] <= 10:
                by_pos[e["position"]] = e
            by_slot[e["batting_order"]] = e
        out[team] = {
            "defense": [by_pos[k] for k in sorted(by_pos)],
            "batting": [by_slot[k] for k in sorted(by_slot)],
        }
    return out


def play(conn, play_id) -> dict:
    play_id = _int(play_id, "play id")
    p = _row(conn, f"SELECT {_PLAY_COLS} FROM plays p WHERE p.play_id = ?",
             (play_id,))
    if not p:
        raise ApiError(404, f"no play {play_id}")
    game = _game_header(conn, p["game_key"])
    out = {"play": p, "game": game}
    out["tags"] = _rows(conn, (
        "SELECT t.name, t.category, pt.confidence, pt.source"
        " FROM play_tags pt JOIN tags t USING (tag_id)"
        " WHERE pt.play_id = ? ORDER BY t.name"), (play_id,))
    out["advances"] = _rows(conn, (
        "SELECT * FROM runner_advances WHERE play_id = ? ORDER BY seq"),
        (play_id,))
    out["credits"] = _rows(conn, (
        "SELECT scope, scope_seq, pos_in_seq, fielder, credit"
        " FROM fielding_credits WHERE play_id = ?"
        " ORDER BY scope, scope_seq, pos_in_seq"), (play_id,))
    out["sequences"] = _rows(conn, (
        "SELECT scope, scope_seq, origin_seq, seq_text, has_error, records_out"
        " FROM credit_sequences WHERE play_id = ? ORDER BY scope, scope_seq"),
        (play_id,))
    out["comments"] = (_rows(conn, (
        "SELECT seq, kind, text, payload FROM comments WHERE play_id = ?"
        " ORDER BY seq"), (play_id,))
        if has(conn, "comments") else missing("comments"))
    out["lineup"] = (_lineup_at(conn, p["game_key"], play_id)
                     if has(conn, "lineup_entries")
                     else missing("lineup_entries"))
    out["source"] = (_row(conn, (
        "SELECT r.line_no, r.raw_line, f.path FROM arc.raw_records r"
        " JOIN arc.source_files f USING (file_id) WHERE r.record_id = ?"),
        (p["record_id"],)) if has(conn, "arc.raw_records")
        and p["record_id"] is not None else missing("arc.raw_records"))
    ids = [p["batter_id"], p["runner_1_before"], p["runner_2_before"],
           p["runner_3_before"]] + [a["runner_id"] for a in out["advances"]]
    out["names"] = _names(conn, ids)
    nav = conn.execute(
        "SELECT (SELECT play_id FROM plays WHERE game_key = ? AND seq < ?"
        "         ORDER BY seq DESC LIMIT 1),"
        "       (SELECT play_id FROM plays WHERE game_key = ? AND seq > ?"
        "         ORDER BY seq LIMIT 1)",
        (p["game_key"], p["seq"], p["game_key"], p["seq"])).fetchone()
    out["prev"], out["next"] = nav
    return out


# -- game (§3.2) ------------------------------------------------------------

def _game_header(conn, game_key: int) -> dict:
    g = _row(conn, "SELECT * FROM games WHERE game_key = ?", (game_key,))
    if not g:
        raise ApiError(404, f"no game {game_key}")
    g.pop("source_sha256", None)
    names = _team_names(conn, [g["home_team"], g["away_team"]], g["season"])
    g["home_name"] = names.get(g["home_team"])
    g["away_name"] = names.get(g["away_team"])
    if has(conn, "parks") and g["site"]:
        park = _row(conn, "SELECT name, city FROM parks WHERE park_id = ?",
                    (g["site"],))
        g["site_name"] = park["name"] if park else None
    return g


def _team_names(conn, team_ids, season) -> dict:
    if not has(conn, "teams"):
        return {}
    ids = [t for t in dict.fromkeys(team_ids) if t]
    if not ids:
        return {}
    return {r[0]: " ".join(x for x in r[1:] if x) for r in conn.execute(
        "SELECT team_id, city, nickname FROM teams WHERE season = ?"
        " AND team_id IN (%s)" % ",".join("?" * len(ids)), [season, *ids])}


def game_by_id(conn, game_id: str) -> dict:
    """Games carrying ``game_id``. A list, since three ids repeat (§3)."""
    return {"game_id": game_id, "games": _rows(conn, (
        "SELECT game_key, occurrence, date, away_team, home_team,"
        " final_away, final_home, plays FROM games WHERE game_id = ?"
        " ORDER BY occurrence"), (game_id,))}


def game(conn, game_key) -> dict:
    game_key = _int(game_key, "game key")
    g = _game_header(conn, game_key)
    out = {"game": g}
    out["info"] = _rows(conn, (
        "SELECT key, value FROM game_info WHERE game_key = ? ORDER BY seq"),
        (game_key,))
    plays = _rows(conn, (
        "SELECT play_id, seq, inning, half, batting_team, batter_id,"
        " count_balls, count_strikes, pitch_seq, event_raw, outs_before,"
        " outs_after, bases_before, bases_after, runs_on_play,"
        " score_batting_before, score_fielding_before, parse_status"
        " FROM plays WHERE game_key = ? ORDER BY seq"), (game_key,))
    tags = {}
    if plays:
        for pid, name in conn.execute(
                "SELECT pt.play_id, t.name FROM play_tags pt"
                " JOIN tags t USING (tag_id)"
                " WHERE pt.play_id BETWEEN ? AND ? ORDER BY t.name",
                (plays[0]["play_id"], plays[-1]["play_id"])):
            tags.setdefault(pid, []).append(name)
    for p in plays:
        p["tags"] = tags.get(p["play_id"], [])
    out["plays"] = plays
    out["line_score"] = _line_score(plays, g)
    if has(conn, "lineup_entries"):
        out["lineup"] = _rows(conn, (
            "SELECT seq, is_sub, play_id, player_id, player_name, team,"
            " batting_order, position FROM lineup_entries WHERE game_key = ?"
            " ORDER BY seq"), (game_key,))
    else:
        out["lineup"] = missing("lineup_entries")
    out["comments"] = (_rows(conn, (
        "SELECT seq, play_id, kind, text, payload FROM comments"
        " WHERE game_key = ? ORDER BY seq"), (game_key,))
        if has(conn, "comments") else missing("comments"))
    out["earned_runs"] = (_earned(conn, game_key)
                          if has(conn, "earned_runs")
                          else missing("earned_runs"))
    out["game_log"] = (_rows(conn, (
        "SELECT DISTINCT game_id, date, game_number, series, away_team,"
        " home_team, away_score, home_score, outs, park_id, completion,"
        " forfeit FROM arc.game_logs WHERE game_id = ? AND date = ?"),
        (g["game_id"], g["date"]))
        if has(conn, "arc.game_logs") else missing("arc.game_logs"))
    ids = [p["batter_id"] for p in plays]
    if isinstance(out["earned_runs"], list):
        ids += [r["pitcher_id"] for r in out["earned_runs"]]
    out["names"] = _names(conn, ids)
    return out


def _line_score(plays: list[dict], g: dict) -> dict:
    """Runs per inning per side, summed from the plays.

    Keyed on `batting_team` rather than `half`: 51 games have the home team
    batting first (spec/03-STATE.md §6.5). Reported beside the stored finals,
    and a disagreement is stated rather than resolved.
    """
    innings = max((p["inning"] for p in plays), default=0)
    runs = {0: [None] * innings, 1: [None] * innings}
    for p in plays:
        row = runs[p["batting_team"]]
        i = p["inning"] - 1
        row[i] = (row[i] or 0) + p["runs_on_play"]
    totals = {t: sum(r or 0 for r in runs[t]) for t in (0, 1)}
    agrees = (totals[0] == g["final_away"] and totals[1] == g["final_home"]
              if g["final_away"] is not None and g["final_home"] is not None
              else None)
    return {"innings": innings, "away": runs[0], "home": runs[1],
            "away_total": totals[0], "home_total": totals[1],
            "final_away": g["final_away"], "final_home": g["final_home"],
            "agrees": agrees}


def _earned(conn, game_key: int) -> list[dict]:
    """Earned runs by pitcher charged, with the certainty split.

    A NULL `earned_pitcher` is the scorer's judgement, not zero, and is
    counted apart (spec/05-DATABASE.md §5.4).
    """
    rows = _rows(conn, (
        "SELECT pitcher_id, batting_team, count(*) AS runs,"
        " sum(earned_pitcher = 1) AS earned,"
        " sum(earned_pitcher = 0) AS unearned,"
        " sum(earned_pitcher IS NULL) AS judgement,"
        " sum(certainty = 'derived') AS derived,"
        " sum(certainty = 'likely') AS likely,"
        " sum(certainty = 'ambiguous') AS ambiguous,"
        " sum(certainty = 'untrusted') AS untrusted"
        " FROM earned_runs WHERE game_key = ?"
        " GROUP BY pitcher_id, batting_team ORDER BY batting_team, min(play_id)"),
        (game_key,))
    return rows


# -- player (§3.3) ----------------------------------------------------------

def player(conn, person_id: str) -> dict:
    out = {"person_id": person_id}
    if has(conn, "people"):
        out["person"] = _row(conn, "SELECT * FROM people WHERE person_id = ?",
                             (person_id,))
    else:
        out["person"] = missing("people")
    out["roster"] = (_rows(conn, (
        "SELECT season, team_id, stated_team_id, first, last, bats, throws,"
        " position FROM roster_entries WHERE person_id = ?"
        " ORDER BY season, team_id"), (person_id,))
        if has(conn, "roster_entries") else missing("roster_entries"))
    out["appearances"] = (_rows(conn, (
        "SELECT * FROM appearances WHERE person_id = ?"
        " ORDER BY season, team_id"), (person_id,))
        if has(conn, "appearances") else missing("appearances"))
    out["batting_seasons"] = _rows(conn, (
        "SELECT g.season, count(*) AS plays FROM plays p"
        " JOIN games g ON g.game_key = p.game_key"
        " WHERE p.batter_id = ? GROUP BY g.season ORDER BY g.season"),
        (person_id,))
    out["lineup_seasons"] = (_rows(conn, (
        "SELECT g.season, count(DISTINCT l.game_key) AS games"
        " FROM lineup_entries l JOIN games g ON g.game_key = l.game_key"
        " WHERE l.player_id = ? GROUP BY g.season ORDER BY g.season"),
        (person_id,)) if has(conn, "lineup_entries")
        else missing("lineup_entries"))
    if ((out["person"] is None or "missing_table" in out["person"])
            and not out["batting_seasons"]
            and not (isinstance(out["lineup_seasons"], list)
                     and out["lineup_seasons"])):
        raise ApiError(404, f"no person {person_id!r} in this database")
    out["tag_counts"] = tag_counts(conn, person_id)
    return out


def tag_counts(conn, person_id: str) -> dict:
    """Plays the person batted in carrying each tag, per season (§3.3).

    Counted over exactly the population `Search().batter(id)` defines --
    default quality filters and game types included -- so each cell equals
    `Search().batter(id).season(y).tag(name).count()`, and the page can link
    every number to the query that reproduces it. Tag counts, not statistics.
    """
    sql, params = Search().batter(person_id)._compile(
        "p.play_id, g.season", order=False)
    rows = conn.execute(
        "SELECT m.season, t.name, count(*) FROM play_tags pt"
        " JOIN tags t USING (tag_id)"
        f" JOIN ({sql}) m ON m.play_id = pt.play_id"
        " WHERE pt.confidence = 'certain'"
        " GROUP BY m.season, t.name ORDER BY m.season, t.name", params)
    counts: dict = {}
    for season, name, n in rows:
        counts.setdefault(name, {})[season] = n
    return {"counts": counts,
            "game_types": list(DEFAULT_GAME_TYPES),
            "note": ("plays in the corpus carrying each tag, with the default"
                     " quality filters; not a batting line")}


# -- teams (§3.4) -----------------------------------------------------------

def team(conn, team_id: str) -> dict:
    if not has(conn, "teams"):
        return {"team_id": team_id, "franchise": missing("teams"),
                "seasons": missing("teams")}
    seasons = _rows(conn, (
        "SELECT season, league, city, nickname FROM teams WHERE team_id = ?"
        " ORDER BY season"), (team_id,))
    franchise = (_row(conn, "SELECT * FROM franchises WHERE team_id = ?",
                      (team_id,)) if has(conn, "franchises") else None)
    if not seasons and not franchise:
        raise ApiError(404, f"no team {team_id!r}")
    return {"team_id": team_id, "franchise": franchise, "seasons": seasons}


def team_season(conn, team_id: str, season) -> dict:
    season = _int(season, "season")
    out = {"team_id": team_id, "season": season}
    out["team"] = (_row(conn, (
        "SELECT * FROM teams WHERE team_id = ? AND season = ?"),
        (team_id, season)) if has(conn, "teams") else missing("teams"))
    out["roster"] = (_rows(conn, (
        "SELECT person_id, first, last, bats, throws, position, stated_team_id"
        " FROM roster_entries WHERE team_id = ? AND season = ?"
        " ORDER BY last, first"), (team_id, season))
        if has(conn, "roster_entries") else missing("roster_entries"))
    # Through ix_games_season, never a scan of games by team (§6).
    games = _rows(conn, (
        "SELECT game_key, game_id, date, game_number, away_team, home_team,"
        " final_away, final_home, game_type, plays, parse_status"
        " FROM games WHERE season = ? AND (home_team = ? OR away_team = ?)"
        " ORDER BY date, game_number, game_key"), (season, team_id, team_id))
    w = l = t = 0
    for g in games:
        if g["final_home"] is None or g["final_away"] is None:
            continue
        mine, theirs = ((g["final_home"], g["final_away"])
                        if g["home_team"] == team_id
                        else (g["final_away"], g["final_home"]))
        w += mine > theirs
        l += mine < theirs
        t += mine == theirs
    out["games"] = games
    out["record"] = {"won": w, "lost": l, "tied": t,
                     "note": "over the games in the corpus only"}
    if has(conn, "arc.game_logs"):
        held = {g["game_id"] for g in games}
        logged = _rows(conn, (
            "SELECT game_id, date, game_number, series, away_team, home_team,"
            " away_score, home_score FROM arc.game_logs"
            " WHERE date >= ? AND date < ? AND (home_team = ? OR away_team = ?)"
            " ORDER BY date, game_number"),
            (f"{season}-01-01", f"{season + 1}-01-01", team_id, team_id))
        # The same game can sit in more than one log file -- the postseason
        # archives have been loaded from two directories -- so rows are
        # collapsed on (game_id, series) and the collapse is counted, not
        # hidden.
        seen, unique = set(), []
        for r in logged:
            key = (r["game_id"], r["series"])
            if key not in seen:
                seen.add(key)
                unique.append(r)
        regular = [r for r in unique if r["series"] == "regular"]
        other = [r for r in unique if r["series"] != "regular"]
        out["game_logs"] = {
            "regular_listed": len(regular),
            # The measured gap: what the logs list and no event file holds.
            "missing_games": [r for r in regular
                              if r["game_id"] not in held],
            # Postseason and all-star games. The corpus holds no event files
            # for them by design (06-QUERY §5), so they are listed apart and
            # never counted as missing -- the rule `coverage` follows too.
            "other_series": other,
            "duplicate_rows": len(logged) - len(unique),
        }
    else:
        out["game_logs"] = missing("arc.game_logs")
    return out


# -- parks, dates, seasons --------------------------------------------------

def park(conn, park_id: str) -> dict:
    record = (_row(conn, "SELECT * FROM parks WHERE park_id = ?", (park_id,))
              if has(conn, "parks") else missing("parks"))
    # The one browse query that scans `games`: `games.site` has no index, and
    # the UI adds none (§6). 26 ms warm over 203,285 rows.
    seasons = _rows(conn, (
        "SELECT season, count(*) AS games, min(date) AS first,"
        " max(date) AS last FROM games WHERE site = ?"
        " GROUP BY season ORDER BY season"), (park_id,))
    if not seasons and not record:
        raise ApiError(404, f"no park {park_id!r}")
    return {"park_id": park_id, "park": record, "seasons": seasons}


def park_games(conn, park_id: str, season) -> dict:
    season = _int(season, "season")
    return {"park_id": park_id, "season": season, "games": _rows(conn, (
        "SELECT game_key, game_id, date, away_team, home_team, final_away,"
        " final_home FROM games WHERE season = ? AND site = ?"
        " ORDER BY date, game_number, game_key"), (season, park_id))}


def date(conn, day: str) -> dict:
    games = _rows(conn, (
        "SELECT game_key, game_id, game_number, away_team, home_team,"
        " final_away, final_home, site, league, game_type, plays"
        " FROM games WHERE date = ? ORDER BY league, home_team, game_number"),
        (day,))
    names = _team_names(conn, [g["home_team"] for g in games]
                        + [g["away_team"] for g in games],
                        int(day[:4]) if day[:4].isdigit() else None)
    for g in games:
        g["home_name"] = names.get(g["home_team"])
        g["away_name"] = names.get(g["away_team"])
    return {"date": day, "games": games}


def season(conn, year) -> dict:
    year = _int(year, "season")
    counts: dict = {}
    for home, away in conn.execute(
            "SELECT home_team, away_team FROM games WHERE season = ?",
            (year,)):
        for t in (home, away):
            counts[t] = counts.get(t, 0) + 1
    teams = (_rows(conn, (
        "SELECT team_id, league, city, nickname FROM teams WHERE season = ?"
        " ORDER BY league, city"), (year,))
        if has(conn, "teams") else [{"team_id": t} for t in sorted(counts)])
    listed = {t["team_id"] for t in teams}
    for t in teams:
        t["games_in_corpus"] = counts.get(t["team_id"], 0)
    for t in sorted(set(counts) - listed):
        teams.append({"team_id": t, "games_in_corpus": counts[t]})
    coverage = (_rows(conn, "SELECT * FROM coverage WHERE season = ?"
                            " ORDER BY league", (year,))
                if has(conn, "coverage") else missing("coverage"))
    return {"season": year, "teams": teams, "coverage": coverage}


def seasons(conn) -> dict:
    """Every season in the corpus, for the browse index."""
    if has(conn, "coverage"):
        rows = _rows(conn, (
            "SELECT season, group_concat(league, '/') AS leagues,"
            " sum(games) AS games FROM coverage GROUP BY season"
            " ORDER BY season"))
    else:
        rows = _rows(conn, (
            "SELECT season, count(*) AS games FROM games GROUP BY season"
            " ORDER BY season"))
    return {"seasons": rows}


def about(conn, attribution: str) -> dict:
    from ..query.search import _corpus_version
    return {"corpus_version": _corpus_version(conn),
            "ontology_version": ONTOLOGY_VERSION,
            "attribution": attribution}
