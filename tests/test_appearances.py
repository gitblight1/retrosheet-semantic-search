"""`allplayers.csv`: the reader, the schema migration, and the pass
(rsse/model/reference.py, rsse/database/schema.py, rsse/database/appearances.py).

Three separate things go wrong here and only one of them is parsing. The file
is positional behind a header, so a shifted column produces plausible counts;
an archive built before the file had a `kind` rejects it mid-ingest; and the
corpus comparison the table exists for reads as a finding when it is really a
missing table.
"""

import sqlite3
import unittest

from rsse.database import appearances as appdb, schema as dbschema
from rsse.model import reference as ref
from rsse.model.reference import LayoutError

HEADER = ",".join(ref.APPEARANCE_HEADER)
#: A real row, trimmed of nothing. Bill Monroe's 1913 line is the one that
#: makes `g < max(position counts)` true in the corpus, so it is the one to
#: hold on to: the reader must not "fix" it.
MONROE = ("monrb102,Monroe,Bill,R,R,CAG,6,0,0,0,0,0,7,0,0,0,0,1,1,0,0,0,"
          "19130720,19130809,1913")
BELL = ("bellw103,Bell,William,?,?,PHG,3,3,3,0,0,0,0,0,0,0,0,0,0,0,0,0,"
        "19030912,19030918,1903")
NO_DATES = ("doejj001,Doe,John,R,R,CAG,4,0,0,0,4,0,0,0,0,0,0,0,0,0,0,0,"
            "0,0,1920")


class Reading(unittest.TestCase):
    def test_a_row_reads(self):
        [a] = ref.read_appearances([HEADER, BELL])
        self.assertEqual(a.person_id, "bellw103")
        self.assertEqual(a.team_id, "PHG")
        self.assertEqual(a.season, 1903)
        self.assertEqual(a.counts["g_sp"], 3)
        self.assertEqual(a.first_game, "1903-09-12")

    def test_zero_is_no_date_not_a_date(self):
        # 1,494 rows. `0` parsed as a date is year zero, which sorts before
        # every real game and is wrong in a way nothing downstream would
        # question.
        [a] = ref.read_appearances([HEADER, NO_DATES])
        self.assertIsNone(a.first_game)
        self.assertIsNone(a.last_game)

    def test_a_contradictory_row_is_kept_as_written(self):
        # 6 games played, 7 at second base. Both numbers are Retrosheet's and
        # the disagreement is a fact about the file; a reader that reconciled
        # it would be inventing one of the two.
        [a] = ref.read_appearances([HEADER, MONROE])
        self.assertEqual(a.counts["g"], 6)
        self.assertEqual(a.counts["g_2b"], 7)

    def test_a_shifted_header_is_refused(self):
        shifted = HEADER.replace("g_lf,g_cf", "g_cf,g_lf")
        with self.assertRaises(LayoutError):
            ref.read_appearances([shifted, BELL])

    def test_a_short_row_is_refused(self):
        with self.assertRaises(LayoutError):
            ref.read_appearances([HEADER, BELL.rsplit(",", 1)[0]])

    def test_pitching_totals_must_agree(self):
        # `g_p == g_sp + g_rp` in all 11,476 rows, so it is a gate: a row
        # where it fails is a row whose columns have shifted.
        broken = BELL.replace("PHG,3,3,3,0", "PHG,3,3,2,0")
        with self.assertRaises(LayoutError):
            ref.read_appearances([HEADER, broken])

    def test_the_outfield_total_is_not_a_gate(self):
        # `g_of` disagrees with `g_lf + g_cf + g_rf` in 207 correct rows: a
        # game in an unspecified outfield spot is counted in `g_of` alone.
        # Asserting the sum would fail on data that is right.
        row = ("xxxxx001,X,Y,R,R,CAG,9,0,0,0,0,0,0,0,0,1,0,0,9,0,0,0,"
               "19200401,19200901,1920")
        [a] = ref.read_appearances([HEADER, row])
        self.assertEqual(a.counts["g_of"], 9)
        self.assertEqual(a.counts["g_lf"], 1)

    def test_a_non_numeric_count_is_refused(self):
        with self.assertRaises(LayoutError):
            ref.read_appearances([HEADER, BELL.replace("PHG,3,3,3", "PHG,3,x,3")])


class Migration(unittest.TestCase):
    """Version 1 archives must take `allplayers.csv` without a re-ingest."""

    V1_AUX_FILES = """
    CREATE TABLE aux_files (
      aux_file_id   INTEGER PRIMARY KEY,
      corpus_id     INTEGER NOT NULL REFERENCES corpus,
      path          TEXT NOT NULL,
      kind          TEXT NOT NULL
        CHECK (kind IN ('roster','team','teamlist','park','bio')),
      season        INTEGER,
      sha256        TEXT NOT NULL,
      byte_length   INTEGER NOT NULL,
      mtime         TEXT NOT NULL,
      line_ending   TEXT NOT NULL CHECK (line_ending IN ('crlf','lf','mixed')),
      final_newline INTEGER NOT NULL DEFAULT 1,
      record_count  INTEGER NOT NULL DEFAULT 0,
      UNIQUE (corpus_id, path)
    );
    """

    def v1(self):
        conn = dbschema.connect(":memory:")
        script = dbschema.ARCHIVE_DDL.replace(
            dbschema._statement(dbschema.ARCHIVE_DDL, "aux_files"), "")
        conn.executescript(script)
        conn.executescript(self.V1_AUX_FILES)
        conn.execute("INSERT INTO corpus (ingested_at, rsse_version,"
                     " parser_version) VALUES ('now','t','t')")
        conn.execute(
            "INSERT INTO aux_files (corpus_id, path, kind, sha256,"
            " byte_length, mtime, line_ending) VALUES"
            " (1,'/x/ANA2010.ROS','roster','abc',10,'now','lf')")
        conn.execute("INSERT INTO aux_records (aux_file_id, line_no, raw_line)"
                     " VALUES (1, 1, 'a,b,c')")
        conn.commit()
        return conn

    def test_a_v1_archive_rejects_the_new_kind(self):
        conn = self.v1()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO aux_files (corpus_id, path, kind, sha256,"
                " byte_length, mtime, line_ending) VALUES"
                " (1,'/x/allplayers.csv','appearances','d',1,'now','lf')")

    def test_migrating_widens_the_check(self):
        conn = self.v1()
        self.assertEqual(len(dbschema.migrate_archive(conn)), 1)
        conn.execute(
            "INSERT INTO aux_files (corpus_id, path, kind, sha256,"
            " byte_length, mtime, line_ending) VALUES"
            " (1,'/x/allplayers.csv','appearances','d',1,'now','lf')")

    def test_migrating_keeps_the_rows(self):
        conn = self.v1()
        dbschema.migrate_archive(conn)
        self.assertEqual(
            list(conn.execute("SELECT path, kind FROM aux_files")),
            [("/x/ANA2010.ROS", "roster")])

    def test_migrating_does_not_redirect_the_foreign_key(self):
        # The trap this migration is written around: `ALTER TABLE ... RENAME
        # TO` rewrites other tables' REFERENCES clauses to follow the rename,
        # so moving `aux_files` out of the way would leave `aux_records`
        # pointing at a table that is about to be dropped -- and nothing would
        # say so until the next foreign key check, if there ever was one.
        conn = self.v1()
        dbschema.migrate_archive(conn)
        sql = conn.execute("SELECT sql FROM sqlite_master"
                           " WHERE name = 'aux_records'").fetchone()[0]
        self.assertIn("REFERENCES aux_files", sql)
        self.assertNotIn("aux_files_v2", sql)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(),
                         [])

    def test_migrating_is_idempotent(self):
        conn = self.v1()
        dbschema.migrate_archive(conn)
        self.assertEqual(dbschema.migrate_archive(conn), [])

    def test_a_current_archive_needs_no_migration(self):
        conn = dbschema.connect(":memory:")
        conn.executescript(dbschema.ARCHIVE_DDL)
        self.assertEqual(dbschema.migrate_archive(conn), [])

    def test_the_check_and_the_constant_agree(self):
        # The pair that drifts: a kind added to one and not the other fails at
        # the first ingest that uses it, on a long run.
        sql = dbschema._statement(dbschema.ARCHIVE_DDL, "aux_files")
        for kind in dbschema.AUX_KINDS:
            self.assertIn(f"'{kind}'", sql)
        listed = sql.split("CHECK (kind IN (")[1].split(")")[0]
        self.assertEqual(len(listed.split(",")), len(dbschema.AUX_KINDS))


def query_db():
    conn = dbschema.connect(":memory:")
    conn.executescript(dbschema.DERIVED_DDL)
    conn.executescript(
        "CREATE TABLE lineup_entries (game_key INTEGER, seq INTEGER,"
        " is_sub INTEGER, play_id INTEGER, player_id TEXT, player_name TEXT,"
        " team INTEGER, batting_order INTEGER, position INTEGER);"
        "CREATE TABLE teams (team_id TEXT, season INTEGER, league TEXT,"
        " city TEXT, nickname TEXT);")
    return conn


def archive_db(lines):
    conn = dbschema.connect(":memory:")
    conn.executescript(dbschema.ARCHIVE_DDL)
    conn.execute("INSERT INTO corpus (ingested_at, rsse_version,"
                 " parser_version) VALUES ('now','t','t')")
    conn.execute("INSERT INTO aux_files (corpus_id, path, kind, sha256,"
                 " byte_length, mtime, line_ending) VALUES"
                 " (1,'/x/allplayers.csv','appearances','d',1,'now','lf')")
    conn.executemany(
        "INSERT INTO aux_records (aux_file_id, line_no, raw_line)"
        " VALUES (1, ?, ?)", list(enumerate(lines, 1)))
    conn.commit()
    return conn


class Building(unittest.TestCase):
    def build(self, extra_games=()):
        query = query_db()
        for key, season, home, away in extra_games:
            query.execute(
                "INSERT INTO games (game_key, game_id, occurrence, date,"
                " season, game_number, home_team, away_team, plays,"
                " parse_status) VALUES (?,?,1,'1903-09-12',?,0,?,?,0,'ok')",
                (key, f"G{key}", season, home, away))
        query.commit()
        archive = archive_db([HEADER, BELL, MONROE])
        return query, appdb.build(query, archive)

    def test_rows_are_written(self):
        query, stats = self.build()
        self.assertEqual(stats.rows, 2)
        self.assertEqual(stats.people, 2)
        self.assertEqual(stats.seasons, (1903, 1913))

    def test_a_person_season_with_no_surviving_game_counts_zero(self):
        # Not NULL. The corpus genuinely holds none, and that is an answer.
        query, stats = self.build()
        self.assertEqual(stats.absent_from_corpus, 2)
        [(n,)] = query.execute("SELECT games_in_corpus FROM appearances"
                               " WHERE person_id = 'bellw103'")
        self.assertEqual(n, 0)

    def test_the_corpus_count_uses_the_club_not_the_side(self):
        # `lineup_entries.team` is 0 visitor / 1 home, so the club has to come
        # from the game. Reading it as a team id would attribute every road
        # appearance to the wrong club and still look plausible.
        query, _ = self.build(extra_games=[(1, 1903, "CAG", "PHG")])
        query.execute(
            "INSERT INTO lineup_entries VALUES (1,1,0,NULL,'bellw103','B',"
            "0,1,1)")
        query.commit()
        archive = archive_db([HEADER, BELL, MONROE])
        stats = appdb.build(query, archive)
        [(n,)] = query.execute("SELECT games_in_corpus FROM appearances"
                               " WHERE person_id = 'bellw103'")
        self.assertEqual(n, 1, "team 0 is the visitor, which here is PHG")
        self.assertEqual(stats.games_held, 1)

    def test_one_game_is_counted_once_however_many_lineup_rows(self):
        # A player who is substituted, or who changes position, has several
        # entries in one game. Counting rows would inflate every regular's
        # season past what the file states and read as a finding.
        query, _ = self.build(extra_games=[(1, 1903, "CAG", "PHG")])
        query.executemany(
            "INSERT INTO lineup_entries VALUES (1,?,0,NULL,'bellw103','B',"
            "0,1,1)", [(1,), (2,), (3,)])
        query.commit()
        stats = appdb.build(query, archive_db([HEADER, BELL, MONROE]))
        self.assertEqual(stats.games_held, 1)

    def test_a_rebuild_replaces_rather_than_doubles(self):
        query, _ = self.build()
        appdb.build(query, archive_db([HEADER, BELL, MONROE]))
        [(n,)] = query.execute("SELECT count(*) FROM appearances")
        self.assertEqual(n, 2)

    def test_teams_with_no_season_file_are_reported(self):
        query, stats = self.build()
        self.assertEqual(stats.unknown_teams, ["CAG", "PHG"])


if __name__ == "__main__":
    unittest.main()
