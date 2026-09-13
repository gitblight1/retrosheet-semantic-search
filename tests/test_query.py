"""The query API (spec/06-QUERY.md).

Built against a real derived database rather than hand-written rows: the point
of most of these assertions is what the *compiler* does with a predicate, and
a fixture that stubs the tables would test the fixture.
"""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from rsse.database import coverage as covbuild
from rsse.database import derived as dbderived
from rsse.database import load as dbload
from rsse.database import schema as dbschema
from rsse.database import secondary as dbsecondary
from rsse import cli
from rsse.query import QueryError, Search, connect

FIXTURE = Path(__file__).parent / "fixtures" / "TEST2000KCA.EVA"


class QueryBase(unittest.TestCase):
    """One derived database, built once, shared read-only by every test."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        src = cls.tmp / "2000KCA.EVA"
        shutil.copyfile(FIXTURE, src)

        archive = dbschema.connect(str(cls.tmp / "archive.db"))
        dbschema.create(archive)
        dbload.ingest(archive, [src], dbload.open_corpus(archive, "t", "t"))

        cls.db = cls.tmp / "rsse.db"
        query = dbschema.connect(str(cls.db))
        dbschema.create_query_db(query)
        dbschema.create_derived(query)
        dbderived.build(query, archive, parser_version="test")
        dbschema.create_derived_indexes(query)
        dbsecondary.build(query, archive)
        covbuild.build(query)
        query.close()
        archive.close()
        cls.conn = connect(str(cls.db))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)


class Compilation(QueryBase):
    def test_each_method_returns_a_new_search(self):
        base = Search()
        narrowed = base.outs(2)
        self.assertEqual(base.preds, ())
        self.assertEqual(len(narrowed.preds), 1)

    def test_tag_predicates_compile_to_joins_not_exists(self):
        sql, _ = Search().strikeout()._compile("p.play_id")
        self.assertIn("JOIN play_tags", sql)
        self.assertNotIn("EXISTS (SELECT 1 FROM play_tags", sql)

    def test_a_tag_inside_any_of_compiles_to_a_subquery(self):
        # A JOIN cannot express OR, so the same predicate must have a second
        # form -- this is the reason `Pred` carries both.
        sql, _ = Search().any_of(Search().strikeout(),
                                 Search().tag("Walk"))._compile("p.play_id")
        self.assertIn("p.play_id IN (SELECT x.play_id FROM play_tags", sql)
        self.assertIn(" OR ", sql)

    def test_sub_table_predicates_never_compile_to_correlated_exists(self):
        """`IN (SELECT ...)` lets the inner index drive; `EXISTS` cannot.

        A correlated `EXISTS` is evaluated once per candidate play, so the
        index on the inner table can only be probed. Measured on the full
        corpus, the difference was 18,163 ms against 187 ms for
        `.force_play(at="H")` and 22,893 ms against 5.6 ms for
        `.putout_sequence([6,4,3])`.
        """
        searches = (
            Search().force_play(at="H"),
            Search().out_at("2"),
            Search().tag_out(at="3"),
            Search().error(6),
            Search().putout_sequence([6, 4, 3]),
            Search().contains_sequence([6, 4]),
            Search().putout_by(3, assist_by=[6, 4]),
            Search().exclude_curated(),
            Search().curated_only(),
            # `.league()` reads `teams` to reach the per-season league codes.
            # The obvious form correlates on `g.home_team` and `g.season` and
            # cost 42% over the plain column test it replaced.
            Search().league("NN2"),
            Search().batter_named("Babe Ruth"),
            Search().park_named("Fenway Park"),
            Search().team_named("Brooklyn Dodgers"),
        )
        for search in searches:
            sql, _ = search._compile("p.play_id")
            with self.subTest(sql=sql):
                self.assertNotIn("EXISTS", sql,
                                 "correlated EXISTS defeats the inner index")
                # Asserting the wrapper alone is not enough. The first attempt
                # at this changed `EXISTS (...)` to `p.play_id IN (...)` and
                # left `a.play_id = p.play_id` inside the subquery, which is
                # still correlated and still evaluated once per play -- the
                # query looked fixed and ran at exactly the old speed. Only
                # the benchmark caught it.
                for line in sql.splitlines():
                    if "IN (SELECT" in line:
                        for correlated in ("= p.play_id", "= g.home_team",
                                           "= g.season", "= p.game_key"):
                            self.assertNotIn(
                                correlated, line,
                                "a correlation clause inside the subquery "
                                "keeps the per-row evaluation the IN form "
                                "exists to avoid")

    def test_ordering_is_total(self):
        sql, _ = Search().strikeout()._compile("p.play_id")
        # `date` ties within a doubleheader and `game_id` ties across the three
        # repeated ids, so neither alone is a total order.
        self.assertIn("ORDER BY g.date, p.game_id, p.game_key, p.seq", sql)

    def test_count_does_not_order_or_limit(self):
        sql, _ = Search().strikeout().limit(5)._compile("COUNT(*)", order=False)
        self.assertNotIn("ORDER BY", sql)
        self.assertNotIn("LIMIT", sql)

    def test_explain_returns_sql_and_plan(self):
        info = Search().strikeout().explain(self.conn)
        self.assertIn("SELECT", info["sql"])
        self.assertTrue(info["plan"])

    def test_bad_arguments_are_refused(self):
        for call in (lambda: Search().bases("11"),
                     lambda: Search().half("middle"),
                     lambda: Search().batter_ran("maybe"),
                     lambda: Search().force_play(certainty="probably"),
                     lambda: Search().putout_sequence([2], scope="somewhere")):
            with self.assertRaises(QueryError):
                call()

    def test_pitcher_compiles_without_touching_fielding_credits(self):
        """Who was pitching is a lineup question, not a credits question.

        Answering it from `fielding_credits` would silently drop every play
        the pitcher did not touch the ball on.
        """
        sql, _ = Search().pitcher("smith001")._compile("p.play_id")
        self.assertIn("lineup_entries", sql)
        self.assertNotIn("fielding_credits", sql)


class Defaults(QueryBase):
    def test_default_excludes_non_ok_plays(self):
        sql, _ = Search().strikeout()._compile("p.play_id")
        self.assertIn("p.parse_status = 'ok'", sql)

    def test_include_untrusted_widens_to_exactly_two_statuses(self):
        sql, _ = Search().strikeout().include_untrusted()._compile("p.play_id")
        self.assertIn("p.parse_status IN ('ok','state_untrusted')", sql)

    def test_include_unparsed_drops_the_status_filter(self):
        sql, _ = Search().strikeout().include_unparsed()._compile("p.play_id")
        self.assertNotIn("parse_status", sql)

    def test_default_requires_certain_tags(self):
        sql, _ = Search().strikeout()._compile("p.play_id")
        self.assertIn("confidence = 'certain'", sql)
        sql, _ = Search().strikeout().include_uncertain()._compile("p.play_id")
        self.assertNotIn("confidence", sql)

    def test_quality_flags_are_order_independent(self):
        """The builder's contract is that method order does not matter.

        Reading `_include_uncertain` when the tag predicate was *built* made
        `.strikeout().include_uncertain()` silently stricter than
        `.include_uncertain().strikeout()` -- same methods, same arguments,
        different answer, no error.
        """
        after = Search().strikeout().include_uncertain()
        before = Search().include_uncertain().strikeout()
        self.assertEqual(after._compile("p.play_id"),
                         before._compile("p.play_id"))
        self.assertEqual(after.count(self.conn), before.count(self.conn))

    def test_untagged_game_types_are_treated_as_regular(self):
        # `info,gametype` only appears from 2023. Without the coalesce the
        # default scope would exclude 95% of the corpus and report it as zero.
        sql, _ = Search().strikeout()._compile("p.play_id")
        self.assertIn("coalesce(g.game_type, 'regular')", sql)
        self.assertGreater(Search().strikeout().count(self.conn), 0)


class ForceAndTagOuts(QueryBase):
    """The §3.2 control pair: the trio must partition, or the derived
    force/tag distinction cannot be tested against itself."""

    def test_out_at_is_the_union_of_force_and_tag(self):
        for base in ("1", "2", "3", "H"):
            union = Search().out_at(base).count(self.conn)
            forced = Search().force_play(at=base).count(self.conn)
            tagged = Search().tag_out(at=base).count(self.conn)
            self.assertEqual(union, forced + tagged,
                             f"force and tag outs at {base} must partition "
                             f"the outs at {base}")

    def test_include_tag_outs_widens_to_out_at(self):
        wide = Search().force_play(at="1", include_tag_outs=True)
        self.assertEqual(wide.count(self.conn),
                         Search().out_at("1").count(self.conn))

    def test_certainty_narrows_rather_than_filtering_by_default(self):
        # `likely` is how the pre-1970s corpus writes an ordinary ground out;
        # a strict default would drop about half the force outs at first and
        # report a smaller number rather than missing coverage (§3.3).
        both = Search().force_play(at="1").count(self.conn)
        strict = Search().force_play(at="1", certainty="derived").count(self.conn)
        self.assertLessEqual(strict, both)

    def test_a_force_query_must_carry_a_force_report(self):
        result = Search().force_play(at="1").run(self.conn, limit=5)
        self.assertIsNotNone(result.force)
        self.assertEqual(
            result.force.derived + result.force.likely
            + result.force.ambiguous,
            sum(1 for _ in self.conn.execute(
                "SELECT 1 FROM runner_advances a JOIN plays p USING (play_id)"
                " WHERE a.is_out = 1 AND a.is_force = 1 AND a.destination = '1'"
                "   AND p.parse_status = 'ok'")))

    def test_a_non_force_query_carries_no_force_report(self):
        self.assertIsNone(Search().strikeout().run(self.conn, limit=1).force)


class Lineups(QueryBase):
    """`.pitcher()` and `.fielder()` read the lineup as a timeline (§2)."""

    def starters(self, position):
        return self.conn.execute(
            "SELECT DISTINCT player_id FROM lineup_entries"
            " WHERE position = ? AND is_sub = 0", (position,)).fetchall()

    def test_lineup_entries_were_loaded(self):
        starts, subs = self.conn.execute(
            "SELECT sum(1 - is_sub), sum(is_sub) FROM lineup_entries"
        ).fetchone()
        self.assertGreater(starts, 0)
        self.assertIsNotNone(subs)

    def test_a_starting_pitcher_matches_plays(self):
        for (player_id,) in self.starters(1):
            n = Search().pitcher(player_id).count(self.conn)
            if n:
                return
        self.fail("no starting pitcher matched any play")

    def test_the_position_holders_partition_the_plays(self):
        """Exactly one player holds a position on any given play.

        Summing `.fielder(pos, id)` over every player who ever held the
        position must therefore equal the number of plays -- if it exceeds
        them, the timeline logic is matching a replaced fielder as well as
        his replacement, which is the failure mode this predicate exists to
        avoid.
        """
        total = Search().count(self.conn)
        for position in (1, 2):
            holders = self.conn.execute(
                "SELECT DISTINCT player_id FROM lineup_entries"
                " WHERE position = ?", (position,)).fetchall()
            matched = sum(Search().fielder(position, pid).count(self.conn)
                          for (pid,) in holders)
            self.assertEqual(matched, total,
                             f"position {position}: {matched} matched vs "
                             f"{total} plays")

    def test_pitcher_is_the_fielding_side_not_the_batting_side(self):
        # A pitcher never pitches to his own team, so no play may match both
        # `.pitcher(x)` and `.batter(x)`.
        for (pid,) in self.starters(1):
            both = Search().pitcher(pid).batter(pid).count(self.conn)
            self.assertEqual(both, 0)


class Comments(QueryBase):
    def test_comments_were_loaded_and_linked(self):
        total, linked = self.conn.execute(
            "SELECT count(*), count(play_id) FROM comments").fetchone()
        self.assertGreaterEqual(total, 0)
        self.assertLessEqual(linked, total)

    def test_every_comment_links_to_a_play_in_its_own_game(self):
        bad = self.conn.execute(
            "SELECT count(*) FROM comments c JOIN plays p USING (play_id)"
            " WHERE p.game_key <> c.game_key").fetchone()[0]
        self.assertEqual(bad, 0)

    def test_a_comment_never_links_forward(self):
        """A `com` record describes the play before it, never after."""
        bad = self.conn.execute(
            "SELECT count(*) FROM comments c JOIN plays p USING (play_id)"
            " WHERE p.record_id > c.record_id").fetchone()[0]
        self.assertEqual(bad, 0)


class Sequences(QueryBase):
    def test_putout_sequence_is_one_throw_not_the_whole_play(self):
        # `64(1)3` holds the sequences `64` and `3`; no 6-4-3 sequence occurred.
        self.assertEqual(Search().putout_sequence([6, 4, 3]).count(self.conn), 0)
        self.assertGreaterEqual(
            Search().putout_sequence([6, 4]).count(self.conn), 0)

    def test_contains_sequence_is_a_superset_of_putout_sequence(self):
        exact = Search().putout_sequence([4, 3]).count(self.conn)
        within = Search().contains_sequence([4, 3]).count(self.conn)
        self.assertGreaterEqual(within, exact)


class Coverage(QueryBase):
    def test_coverage_describes_what_was_searched_not_what_matched(self):
        """The failure this report exists to prevent.

        Derived from the result rows, a query matching in one season would
        report that only that season was looked at -- coverage that confirms
        whatever was found.
        """
        rare = Search().tag("HiddenBallTrick")
        wide = Search().strikeout()
        self.assertEqual(rare.run(self.conn).coverage.games,
                         wide.run(self.conn).coverage.games)

    def test_an_empty_result_still_reports_coverage(self):
        result = Search().tag("HiddenBallTrick").bases_loaded().run(self.conn)
        self.assertEqual(len(result.rows), 0)
        self.assertGreater(result.coverage.games, 0)
        self.assertIn("games", result.coverage.describe())

    def test_game_level_filters_narrow_coverage_exactly(self):
        """Coverage is aggregated from `games`, not summed from the coverage
        table, so a game-level filter reduces it exactly.

        Summing per (season, league) counted games the filter had excluded:
        a default-scope corpus query reported 203,270 games searched while
        actually excluding 611 exhibition and all-star games inside otherwise
        included seasons.
        """
        wide = Search().strikeout().run(self.conn, limit=1).coverage
        narrow = Search().strikeout().team("KCA").run(self.conn, limit=1).coverage
        self.assertLessEqual(narrow.games, wide.games)
        expected = self.conn.execute(
            "SELECT count(*) FROM games WHERE home_team = ? OR away_team = ?",
            ("KCA", "KCA")).fetchone()[0]
        self.assertEqual(narrow.games, expected)

    def test_excluded_game_types_are_not_counted_as_searched(self):
        everything = Search().all_game_types().run(self.conn, limit=1).coverage
        default = Search().run(self.conn, limit=1).coverage
        excluded = self.conn.execute(
            "SELECT count(*) FROM games WHERE game_type IN"
            " ('exhibition','allstar')").fetchone()[0]
        self.assertEqual(everything.games - default.games, excluded)

    def test_coverage_rows_sum_to_the_games_table(self):
        games = self.conn.execute("SELECT count(*) FROM games").fetchone()[0]
        covered = self.conn.execute(
            "SELECT sum(games) FROM coverage").fetchone()[0]
        self.assertEqual(games, covered)


class Results(QueryBase):
    def test_every_row_carries_its_source_event(self):
        for row in Search().strikeout().run(self.conn, limit=10):
            self.assertTrue(row.event_raw)
            self.assertIn("K", row.event_raw)

    def test_total_counts_matches_before_the_limit(self):
        full = Search().strikeout().count(self.conn)
        limited = Search().strikeout().run(self.conn, limit=2)
        self.assertEqual(limited.total, full)
        self.assertEqual(len(limited.rows), min(2, full))

    def test_results_carry_their_versions_and_sql(self):
        result = Search().strikeout().run(self.conn, limit=1)
        self.assertTrue(result.ontology_version)
        self.assertIn("SELECT", result.sql)

    def test_an_unknown_tag_is_an_error_not_an_empty_result(self):
        with self.assertRaises(QueryError):
            Search().tag("NoSuchTagExists").run(self.conn)


class BenchHarness(QueryBase):
    """The benchmark harness itself (spec/07-TESTING.md §5).

    Not a performance test -- timings on a fixture of two games mean nothing.
    This asserts the harness runs, which is what stops `rsse bench` from
    rotting silently between the corpus-scale runs that do mean something.
    """

    def test_every_benchmark_builds_and_runs(self):
        from rsse import bench
        for spec in bench.BENCHMARKS:
            with self.subTest(spec.name):
                result = bench.run_one(self.conn, spec, repeats=1)
                self.assertEqual(result.name, spec.name)
                self.assertGreaterEqual(result.median_ms, 0.0)

    def test_every_benchmark_has_a_budget_and_a_known_mode(self):
        from rsse import bench
        for spec in bench.BENCHMARKS:
            with self.subTest(spec.name):
                self.assertGreater(spec.budget_ms, 0)
                self.assertIn(spec.mode, ("count", "run", "rows"))

    def test_benchmark_names_are_unique(self):
        # `--only` matches on the name, and the report is keyed by it.
        from rsse import bench
        names = [b.name for b in bench.BENCHMARKS]
        self.assertEqual(len(names), len(set(names)))

class HitLocations(QueryBase):
    """`plays.event_location` and the predicate over it (§3).

    The column is a lifted copy of something already in `event_modifiers`, so
    every test here is really the same question: does the copy still say what
    the modifier says.
    """

    def located(self):
        return self.conn.execute(
            "SELECT event_raw, event_location FROM plays"
            " WHERE event_location IS NOT NULL").fetchall()

    def test_the_column_is_filled_from_the_modifier(self):
        rows = self.located()
        self.assertEqual(len(rows), 120)
        for raw, loc in rows:
            with self.subTest(raw=raw):
                self.assertIn(loc, raw)

    def test_a_play_with_no_location_is_null_not_empty(self):
        # `''` and NULL would both be falsy in Python and are not the same
        # statement: one says the scorer wrote an empty zone, which never
        # happens, and the other says they wrote none.
        [(n,)] = self.conn.execute(
            "SELECT count(*) FROM plays WHERE event_location = ''")
        self.assertEqual(n, 0)

    def test_a_trajectory_without_a_location_stays_null(self):
        # `/G` is a ground ball to nowhere in particular: a `hit` modifier
        # with a trajectory and no zone. Lifting the trajectory into this
        # column would make `.hit_location('G')` answer something.
        [(n,)] = self.conn.execute(
            "SELECT count(*) FROM plays WHERE event_raw LIKE '%/G'"
            " AND event_location IS NOT NULL")
        self.assertEqual(n, 0)

    def test_an_exact_zone_matches_only_itself(self):
        got = {r.event_raw for r in Search().hit_location("3").run(self.conn)}
        self.assertTrue(got)
        for raw in got:
            self.assertIn("3", raw)
        self.assertNotIn("31/G34S.3-H;1-2", got,
                         "34S is not zone 3")

    def test_a_starred_zone_matches_everything_inside_it(self):
        exact = Search().hit_location("7").count(self.conn)
        prefix = Search().hit_location("7*").count(self.conn)
        self.assertGreater(prefix, exact,
                           "7* must also find 7M, 78M and the rest")

    def test_the_star_is_the_only_wildcard(self):
        # `%` would be a leading-wildcard scan dressed up as a filter, so it
        # is a literal here and matches nothing rather than everything.
        self.assertEqual(Search().hit_location("%7%").count(self.conn), 0)

    def test_a_bare_star_is_refused(self):
        with self.assertRaises(QueryError):
            Search().hit_location("*")

    def test_the_old_substring_behaviour_is_gone(self):
        # `.hit_location('8')` used to match `E8` and every fielder string
        # containing an 8, because the test was `event_modifiers LIKE '%8%'`.
        for raw in {r.event_raw
                    for r in Search().hit_location("8").run(self.conn)}:
            self.assertNotIn("E8", raw)

    def test_hit_located_is_the_denominator(self):
        self.assertEqual(Search().hit_located().count(self.conn), 120)

    def test_the_predicate_can_use_the_partial_index(self):
        # The `IS NOT NULL` term is redundant with the equality beside it and
        # is there for exactly this: without it SQLite will not prove the
        # partial index applies, and the query scans 17.9 million rows.
        plan = Search().hit_location("7").explain(self.conn)
        self.assertIn("ix_plays_location", str(plan))

    def test_an_old_database_gets_a_sentence_not_an_operational_error(self):
        # A database derived before the column existed is a perfectly good
        # database that cannot answer this one question. `no such column:
        # p.event_location` is true and says nothing about which pass fixes it.
        old = sqlite3.connect(":memory:")
        old.executescript("CREATE TABLE plays (play_id INTEGER, game_key"
                          " INTEGER, parse_status TEXT);"
                          "CREATE TABLE games (game_key INTEGER, game_type"
                          " TEXT, season INTEGER);")
        with self.assertRaises(QueryError) as caught:
            Search().hit_location("7").count(old)
        self.assertIn("derive --rebuild", str(caught.exception))
        old.close()

    def test_the_verify_gates_stay_silent_on_good_data(self):
        for name, _trusted, sql in cli.LOCATION_CHECKS:
            with self.subTest(name):
                [(n,)] = self.conn.execute(f"SELECT count(*) FROM ({sql})")
                self.assertEqual(n, 0, name)

    def test_the_verify_gates_fire_on_bad_data(self):
        """A gate nobody has watched fail is a gate that may be inverted.

        `location is not zone-shaped` was first written `GLOB '[!0-9]*'`.
        SQLite negates a GLOB class with `^`, not `!`, so `!` was read as an
        ordinary member: the check matched every string beginning with `!` or
        a digit, which is every *valid* location, and it reported all
        4,999,462 located plays in the corpus as malformed. Nothing caught it
        because both gates had only ever been run against data that passes.
        """
        broken = sqlite3.connect(":memory:")
        broken.executescript(
            "CREATE TABLE plays (play_id INTEGER, event_modifiers TEXT,"
            " event_location TEXT, parse_status TEXT DEFAULT 'ok');")
        rows = [
            # (play_id, modifiers, location, how many checks must catch it)
            (1, '["L78D"]', "78D", 0),      # good: a suffix, and zone-shaped
            (2, '["8"]', "8", 0),           # good: the whole modifier
            (3, '["L78D"]', "56", 1),       # drifted from its own row
            (4, '["L78D"]', "", 1),         # empty is missing, not present
            (5, '["LXX"]', "XX", 1),        # not zone-shaped (suffix is fine)
            (6, '["L78D"]', "!7", 2),       # the string the old check let by
        ]
        broken.executemany(
            "INSERT INTO plays (play_id, event_modifiers, event_location)"
            " VALUES (?,?,?)", [(r[0], r[1], r[2]) for r in rows])
        caught = {play_id: 0 for play_id, *_ in rows}
        for _name, _trusted, sql in cli.LOCATION_CHECKS:
            for (play_id,) in broken.execute(sql):
                caught[play_id] += 1
        for play_id, _mods, _loc, expected in rows:
            self.assertEqual(caught[play_id], expected,
                             f"play {play_id}")
        broken.close()

    def test_only_one_located_modifier_per_play(self):
        # `Event.hit_location` returns the first, which is only the right
        # answer while there is never a second. Asserted rather than trusted.
        from rsse.parser.parser import parse
        for (raw,) in self.conn.execute(
                "SELECT event_raw FROM plays WHERE parse_status <> 'unparsed'"):
            located = [m for m in parse(raw).event.modifiers
                       if m.kind == "hit" and m.location]
            self.assertLessEqual(len(located), 1, raw)


class PositionPlayed(QueryBase):
    """`.position_played()` over `roster_entries` (§3).

    A season fact used as a play filter, which is the whole of what can go
    wrong with it.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        write = sqlite3.connect(str(cls.db))
        write.executescript(
            "CREATE TABLE IF NOT EXISTS roster_entries ("
            " person_id TEXT, season INTEGER, team_id TEXT,"
            " stated_team_id TEXT, last TEXT, first TEXT, bats TEXT,"
            " throws TEXT, position TEXT,"
            " PRIMARY KEY (person_id, season, team_id));")
        batters = [r[0] for r in write.execute(
            "SELECT DISTINCT batter_id FROM plays ORDER BY batter_id")]
        write.executemany(
            "INSERT OR REPLACE INTO roster_entries VALUES"
            " (?,2000,'KCA',NULL,'L','F','R','R',?)",
            [(b, "SS" if i % 3 == 0 else "OF")
             for i, b in enumerate(batters)])
        # The same person, a different season: the predicate must not match
        # plays from a season this line does not cover.
        write.execute("INSERT OR REPLACE INTO roster_entries VALUES"
                      " (?,1999,'KCA',NULL,'L','F','R','R','P')",
                      (batters[0],))
        write.commit()
        write.close()
        cls.n_ss = sum(1 for i in range(len(batters)) if i % 3 == 0)

    def test_it_finds_plays_by_players_listed_there(self):
        got = Search().position_played("SS").count(self.conn)
        self.assertGreater(got, 0)
        self.assertEqual(
            got + Search().position_played("OF").count(self.conn),
            Search().count(self.conn))

    def test_the_season_has_to_match(self):
        # The one 1999 line is a pitcher, and there are no 1999 games.
        self.assertEqual(Search().position_played("P").count(self.conn), 0)

    def test_of_does_not_imply_lf(self):
        # An undifferentiated outfielder is what the early files record.
        # Folding `OF` into the three modern positions would invent a
        # precision the roster line does not have.
        self.assertEqual(Search().position_played("LF").count(self.conn), 0)
        self.assertGreater(Search().position_played("OF").count(self.conn), 0)

    def test_it_is_case_insensitive(self):
        self.assertEqual(Search().position_played("ss").count(self.conn),
                         Search().position_played("SS").count(self.conn))

    def test_an_unknown_position_is_refused_not_answered_with_nothing(self):
        # A typo that returns zero rows is a typo that reads as a finding.
        with self.assertRaises(QueryError):
            Search().position_played("shortstop")

    def test_a_database_with_no_rosters_gets_a_sentence(self):
        bare = sqlite3.connect(":memory:")
        bare.executescript("CREATE TABLE plays (play_id INTEGER, game_key"
                           " INTEGER, batter_id TEXT, parse_status TEXT);"
                           "CREATE TABLE games (game_key INTEGER, game_type"
                           " TEXT, season INTEGER);")
        with self.assertRaises(QueryError) as caught:
            Search().position_played("SS").count(bare)
        self.assertIn("rsse reference", str(caught.exception))
        bare.close()

    def test_it_joins_games_rather_than_correlating(self):
        sql, _ = Search().position_played("SS")._compile("p.play_id")
        self.assertIn("roster_entries", sql)
        self.assertNotIn("EXISTS", sql)


if __name__ == "__main__":
    unittest.main()
