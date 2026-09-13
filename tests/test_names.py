"""Resolving names to ids in the query API (rsse/query/names.py, §8).

Two facts drive every case here, and both were measured rather than assumed:
the name a person is known by is usually not `people.first`, and names are not
unique.
"""

import unittest

from rsse.database import schema as dbschema
from rsse.query import names
from rsse.query.search import QueryError, Search


def database():
    """A query database with just enough reference data to resolve against."""
    conn = dbschema.connect(":memory:")
    conn.executescript(dbschema.DERIVED_DDL)
    conn.executescript("""
      CREATE TABLE people (person_id TEXT PRIMARY KEY, last TEXT, first TEXT,
        nickname TEXT, birthdate TEXT, birth_city TEXT, birth_state TEXT,
        birth_country TEXT, play_debut TEXT, play_last TEXT, mgr_debut TEXT,
        mgr_last TEXT, coach_debut TEXT, coach_last TEXT, ump_debut TEXT,
        ump_last TEXT, deathdate TEXT, source TEXT);
      CREATE TABLE roster_entries (person_id TEXT, season INT, team_id TEXT,
        stated_team_id TEXT, last TEXT, first TEXT, bats TEXT, throws TEXT,
        position TEXT);
      CREATE TABLE teams (team_id TEXT, season INT, league TEXT, city TEXT,
        nickname TEXT);
      CREATE TABLE parks (park_id TEXT PRIMARY KEY, name TEXT, aka TEXT,
        city TEXT, state TEXT, start_date TEXT, end_date TEXT, start_iso TEXT,
        end_iso TEXT, league TEXT, notes TEXT, source TEXT);
    """)
    conn.executescript("""
      -- Ruth is the case the whole design turns on: the biography says
      -- 'George Herman' and nobody has ever searched for that.
      INSERT INTO people (person_id, last, first, nickname, birthdate,
                          play_debut, source)
        VALUES ('ruthb101','Ruth','George Herman','Babe','02/06/1895',
                '07/11/1914','bio'),
               ('robij101','Robinson','John Edward','Jack','01/31/1919',
                '04/15/1947','bio'),
               ('robij102','Robinson','Jack','Jack','1880','1902','bio'),
               ('ghost001',NULL,NULL,NULL,NULL,NULL,'observed');
      INSERT INTO roster_entries (person_id, season, team_id, last, first)
        VALUES ('ruthb101',1927,'NYA','Ruth','Babe'),
               ('robij101',1947,'BRO','Robinson','Jackie');
      INSERT INTO parks (park_id, name, city, start_iso, end_iso, source)
        VALUES ('CHI11','Wrigley Field','Chicago','1914-04-23',NULL,'file'),
               ('LOS02','Wrigley Field','Los Angeles','1961-04-27',
                '1961-10-01','file'),
               ('BOS07','Fenway Park','Boston','1912-04-20',NULL,'file');
      INSERT INTO teams (team_id, season, league, city, nickname)
        VALUES ('BRO',1947,'NL','Brooklyn','Dodgers'),
               ('LAN',1958,'NL','Los Angeles','Dodgers'),
               ('NYA',1927,'AL','New York','Yankees');
    """)
    conn.commit()
    return conn


class PlayingNames(unittest.TestCase):
    """The name people use is in `nickname` and on the roster, not `first`."""

    def setUp(self):
        self.conn = database()

    def test_the_nickname_finds_the_person(self):
        found = names.people_named(self.conn, "Babe Ruth")
        self.assertEqual([c.id for c in found], ["ruthb101"])

    def test_the_legal_name_also_finds_them(self):
        found = names.people_named(self.conn, "George Herman Ruth")
        self.assertEqual([c.id for c in found], ["ruthb101"])

    def test_a_roster_spelling_finds_them(self):
        # The rosters call him Jackie; the biography says Jack. Neither
        # spelling should be the only one that works.
        found = names.people_named(self.conn, "Jackie Robinson")
        self.assertEqual([c.id for c in found], ["robij101"])

    def test_case_and_spacing_do_not_matter(self):
        self.assertEqual(
            [c.id for c in names.people_named(self.conn, "  bABe   ruTH ")],
            ["ruthb101"])

    def test_a_surname_alone_matches_everyone_with_it(self):
        found = names.people_named(self.conn, "Robinson")
        self.assertEqual({c.id for c in found}, {"robij101", "robij102"})


class Ambiguity(unittest.TestCase):
    def setUp(self):
        self.conn = database()

    def test_two_people_with_one_name_is_refused(self):
        with self.assertRaises(QueryError) as caught:
            Search().batter_named("Jack Robinson").count(self.conn)
        # The error has to carry the candidates: "ambiguous" alone is not
        # something a caller can act on.
        self.assertIn("robij101", str(caught.exception))
        self.assertIn("robij102", str(caught.exception))

    def test_two_parks_with_one_name_is_refused(self):
        # Wrigley Field is Chicago's and the Los Angeles park the 1961 Angels
        # and the Negro Leagues used.
        with self.assertRaises(QueryError) as caught:
            Search().park_named("Wrigley Field").count(self.conn)
        self.assertIn("Los Angeles", str(caught.exception))

    def test_an_unknown_name_is_refused(self):
        with self.assertRaises(QueryError):
            Search().batter_named("Nobody At All").count(self.conn)

    def test_an_unambiguous_name_compiles(self):
        self.assertEqual(Search().park_named("Fenway Park").count(self.conn), 0)

    def test_a_club_that_moved_is_two_clubs(self):
        # Brooklyn and Los Angeles are different ids, so `Dodgers` alone
        # cannot be answered.
        with self.assertRaises(QueryError):
            Search().team_named("Dodgers").count(self.conn)
        self.assertEqual(
            Search().team_named("Brooklyn Dodgers").count(self.conn), 0)


class WithoutReferenceTables(unittest.TestCase):
    """A database built before `rsse reference` must still work."""

    def setUp(self):
        self.conn = dbschema.connect(":memory:")
        self.conn.executescript(dbschema.DERIVED_DDL)

    def test_a_name_query_says_what_is_missing(self):
        with self.assertRaises(QueryError) as caught:
            Search().batter_named("Babe Ruth").count(self.conn)
        self.assertIn("rsse reference", str(caught.exception))

    def test_an_ordinary_query_is_unaffected(self):
        # Results without names are a smaller loss than refusing to answer.
        self.assertEqual(Search().outs(1).count(self.conn), 0)


class ObservedPeople(unittest.TestCase):
    def test_a_person_with_no_name_falls_back_to_the_id(self):
        # Four people in the corpus have a row and no attributes. The id is
        # the only name there is, and it beats an empty column.
        conn = database()
        self.assertEqual(names.names_for(conn, ["ghost001"]),
                         {"ghost001": "ghost001"})

    def test_names_come_back_in_one_query(self):
        conn = database()
        found = names.names_for(conn, ["ruthb101", "robij101", "ruthb101"])
        self.assertEqual(found["ruthb101"], "Babe Ruth")
        self.assertEqual(len(found), 2)
