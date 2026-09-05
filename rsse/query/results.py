"""Result types for the query API (spec/06-QUERY.md §6).

The governing rule is that **a count is never returned bare**. Every result
carries what was searched and what was left out, because the questions this
project exists to answer -- "has this ever happened?" -- are the questions
where an unqualified zero is a wrong answer rather than a small one.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CoverageReport:
    """What was actually searched (§6).

    Mandatory and not suppressible. An empty `rows` with this attached reads
    "no such play in the N games from YYYY to YYYY"; without it, it reads
    "never happened", which the data does not support.
    """

    seasons: tuple[int, ...]
    leagues: tuple[str, ...]
    games: int
    plays: int
    first_date: str
    last_date: str
    #: Games whose pitch sequences are recorded, count-only, or absent. A query
    #: about counts or pitch sequences is answerable only over the first group.
    games_with_pitches: int = 0
    games_with_count_only: int = 0
    games_without_pitch_data: int = 0
    #: Plays the corpus could not parse or could not trust, in the searched
    #: range -- the denominator's own defects, stated up front.
    plays_unparsed: int = 0
    plays_inconsistent: int = 0
    #: Caveats that apply to this particular search, in plain English.
    notes: tuple[str, ...] = ()

    @property
    def season_range(self) -> str:
        if not self.seasons:
            return "no seasons"
        return f"{min(self.seasons)}-{max(self.seasons)}"

    def describe(self) -> str:
        return (f"{self.games:,} games, {self.plays:,} plays, "
                f"{self.season_range}, "
                f"leagues {'/'.join(self.leagues) or 'none'}, "
                f"{self.first_date} to {self.last_date}")


@dataclass(frozen=True)
class ExcludedCounts:
    """What the default filters removed, by reason (§4).

    Exclusion is never silent. Each field counts plays that matched every
    *predicate* of the query and were then dropped by a quality filter, so the
    numbers are directly comparable with `ResultSet.total`.
    """

    uncertain_tags: int = 0
    untrusted_state: int = 0
    unparsed: int = 0
    other_status: int = 0
    curated: int = 0

    @property
    def total(self) -> int:
        return (self.uncertain_tags + self.untrusted_state + self.unparsed
                + self.other_status + self.curated)

    def describe(self) -> str:
        if not self.total:
            return "nothing excluded"
        parts = [f"{v:,} {k.replace('_', ' ')}"
                 for k, v in (("uncertain tags", self.uncertain_tags),
                              ("untrusted state", self.untrusted_state),
                              ("unparsed", self.unparsed),
                              ("other status", self.other_status),
                              ("curated", self.curated)) if v]
        return "excluded: " + ", ".join(parts)


@dataclass(frozen=True)
class ForceReport:
    """The mandatory disclosure on any force query (§3.3).

    `derived` and `likely` are both returned by default, so the split has to be
    stated or a user cannot tell a strong result from a weak one. `likely` is
    how the pre-1970s corpus writes an ordinary ground out, which is why it is
    not excluded and why it must not go unmentioned.
    """

    derived: int = 0
    likely: int = 0
    ambiguous: int = 0
    #: Plays in the matched population claiming no force because the batter's
    #: fate is undetermined. Included in results; the doubt is about the force,
    #: not the play (03-STATE §4.2 rule 5).
    batter_ran_unknown: int = 0
    #: Plays *excluded* from results because they sat on a base-out state known
    #: to be wrong (03-STATE §7.1). The opposite disposition to the field
    #: above, and the report must not blur the two.
    state_untrusted: int = 0

    def describe(self) -> str:
        return (f"force certainty: {self.derived:,} derived, "
                f"{self.likely:,} likely, {self.ambiguous:,} ambiguous; "
                f"{self.batter_ran_unknown:,} plays with batter_ran unknown "
                f"(included); {self.state_untrusted:,} untrusted-state "
                f"(excluded)")


@dataclass(frozen=True)
class PlayResult:
    """One matched play (§6)."""

    play_id: int
    game_id: str
    date: str | None
    season: int | None
    inning: int
    half: str
    batter_id: str
    #: The source event string, byte for byte. Every derived field above is a
    #: claim about this; the reader can always check it.
    event_raw: str
    outs_before: int | None
    bases_before: str | None
    runs_on_play: int
    parse_status: str
    tags: tuple[str, ...] = ()

    def describe(self) -> str:
        """A one-line English rendering, for `--format table`."""
        where = f"{self.half[0]}{self.inning}"
        state = (f"{self.outs_before} out, bases {self.bases_before}"
                 if self.outs_before is not None else "state unknown")
        return (f"{self.date or '????-??-??'} {self.game_id} {where:>4} "
                f"{state:<22} {self.event_raw}")


@dataclass(frozen=True)
class ResultSet:
    rows: tuple[PlayResult, ...]
    total: int
    coverage: CoverageReport
    excluded: ExcludedCounts
    sql: str
    params: tuple = ()
    corpus_version: str = ""
    ontology_version: str = ""
    #: Present only when the query used a force predicate (§3.3).
    force: ForceReport | None = None

    def __iter__(self):
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)
