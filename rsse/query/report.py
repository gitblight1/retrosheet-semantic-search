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


#: Predicates that narrow which games matched but are not visible to the
#: coverage scope, because the coverage table is keyed by (season, league).
_NARROWED = ("a team or park filter narrows the games matched; the coverage "
             "totals below are for the whole seasons and leagues searched")


def build_coverage(conn, search) -> CoverageReport:
    """What the query searched, from the `coverage` table (§6).

    Scope comes from the query's own game-level predicates, never from the
    rows it matched: coverage derived from results says "we looked exactly
    where we found something", which is worse than no coverage at all.
    """
    scope_sql, scope_params = search.scope_sql()
    pairs = conn.execute(scope_sql, scope_params).fetchall()
    if not pairs:
        return CoverageReport((), (), 0, 0, "", "",
                              notes=("no games are in scope for this query",))

    rows = []
    select = ("SELECT season, league, games, plays, first_date, last_date,"
              " games_with_pitches, games_with_count_only,"
              " games_without_pitch_data, plays_unparsed, plays_inconsistent"
              " FROM coverage WHERE season = ? AND league = ?")
    for season, league in pairs:
        rows.extend(conn.execute(select, (season, league)).fetchall())

    if not rows:
        return CoverageReport((), (), 0, 0, "", "",
                              notes=("the `coverage` table is empty or does "
                                     "not cover the seasons searched; run "
                                     "`rsse coverage --rebuild`",))

    dates = [r[4] for r in rows if r[4]] + [r[5] for r in rows if r[5]]
    notes = []
    if search._game_types and set(search._game_types).issubset(
            set(POSTSEASON_TYPES)):
        notes.append(_NO_POSTSEASON_FILES)
    if any(p.needs_games and not p.games_only for p in search.preds) or \
            any("g.home_team" in p.where or "g.site" in p.where
                for p in search.preds):
        notes.append(_NARROWED)
    return CoverageReport(
        seasons=tuple(sorted({r[0] for r in rows})),
        leagues=tuple(sorted({r[1] for r in rows})),
        games=sum(r[2] for r in rows),
        plays=sum(r[3] for r in rows),
        first_date=min(dates) if dates else "",
        last_date=max(dates) if dates else "",
        games_with_pitches=sum(r[6] for r in rows),
        games_with_count_only=sum(r[7] for r in rows),
        games_without_pitch_data=sum(r[8] for r in rows),
        plays_unparsed=sum(r[9] for r in rows),
        plays_inconsistent=sum(r[10] for r in rows),
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
    base = search.count(conn)

    # Status exclusions: one pass over the population with the status filter
    # dropped entirely, grouped by what the status actually was.
    widened = replace(search, _include_unparsed=True)
    sql, params = widened._compile("p.play_id, p.parse_status", order=False)
    by_status = dict(conn.execute(
        f"SELECT parse_status, COUNT(*) FROM ({sql}) GROUP BY parse_status",
        params))

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

    return ExcludedCounts(uncertain_tags=uncertain, untrusted_state=untrusted,
                          unparsed=unparsed, other_status=other,
                          curated=curated)


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

    counts = {}
    for level in ("derived", "likely", "ambiguous"):
        counts[level] = conn.execute(
            f"SELECT COUNT(*) FROM ({sql}) m"
            " JOIN runner_advances a ON a.play_id = m.play_id"
            " WHERE a.is_out = 1 AND a.is_force = 1"
            f"{at_clause} AND a.force_certainty = ?",
            params + at_params + (level,)).fetchone()[0]

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

    return ForceReport(derived=counts["derived"], likely=counts["likely"],
                       ambiguous=counts["ambiguous"],
                       batter_ran_unknown=unknown, state_untrusted=untrusted)
