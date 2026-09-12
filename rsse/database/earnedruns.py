"""`earned_runs`, and the reconciliation that checks it (spec/05-DATABASE.md §6).

A third pass over the query database rather than part of `derive`, and for a
sharper version of `secondary`'s reason: this needs nothing from the archive
that the derived tables do not already hold. `runner_advances` carries every
movement with its error negation resolved, `credit_sequences` carries the
errors, and -- the part that makes the check possible at all --
`runner_advances.raw` preserves the `(UR)` and `(TUR)` flags exactly as
Retrosheet wrote them. So the derivation runs in minutes against an existing
database instead of re-deriving 17.9 million plays.

The archive is still opened, for two records the derived tables do not keep:
`presadj`, which restates who is responsible for a runner, and `data,er`,
which is one of the three things the result is checked against.

**Three sources disagree about earned runs, and all three are checked.**
`(UR)`/`(TUR)` adjudicate 1.8 million individual runs; `data,er` gives a
per-pitcher total for every game; the game logs give a per-team total compiled
from box scores. They are not independent of each other -- Retrosheet compiled
all three -- but they are independent of *this*, and they disagree with each
other in ways that locate the weak spot faster than any one of them alone.
"""

from __future__ import annotations

import bisect
import collections
from dataclasses import dataclass, field

from ..model import earnedruns as ER
from ..parser import grammar as G
from ..parser.parser import ParseError, parse

EARNED_DDL = """
CREATE TABLE IF NOT EXISTS earned_runs (
  play_id        INTEGER NOT NULL REFERENCES plays,
  adv_seq        INTEGER NOT NULL,
  game_key       INTEGER NOT NULL REFERENCES games,
  inning         INTEGER NOT NULL,
  half           TEXT NOT NULL CHECK (half IN ('top','bottom')),
  -- The side that scored. Stored rather than read off `half`, because 51
  -- games have the home team batting first (spec/03-STATE.md §6.5) and every
  -- total here is charged to the *other* side.
  batting_team   INTEGER NOT NULL CHECK (batting_team IN (0,1)),
  runner_id      TEXT,
  -- The pitcher charged, which is the one who put the runner on base and not
  -- necessarily the one who threw the pitch he scored on (9.16(f)).
  pitcher_id     TEXT,
  -- NULL is a verdict, not a gap: 9.16 hands several of its own clauses to
  -- the scorer, and a derivation that answers anyway is inventing a fact.
  -- `certainty` says which, and every consumer counts these apart.
  earned_team    INTEGER,
  earned_pitcher INTEGER,
  certainty      TEXT NOT NULL
    CHECK (certainty IN ('derived','likely','ambiguous','untrusted')),
  reason         TEXT NOT NULL,
  -- What Retrosheet wrote on the advance: '', 'UR' or 'TUR'. Stored beside
  -- the derivation rather than used by it, so the comparison stays possible
  -- after the fact and nobody has to re-parse to run it.
  recorded       TEXT NOT NULL,
  PRIMARY KEY (play_id, adv_seq)
);
"""

EARNED_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_er_game ON earned_runs (game_key);
CREATE INDEX IF NOT EXISTS ix_er_pitcher ON earned_runs (pitcher_id);
CREATE INDEX IF NOT EXISTS ix_er_certainty ON earned_runs (certainty);
CREATE INDEX IF NOT EXISTS ix_er_disagree ON earned_runs (game_key)
  WHERE earned_team IS NOT NULL;
"""

#: Games are read in batches. One query per game would be 200,000 round trips
#: against tables this size; one query for all of them would hold the whole
#: corpus in memory.
BATCH = 250

#: `(TUR)` is not used before this season -- 3 records in 1911, then none at
#: all until 1969 (spec/07-TESTING.md §4.5). A derived `TUR` against a
#: recorded `UR` in an older game is a notation the source did not have, not a
#: disagreement about the rule, and it is counted as its own thing.
TUR_FROM = 1969


@dataclass
class BuildStats:
    #: Games read. Counted from the batch rather than from the results,
    #: because `_derive_batch` yields nothing for a game where neither side
    #: scored and a shutout is still a game that was processed.
    games: int = 0
    #: Games with at least one run in them.
    scoring_games: int = 0
    runs: int = 0
    #: certainty -> count. The deferred share is a coverage number, not a
    #: failure: it is how much of 9.16 the record cannot settle.
    by_certainty: collections.Counter = field(default_factory=collections.Counter)
    by_reason: collections.Counter = field(default_factory=collections.Counter)
    parse_errors: int = 0

    @property
    def undetermined(self) -> int:
        return self.by_certainty[ER.AMBIGUOUS] + self.by_certainty[ER.UNTRUSTED]


def build(query_conn, archive_conn, *, progress=None,
          seasons: list[int] | None = None) -> BuildStats:
    """Derive every run in the corpus and store the verdicts."""
    query_conn.executescript(EARNED_DDL)
    keys = _game_keys(query_conn, seasons)
    presadj = _presadj(archive_conn)

    stats = BuildStats()
    for start in range(0, len(keys), BATCH):
        batch = keys[start:start + BATCH]
        # Deleted a batch at a time rather than up front: a whole-corpus
        # `IN (...)` would be a 200,000-term list, and a bare `DELETE FROM`
        # would quietly discard the seasons a `--seasons` run did not ask for.
        query_conn.execute(
            "DELETE FROM earned_runs WHERE game_key IN (%s)"
            % ",".join("?" * len(batch)), batch)
        rows = []
        stats.games += len(batch)
        for _gk, verdicts in _derive_batch(query_conn, batch, presadj, stats):
            stats.scoring_games += 1
            rows.extend(verdicts)
        query_conn.executemany(
            "INSERT INTO earned_runs (play_id, adv_seq, game_key, inning,"
            " half, batting_team, runner_id, pitcher_id, earned_team,"
            " earned_pitcher, certainty, reason, recorded)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        stats.runs += len(rows)
        if progress:
            progress(stats.games, len(keys))
    query_conn.executescript(EARNED_INDEXES)
    query_conn.commit()
    return stats


def _game_keys(conn, seasons: list[int] | None) -> list[int]:
    if seasons:
        sql = ("SELECT game_key FROM games WHERE season IN (%s) ORDER BY 1"
               % ",".join("?" * len(seasons)))
        return [r[0] for r in conn.execute(sql, seasons)]
    return [r[0] for r in conn.execute("SELECT game_key FROM games ORDER BY 1")]


# ---------------------------------------------------------------------------
# feeding the rule
# ---------------------------------------------------------------------------

def _presadj(archive_conn) -> list[tuple[int, str, str]]:
    """Every `presadj` record in the corpus, by archive record id.

    3,445 records over 118 seasons, so they are read once and searched rather
    than queried per game. A `presadj` restates which pitcher is responsible
    for the runner on a base ([01-CORPUS](../../spec/01-CORPUS.md) §3), and it
    is the one thing that can overrule the derivation's own bookkeeping.
    """
    out = []
    for rid, raw in archive_conn.execute(
            "SELECT record_id, raw_line FROM raw_records"
            " WHERE record_type = 'presadj' ORDER BY record_id"):
        parts = raw.split(",")
        if len(parts) >= 3 and parts[2].strip() in ("1", "2", "3"):
            out.append((rid, parts[1].strip(), parts[2].strip()))
    return out


def _pitchers(conn, keys: list[int]) -> dict:
    """game_key -> team -> [(after_play_id, pitcher_id)] in entry order."""
    out: dict = collections.defaultdict(lambda: {0: [], 1: []})
    sql = ("SELECT game_key, team, coalesce(play_id, 0), player_id"
           "  FROM lineup_entries WHERE position = 1 AND game_key IN (%s)"
           " ORDER BY game_key, seq" % ",".join("?" * len(keys)))
    for gk, team, after, pid in conn.execute(sql, keys):
        out[gk][team].append((after, pid))
    return out


def _pitcher_at(entries: list[tuple[int, str]], play_id: int) -> str | None:
    """Who was pitching for a given play.

    `lineup_entries.play_id` is the play a substitute entered *after*, so a
    reliever owns every play strictly greater than it. Linear over a handful
    of entries per game; the window-function form belongs in SQL, where
    `.pitcher()` already has it (spec/06-QUERY.md §2.1).
    """
    who = None
    for after, pid in entries:
        if after >= play_id:
            break
        who = pid
    return who


#: Statuses whose state the reconstruction can rely on. Anything else leaves
#: an out uncounted, and an uncounted out is the difference between an inning
#: that ended in the reconstruction and one that did not.
_TRUSTED = ("ok", "parsed_untagged")


def _derive_batch(conn, keys: list[int],
                  presadj: list[tuple[int, str, str]],
                  stats: BuildStats) -> list[tuple[int, list]]:
    """Reconstruct every half-inning in a batch of games."""
    ph = ",".join("?" * len(keys))
    pitchers = _pitchers(conn, keys)
    advances: dict[int, list] = collections.defaultdict(list)
    for row in conn.execute(
            "SELECT a.play_id, a.seq, a.runner_id, a.origin, a.destination,"
            "       a.marked_out, a.is_out, a.scored, a.raw"
            "  FROM runner_advances a JOIN plays p ON p.play_id = a.play_id"
            " WHERE p.game_key IN (%s) ORDER BY a.play_id, a.seq" % ph, keys):
        advances[row[0]].append(row)

    halves: dict = collections.OrderedDict()
    ids: dict = {}
    sides: dict = {}
    prev_record: dict[int, int] = {}
    for (play_id, gk, seq, inning, half, bteam, batter, raw, record_id,
         outs_before, outs_recorded, bases_before, status) in conn.execute(
            "SELECT play_id, game_key, seq, inning, half, batting_team,"
            "       batter_id, event_raw, record_id, outs_before,"
            "       outs_recorded, bases_before, parse_status"
            "  FROM plays WHERE game_key IN (%s)"
            " ORDER BY game_key, seq" % ph, keys):
        try:
            event = parse(raw).event
        except ParseError:
            event = G.Event(groups=())
            status = "unparsed"
            stats.parse_errors += 1
        trusted = status in _TRUSTED and outs_before is not None

        movements = []
        for (_p, aseq, runner, origin, dest, marked, is_out, scored,
             araw) in advances.get(play_id, []):
            aided = pb = False
            recorded = ""
            for adv in event.advances:
                # Matched on the origin base, not on the text: the state
                # machine synthesises a row for the batter when the event
                # writes none (§3 rule 3), and that row's `raw` is its own
                # invention rather than anything the file contains.
                if adv.origin == origin:
                    aided, pb, recorded = ER.movement_flags(adv)
                    break
            movements.append(ER.Movement(
                adv_seq=aseq, origin=origin, dest=dest,
                marked_out=bool(marked), is_out=bool(is_out),
                scored=bool(scored), runner_id=runner,
                aided_by_error=aided, aided_by_passed_ball=pb,
                recorded=recorded))

        adjustments = _adjustments(presadj, prev_record.get(gk), record_id)
        prev_record[gk] = record_id if record_id is not None else prev_record.get(gk)

        halves.setdefault((gk, inning, half), []).append(ER.play_facts(
            event, seq, batter, outs_before or 0, outs_recorded or 0,
            _pitcher_at(pitchers[gk][1 - bteam], play_id), movements,
            trusted, bases_before, adjustments))
        ids[(gk, seq)] = play_id
        sides[(gk, inning, half)] = bteam

    out: dict = collections.defaultdict(list)
    for (gk, inning, half), plays in halves.items():
        for verdict in ER.reconstruct(plays):
            stats.by_certainty[verdict.certainty] += 1
            stats.by_reason[verdict.reason] += 1
            out[gk].append((
                ids[(gk, verdict.seq)], verdict.adv_seq, gk, inning, half,
                sides[(gk, inning, half)], verdict.runner_id,
                verdict.pitcher_id,
                _bit(verdict.earned_team), _bit(verdict.earned_pitcher),
                verdict.certainty, verdict.reason, verdict.recorded))
    return list(out.items())


def _bit(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _adjustments(presadj: list[tuple[int, str, str]], after: int | None,
                 before: int | None) -> tuple[tuple[str, str], ...]:
    """`presadj` records standing between two plays, as `(base, pitcher)`.

    A linear scan of 3,445 records per play would be 60 billion comparisons
    over the corpus, so the list is bisected. It is sorted by `record_id`
    because that is the order the file is in, and a `presadj` belongs to the
    play it precedes.
    """
    if after is None or before is None:
        return ()
    lo = bisect.bisect_right(presadj, (after, chr(0x10FFFF), ""))
    hi = bisect.bisect_left(presadj, (before, "", ""))
    return tuple((base, pitcher) for _rid, pitcher, base in presadj[lo:hi])


# ---------------------------------------------------------------------------
# reconciliation (spec/07-TESTING.md §4.5)
# ---------------------------------------------------------------------------

@dataclass
class RunLevel:
    """The derivation against Retrosheet's own per-run verdicts."""

    compared: int = 0
    agree: int = 0
    differ: int = 0
    #: Runs where 9.16 defers and this makes no claim. Not a failure and not
    #: an agreement -- a third thing, counted as one.
    deferred: int = 0
    #: A derived `TUR` against a recorded `UR` in a season before Retrosheet
    #: used the notation. The source cannot express the distinction, so this
    #: is neither agreement nor disagreement about the rule.
    tur_before_notation: int = 0
    by_reason: collections.Counter = field(default_factory=collections.Counter)
    by_decade: dict = field(default_factory=dict)

    @property
    def rate(self) -> float:
        return self.agree / self.compared if self.compared else 0.0


@dataclass
class TotalLevel:
    """A derived total against a published one, as a bound.

    Where the derivation defers, it produces a range rather than a number:
    `low` counts the runs it calls earned and `high` adds the ones it declines
    to call either way. The published figure either falls inside that range or
    it does not, and that is a real check without pretending to have resolved
    an ambiguity the rule leaves open.
    """

    name: str = ""
    compared: int = 0
    #: Comparisons where nothing was deferred, so the bound is a single
    #: number. These are the strict ones.
    exact: int = 0
    exact_agree: int = 0
    #: The published figure lies inside the bound.
    within: int = 0
    outside: int = 0
    #: Signed differences from the nearest edge of the bound, for the ones
    #: that fall outside it.
    spread: collections.Counter = field(default_factory=collections.Counter)

    @property
    def exact_rate(self) -> float:
        return self.exact_agree / self.exact if self.exact else 0.0

    @property
    def within_rate(self) -> float:
        return self.within / self.compared if self.compared else 0.0


@dataclass
class EarnedReconciliation:
    runs: RunLevel = field(default_factory=RunLevel)
    pitchers: TotalLevel = field(default_factory=lambda: TotalLevel("data,er"))
    individual: TotalLevel = field(
        default_factory=lambda: TotalLevel("game log individual ER"))
    team: TotalLevel = field(
        default_factory=lambda: TotalLevel("game log team ER"))
    #: Games the comparison could not reach, by cause.
    skipped: collections.Counter = field(default_factory=collections.Counter)


#: The derived verdict in Retrosheet's vocabulary. `earned_pitcher = 0` is
#: `(UR)` whatever the team ledger says, because the flag records the pitcher
#: charge; `(TUR)` is the case where only the team ledger is unearned.
_DERIVED_FLAG = ("CASE WHEN earned_pitcher = 0 THEN 'UR'"
                 "     WHEN earned_team = 0 THEN 'TUR' ELSE '' END")


def reconcile_runs(conn) -> RunLevel:
    """Compare every derived verdict against the flag Retrosheet wrote."""
    out = RunLevel()
    decade: dict = collections.defaultdict(lambda: [0, 0, 0])
    for season, derived, recorded, reason, certainty, n in conn.execute(
            "SELECT g.season, %s, e.recorded, e.reason, e.certainty, count(*)"
            "  FROM earned_runs e JOIN games g ON g.game_key = e.game_key"
            " GROUP BY 1, 2, 3, 4, 5" % _DERIVED_FLAG):
        bucket = decade[((season or 0) // 10) * 10]
        if certainty in (ER.AMBIGUOUS, ER.UNTRUSTED):
            out.deferred += n
            bucket[2] += n
            continue
        if derived == "TUR" and recorded == "UR" and (season or 0) < TUR_FROM:
            out.tur_before_notation += n
            continue
        out.compared += n
        if derived == recorded:
            out.agree += n
            bucket[1] += n
        else:
            out.differ += n
            bucket[0] += n
            out.by_reason[(recorded or "-", derived or "-", certainty,
                           reason)] += n
    out.by_decade = {k: tuple(v) for k, v in sorted(decade.items())}
    return out


def _bounds(conn, group: str) -> dict:
    """`key -> (low, high)` earned-run bounds, for a grouping of the runs.

    `low` counts the runs called earned; `high` adds the ones where 9.16
    defers. A published total is checked against the interval rather than
    against a point, because a point would require this to have decided
    something the rule does not.
    """
    out: dict = {}
    for *key, low, high in conn.execute(
            "SELECT %s, sum(%s = 1), sum(%s = 1 OR %s IS NULL)"
            "  FROM earned_runs GROUP BY %s"
            % (group[0], group[1], group[1], group[1], group[0])):
        out[tuple(key)] = (low or 0, high or 0)
    return out


def data_er(archive_conn) -> tuple[dict, set]:
    """`(game_key, pitcher) -> earned runs` from the event files' own records.

    `data,er` is Retrosheet's per-pitcher earned-run figure, written after the
    last play of each game. Records are located by `record_id` against
    `game_spans` rather than by `game_id`, because three game ids repeat in
    the corpus (spec/01-CORPUS.md §5.4) and joining on one of those would file
    a game's earned runs against the wrong game.

    Returns the totals and the set of games where at least one figure is not a
    number -- old files write `(unknown)` -- since one unknown makes the
    game's total unusable rather than merely smaller.
    """
    starts, keys = [], []
    for first, key in archive_conn.execute(
            "SELECT first_record_id, game_key FROM game_spans ORDER BY 1"):
        starts.append(first)
        keys.append(key)

    totals: dict = {}
    unknown: set = set()
    for rid, raw in archive_conn.execute(
            "SELECT record_id, raw_line FROM raw_records"
            " WHERE record_type = 'data' ORDER BY record_id"):
        parts = raw.split(",")
        if len(parts) < 4 or parts[1].strip() != "er":
            continue
        key = keys[bisect.bisect_right(starts, rid) - 1]
        value = parts[3].strip()
        if not value.isdigit():
            unknown.add(key)
            continue
        pitcher = parts[2].strip()
        totals[(key, pitcher)] = totals.get((key, pitcher), 0) + int(value)
    return totals, unknown


def _check(level: TotalLevel, low: int, high: int, published: int) -> None:
    """Score one published total against a derived bound."""
    level.compared += 1
    if low == high:
        level.exact += 1
        if low == published:
            level.exact_agree += 1
    if low <= published <= high:
        level.within += 1
    else:
        level.outside += 1
        level.spread[published - (high if published > high else low)] += 1


def reconcile_totals(query_conn, archive_conn,
                     result: EarnedReconciliation) -> None:
    """Check the derived bounds against the three published totals."""
    published, unknown = data_er(archive_conn)
    # Only games this database actually holds verdicts for. Without it a
    # `--seasons` build is scored against the whole corpus's `data,er`
    # records, and 190,000 games with no derivation read as 190,000 failures.
    derived = {gk for (gk,) in query_conn.execute(
        "SELECT DISTINCT game_key FROM earned_runs")}

    pitcher_bounds: dict = {}
    for gk, pitcher, low, high in query_conn.execute(
            "SELECT game_key, pitcher_id, sum(earned_pitcher = 1),"
            "       sum(earned_pitcher = 1 OR earned_pitcher IS NULL)"
            "  FROM earned_runs GROUP BY game_key, pitcher_id"):
        pitcher_bounds[(gk, pitcher)] = (low or 0, high or 0)

    for (gk, pitcher), value in published.items():
        if gk not in derived:
            continue
        if gk in unknown:
            result.skipped["data,er not a number"] += 1
            continue
        # `data,er` is the authority on which pitchers appeared, so a pitcher
        # with no runs against him is a real comparison against a bound of
        # (0, 0) rather than a missing row.
        low, high = pitcher_bounds.pop((gk, pitcher), (0, 0))
        _check(result.pitchers, low, high, value)
    for (gk, pitcher), (low, high) in pitcher_bounds.items():
        if high:
            # A run charged to a pitcher the event file never names. Either
            # the lineup timeline put the wrong man on the mound or the file
            # is missing a `data` record; both are findings, not noise.
            result.skipped["pitcher absent from data,er" if pitcher
                           else "no pitcher on the mound"] += 1

    logs: dict = {}
    duplicates: set = set()
    for row in archive_conn.execute(
            "SELECT game_id, home_er_individual, home_er_team,"
            "       away_er_individual, away_er_team FROM game_logs"):
        if row[0] in logs:
            duplicates.add(row[0])
        logs[row[0]] = row[1:]

    side_bounds: dict = collections.defaultdict(dict)
    for gk, batting, plow, phigh, tlow, thigh in query_conn.execute(
            "SELECT game_key, batting_team, sum(earned_pitcher = 1),"
            "       sum(earned_pitcher = 1 OR earned_pitcher IS NULL),"
            "       sum(earned_team = 1),"
            "       sum(earned_team = 1 OR earned_team IS NULL)"
            "  FROM earned_runs GROUP BY game_key, batting_team"):
        side_bounds[gk][("pitcher", 1 - batting)] = (plow or 0, phigh or 0)
        side_bounds[gk][("team", 1 - batting)] = (tlow or 0, thigh or 0)

    for gk, game_id in query_conn.execute(
            "SELECT game_key, game_id FROM games ORDER BY game_key"):
        if gk not in derived:
            continue
        if game_id in duplicates:
            result.skipped["duplicate game id"] += 1
            continue
        row = logs.get(game_id)
        if row is None:
            result.skipped["no game log"] += 1
            continue
        bounds = side_bounds.get(gk, {})
        # Field 40 and 68 of the log are *team* earned runs, 39 and 67 the
        # sum of the individual pitchers'. They are the two ledgers 9.16
        # keeps, so each is checked against the matching one and not against
        # whichever happens to be nearer.
        for fielding, individual, team in ((1, row[0], row[1]),
                                           (0, row[2], row[3])):
            if individual is not None:
                low, high = bounds.get(("pitcher", fielding), (0, 0))
                _check(result.individual, low, high, individual)
            if team is not None:
                low, high = bounds.get(("team", fielding), (0, 0))
                _check(result.team, low, high, team)


def reconcile(query_conn, archive_conn) -> EarnedReconciliation:
    """The whole earned-run check: per run, per pitcher, per team."""
    result = EarnedReconciliation()
    result.runs = reconcile_runs(query_conn)
    reconcile_totals(query_conn, archive_conn, result)
    return result
