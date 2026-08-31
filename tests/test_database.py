"""Raw-layer tests (spec/05-DATABASE.md §1, spec/01-CORPUS.md §4).

The raw layer's whole job is to be byte-exact and rebuildable, so the tests are
about losslessness and provenance rather than behaviour.
"""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from rsse.database import load as dbload
from rsse.database import schema as dbschema

FIXTURE = Path(__file__).parent / "fixtures" / "TEST2000KCA.EVA"


class RawLayer(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "archive.db"
        self.src = self.tmp / "2000KCA.EVA"
        shutil.copyfile(FIXTURE, self.src)
        self.conn = dbschema.connect(str(self.db))
        dbschema.create(self.conn)
        self.corpus = dbload.open_corpus(self.conn, "test", "test")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ingest(self):
        return dbload.ingest(self.conn, [self.src], self.corpus)

    # -- losslessness ----------------------------------------------------

    def test_every_line_is_stored_verbatim(self):
        self.ingest()
        stored = [r[0] for r in self.conn.execute(
            "SELECT raw_line FROM raw_records ORDER BY record_id")]
        # Read as bytes: text mode applies universal newlines and would
        # collapse the CRLF the fixture is deliberately written with.
        expected = [line for line
                    in self.src.read_bytes().decode("latin-1").split("\r\n")
                    if line]
        self.assertEqual(stored, expected)

    def test_record_count_matches_the_file(self):
        stats = self.ingest()
        lines = [x for x in self.src.read_bytes().decode("latin-1").split("\r\n") if x]
        self.assertEqual(stats.records, len(lines))
        self.assertEqual(
            self.conn.execute("SELECT record_count FROM source_files").fetchone()[0],
            len(lines))

    def test_roundtrip_gate_runs_and_passes(self):
        stats = self.ingest()
        self.assertGreater(stats.plays, 0)
        self.assertEqual(stats.parse_failures, [])
        self.assertEqual(stats.roundtrip_failures, [])

    # -- provenance ------------------------------------------------------

    def test_records_digest_and_line_ending(self):
        self.ingest()
        row = self.conn.execute(
            "SELECT sha256, byte_length, line_ending FROM source_files").fetchone()
        self.assertEqual(row[0], dbload.sha256(self.src))
        self.assertEqual(row[1], self.src.stat().st_size)
        self.assertEqual(row[2], "crlf")

    def test_reingesting_an_unchanged_file_is_skipped(self):
        self.ingest()
        before = self.conn.execute("SELECT count(*) FROM raw_records").fetchone()[0]
        stats = self.ingest()
        after = self.conn.execute("SELECT count(*) FROM raw_records").fetchone()[0]
        self.assertEqual(stats.skipped_files, 1)
        self.assertEqual(before, after, "re-ingest must not duplicate records")

    def test_a_changed_file_is_fatal(self):
        """Retrosheet reissues corrected files; mixing vintages is undetectable."""
        self.ingest()
        with open(self.src, "ab") as fh:
            fh.write(b'com,"added"\r\n')
        with self.assertRaises(dbload.CorpusChanged):
            self.ingest()

    def test_a_failed_file_leaves_no_partial_rows(self):
        broken = self.tmp / "2000BRK.EVA"
        shutil.copyfile(FIXTURE, broken)
        original = dbload._verify_play

        def explode(rec, game_id, stats):
            raise RuntimeError("boom")

        dbload._verify_play = explode
        try:
            with self.assertRaises(RuntimeError):
                dbload.ingest(self.conn, [broken], self.corpus)
        finally:
            dbload._verify_play = original
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM raw_records").fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM source_files").fetchone()[0], 0)

    # -- game spans ------------------------------------------------------

    def test_spans_partition_the_records_exactly(self):
        self.ingest()
        total = self.conn.execute("SELECT count(*) FROM raw_records").fetchone()[0]
        covered = self.conn.execute(
            "SELECT sum(record_count) FROM game_spans").fetchone()[0]
        self.assertEqual(total, covered)

    def test_span_lookup_matches_a_game_id_scan(self):
        self.ingest()
        for (gid,) in self.conn.execute("SELECT game_id FROM game_spans"):
            with self.subTest(game=gid):
                scan = self.conn.execute(
                    "SELECT raw_line FROM raw_records WHERE game_id = ?"
                    " ORDER BY record_id", (gid,)).fetchall()
                span = self.conn.execute(
                    "SELECT raw_line FROM raw_records WHERE record_id BETWEEN"
                    " (SELECT first_record_id FROM game_spans WHERE game_id = ?)"
                    " AND (SELECT last_record_id FROM game_spans WHERE game_id = ?)"
                    " ORDER BY record_id", (gid, gid)).fetchall()
                self.assertEqual(scan, span)

    def test_repeated_game_ids_are_kept_and_numbered(self):
        """Three real corpus games share an id with another; none may be lost."""
        dup = self.tmp / "1943NGL.EVR"
        body = self.src.read_bytes()
        first = body.split(b"id,", 2)
        # duplicate the first game block verbatim
        dup.write_bytes(body + body.split(b"\r\n")[0].join([b"", b""]) + body)
        stats = dbload.ingest(self.conn, [dup], self.corpus)
        rows = self.conn.execute(
            "SELECT game_id, occurrence FROM game_spans"
            " ORDER BY game_id, occurrence").fetchall()
        ids = [r[0] for r in rows]
        self.assertGreater(len(rows), len(set(ids)),
                           "the duplicated game should appear twice")
        for gid in set(ids):
            occ = sorted(o for g, o in rows if g == gid)
            self.assertEqual(occ, list(range(1, len(occ) + 1)),
                             "occurrences must be numbered 1..n")
        self.assertTrue(stats.repeated_game_ids)

    def test_spans_do_not_overlap(self):
        self.ingest()
        overlaps = self.conn.execute(
            "SELECT count(*) FROM game_spans a JOIN game_spans b"
            " ON a.game_id < b.game_id"
            " AND a.first_record_id <= b.last_record_id"
            " AND b.first_record_id <= a.last_record_id").fetchone()[0]
        self.assertEqual(overlaps, 0)


if __name__ == "__main__":
    unittest.main()
