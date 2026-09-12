"""Pinned query benchmarks (spec/07-TESTING.md §5).

A budget nobody measures is a wish. These are the queries the project claims
to be fast at, each with a wall-clock budget, run against the real corpus so a
regression fails rather than being noticed later by a user.

Two things this harness is careful about.

**Warm cache, stated.** Every query is run once to warm SQLite's page cache and
then timed over repeats, and the median is reported rather than the mean: one
unlucky run competing with another process should not move the number. Cold
timings are a different measurement and are not what §5 budgets.

**The plan is part of the result.** A query can hit its budget today and be one
row-count away from a full scan of `plays`. Every benchmark also records
whether its plan scans 17.9 million rows, so the *reason* a number is good is
pinned alongside the number.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

from .query import Search


@dataclass(frozen=True)
class Benchmark:
    name: str
    #: Milliseconds: roughly three times the measured median on the reference
    #: corpus, floored at 100 ms.
    #:
    #: Three times, not the measured value, because this machine's timings move
    #: with disk contention -- a derive running alongside took one measurement
    #: from 100% CPU to 9%. A budget set at the observed number fails on a busy
    #: afternoon and teaches everyone to ignore it. Three times still catches
    #: what matters: the regressions these budgets exist for were 160x and
    #: 3,800x, not 20%.
    #:
    #: Measured values are in spec/07-TESTING.md §5 alongside the budgets, so
    #: the headroom is visible rather than implied.
    budget_ms: float
    build: object                 # () -> Search
    mode: str = "count"           # count | run
    note: str = ""


@dataclass
class Result:
    name: str
    budget_ms: float
    median_ms: float
    best_ms: float
    rows: int
    scans_plays: bool
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.median_ms <= self.budget_ms

    @property
    def ratio(self) -> float:
        return self.median_ms / self.budget_ms if self.budget_ms else 0.0


def _motivating() -> Search:
    return (Search().bases_loaded().outs(2).dropped_third()
            .force_play(at="H").putout_sequence([2, 1]))


#: The pinned set. Each names a *shape* of query rather than one question, so a
#: regression in the compiler shows up even if no user asks this exactly.
BENCHMARKS = (
    Benchmark("motivating: sql", 500, _motivating, "rows",
              "the compiled query alone"),
    Benchmark("motivating: full", 3000, _motivating, "run",
              "the same query with its mandatory reports -- what a user waits"),
    Benchmark("common tag count", 10000,
              lambda: Search().tag("GroundOut"), "count",
              "counts ~3.9M play_tags rows; the worst realistic case"),
    Benchmark("rare tag count", 100,
              lambda: Search().tag("HiddenBallTrick"), "count",
              "the index should make rarity cheap"),
    Benchmark("rare tag with rows", 1000,
              lambda: Search().tag("TriplePlay"), "run",
              "materialising results, not just counting"),
    Benchmark("tag + season", 700,
              lambda: Search().strikeout().seasons(2000, 2000), "count"),
    Benchmark("two tags", 800,
              lambda: Search().dropped_third().tag("ForceOutAtHome"), "count"),
    Benchmark("force predicate", 500,
              lambda: Search().force_play(at="H"), "count",
              "EXISTS over runner_advances on its partial index"),
    Benchmark("putout sequence", 100,
              lambda: Search().putout_sequence([6, 4, 3]), "count",
              "equality on ix_credseq_text"),
    Benchmark("contains_sequence", 5000,
              lambda: Search().contains_sequence([6, 4]), "count",
              "LIKE '%64%' -- unindexable by construction, budgeted apart"),
    Benchmark("context only", 2000,
              lambda: Search().bases_loaded().outs(2), "count",
              "no tag to drive the plan; the slow path by design"),
    Benchmark("pitcher lookup", 200,
              lambda: Search().pitcher("hampm001"), "count",
              "lineup timeline: EXISTS over NOT EXISTS"),
)


def run_one(conn, bench: Benchmark, repeats: int = 3) -> Result:
    search = bench.build()
    plan = search.explain(conn)
    scans = bool(plan["warnings"])

    # Warm the cache first: a cold page-cache timing measures the disk, and
    # §5 budgets a warm one.
    rows = _execute(conn, search, bench.mode)
    times = []
    for _ in range(repeats):
        started = time.perf_counter()
        _execute(conn, search, bench.mode)
        times.append((time.perf_counter() - started) * 1000)
    return Result(bench.name, bench.budget_ms, statistics.median(times),
                  min(times), rows, scans, bench.note)


def _execute(conn, search: Search, mode: str) -> int:
    if mode == "count":
        return search.count(conn)
    if mode == "rows":
        # The compiled SQL alone: no CoverageReport, no ExcludedCounts, no
        # ForceReport. Measured separately from `run` because those are not
        # free -- `ExcludedCounts` re-runs the query with the status filter
        # relaxed and `ForceReport` runs four more aggregates -- and a budget
        # that cannot tell the query from its disclosure cannot say which one
        # regressed.
        sql, params = search._compile(
            "p.play_id, p.game_id, g.date, p.event_raw")
        return len(conn.execute(sql + " LIMIT 25", params).fetchall())
    result = search.run(conn, limit=25)
    return result.total


def run_all(conn, repeats: int = 3, only: str | None = None,
            progress=None) -> list[Result]:
    results = []
    for bench in BENCHMARKS:
        if only and only not in bench.name:
            continue
        if progress:
            progress(bench)
        results.append(run_one(conn, bench, repeats))
    return results
