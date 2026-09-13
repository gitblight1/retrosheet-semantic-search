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

from ..model import reference as ref
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

#: What to run to get the thing a predicate needs. Kept beside the check for
#: the reason `NON_DERIVED_TABLES` in the CLI is: "the derived tables are
#: stale" is the half-answer that sends somebody to the wrong command twice.
_REBUILD_FOR = {
    "plays.event_location": ("It was added after this database was derived;"
                             " `rsse derive --rebuild` adds it."),
    "roster_entries": "Run `rsse reference` to build it.",
}

#: Placeholder for the tag-confidence filter, substituted in `_compile` with
#: the alias in scope. See `Search.tag`.
CONF_TOKEN = "@@CONF@@"


class QueryError(ValueError):
    """A query that cannot be answered, stated as such rather than guessed at."""


#: Sub-table predicates compile to ``p.play_id IN (SELECT ...)``, never to a
#: correlated ``EXISTS``. The two are logically identical and are not remotely
#: the same query: `EXISTS` correlates on `p.play_id`, so SQLite evaluates it
#: once per candidate play and the inner index can only ever be probed, never
#: driven. `IN` lets the subquery run once and drive. Measured on the full
#: corpus, `.force_play(at="H")` went from 18,163 ms to 187 ms and
#: `.putout_sequence([6,4,3])` from 22,893 ms to 5.6 ms -- the second is a
#: 4,000x difference produced by moving three words.


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
    #: `(kind, name)` when the predicate resolves a human name against the
    #: reference tables. The SQL matches every id the name resolves to, and
    #: `Search.validate()` is what refuses an ambiguous one -- the same
    #: division of labour as tags, which also compile to a subquery and are
    #: reported by name at `run()` rather than raising while the query is
    #: being built.
    resolves: tuple[str, str] | None = None
    #: `(kind, name)` for a table or column the predicate reads that an older
    #: database may not have -- `('column', 'plays.event_location')`. Checked
    #: before the query runs so the failure is a sentence naming the pass that
    #: fixes it, rather than `OperationalError: no such column`.
    requires: tuple[str, str] | None = None


def _key(name: str) -> str:
    """Normalise a typed name: case and internal whitespace do not matter."""
    return " ".join(name.split()).lower()


#: Name resolution happens in SQL for the same reason tag lookup does: the
#: fluent API has no connection while a query is being built, and a subquery
#: with no matches is a query that returns nothing rather than one that
#: crashes. `Search.validate()` turns "no matches" and "too many" into errors
#: that name the candidates, at `run()`.
#:
#: Four spellings, because 89% of people have a different first name in
#: `biofile.csv` than on their roster line: Ruth is `George Herman` in one and
#: `Babe` in the other (spec/06-QUERY.md §8).
#: Placeholders are positional `?`, repeated, **not** numbered `?1`. The
#: compiler concatenates every predicate's SQL and parameters into one
#: statement, so a numbered placeholder here would renumber against the whole
#: query rather than against this fragment.
_PERSON_SQL = """(
 SELECT p.person_id FROM people p
  WHERE lower(trim(coalesce(p.nickname,'') || ' ' || coalesce(p.last,''))) = ?
     OR lower(trim(coalesce(p.first,'') || ' ' || coalesce(p.last,''))) = ?
     OR lower(trim(coalesce(p.last,''))) = ?
     OR p.person_id IN (SELECT r.person_id FROM roster_entries r
        WHERE lower(trim(coalesce(r.first,'') || ' '
                         || coalesce(r.last,''))) = ?))"""
_PERSON_PARAMS = 4
_PARK_PARAMS = 2
_TEAM_PARAMS = 3

_PARK_SQL = """(
 SELECT park_id FROM parks
  WHERE lower(coalesce(name,'')) = ? OR lower(coalesce(aka,'')) = ?)"""

_TEAM_SQL = """(
 SELECT team_id FROM teams
  WHERE lower(trim(coalesce(city,'') || ' ' || coalesce(nickname,''))) = ?
     OR lower(coalesce(nickname,'')) = ? OR lower(coalesce(city,'')) = ?)"""


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
        """Games in a league, by either vocabulary (§8.1).

        Two exist and they are not the same size. `games.league` is the event
        file's own four-value code -- `AL`, `NL`, `FL`, `NGL` -- which
        collapses every Negro League into one. `teams.league` is Retrosheet's
        per-season code, kept verbatim, which distinguishes `NN1`, `NN2`,
        `NAL`, `ECL`, `ANL`, `NSL` and `EW`, and spells the majors `A`/`N`
        before 1920 and after 1949.

        This matches **either**, so `NGL` still works and `NN2` now does too.
        The two overlap only on `AL` and `NL`, where they agree.
        """
        # A row-value `IN`, not a correlated `EXISTS`. The `EXISTS` form
        # probes `teams` once per game and cost 42% over the plain
        # `g.league = ?` it replaced; this builds the subquery once and is
        # indistinguishable from it. The same distinction the performance
        # suite found four times over (spec/07-TESTING.md §5.1).
        return self._with(Pred(
            "(g.league = ? OR (g.home_team, g.season) IN"
            " (SELECT team_id, season FROM teams WHERE league = ?))",
            (code, code), needs_games=True, games_only=True))

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

    def batter_named(self, name: str) -> "Search":
        """Plays batted by the person called ``name`` (§8).

        Refuses an ambiguous name rather than choosing. `Jack Robinson` is two
        players -- the one who debuted in 1949 and one who debuted in 1902 --
        and answering for whichever sorts first would be answering a different
        question in silence.
        """
        return self._with(Pred(
            "p.batter_id IN " + _PERSON_SQL,
            (_key(name),) * _PERSON_PARAMS,
            resolves=("person", name)))

    def park(self, site_id: str) -> "Search":
        return self._with(Pred("g.site = ?", (site_id,), needs_games=True,
                               games_only=True))

    def park_named(self, name: str) -> "Search":
        """Plays at the ballpark called ``name`` (§8).

        Also refuses ambiguity, and here it matters most: *Wrigley Field* is
        both Chicago's and the Los Angeles park that hosted Negro Leagues
        games and the 1961 Angels.
        """
        return self._with(Pred(
            "g.site IN " + _PARK_SQL, (_key(name),) * _PARK_PARAMS,
            needs_games=True, games_only=True, resolves=("park", name)))

    def team_named(self, name: str) -> "Search":
        """Games involving the club called ``name`` (§8).

        Matches city, nickname, or both. One club, however many seasons: the
        Brooklyn and Los Angeles Dodgers are different ids and so are two
        different answers, which is why `Dodgers` alone is ambiguous.
        """
        return self._with(Pred(
            "(g.home_team IN {t} OR g.away_team IN {t})".format(t=_TEAM_SQL),
            (_key(name),) * (_TEAM_PARAMS * 2), needs_games=True,
            games_only=True, resolves=("team", name)))

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
        # Expressed as the *interval* each entry holds the position for,
        # rather than as "no later entry has taken effect yet".
        #
        # The obvious form is `EXISTS ... AND NOT EXISTS ...` correlated on
        # `p.play_id`, which reads directly from the rule and takes 32.9
        # seconds: it walks the lineup twice for every candidate play. A
        # window function computes each holder's interval once per game --
        # `LEAD` gives the play the *next* entry took effect after, which is
        # exactly where this one stops holding -- and the whole thing becomes
        # a range test. Same answer, 90 ms.
        #
        # `team` in lineup_entries is 0 visitor / 1 home, as in `batting_team`,
        # so the fielding side is its complement. A starter has a NULL
        # `play_id` and holds from the beginning, which `coalesce(..., 0)`
        # expresses without a special case.
        return self._with(Pred(
            "p.play_id IN ("
            " SELECT pl.play_id FROM plays pl"
            "   JOIN (SELECT game_key, team, player_id,"
            "                coalesce(play_id, 0) AS start_after,"
            "                LEAD(coalesce(play_id, 0)) OVER ("
            "                    PARTITION BY game_key, team ORDER BY seq)"
            "                  AS next_after"
            "           FROM lineup_entries"
            "          WHERE position = ?"
            "            AND game_key IN (SELECT game_key FROM lineup_entries"
            "                              WHERE position = ?"
            "                                AND player_id = ?)) h"
            "     ON h.game_key = pl.game_key"
            "    AND h.team = 1 - pl.batting_team"
            "  WHERE h.player_id = ?"
            "    AND pl.play_id > h.start_after"
            "    AND (h.next_after IS NULL OR pl.play_id <= h.next_after))",
            (position, position, player_id, player_id)))

    # -- §3 play predicates ----------------------------------------------

    def position_played(self, position: str) -> "Search":
        """Plays batted by someone whose roster line that season lists
        ``position`` (§3).

        A **season** fact, not a play fact, and the difference is the whole
        caveat. The roster file gives one position per person per club per
        season -- the position they were carried at -- so this finds plays by
        players *listed* as shortstops, not plays on which the batter was
        standing at short. `.fielder(6, id)` answers that one, from the lineup
        timeline, and only for the fielding side.

        `OF` is its own value and does not imply `LF`/`CF`/`RF`: a corpus that
        spans 1908 to now carries both the undifferentiated outfielder of the
        early files and the three modern ones, and folding them together here
        would invent a precision the roster line does not have. 5,661 lines
        carry no position at all, and those people match nothing rather than
        matching everything.
        """
        position = position.upper()
        if position not in ref.POSITIONS or not position:
            raise QueryError(
                f"unknown position {position!r}; roster lines carry "
                + ", ".join(sorted(p for p in ref.POSITIONS if p)))
        # A row-value `IN` over (person, season), not a correlated `EXISTS` on
        # `g.season`: the same reason as every other sub-table predicate here
        # -- correlating makes SQLite walk 121,600 roster lines once per
        # candidate play instead of once.
        return self._with(Pred(
            "(p.batter_id, g.season) IN ("
            " SELECT r.person_id, r.season FROM roster_entries r"
            "  WHERE r.position = ?)",
            (position,), needs_games=True,
            requires=("table", "roster_entries")))

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
            where=("p.play_id IN (SELECT x.play_id FROM play_tags x WHERE"
                   " x.tag_id = " + _tag_id_sql(name) + CONF_TOKEN + ")"),
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
        # No `a.play_id = p.play_id` here: the correlation is what the
        # `IN` form exists to avoid, and leaving it inside the subquery
        # keeps the per-play evaluation while looking like the fix.
        clauses = ["a.is_out = 1"]
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
            "p.play_id IN (SELECT a.play_id FROM runner_advances a WHERE "
            + " AND ".join(clauses) + ")",
            tuple(params), is_force=True, force_at=at))

    def out_at(self, base: str) -> "Search":
        return self._with(Pred(
            "p.play_id IN (SELECT a.play_id FROM runner_advances a"
            " WHERE a.is_out = 1 AND a.destination = ?)", (base,)))

    def tag_out(self, at: str | None = None) -> "Search":
        clauses = ["a.is_out = 1", "a.is_force = 0"]
        params: list = []
        if at is not None:
            clauses.append("a.destination = ?")
            params.append(at)
        return self._with(Pred(
            "p.play_id IN (SELECT a.play_id FROM runner_advances a WHERE "
            + " AND ".join(clauses) + ")", tuple(params)))

    def error(self, fielder: int | None = None) -> "Search":
        clauses = ["f.credit = 'error'"]
        params: list = []
        if fielder is not None:
            clauses.append("f.fielder = ?")
            params.append(int(fielder))
        return self._with(Pred(
            "p.play_id IN (SELECT f.play_id FROM fielding_credits f WHERE "
            + " AND ".join(clauses) + ")", tuple(params)))

    def hit_location(self, zone: str) -> "Search":
        """Plays whose hit modifier names Retrosheet zone ``zone`` (§3).

        A trailing `*` means "and everything inside it": `7*` is left field
        proper plus `7D`, `7LS`, `78`, `78D` and the rest, because the zone
        string is written outward from the fielder. Nothing else is a
        wildcard, and `%` in particular is not -- offering full `LIKE` here
        would be offering a leading-wildcard scan of 17.9 million rows dressed
        up as a filter.

        This used to be `event_modifiers LIKE '%zone%'`, which was wrong twice
        over. It matched the JSON text rather than the location, so `8` also
        found every play carrying `E8` or an `8` anywhere in a fielder string;
        and a leading wildcard cannot use an index, so the one query shape
        anybody would actually write was the one that scanned. The location is
        now lifted to its own column at derive time (spec/05-DATABASE.md §3.1)
        and this is a range test over `ix_plays_location`.

        27.9% of parsed plays carry one, and almost all of them are modern:
        60.4% of the 1990s against 2.8% of the 1910s. A query over the whole
        corpus is asking a question the early corpus cannot answer, which is
        `.coverage()`'s department rather than this one's.
        """
        if zone.endswith("*"):
            prefix = zone[:-1]
            if not prefix:
                raise QueryError("`*` alone is not a zone")
            # An explicit range rather than `LIKE 'x%'`: with the default
            # `case_sensitive_like=OFF` SQLite will not use a BINARY index for
            # a LIKE whose pattern contains a letter, and every qualifier in
            # this vocabulary is a letter. The upper bound is the prefix with
            # its last character bumped, which is the standard trick and needs
            # no assumption about what may follow.
            hi = prefix[:-1] + chr(ord(prefix[-1]) + 1)
            return self._with(Pred(
                "(p.event_location IS NOT NULL"
                " AND p.event_location >= ? AND p.event_location < ?)",
                (prefix, hi), requires=("column", "plays.event_location")))
        return self._with(Pred(
            "(p.event_location IS NOT NULL AND p.event_location = ?)",
            (zone,), requires=("column", "plays.event_location")))

    def hit_located(self) -> "Search":
        """Plays whose scorer recorded *a* hit location, whatever it was.

        The denominator for any question about locations, and worth having
        separately: "no plays to zone 7 in 1912" and "no located plays at all
        in 1912" are different answers and the first one is false.
        """
        return self._with(Pred(
            "p.event_location IS NOT NULL",
            requires=("column", "plays.event_location")))

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
        clauses = [test]
        params: list = [value]
        if scope is not None:
            if scope not in ("basic", "advance"):
                raise QueryError("scope must be 'basic' or 'advance'")
            clauses.append("cs.scope = ?")
            params.append(scope)
        return self._with(Pred(
            "p.play_id IN (SELECT cs.play_id FROM credit_sequences cs WHERE "
            + " AND ".join(clauses) + ")", tuple(params)))

    def putout_by(self, fielder: int, assist_by=()) -> "Search":
        clauses = ["p.play_id IN (SELECT f.play_id FROM fielding_credits f"
                   " WHERE f.credit = 'putout' AND f.fielder = ?)"]
        params: list = [int(fielder)]
        for a in assist_by:
            clauses.append("p.play_id IN (SELECT f.play_id FROM"
                           " fielding_credits f"
                           " WHERE f.credit = 'assist' AND f.fielder = ?)")
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
            wheres.append("p.play_id IN (SELECT c.play_id FROM play_tags c"
                          " WHERE c.source = 'curated')")
        elif self._curated == "exclude":
            wheres.append("p.play_id NOT IN (SELECT c.play_id FROM play_tags c"
                          " WHERE c.source = 'curated')")

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
        self.check_schema(conn)
        self.check_names(conn)
        sql, params = self._compile("COUNT(*)", order=False)
        return conn.execute(sql, params).fetchone()[0]

    def check_schema(self, conn) -> None:
        """Refuse a predicate this database has no column or table for.

        The derived layer grows, and a database derived a month ago is a
        perfectly good database that cannot answer every question this API can
        ask. `.hit_location()` against one built before `plays.event_location`
        existed raised `no such column: p.event_location` from inside SQLite,
        which is true, unhelpful, and does not say that one `derive` fixes it.
        """
        for pred in self.preds:
            if not pred.requires:
                continue
            kind, name = pred.requires
            if kind == "table":
                present = conn.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
                    " AND name = ?", (name,)).fetchone()[0]
            else:
                table, column = name.split(".")
                present = any(r[1] == column for r in
                              conn.execute(f"PRAGMA table_info({table})"))
            if not present:
                raise QueryError(
                    f"this database has no {name}, so that predicate cannot"
                    f" be answered here. {_REBUILD_FOR[name]}")

    def check_names(self, conn) -> None:
        """Refuse a human name that resolves to none or to several.

        Checked here rather than when the predicate is built, because the
        fluent API has no connection until then -- the same reason tag names
        are reported by `validate()`. Costs nothing unless a name predicate is
        in use.
        """
        from .names import (AmbiguousName, parks_named, people_named,
                            resolve, teams_named)

        lookup = {"person": people_named, "park": parks_named,
                  "team": teams_named}
        for pred in self.preds:
            if not pred.resolves:
                continue
            kind, name = pred.resolves
            if not _has_reference_tables(conn):
                raise QueryError(
                    f"cannot resolve the name {name!r}: this database has no "
                    "reference tables. Run `rsse reference` to build them.")
            try:
                resolve(lookup[kind](conn, name), kind, name)
            except AmbiguousName as exc:
                raise QueryError(str(exc)) from exc

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
        search.check_schema(conn)
        search.check_names(conn)
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
        # One query for every name in the result set, not one per row.
        names = _names_for(conn, [r.batter_id for r in rows])
        if names:
            rows = tuple(replace(r, batter_name=names.get(r.batter_id))
                         for r in rows)

        # `build_excluded` groups the population by `parse_status`, and its
        # `ok` bucket *is* the number of matches -- so the total comes from
        # there rather than from a second full execution of the query.
        excluded = build_excluded(conn, search)
        total = excluded.matched if search._limit else len(rows)
        return ResultSet(
            rows=rows, total=total,
            coverage=build_coverage(conn, search),
            excluded=excluded,
            force=build_force(conn, search) if search.uses_force else None,
            sql=sql, params=params,
            ontology_version=ONTOLOGY_VERSION,
            corpus_version=_corpus_version(conn),
        )


def _has_reference_tables(conn) -> bool:
    return bool(conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        " AND name = 'people'").fetchone()[0])


def _names_for(conn, person_ids) -> dict:
    """Batter names for a result set, or `{}` if the tables are absent.

    A query database without `rsse reference` still returns results; it
    returns them without names, which is a smaller loss than refusing.
    """
    if not _has_reference_tables(conn):
        return {}
    from .names import names_for
    return names_for(conn, person_ids)


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
