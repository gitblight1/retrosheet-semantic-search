"""Reference data: people, rosters, teams and parks.

The readers (`rsse/model/reference.py`) and the tables built from them
(`rsse/database/reference.py`). Two themes run through these: a positional
format must be checked against something other than itself, and a blank in
the source is a fact to preserve rather than a hole to fill.
"""

import unittest

from rsse.database import reference as refdb, schema as dbschema
from rsse.model import reference as ref


class RosterLines(unittest.TestCase):
    def test_a_normal_line(self):
        e = ref.parse_roster_line("beckr001,Beck,Rod,R,R,BOS,P")
        self.assertEqual((e.player_id, e.bats, e.throws, e.team_id, e.position),
                         ("beckr001", "R", "R", "BOS", "P"))

    def test_blanks_become_none_rather_than_a_default(self):
        # 7 roster lines have no `bats`, 3 no `throws`, 5,661 no position.
        # Filling those in with the common value would invent 5,671 facts.
        e = ref.parse_roster_line("willr106,Williams,Roy K.,?,R,NY5,")
        self.assertIsNone(e.position)
        self.assertEqual(e.bats, "?")

    def test_a_switch_thrower_is_accepted(self):
        # `B` in `throws` appears 9 times in 121,600 lines. Rare enough to
        # look like a defect to anyone who had not counted, and real.
        self.assertEqual(ref.parse_roster_line("x0000001,A,B,R,B,BOS,P").throws,
                         "B")

    def test_a_short_line_is_refused(self):
        with self.assertRaises(ref.LayoutError):
            ref.parse_roster_line("beckr001,Beck,Rod,R,R,BOS")

    def test_a_shifted_line_is_caught_by_the_value_domains(self):
        # The failure a positional format actually has: a dropped field moves
        # every field after it, and the row still has plausible-looking text
        # in it. The field count catches most of that; the domains catch the
        # rest, which is why they are checked and not merely documented.
        with self.assertRaises(ref.LayoutError):
            ref.parse_roster_line("beckr001,Beck,Rod,BOS,P,R,R")

    def test_a_short_player_id_is_refused(self):
        with self.assertRaises(ref.LayoutError):
            ref.parse_roster_line("beck,Beck,Rod,R,R,BOS,P")


class TeamLines(unittest.TestCase):
    def test_the_league_is_kept_verbatim(self):
        # Retrosheet writes `A` in 88 seasons and `AL` in 1920-1949. Both go
        # in unchanged: it is their notation to reconcile, not this project's.
        self.assertEqual(ref.parse_team_line("BOS,A,Boston,Red Sox").league, "A")
        self.assertEqual(ref.parse_team_line("BOS,AL,Boston,Red Sox").league, "AL")

    def test_no_league_is_none_and_not_an_error(self):
        # 344 of 3,493 rows. Barnstorming clubs had no affiliation, so this is
        # a fact about the team rather than a missing value.
        self.assertIsNone(ref.parse_team_line("PRG,,Pittsburgh,Crawfords").league)


class HeadedFiles(unittest.TestCase):
    PARKS = ['PARKID,NAME,AKA,CITY,STATE,START,END,LEAGUE,NOTES',
             'ACY01,Inlet Park,,Atlantic City,NJ,,,,',
             'ALB01,Riverside Park,,Albany,NY,09/11/1880,05/30/1882,NL,'
             '"TRN:9/11/80;6/15&9/10/1881"']

    def test_a_quoted_field_containing_commas_stays_one_field(self):
        # `str.split(",")` turns the last row into two parks and shifts
        # nothing else, which is the hardest kind of corruption to notice
        # because the row count stays plausible.
        parks = ref.read_parks(self.PARKS)
        self.assertEqual(len(parks), 2)
        self.assertIn(";", parks[1].notes)

    def test_a_changed_header_is_refused(self):
        bad = ['PARKID,NAME,AKA,CITY,STATE,OPENED,END,LEAGUE,NOTES',
               'ACY01,Inlet Park,,Atlantic City,NJ,,,,']
        with self.assertRaises(ref.LayoutError):
            ref.read_parks(bad)

    def test_a_missing_bio_column_is_refused(self):
        header = ",".join(ref.BIO_HEADER_HEAD) + "," + ",".join(
            f"X{i}" for i in range(ref.BIO_COLUMNS - len(ref.BIO_HEADER_HEAD) - 1))
        with self.assertRaises(ref.LayoutError):
            ref.read_people([header, "a,b,c"])


class ParkDates(unittest.TestCase):
    def test_unpadded_dates_normalise(self):
        # The file writes `4/20/1912`, not `04/20/1912`. Slicing that with
        # SQL `substr` produced a check that fired on 106,537 correct games.
        self.assertEqual(refdb._iso("4/20/1912"), "1912-04-20")
        self.assertEqual(refdb._iso("12/27/1981"), "1981-12-27")

    def test_a_blank_or_unparseable_date_is_none(self):
        self.assertIsNone(refdb._iso(""))
        self.assertIsNone(refdb._iso(None))
        self.assertIsNone(refdb._iso("unknown"))


class BuiltTables(unittest.TestCase):
    """Building against a small corpus that exercises each decision."""

    ROSTER = "beckr001,Beck,Rod,R,R,BOS,P"
    #: A line whose team field disagrees with the file holding it. 85 real
    #: ones exist, and one of them collides with a row in the other team's
    #: file -- which is why the key is the file's team, not the field's.
    STRAY = "willr106,Williams,Roy K.,?,R,NY5,"

    def build(self):
        archive = dbschema.connect(":memory:")
        archive.executescript(dbschema.ARCHIVE_DDL)
        archive.execute("INSERT INTO corpus (ingested_at, rsse_version,"
                        " parser_version) VALUES ('now','t','t')")
        for kind, path, season, lines in (
                ("roster", "/x/BOS1998.ROS", 1998, [self.ROSTER]),
                ("roster", "/x/PH51933.ROS", 1933, [self.STRAY]),
                ("team", "/x/TEAM1998", 1998, ["BOS,A,Boston,Red Sox"]),
                ("park", "/x/ballparks.csv", None, HeadedFiles.PARKS),
                ("bio", "/x/biofile.csv", None, [
                    ",".join(ref.BIO_HEADER_HEAD) + "," + ",".join(
                        f"X{i}" for i in range(
                            ref.BIO_COLUMNS - len(ref.BIO_HEADER_HEAD))),
                    "beckr001,Beck,Rod,," + ",".join([""] * 29)]),
        ):
            cur = archive.execute(
                "INSERT INTO aux_files (corpus_id, path, kind, season, sha256,"
                " byte_length, mtime, line_ending, record_count)"
                " VALUES (1,?,?,?,'d',0,'t','crlf',?)",
                (path, kind, season, len(lines)))
            archive.executemany(
                "INSERT INTO aux_records (aux_file_id, line_no, raw_line)"
                " VALUES (?,?,?)",
                [(cur.lastrowid, n, l) for n, l in enumerate(lines, 1)])
        archive.commit()

        query = dbschema.connect(":memory:")
        query.executescript(dbschema.DERIVED_DDL)
        query.executescript(
            "CREATE TABLE lineup_entries (game_key INT, seq INT, is_sub INT,"
            " play_id INT, player_id TEXT, player_name TEXT, team INT,"
            " batting_order INT, position INT);")
        query.execute("INSERT INTO games (game_key, game_id, season, site,"
                      " home_team, away_team) VALUES"
                      " (1,'G',1998,'ACY01','BOS','NYA')")
        query.execute(
            "INSERT INTO plays (play_id, game_key, game_id, seq, inning, half,"
            " batting_team, batter_id, event_raw, event_basic, event_modifiers,"
            " event_advances, annotations, parser_version)"
            " VALUES (1,1,'G',1,1,'top',0,'ghost001','K','K','','','','t')")
        query.executescript(
            "INSERT INTO lineup_entries VALUES (1,1,0,NULL,'beckr001','B',0,1,1);")
        query.commit()
        return query, refdb.build(query, archive)

    def test_the_roster_key_is_the_file_not_the_field(self):
        query, _stats = self.build()
        row = query.execute(
            "SELECT team_id, stated_team_id FROM roster_entries"
            " WHERE person_id = 'willr106'").fetchone()
        self.assertEqual(row, ("PH5", "NY5"))

    def test_an_agreeing_line_records_no_conflict(self):
        query, stats = self.build()
        self.assertEqual(stats.team_field_conflicts, 1)
        self.assertIsNone(query.execute(
            "SELECT stated_team_id FROM roster_entries"
            " WHERE person_id = 'beckr001'").fetchone()[0])

    def test_an_observed_person_gets_a_row_with_null_attributes(self):
        # `ghost001` bats in the corpus and appears in no reference file. The
        # row exists so the join resolves; every attribute is NULL so the row
        # does not pretend to know anything.
        query, stats = self.build()
        row = query.execute(
            "SELECT last, birthdate, source FROM people"
            " WHERE person_id = 'ghost001'").fetchone()
        self.assertEqual(row, (None, None, "observed"))
        self.assertEqual(stats.people_observed_only, 1)

    def test_every_observed_id_resolves(self):
        query, _stats = self.build()
        for sql in (
            "SELECT count(*) FROM (SELECT DISTINCT batter_id FROM plays) b"
            " LEFT JOIN people p ON p.person_id = b.batter_id"
            " WHERE p.person_id IS NULL",
            "SELECT count(*) FROM (SELECT DISTINCT site FROM games) g"
            " LEFT JOIN parks p ON p.park_id = g.site"
            " WHERE p.park_id IS NULL",
        ):
            self.assertEqual(query.execute(sql).fetchone()[0], 0, sql)

    def test_the_league_reaches_the_table_unchanged(self):
        query, _stats = self.build()
        self.assertEqual(query.execute(
            "SELECT league FROM teams WHERE team_id='BOS'").fetchone()[0], "A")
