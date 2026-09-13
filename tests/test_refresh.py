"""Re-ingesting a reissued file, and rebuilding only its games.

Retrosheet reissues corrected files. `ingest_file` has always detected that
exactly -- it hashes every file against `source_files.sha256` -- and has
always refused to act on it, because mixing vintages inside one corpus is
wrong in a way nothing downstream detects. These cover the way to say yes.

The pair has to be tested together. Re-ingesting one file takes seconds; until
the derived layer could be brought back into agreement without a full rebuild,
that saving was worth nothing against a 79-minute `derive`.
"""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from rsse.database import derived, load as dbload, schema as dbschema

FIXTURE = Path(__file__).parent / "fixtures" / "TEST2000KCA.EVA"


class Refresh(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = self.tmp / "2000KCA.EVA"
        shutil.copyfile(FIXTURE, self.src)

        self.archive = dbschema.connect(str(self.tmp / "archive.db"))
        dbschema.create(self.archive)
        self.corpus = dbload.open_corpus(self.archive, "test", "test")
        dbload.ingest(self.archive, [self.src], self.corpus)

        self.query = dbschema.connect(str(self.tmp / "rsse.db"))
        dbschema.create_query_db(self.query)
        dbschema.create_derived(self.query)
        self.ro = dbschema.connect(f"file:{self.tmp / 'archive.db'}?mode=ro")
        derived.build(self.query, self.ro, "test")

    def reissue(self):
        """Retrosheet publishes a corrected file: one comment added."""
        with open(self.src, "ab") as fh:
            fh.write(b'com,"corrected by Retrosheet"\r\n')

    def count(self, conn, table, where=""):
        return conn.execute(f"SELECT count(*) FROM {table} {where}").fetchone()[0]

    # -- the refusal is still the default --------------------------------

    def test_without_refresh_a_reissue_is_still_fatal(self):
        self.reissue()
        with self.assertRaises(dbload.CorpusChanged):
            dbload.ingest(self.archive, [self.src], self.corpus)

    def test_the_refusal_names_the_way_to_say_yes(self):
        self.reissue()
        with self.assertRaises(dbload.CorpusChanged) as caught:
            dbload.ingest(self.archive, [self.src], self.corpus)
        self.assertIn("--refresh", str(caught.exception))

    # -- the archive half ------------------------------------------------

    def test_refresh_replaces_the_records_rather_than_adding_them(self):
        before = self.count(self.archive, "raw_records")
        self.reissue()
        stats = dbload.ingest(self.archive, [self.src], self.corpus,
                              refresh=True)
        self.assertEqual(stats.refreshed_files, 1)
        self.assertEqual(self.count(self.archive, "source_files"), 1,
                         "one file, not two")
        self.assertEqual(self.count(self.archive, "raw_records"), before + 1)

    def test_the_spans_still_tile_the_records(self):
        self.reissue()
        dbload.ingest(self.archive, [self.src], self.corpus, refresh=True)
        records = self.count(self.archive, "raw_records")
        spanned = self.archive.execute(
            "SELECT sum(record_count) FROM game_spans").fetchone()[0]
        declared = self.archive.execute(
            "SELECT sum(record_count) FROM source_files").fetchone()[0]
        self.assertEqual(records, spanned)
        self.assertEqual(records, declared)

    def test_the_replaced_game_keys_are_reported(self):
        old_keys = {r[0] for r in self.archive.execute(
            "SELECT game_key FROM game_spans")}
        self.reissue()
        stats = dbload.ingest(self.archive, [self.src], self.corpus,
                              refresh=True)
        _path, _old, _new, dropped = stats.refreshed[0]
        self.assertEqual(set(dropped), old_keys)

    def test_a_refresh_hands_back_the_same_keys(self):
        """Which is why agreement is decided on content, not identity.

        `game_spans.game_key` is a plain `INTEGER PRIMARY KEY`, so deleting a
        file's rows frees those rowids and the replacement takes them straight
        back. A reconciler that compared key sets would report nothing to do
        and leave the old vintage of a corrected file in place, with every
        check green.
        """
        old_keys = {r[0] for r in self.archive.execute(
            "SELECT game_key FROM game_spans")}
        self.reissue()
        dbload.ingest(self.archive, [self.src], self.corpus, refresh=True)
        new_keys = {r[0] for r in self.archive.execute(
            "SELECT game_key FROM game_spans")}
        self.assertEqual(new_keys, old_keys)

    def test_the_digest_is_what_changes(self):
        before = self.query.execute(
            "SELECT DISTINCT source_sha256 FROM games").fetchone()[0]
        self.refresh_both()
        after = self.query.execute(
            "SELECT DISTINCT source_sha256 FROM games").fetchone()[0]
        self.assertNotEqual(before, after)
        self.assertEqual(after, self.archive.execute(
            "SELECT sha256 FROM source_files").fetchone()[0])

    def test_the_supersession_is_recorded_in_the_archive(self):
        """Reporting it at the terminal is not the same as retaining it."""
        before = self.count(self.archive, "raw_records")
        self.reissue()
        dbload.ingest(self.archive, [self.src], self.corpus, refresh=True)
        row = self.archive.execute(
            "SELECT path, old_sha256, new_sha256, old_record_count,"
            " new_record_count, games_replaced FROM file_revisions").fetchone()
        self.assertIsNotNone(row)
        self.assertTrue(row[0].endswith("2000KCA.EVA"))
        self.assertNotEqual(row[1], row[2])
        self.assertEqual(row[3], before)
        self.assertEqual(row[4], before + 1)
        self.assertEqual(row[5], 2)

    def test_nothing_is_recorded_when_nothing_changed(self):
        dbload.ingest(self.archive, [self.src], self.corpus, refresh=True)
        self.assertEqual(self.count(self.archive, "file_revisions"), 0)

    def test_an_unchanged_file_is_still_skipped_under_refresh(self):
        stats = dbload.ingest(self.archive, [self.src], self.corpus,
                              refresh=True)
        self.assertEqual(stats.skipped_files, 1)
        self.assertEqual(stats.refreshed_files, 0)

    # -- the derived half ------------------------------------------------

    def refresh_both(self):
        self.reissue()
        dbload.ingest(self.archive, [self.src], self.corpus, refresh=True)
        self.archive.commit()
        stale, missing = derived.stale_and_missing(self.query, self.ro)
        derived.forget_games(self.query, stale)
        derived.build(self.query, self.ro, "test", game_keys=missing)
        self.query.commit()
        return stale, missing

    def test_a_refresh_shows_up_as_both_stale_and_missing(self):
        stale, missing = self.refresh_both()
        self.assertEqual(len(stale), 2)
        self.assertEqual(len(missing), 2)

    def test_afterwards_the_two_databases_agree(self):
        self.refresh_both()
        stale, missing = derived.stale_and_missing(self.query, self.ro)
        self.assertEqual((stale, missing), ([], []))

    def test_the_game_count_is_unchanged(self):
        before = self.count(self.query, "games")
        self.refresh_both()
        self.assertEqual(self.count(self.query, "games"), before)

    def test_no_row_is_left_pointing_at_a_deleted_play(self):
        self.refresh_both()
        for table in ("runner_advances", "fielding_credits",
                      "credit_sequences", "play_tags"):
            orphans = self.count(
                self.query, table,
                "WHERE play_id NOT IN (SELECT play_id FROM plays)")
            self.assertEqual(orphans, 0, f"{table} left dangling")

    def test_no_row_is_left_pointing_at_a_deleted_game(self):
        self.refresh_both()
        for table in ("plays", "game_info"):
            orphans = self.count(
                self.query, table,
                "WHERE game_key NOT IN (SELECT game_key FROM games)")
            self.assertEqual(orphans, 0, f"{table} left dangling")

    def test_secondary_rows_for_the_replaced_games_are_removed(self):
        """`comments` is not built by `derive`, but its `play_id` is."""
        self.query.executescript(
            "CREATE TABLE IF NOT EXISTS comments (game_key INTEGER,"
            " seq INTEGER, play_id INTEGER, record_id INTEGER, kind TEXT,"
            " text TEXT, payload TEXT, marker INTEGER)")
        key, play = self.query.execute(
            "SELECT game_key, play_id FROM plays LIMIT 1").fetchone()
        self.query.execute(
            "INSERT INTO comments VALUES (?,0,?,0,'text','x',NULL,0)",
            (key, play))
        self.query.commit()
        self.refresh_both()
        self.assertEqual(self.count(self.query, "comments"), 0,
                         "a comment on a replaced game must not survive it")


class AdoptingDigests(unittest.TestCase):
    """The migration for databases built before `games.source_sha256`."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        src = self.tmp / "2000KCA.EVA"
        shutil.copyfile(FIXTURE, src)
        self.archive = dbschema.connect(str(self.tmp / "archive.db"))
        dbschema.create(self.archive)
        corpus = dbload.open_corpus(self.archive, "test", "test")
        dbload.ingest(self.archive, [src], corpus)
        self.ro = dbschema.connect(f"file:{self.tmp / 'archive.db'}?mode=ro")

        self.query = dbschema.connect(str(self.tmp / "rsse.db"))
        dbschema.create_query_db(self.query)
        dbschema.create_derived(self.query)
        derived.build(self.query, self.ro, "test")
        self.query.commit()

    def drop_the_column(self):
        """An older database: the column simply is not there."""
        self.query.commit()
        self.query.execute("PRAGMA foreign_keys=OFF")
        self.query.executescript(
            "CREATE TABLE games_old AS"
            " SELECT game_key, game_id, occurrence, plays FROM games;"
            " DROP TABLE games;"
            " ALTER TABLE games_old RENAME TO games;")
        self.query.commit()
        self.query.execute("PRAGMA foreign_keys=ON")

    def test_a_database_without_the_column_refuses_to_guess(self):
        self.drop_the_column()
        with self.assertRaises(derived.NoSourceDigests):
            derived.stale_and_missing(self.query, self.ro)

    def test_a_database_with_the_column_all_null_refuses_too(self):
        self.query.execute("UPDATE games SET source_sha256 = NULL")
        self.query.commit()
        with self.assertRaises(derived.NoSourceDigests):
            derived.stale_and_missing(self.query, self.ro)

    def test_adopting_makes_the_two_agree(self):
        self.drop_the_column()
        adopted = derived.adopt_digests(self.query, self.ro)
        self.assertEqual(adopted, 2)
        self.assertEqual(derived.stale_and_missing(self.query, self.ro),
                         ([], []))

    def test_adopting_records_the_archive_digest(self):
        self.drop_the_column()
        derived.adopt_digests(self.query, self.ro)
        want = self.ro.execute("SELECT sha256 FROM source_files").fetchone()[0]
        got = self.query.execute(
            "SELECT DISTINCT source_sha256 FROM games").fetchall()
        self.assertEqual(got, [(want,)])


if __name__ == "__main__":
    unittest.main()
