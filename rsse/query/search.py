"""The search builder (spec/06-QUERY.md).

`Search()` is immutable: every method returns a new instance, so a partially
built query can be shared and specialised without surprises. Nothing touches
the database until `.run()`, `.count()` or `.explain()`.

The compiler's one structural decision is in `_compile`: tag predicates become
JOINs at the top level, because `ix_playtags_tag (tag_id, play_id)` is by far
the most selective index available, but the same predicate becomes an `EXISTS`
inside `.any_of()` / `.none_of()`, where a JOIN cannot express OR. Each
predicate therefore carries both forms and the compiler picks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from ..semantic.ontology import ONTOLOGY_VERSION, REGISTRY
from .results import (CoverageReport, ExcludedCounts, ForceReport, PlayResult,
                      ResultSet)

#: Game types that count, per §5. Everything the corpus does not label is a
#: regular-season game -- see `_game_type_sql`.
DEFAULT_GAME_TYPES = ("regular", "playoff", "worldseries", "lcs",
                      "divisionseries", "wildcard", "championship")
POSTSEASON_TYPES = ("worldseries", "lcs", "divisionseries", "wildcard",
                    "championship")

_BASES = {"1": 0, "2": 1, "3": 2}

#: Placeholder for the tag-confidence filter, substituted in `_compile` with
#: the alias in scope. See `Search.tag`.
CONF_TOKEN = "@@CONF@@"


class QueryError(ValueError):
    """A query that cannot be answered, stated as such rather than guessed at."""


@dataclass(frozen=True)
class Pred:
    """One predicate, in both of the forms the compiler may need.

    ``where`` is a boolean expression over the aliases ``p`` (plays) and ``g``
    (games), always available. ``join`` is the faster equivalent for a
    top-level AND, a template with ``{a}`` for the alias the compiler assigns.
    """

    where: str
    params: tuple = ()
    join: str | None = None
    join_params: tuple = ()
    #: Set when the predicate reads `runner_advances.force_certainty`, which
    #: obliges the result to carry a ForceReport (§3.3).
    is_force: bool = False
    #: The base the force predicate asked about, so the report can describe the
    #: force outs the query *selected on* rather than every force out that
    #: happens to sit on a matched play.
    force_at: str | None = None
    #: Set when the predicate needs `games`, so the compiler joins it.
    needs_games: bool = False
    #: Set when the predicate narrows which *games* were searched, and can
    #: therefore be applied to the coverage scope. Only predicates that are
    #: pure `games` column tests qualify: `.batting_team()` reads
    #: `p.batting_team` and so cannot be evaluated without `plays`.
    games_only: bool = False


def _tag_id_sql(name: str) -> str:
    """Resolve a tag name to its id inside SQL rather than in Python.

    A subquery keeps the API usable against a database whose `tags` table was
    loaded by a different ontology version: an unknown name yields no rows
    rather than a KeyError, and `Search.validate()` is what reports it.
    """
    return "(SELECT tag_id FROM tags WHERE name = ?)"


@dataclass(frozen=True)
class Search:
    """An immutable query. See spec/06-QUERY.md."""

    preds: tuple[Pred, ...] = ()
    _include_uncertain: bool = False
    _include_untrusted: bool = False
    _include_unparsed: bool = False
    _curated: str | None = None          # None | 'only' | 'exclude'
    _game_types: tuple[str, ...] = DEFAULT_GAME_TYPES
    _order: str | None = None
    _limit: int | None = None

    # -- plumbing --------------------------------------------------------

    def _with(self, *preds: Pred) -> "Search":
        return replace(self, preds=self.preds + preds)

    # -- §2 context predicates -------------------------------------------

    def outs(self, n: int) -> "Search":
        return self._with(Pred("p.outs_before = ?", (n,)))

    def outs_after(self, n: int) -> "Search":
        return self._with(Pred("p.outs_after = ?", (n,)))

    def bases(self, state: str) -> "Search":
        if not re.fullmatch(r"[01]{3}", state):
            raise QueryError(f"bases state must be '000'..'111', got {state!r}")
        return self._with(Pred("p.bases_before = ?", (state,)))

    def bases_loaded(self) -> "Search":
        return self.bases("111")

    def bases_empty(self) -> "Search":
        return self.bases("000")

    def runner_on(self, base: str) -> "Search":
        if str(base) not in _BASES:
            raise QueryError(f"base must be 1, 2 or 3, got {base!r}")
        return self._with(Pred(
            f"substr(p.bases_before, {_BASES[str(base)] + 1}, 1) = '1'"))

    def scoring_position(self) -> "Search":
        return self._with(Pred("(substr(p.bases_before,2,1) = '1'"
                               " OR substr(p.bases_before,3,1) = '1')"))

    def inning(self, n: int) -> "Search":
        return self._with(Pred("p.inning = ?", (n,)))

    def inning_at_least(self, n: int) -> "Search":
        return self._with(Pred("p.inning >= ?", (n,)))

    def half(self, which: str) -> "Search":
        if which not in ("top", "bottom"):
            raise QueryError("half must be 'top' or 'bottom'")
        return self._with(Pred("p.half = ?", (which,)))

    def inning_ending(self) -> "Search":
        return self._with(Pred("p.is_inning_ending = 1"))

    def walkoff(self) -> "Search":
        return self._with(Pred("p.is_walkoff = 1"))

    def score_diff(self, lo: int, hi: int) -> "Search":
        return self._with(Pred(
            "(p.score_batting_before - p.score_fielding_before) BETWEEN ? AND ?",
            (lo, hi)))

    def season(self, year: int) -> "Search":
        return self._with(Pred("g.season = ?", (year,), needs_games=True,
                               games_only=True))

    def seasons(self, lo: int, hi: int) -> "Search":
        return self._with(Pred("g.season BETWEEN ? AND ?", (lo, hi),
                               needs_games=True,
                               games_only=True))

    def league(self, code: str) -> "Search":
        return self._with(Pred("g.league = ?", (code,), needs_games=True,
                               games_only=True))

    def team(self, code: str) -> "Search":
        return self._with(Pred("(g.home_team = ? OR g.away_team = ?)",
                               (code, code), needs_games=True,
                               games_only=True))

    def batting_team(self, code: str) -> "Search":
        # `batting_team` is 0 visitor / 1 home as Retrosheet numbers it, and
        # `htbf` does not change that -- it changes only which half they bat in.
        return self._with(Pred(
            "((p.batting_team = 1 AND g.home_team = ?)"
            " OR (p.batting_team = 0 AND g.away_team = ?))",
            (code, code), needs_games=True))

    def fielding_team(self, code: str) -> "Search":
        return self._with(Pred(
            "((p.batting_team = 0 AND g.home_team = ?)"
            " OR (p.batting_team = 1 AND g.away_team = ?))",
            (code, code), needs_games=True))

    def batter(self, player_id: str) -> "Search":
        return self._with(Pred("p.batter_id = ?", (player_id,)))

    def park(self, site_id: str) -> "Search":
        return self._with(Pred("g.site = ?", (site_id,), needs_games=True,
                               games_only=True))

    def pitcher(self, player_id: str) -> "Search":
        """Plays on which ``player_id`` was the pitcher of record.

        Not a column: `plays` names the batter only. The pitcher has to be
        reconstructed from `lineup_entries` -- the latest entry at position 1
        for the fielding team that took effect at or before this play.
        """
        return self.fielder(1, player_id)

    def fielder(self, position: int, player_id: str) -> "Search":
        """Plays on which ``player_id`` held ``position`` for the fielding team.

        The lineup is a *timeline*, not a mapping. A `start` record applies
        from the first play; a `sub` record applies only from the play after
        the one it follows. So the holder of a position at a given play is the
        last entry that had taken effect by then -- which is why this compiles
        to a NOT EXISTS over later entries rather than an equality join.

        Deliberately not answered with `fielding_credits`: those record who
        *touched the ball*, which is a different question, and a pitcher who
        faced nine batters without a putout would vanish from the results.
        See `.putout_by()` for the credits question.
        """
        position = int(position)
        # `team` in lineup_entries is 0 visitor / 1 home, as in `batting_team`,
        # so the fielding side is its complement.
        effective = ("(le.is_sub = 0 OR le.play_id IS NULL"
                     " OR le.play_id < p.play_id)")
        later = ("(le2.is_sub = 0 OR le2.play_id IS NULL"
                 " OR le2.play_id < p.play_id)")
        return self._with(Pred(
            "EXISTS (SELECT 1 FROM lineup_entries le"
            "  WHERE le.game_key = p.game_key AND le.position = ?"
            "    AND le.team = 1 - p.batting_team AND le.player_id = ?"
            f"   AND {effective}"
            "    AND NOT EXISTS (SELECT 1 FROM lineup_entries le2"
            "         WHERE le2.game_key = p.game_key AND le2.position = ?"
            "           AND le2.team = le.team AND le2.seq > le.seq"
            f"          AND {later}))",
            (position, player_id, position)))

    # -- §3 play predicates ----------------------------------------------

    def tag(self, name: str) -> "Search":
        """Match a tag from the ontology (§3).

        The confidence filter is left as a token and resolved in `_compile`,
        not baked in here. Reading `self._include_uncertain` at this point
        would make `.strikeout().include_uncertain()` mean something different
        from `.include_uncertain().strikeout()` -- an order-dependence with no
        visible symptom, in a builder whose whole contract is that order does
        not matter.
        """
        td = REGISTRY.get(name)
        if td is not None and td.alias_of:
            name = td.alias_of
        return self._with(Pred(
            where=("EXISTS (SELECT 1 FROM play_tags x WHERE"
                   " x.play_id = p.play_id AND x.tag_id = "
                   + _tag_id_sql(name) + CONF_TOKEN + ")"),
            params=(name,),
            join=("JOIN play_tags {a} ON {a}.play_id = p.play_id"
                  " AND {a}.tag_id = " + _tag_id_sql(name) + CONF_TOKEN),
            join_params=(name,)))

    def strikeout(self) -> "Search":
        return self.tag("Strikeout")

    def uncaught_third_strike(self) -> "Search":
        return self.tag("UncaughtThirdStrike")

    def dropped_third(self) -> "Search":
        return self.tag("UncaughtThirdStrike")

    def batter_reached_on_k(self) -> "Search":
        return self.tag("BatterReachedOnK")

    def double_play(self) -> "Search":
        return self.tag("DoublePlay")

    def triple_play(self) -> "Search":
        return self.tag("TriplePlay")

    def stolen_base(self, base: str | None = None) -> "Search":
        s = self.tag("StolenBase")
        if base is None:
            return s
        # The base is in the event string, not a column: `SB%` is a basic
        # event, and `runner_advances` records the movement without saying it
        # was a steal.
        return s._with(Pred("p.event_basic LIKE ?", (f"%SB{base}%",)))

    def caught_stealing(self, base: str | None = None) -> "Search":
        s = self.tag("CaughtStealing")
        if base is None:
            return s
        return s._with(Pred("p.event_basic LIKE ?", (f"%CS{base}%",)))

    def batter_ran(self, value: str) -> "Search":
        if value not in ("yes", "no", "unknown"):
            raise QueryError("batter_ran must be 'yes', 'no' or 'unknown'")
        return self._with(Pred("p.batter_ran = ?", (value,)))

    def force_play(self, at: str | None = None, include_tag_outs: bool = False,
                   certainty: str | None = None) -> "Search":
        """An out on a runner who was forced (§3.2, §3.3).

        `is_out` is required, not merely the `X` as written: an advance whose
        credit sequence contains an error is not an out (02-GRAMMAR §5), and a
        predicate matching the `X` over-counts.
        """
        clauses = ["a.play_id = p.play_id", "a.is_out = 1"]
        params: list = []
        if not include_tag_outs:
            clauses.append("a.is_force = 1")
        if at is not None:
            clauses.append("a.destination = ?")
            params.append(at)
        if certainty is not None:
            if certainty not in ("derived", "likely", "ambiguous"):
                raise QueryError(
                    "certainty must be 'derived', 'likely' or 'ambiguous'")
            clauses.append("a.force_certainty = ?")
            params.append(certainty)
        return self._with(Pred(
            "EXISTS (SELECT 1 FROM runner_advances a WHERE "
            + " AND ".join(clauses) + ")",
            tuple(params), is_force=True, force_at=at))

    def out_at(self, base: str) -> "Search":
        return self._with(Pred(
            "EXISTS (SELECT 1 FROM runner_advances a WHERE a.play_id = p.play_id"
            " AND a.is_out = 1 AND a.destination = ?)", (base,)))

    def tag_out(self, at: str | None = None) -> "Search":
        clauses = ["a.play_id = p.play_id", "a.is_out = 1", "a.is_force = 0"]
        params: list = []
        if at is not None:
            clauses.append("a.destination = ?")
            params.append(at)
        return self._with(Pred(
            "EXISTS (SELECT 1 FROM runner_advances a WHERE "
            + " AND ".join(clauses) + ")", tuple(params)))

    def error(self, fielder: int | None = None) -> "Search":
        clauses = ["f.play_id = p.play_id", "f.credit = 'error'"]
        params: list = []
        if fielder is not None:
            clauses.append("f.fielder = ?")
            params.append(int(fielder))
        return self._with(Pred(
            "EXISTS (SELECT 1 FROM fielding_credits f WHERE "
            + " AND ".join(clauses) + ")", tuple(params)))

    def hit_location(self, pattern: str) -> "Search":
        # Locations live in the modifier list, kept as emitted text.
        return self._with(Pred("p.event_modifiers LIKE ?", (f"%{pattern}%",)))

    def event_matches(self, pattern: str) -> "Search":
        return self._with(Pred("p.event_raw REGEXP ?", (pattern,)))

    # -- §3.1 fielding sequences -----------------------------------------

    def putout_sequence(self, fielders, scope: str | None = None) -> "Search":
        """One throw sequence that **is** exactly this, e.g. `[2,1]` -> `'21'`.

        Not the whole play: `64(1)3` holds the sequences `64` and `3`, so
        `[6,4,3]` matches nothing, correctly (§3.1).
        """
        return self._sequence("cs.seq_text = ?",
                              "".join(str(f) for f in fielders), scope)

    def contains_sequence(self, fielders, scope: str | None = None) -> "Search":
        return self._sequence("cs.seq_text LIKE ?",
                              "%" + "".join(str(f) for f in fielders) + "%",
                              scope)

    def _sequence(self, test: str, value: str, scope: str | None) -> "Search":
        clauses = ["cs.play_id = p.play_id", test]
        params: list = [value]
        if scope is not None:
            if scope not in ("basic", "advance"):
                raise QueryError("scope must be 'basic' or 'advance'")
            clauses.append("cs.scope = ?")
            params.append(scope)
        return self._with(Pred(
            "EXISTS (SELECT 1 FROM credit_sequences cs WHERE "
            + " AND ".join(clauses) + ")", tuple(params)))

    def putout_by(self, fielder: int, assist_by=()) -> "Search":
        clauses = ["EXISTS (SELECT 1 FROM fielding_credits f"
                   " WHERE f.play_id = p.play_id AND f.credit = 'putout'"
                   " AND f.fielder = ?)"]
        params: list = [int(fielder)]
        for a in assist_by:
            clauses.append("EXISTS (SELECT 1 FROM fielding_credits f"
                           " WHERE f.play_id = p.play_id AND f.credit = 'assist'"
                           " AND f.fielder = ?)")
            params.append(int(a))
        return self._with(Pred("(" + " AND ".join(clauses) + ")", tuple(params)))

    # -- §1 combinators ---------------------------------------------------

    def any_of(self, *searches: "Search") -> "Search":
        """OR over sub-queries. Only their predicates are used; quality and
        game-type settings come from the outer query, which is the only place
        they can be applied coherently."""
        return self._combine(searches, " OR ", negate=False)

    def none_of(self, *searches: "Search") -> "Search":
        return self._combine(searches, " OR ", negate=True)

    def _combine(self, searches, joiner: str, negate: bool) -> "Search":
        parts, params = [], []
        force = False
        force_at = None
        needs_games = False
        for sub in searches:
            if not sub.preds:
                raise QueryError("any_of/none_of needs at least one predicate "
                                 "per sub-query")
            parts.append("(" + " AND ".join(p.where for p in sub.preds) + ")")
            for p in sub.preds:
                params.extend(p.params)
                force = force or p.is_force
                # Only carried when every branch asked about the same base;
                # otherwise the report would claim a scope the query never had.
                force_at = p.force_at if force_at in (None, p.force_at) else "*"
                needs_games = needs_games or p.needs_games
        sql = "(" + joiner.join(parts) + ")"
        if negate:
            sql = "NOT " + sql
        return self._with(Pred(sql, tuple(params), is_force=force,
                               force_at=None if force_at == "*" else force_at,
                               needs_games=needs_games))

    # -- §4 certainty and quality ----------------------------------------

    def include_uncertain(self) -> "Search":
        return replace(self, _include_uncertain=True)

    def include_untrusted(self) -> "Search":
        return replace(self, _include_untrusted=True)

    def include_unparsed(self) -> "Search":
        return replace(self, _include_unparsed=True)

    def curated_only(self) -> "Search":
        return replace(self, _curated="only")

    def exclude_curated(self) -> "Search":
        return replace(self, _curated="exclude")

    # -- §5 game types ----------------------------------------------------

    def include_exhibition(self) -> "Search":
        return replace(self, _game_types=self._game_types + ("exhibition",))

    def include_allstar(self) -> "Search":
        return replace(self, _game_types=self._game_types + ("allstar",))

    def only_postseason(self) -> "Search":
        return replace(self, _game_types=POSTSEASON_TYPES)

    def all_game_types(self) -> "Search":
        return replace(self, _game_types=())

    # -- §6 ordering -------------------------------------------------------

    def order_by(self, expr: str) -> "Search":
        return replace(self, _order=expr)

    def limit(self, n: int) -> "Search":
        return replace(self, _limit=int(n))

    # -- compilation -------------------------------------------------------

    @property
    def uses_force(self) -> bool:
        return any(p.is_force for p in self.preds)

    @property
    def force_at(self) -> str | None:
        """The base every force predicate agreed on, or None if they differ."""
        bases = {p.force_at for p in self.preds if p.is_force}
        return bases.pop() if len(bases) == 1 else None

    def _status_clause(self) -> str:
        if self._include_unparsed:
            return ""
        if self._include_untrusted:
            return "p.parse_status IN ('ok','state_untrusted')"
        return "p.parse_status = 'ok'"

    def scope_sql(self) -> tuple[str, tuple]:
        """The `FROM games` clause selecting the games this query searched.

        Returned as a fragment so callers can aggregate it however they need.
        Coverage must describe what was *searched*, not what matched: deriving
        it from the result rows makes it circular -- a query that matches in
        two seasons would report that only those two were looked at, which is
        the exact misreading the report exists to prevent.

        Only predicates on `games` columns count. A predicate on `plays`
        narrows the *result*, which is its job, and must not narrow the
        denominator the result is read against.
        """
        where, params = [], []
        for pred in self.preds:
            if pred.games_only:
                where.append(pred.where.replace(CONF_TOKEN, ""))
                params.extend(pred.params)
        gt_sql, gt_params = self._game_type_sql()
        if gt_sql:
            where.append(gt_sql)
            params.extend(gt_params)
        sql = " FROM games g"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return sql, tuple(params)

    def _game_type_sql(self) -> tuple[str, tuple]:
        """Default game-type scope (§5).

        `games.game_type` is NULL wherever the file states no `info,gametype`
        record -- which is 95% of the corpus, since the key only appears from
        2023. NULL is kept on the row rather than backfilled, so that "we were
        told this was a regular-season game" stays distinct from "nothing said
        otherwise"; the documented default is applied here instead.
        """
        if not self._game_types:
            return "", ()
        marks = ",".join("?" for _ in self._game_types)
        return (f"coalesce(g.game_type, 'regular') IN ({marks})",
                tuple(self._game_types))

    def _compile(self, select: str, order: bool = True) -> tuple[str, tuple]:
        joins, wheres = [], []
        jparams, wparams = [], []
        needs_games = any(p.needs_games for p in self.preds)

        for i, pred in enumerate(self.preds):
            if pred.join:
                alias = f"t{i}"
                conf = ("" if self._include_uncertain
                        else f" AND {alias}.confidence = 'certain'")
                joins.append(pred.join.format(a=alias).replace(CONF_TOKEN, conf))
                jparams.extend(pred.join_params)
            else:
                conf = ("" if self._include_uncertain
                        else " AND x.confidence = 'certain'")
                wheres.append(pred.where.replace(CONF_TOKEN, conf))
                wparams.extend(pred.params)

        status = self._status_clause()
        if status:
            wheres.append(status)
        if self._curated == "only":
            wheres.append("EXISTS (SELECT 1 FROM play_tags c"
                          " WHERE c.play_id = p.play_id AND c.source='curated')")
        elif self._curated == "exclude":
            wheres.append("NOT EXISTS (SELECT 1 FROM play_tags c"
                          " WHERE c.play_id = p.play_id AND c.source='curated')")

        gt_sql, gt_params = self._game_type_sql()
        if gt_sql:
            wheres.append(gt_sql)
            wparams.extend(gt_params)
            needs_games = True

        sql = [f"SELECT {select} FROM plays p"]
        if needs_games or order:
            sql.append("JOIN games g ON g.game_key = p.game_key")
        sql.extend(joins)
        if wheres:
            sql.append("WHERE " + "\n   AND ".join(wheres))
        if order:
            # Always total, never left to SQLite: `date` alone ties within a
            # doubleheader and `game_id` alone ties across the three repeated
            # ids (05-DATABASE §2).
            sql.append("ORDER BY " + (self._order or
                                      "g.date, p.game_id, p.game_key, p.seq"))
        if order and self._limit:
            sql.append(f"LIMIT {int(self._limit)}")
        return "\n".join(sql), tuple(jparams) + tuple(wparams)

    # -- execution ---------------------------------------------------------

    def explain(self, conn) -> dict:
        """The SQL, its parameters and the query plan (§1).

        Public API on purpose: a researcher must be able to audit what a
        "never happened" answer actually asked.
        """
        sql, params = self._compile("p.play_id")
        plan = [tuple(r) for r in
                conn.execute("EXPLAIN QUERY PLAN " + sql, params)]
        scan = [r for r in plan if "SCAN p" in str(r)]
        return {"sql": sql, "params": params, "plan": plan,
                "warnings": (["full scan of plays: expect seconds, not "
                              "milliseconds"] if scan else [])}

    def count(self, conn) -> int:
        sql, params = self._compile("COUNT(*)", order=False)
        return conn.execute(sql, params).fetchone()[0]

    def validate(self, conn) -> list[str]:
        """Names used by this query that the database does not know."""
        missing = []
        for pred in self.preds:
            if pred.join and pred.join_params:
                name = pred.join_params[0]
                if not conn.execute("SELECT 1 FROM tags WHERE name = ?",
                                    (name,)).fetchone():
                    missing.append(name)
        return missing

    def run(self, conn, limit: int | None = None) -> ResultSet:
        from .report import build_coverage, build_excluded, build_force
        search = self if limit is None else self.limit(limit)
        unknown = search.validate(conn)
        if unknown:
            raise QueryError(f"no such tag(s) in this database: {unknown}. "
                             "The ontology and the loaded corpus disagree.")

        cols = ("p.play_id, p.game_id, g.date, g.season, p.inning, p.half,"
                " p.batter_id, p.event_raw, p.outs_before, p.bases_before,"
                " p.runs_on_play, p.parse_status")
        sql, params = search._compile(cols)
        rows = []
        for r in conn.execute(sql, params):
            rows.append(PlayResult(*r))
        rows = tuple(replace(r, tags=_tags_for(conn, r.play_id)) for r in rows)

        total = search.count(conn) if search._limit else len(rows)
        return ResultSet(
            rows=rows, total=total,
            coverage=build_coverage(conn, search),
            excluded=build_excluded(conn, search),
            force=build_force(conn, search) if search.uses_force else None,
            sql=sql, params=params,
            ontology_version=ONTOLOGY_VERSION,
            corpus_version=_corpus_version(conn),
        )


def _tags_for(conn, play_id: int) -> tuple[str, ...]:
    return tuple(r[0] for r in conn.execute(
        "SELECT t.name FROM play_tags pt JOIN tags t USING (tag_id)"
        " WHERE pt.play_id = ? ORDER BY t.name", (play_id,)))


def _corpus_version(conn) -> str:
    """Identify the data a result came from.

    `corpus_ref` is written by ingest and may be absent from a query database
    built on its own, so the derive run is the fallback -- it is the thing that
    actually produced these rows.
    """
    row = conn.execute("SELECT rsse_version, ingested_at FROM corpus_ref"
                       " ORDER BY corpus_id DESC LIMIT 1").fetchone()
    if row:
        return f"{row[0]} ({row[1]})"
    row = conn.execute("SELECT parser_version, derived_at, games FROM derive_runs"
                       " ORDER BY derive_id DESC LIMIT 1").fetchone()
    if row:
        return f"parser {row[0]}, derived {row[1]}, {row[2]:,} games"
    return "unknown"
