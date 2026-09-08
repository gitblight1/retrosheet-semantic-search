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

    def test_a_tag_inside_any_of_compiles_to_exists(self):
        # A JOIN cannot express OR, so the same predicate must have a second
        # form -- this is the reason `Pred` carries both.
        sql, _ = Search().any_of(Search().strikeout(),
                                 Search().tag("Walk"))._compile("p.play_id")
        self.assertIn("EXISTS (SELECT 1 FROM play_tags", sql)

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

    def test_predicates_needing_unbuilt_tables_refuse(self):
        # Silently matching nothing would read as "it never happened".
        with self.assertRaises(NotImplementedError):
            Search().pitcher("smith001")
        with self.assertRaises(NotImplementedError):
            Search().fielder(6, "smith001")


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


if __name__ == "__main__":
    unittest.main()
