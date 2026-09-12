"""Game logs: parsing, layout validation, reconciliation (spec/07-TESTING §4).

The game logs are the project's first *external* check: `games.final_home` is
produced by replaying 17.9 million plays, and the log's score was compiled by
Retrosheet from box scores, so nothing in the pipeline can make both wrong the
same way.

That is also why the tests here are careful about what they prove. A fixture
written from `FIELDS` agrees with `FIELDS` however wrong it is, so these tests
cover the *plumbing* and the *layout guard*; the offsets themselves are checked
against real data by `check_layout`, and the test below proves that guard fires
by shifting a field deliberately.
"""

import unittest

from rsse.model.gamelog import (EXPECTED_FIELDS, FIELDS, GameLogError,
                                check_layout, parse_line)


def make_line(**overrides) -> str:
    """A game log line with each named field set and the rest blank.

    Built from `FIELDS` so a change to the map is reflected here rather than
    silently diverging -- which is exactly why this cannot validate the map.
    """
    fields = [""] * EXPECTED_FIELDS
    values = {
        "date": "20000407", "game_number": "0", "day_of_week": "Fri",
        "away_team": "MIN", "away_league": "AL",
        "home_team": "KCA", "home_league": "AL",
        "away_score": "3", "home_score": "7", "outs": "54",
        "park_id": "KAN06", "attendance": "12345", "duration_minutes": "180",
        "away_er_individual": "6", "away_er_team": "6",
        "home_er_individual": "2", "home_er_team": "2",
    }
    values.update(overrides)
    for name, value in values.items():
        fields[FIELDS[name]] = str(value)
    # Managers and umpires are quoted free text in the real files; one is
    # included so the csv path is exercised rather than str.split.
    fields[91] = '"Wright, Harry"'
    return ",".join(fields)


class Parsing(unittest.TestCase):
    def test_a_well_formed_line(self):
        gl = parse_line(make_line())
        self.assertEqual(gl.date, "2000-04-07")
        self.assertEqual(gl.home_team, "KCA")
        self.assertEqual(gl.away_team, "MIN")
        self.assertEqual(gl.away_score, 3)
        self.assertEqual(gl.home_score, 7)
        self.assertEqual(gl.outs, 54)

    def test_the_game_id_matches_the_event_file_convention(self):
        self.assertEqual(parse_line(make_line()).retrosheet_game_id,
                         "KCA200004070")

    def test_lettered_doubleheaders_map_to_numbers(self):
        # Separate-admission doubleheaders are A/B in the logs and 1/2 in the
        # event files; passing the letter through would match no game.
        self.assertEqual(
            parse_line(make_line(game_number="A")).retrosheet_game_id,
            "KCA200004071")
        self.assertEqual(
            parse_line(make_line(game_number="B")).retrosheet_game_id,
            "KCA200004072")

    def test_a_comma_in_a_quoted_name_does_not_shift_the_fields(self):
        gl = parse_line(make_line())
        self.assertEqual(gl.home_score, 7, "csv parsing, not str.split")

    def test_a_short_line_is_refused_not_parsed(self):
        # A short line silently shifts every field after the gap, so a
        # best-effort parse would produce plausible wrong numbers.
        with self.assertRaises(GameLogError):
            parse_line(",".join([""] * 100))

    def test_a_missing_score_is_refused(self):
        with self.assertRaises(GameLogError):
            parse_line(make_line(home_score=""))

    def test_forfeits_and_suspended_games_are_marked_unfair_tests(self):
        self.assertTrue(parse_line(make_line()).is_complete)
        forfeit = make_line()
        parts = forfeit.split(",")
        parts[FIELDS["forfeit"]] = "H"
        self.assertFalse(parse_line(",".join(parts)).is_complete)


class LayoutGuard(unittest.TestCase):
    """The guard that makes the offsets checkable at all.

    A fixture cannot validate a positional field map. What can be validated is
    that a *shift* in the map is detected -- which is what this asserts.
    """

    def test_clean_rows_pass_every_check(self):
        rows = [parse_line(make_line()) for _ in range(50)]
        for finding in check_layout(rows):
            self.assertTrue(finding.ok, finding.description)

    def test_a_shifted_field_is_caught(self):
        # The real failure mode: every field one position to the right, so a
        # league code lands where a score belongs. Re-serialised through csv
        # so the quoted name stays one field rather than becoming two.
        import csv
        import io

        def shift(line):
            fields = next(csv.reader(io.StringIO(line)))
            fields.insert(0, "")
            buf = io.StringIO()
            csv.writer(buf, lineterminator="").writerow(
                fields[:EXPECTED_FIELDS])
            return buf.getvalue()

        shifted = []
        for _ in range(50):
            try:
                shifted.append(parse_line(shift(make_line())))
            except GameLogError:
                # Refusing outright is an even better outcome than failing a
                # shape check, and is what a shift into a non-numeric score
                # produces.
                return
        findings = check_layout(shifted)
        self.assertTrue(any(not f.ok for f in findings),
                        "a one-field shift must fail at least one shape check")

    def test_implausible_scores_are_caught(self):
        rows = [parse_line(make_line(home_score="900")) for _ in range(50)]
        failed = [f for f in check_layout(rows) if not f.ok]
        self.assertTrue(any("plausible run totals" in f.description
                            for f in failed))

    def test_out_counts_that_are_not_multiples_of_three_are_caught(self):
        # An away win with a partial final half-inning cannot happen: the home
        # team always finishes its half unless it has already won.
        rows = [parse_line(make_line(outs="53", away_score="9",
                                     home_score="1")) for _ in range(50)]
        failed = [f for f in check_layout(rows) if not f.ok]
        self.assertTrue(any("multiple of three" in f.description
                            for f in failed))

    def test_a_walk_off_is_not_a_layout_error(self):
        """7.7% of real games have a partial final half-inning.

        The first version of this check did not know that and flagged every
        walk-off in the corpus -- 16,838 games -- as evidence the field
        offsets were wrong. A guard that fires on correct data is worse than
        no guard, because the next real failure gets waved through.
        """
        rows = [parse_line(make_line(outs="52", away_score="3",
                                     home_score="4")) for _ in range(200)]
        findings = {f.description: f for f in check_layout(rows)}
        key = "out counts are a multiple of three, unless the home team won"
        self.assertTrue(findings[key].ok)

    def test_tolerance_admits_a_few_genuine_oddities(self):
        # Called and forfeited games really do have odd out counts with the
        # away team ahead; one bad row in two hundred must not read as a
        # layout error.
        rows = ([parse_line(make_line(outs="53", away_score="9",
                                      home_score="1"))]
                + [parse_line(make_line()) for _ in range(399)])
        findings = {f.description: f for f in check_layout(rows)}
        key = "out counts are a multiple of three, unless the home team won"
        self.assertTrue(findings[key].ok)


class Reconcile(unittest.TestCase):
    """Loading and reconciliation, end to end, on temporary databases.

    Never against the real archive: the archive is the project's source of
    truth and a test must not put fabricated rows in it.
    """

    def setUp(self):
        import sqlite3
        import tempfile
        from pathlib import Path

        self.tmp = Path(tempfile.mkdtemp())
        self.archive = sqlite3.connect(str(self.tmp / "archive.db"))
        self.query = sqlite3.connect(str(self.tmp / "rsse.db"))
        self.query.executescript(
            "CREATE TABLE games (game_key INTEGER PRIMARY KEY, game_id TEXT,"
            " season INTEGER, final_home INTEGER, final_away INTEGER);")

    def tearDown(self):
        import shutil
        self.archive.close()
        self.query.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_game(self, key, game_id, away, home, season=2000):
        self.query.execute(
            "INSERT INTO games (game_key, game_id, season, final_away,"
            " final_home) VALUES (?,?,?,?,?)",
            (key, game_id, season, away, home))
        self.query.commit()

    def write_logs(self, lines):
        path = self.tmp / "GL2000.TXT"
        path.write_text("\n".join(lines) + "\n", encoding="latin-1")
        from rsse.database import gamelogs
        return gamelogs.load(self.archive, [path])

    def run_reconcile(self):
        from rsse.database import gamelogs
        return gamelogs.reconcile(self.query, self.archive)

    def test_matching_scores_agree(self):
        self.add_game(1, "KCA200004070", away=3, home=7)
        self.write_logs([make_line(away_score="3", home_score="7")])
        result = self.run_reconcile()
        self.assertEqual(result.compared, 1)
        self.assertEqual(result.agreed, 1)
        self.assertEqual(result.mismatches, [])

    def test_a_disagreement_is_reported_with_both_scores(self):
        # The check has to fire, or it is decoration. This is the whole point
        # of holding the game logs at all.
        self.add_game(1, "KCA200004070", away=3, home=7)
        self.write_logs([make_line(away_score="3", home_score="8")])
        result = self.run_reconcile()
        self.assertEqual(result.agreed, 0)
        self.assertEqual(result.mismatches,
                         [("KCA200004070", 3, 7, 3, 8)])

    def test_games_in_one_source_only_are_coverage_not_error(self):
        # Retrosheet's logs are Major League games; the corpus includes the
        # Negro Leagues. A game in one and not the other must not read as a
        # failed reconciliation.
        self.add_game(1, "KCA200004070", away=3, home=7)
        self.add_game(2, "NGL192507040", away=1, home=2)
        self.write_logs([make_line(away_score="3", home_score="7"),
                         make_line(home_team="BOS", away_score="1",
                                   home_score="0")])
        result = self.run_reconcile()
        self.assertEqual(result.compared, 1)
        self.assertEqual(result.agreed, 1)
        self.assertEqual(result.replay_only, 1, "NGL game absent from the logs")
        self.assertEqual(result.log_only, 1, "BOS game absent from the replay")
        self.assertEqual(result.mismatches, [])

    def test_log_only_games_are_broken_down_by_cause(self):
        """One lumped number reads as a defect; the parts do not.

        Of 34,393 games the logs have and the corpus does not, 29,133 predate
        the corpus and 1,992 are postseason or all-star games the event files
        never contained. Only the remainder is a coverage gap.
        """
        self.add_game(1, "KCA200004070", away=3, home=7, season=2000)
        self.write_logs([
            make_line(away_score="3", home_score="7"),                  # match
            make_line(date="19000501", home_team="BSN"),                # early
            make_line(date="20001015", home_team="NYN"),                # October
            make_line(date="20000711", home_team="NLS"),                # all-star
            make_line(date="20000612", home_team="CHN"),                # real gap
        ])
        r = self.run_reconcile()
        self.assertEqual(r.compared, 1)
        self.assertEqual(r.log_only_before_corpus, 1)
        self.assertEqual(r.log_only_postseason, 1)
        self.assertEqual(r.log_only_allstar, 1)
        self.assertEqual(r.log_only_gap, 1)
        self.assertEqual(r.log_only, 4)

    def test_a_skipped_log_row_is_not_reported_as_absent(self):
        # A forfeited game the corpus *does* hold is not "in the replay only":
        # the log had something to say about it, it just was not a fair test.
        self.add_game(1, "KCA200004070", away=3, home=7)
        fields = make_line().split(",")
        fields[FIELDS["forfeit"]] = "H"
        self.write_logs([",".join(fields)])
        r = self.run_reconcile()
        self.assertEqual(r.replay_only, 0)
        self.assertEqual(r.replay_only_skipped, 1)

    def test_forfeits_are_excluded_rather_than_counted_wrong(self):
        # A forfeit's score is awarded by rule, not scored on the field, so it
        # is not a fair test of the replay.
        self.add_game(1, "KCA200004070", away=3, home=7)
        fields = make_line(away_score="9", home_score="0").split(",")
        fields[FIELDS["forfeit"]] = "V"
        stats = self.write_logs([",".join(fields)])
        self.assertEqual(stats.loaded, 1, "the row is still stored verbatim")
        result = self.run_reconcile()
        self.assertEqual(result.skipped_forfeit, 1)
        self.assertEqual(result.compared, 0)
        self.assertEqual(result.mismatches, [])

    def test_the_raw_line_is_stored_verbatim(self):
        line = make_line()
        self.write_logs([line])
        stored = self.archive.execute("SELECT raw FROM game_logs").fetchone()[0]
        self.assertEqual(stored, line,
                         "the published line must survive the load unchanged")

    def test_loading_the_same_file_twice_works(self):
        """Reloading must be safe -- the first version was not.

        `INSERT OR REPLACE` on the file row assigned a new `gl_file_id` and
        orphaned every `game_logs` row pointing at the old one, so the first
        load succeeded and the second died on a foreign key. A loader that
        only works once is a loader nobody can correct data with.
        """
        self.write_logs([make_line()])
        stats = self.write_logs([make_line(home_score="9")])
        self.assertEqual(stats.loaded, 1)
        rows = self.archive.execute(
            "SELECT home_score FROM game_logs").fetchall()
        self.assertEqual(rows, [(9,)], "the reload must replace, not duplicate")
        files = self.archive.execute(
            "SELECT count(*) FROM game_log_files").fetchone()[0]
        self.assertEqual(files, 1)

    def test_a_malformed_line_is_recorded_not_dropped(self):
        stats = self.write_logs([make_line(), ",".join([""] * 12)])
        self.assertEqual(stats.loaded, 1)
        self.assertEqual(len(stats.malformed), 1)


if __name__ == "__main__":
    unittest.main()
