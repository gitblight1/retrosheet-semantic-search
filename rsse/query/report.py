"""Coverage, exclusion and force reporting (spec/06-QUERY.md §3.3, §4, §6).

These are the parts of a result that exist so a number is never quoted bare.
Each is computed by re-running the query's *predicates* with one filter
changed, so the counts are directly comparable with the result total rather
than being an unrelated corpus statistic.
"""

from __future__ import annotations

from dataclasses import replace

from .results import CoverageReport, ExcludedCounts, ForceReport
from .search import POSTSEASON_TYPES

#: The corpus holds regular-season team files only -- no `.EVE` postseason
#: files exist in it -- so a postseason-scoped query can only match the
#: tiebreaker and Negro Leagues championship games recorded inside team files.
#: Reported as a note rather than left to be read as "never happened".
_NO_POSTSEASON_FILES = (
    "the corpus contains no postseason event files; postseason results are "
    "limited to games labelled inside regular-season team files")


#: `plays_unparsed` / `plays_inconsistent` are the only figures still taken at
#: (season, league) granularity, because that is how the `coverage` table is
#: keyed. Every other figure is aggregated from `games` under the query's own
#: scope, so it is exact.
_COARSE_DEFECTS = ("plays_unparsed and plays_inconsistent are per season and "
                   "league; the other totals are exact for this query's scope")


def build_coverage(conn, search) -> CoverageReport:
    """What the query searched (§6.1).

    Scope comes from the query's own game-level predicates, never from the rows
    it matched: coverage derived from results says "we looked exactly where we
    found something", which is worse than no coverage at all.

    Aggregated directly from `games` rather than by summing `coverage` rows.
    Summing at (season, league) granularity counts games the query's game-type,
    team or park filters excluded -- it reported 203,270 games searched for a
    default-scope query that had in fact excluded 611 exhibition and all-star
    games sitting inside otherwise-included seasons.
    """
    scope, params = search.scope_sql()
    row = conn.execute(
        "SELECT count(*), coalesce(sum(g.plays), 0),"
        " min(g.date), max(g.date),"
        " sum(CASE WHEN g.pitch_detail = 'pitches' THEN 1 ELSE 0 END),"
        " sum(CASE WHEN g.pitch_detail = 'count' THEN 1 ELSE 0 END),"
        " sum(CASE WHEN g.pitch_detail IS NULL OR g.pitch_detail"
        "     NOT IN ('pitches','count') THEN 1 ELSE 0 END)"
        + scope, params).fetchone()
    if not row or not row[0]:
        return CoverageReport((), (), 0, 0, "", "",
                              notes=("no games are in scope for this query",))

    pairs = conn.execute(
        "SELECT DISTINCT g.season, coalesce(g.league, '??')" + scope,
        params).fetchall()
    seasons = tuple(sorted({p[0] for p in pairs if p[0] is not None}))
    leagues = tuple(sorted({p[1] for p in pairs}))

    unparsed = inconsistent = 0
    missing = 0
    unmeasured = 0
    measured_any = False
    for season, league in pairs:
        defects = conn.execute(
            "SELECT plays_unparsed, plays_inconsistent, games_missing, games"
            " FROM coverage WHERE season = ? AND league = ?",
            (season, league)).fetchone()
        if defects:
            unparsed += defects[0]
            inconsistent += defects[1]
            if defects[2] is None:
                # No external list covers this slice, so its games are neither
                # held-and-verified nor missing. Counting them either way would
                # be a claim nothing checked.
                unmeasured += defects[3]
            else:
                measured_any = True
                missing += defects[2]
    missing_total = missing if measured_any else None

    notes = []
    if search._game_types and set(search._game_types).issubset(
            set(POSTSEASON_TYPES)):
        notes.append(_NO_POSTSEASON_FILES)
    if unparsed or inconsistent:
        notes.append(_COARSE_DEFECTS)
    if missing_total:
        notes.append(
            f"{missing_total:,} games in range have no event file; a zero "
            "result cannot rule them out")
    if unmeasured:
        notes.append(
            f"{unmeasured:,} games are in leagues the game logs do not cover, "
            "so their completeness is unknown rather than confirmed")
    return CoverageReport(
        seasons=seasons, leagues=leagues,
        games=row[0], plays=row[1],
        first_date=row[2] or "", last_date=row[3] or "",
        games_with_pitches=row[4] or 0,
        games_with_count_only=row[5] or 0,
        games_without_pitch_data=row[6] or 0,
        plays_unparsed=unparsed, plays_inconsistent=inconsistent,
        games_missing=missing_total, games_unmeasured=unmeasured,
        notes=tuple(notes),
    )


def build_excluded(conn, search) -> ExcludedCounts:
    """What the quality filters removed from this query's own population.

    Counted by re-running the query with the filters relaxed and grouping by
    `parse_status`, so every number is a subset of *this* result's population
    rather than an unrelated corpus statistic. That costs one extra pass and
    is the only version of the number that means anything to whoever is
    reading this particular result.
    """
    # One pass over the population with the *status* filter dropped, grouped
    # by what the status actually was. `_include_unparsed` relaxes only the
    # status, never the tag confidence, so the `ok` bucket of this grouping is
    # by construction the count of the query as asked -- and running
    # `search.count()` separately to get the same number was a second full
    # execution of the expensive part for nothing.
    widened = replace(search, _include_unparsed=True)
    sql, params = widened._compile("p.play_id, p.parse_status", order=False)
    by_status = dict(conn.execute(
        f"SELECT parse_status, COUNT(*) FROM ({sql}) GROUP BY parse_status",
        params))
    base = by_status.get("ok", 0)

    untrusted = 0 if (search._include_untrusted or search._include_unparsed) \
        else by_status.get("state_untrusted", 0)
    if search._include_unparsed:
        unparsed = other = 0
    else:
        unparsed = by_status.get("unparsed", 0)
        other = sum(v for k, v in by_status.items()
                    if k not in ("ok", "unparsed", "state_untrusted"))

    uncertain = 0
    if not search._include_uncertain:
        uncertain = max(0, replace(search, _include_uncertain=True).count(conn)
                        - base)

    curated = 0
    if search._curated == "exclude":
        curated = max(0, replace(search, _curated=None).count(conn) - base)

    return ExcludedCounts(matched=base, uncertain_tags=uncertain,
                          untrusted_state=untrusted, unparsed=unparsed,
                          other_status=other, curated=curated)


def build_force(conn, search) -> ForceReport:
    """The §3.3 disclosure. Mandatory on any query using a force predicate.

    The certainty split counts *advances*, not plays: one play can hold two
    force outs settled with different confidence, and collapsing them to the
    play would hide the weaker one.
    """
    sql, params = search._compile("p.play_id", order=False)

    # Scoped to the base the query asked about. Without this a play matching
    # "force at first" that also turned a force at second would contribute two
    # advances, and the split would describe force outs the query never
    # selected on.
    at = search.force_at
    at_clause = " AND a.destination = ?" if at else ""
    at_params = (at,) if at else ()

    # One grouped pass, not one query per level. The subquery is the
    # expensive part and running it three times to split three ways tripled
    # the cost of the disclosure for no extra information.
    counts = dict(conn.execute(
        f"SELECT a.force_certainty, COUNT(*) FROM ({sql}) m"
        " JOIN runner_advances a ON a.play_id = m.play_id"
        " WHERE a.is_out = 1 AND a.is_force = 1"
        f"{at_clause} GROUP BY a.force_certainty",
        params + at_params))

    unknown = conn.execute(
        f"SELECT COUNT(*) FROM ({sql}) m JOIN plays p2 ON p2.play_id = m.play_id"
        " WHERE p2.batter_ran = 'unknown'", params).fetchone()[0]

    # Untrusted plays are *excluded* by the default status filter, so counting
    # them means relaxing it -- the opposite disposition to `batter_ran`, which
    # is included. Reporting both without saying which is which would be worse
    # than reporting neither.
    widened = replace(search, _include_untrusted=True)
    wsql, wparams = widened._compile("p.play_id, p.parse_status", order=False)
    untrusted = conn.execute(
        f"SELECT COUNT(*) FROM ({wsql}) WHERE parse_status = 'state_untrusted'",
        wparams).fetchone()[0]

    return ForceReport(derived=counts.get("derived", 0),
                       likely=counts.get("likely", 0),
                       ambiguous=counts.get("ambiguous", 0),
                       batter_ran_unknown=unknown, state_untrusted=untrusted)
