"""Raw-layer tests (spec/05-DATABASE.md §1, spec/01-CORPUS.md §4).

The raw layer's whole job is to be byte-exact and rebuildable, so the tests are
about losslessness and provenance rather than behaviour.
"""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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



class UntrustedState(unittest.TestCase):
    """An unparsed play poisons the rest of its half-inning (03-STATE §7.1).

    The state machine cannot see this: it is handed one play at a time and each
    of these parses perfectly. Only the loader, which holds the whole
    half-inning, can tell that the state was already wrong -- so this is where
    the flag has to be tested.
    """

    #: `S7/L6d` is one of the seven records malformed at source: a lowercase
    #: location qualifier. It is a single, so the batter belongs on first, and
    #: the play after it advances a runner from a base the state thinks is
    #: empty. Everything else here is ordinary.
    GAME = [
        "id,TST200004070",
        "version,2",
        "info,visteam,MIN",
        "info,hometeam,KCA",
        "info,date,2000/04/07",
        "play,1,0,aaaaa001,00,,S8/L8",          # clean, before the defect
        "play,1,0,bbbbb001,00,,S7/L6d",         # unparseable
        "play,1,0,ccccc001,00,,D9/L9LD.1-3",    # advances a runner not there
        "play,1,0,ddddd001,00,,8/F8",
        "play,1,0,eeeee001,00,,8/F8",
        "play,1,0,fffff001,00,,8/F8",
        "play,1,1,ggggg001,00,,8/F8",           # next half: state has reset
        "play,1,1,hhhhh001,00,,8/F8",
        "play,1,1,iiiii001,00,,8/F8",
    ]

    def derive(self):
        from rsse.database.derived import _PLAY_COLUMNS, derive_game
        from rsse.parser.records import parse_line
        from rsse.semantic.derive import load_curated, tag_rows

        records = [parse_line(i, line) for i, line in enumerate(self.GAME, 1)]
        records = [r for r in records if r is not None]
        tag_ids = {t["name"]: i for i, t in enumerate(tag_rows(), 1)}
        stats = SimpleNamespace(games=0, plays=0, advances=0, credits=0,
                                sequences=0, tags=0, curated=0, unparsed=0,
                                status={})
        span = {"game_key": 1, "game_id": "TST200004070", "occurrence": 1,
                "file_id": 1, "path": "test", "season": 2000}
        out = derive_game(span, records, list(range(1, len(records) + 1)),
                          0, "test", load_curated(), tag_ids, stats)
        return [dict(zip(_PLAY_COLUMNS, row))
                for row in out["rows"]["plays"]]

    def test_unparsed_row_stores_no_state(self):
        rows = self.derive()
        bad = [r for r in rows if r["parse_status"] == "unparsed"]
        self.assertEqual(len(bad), 1)
        for column in ("outs_before", "outs_recorded", "outs_after",
                       "bases_before", "bases_after"):
            self.assertIsNone(bad[0][column],
                              f"{column} must be NULL, not an invented zero")

    def test_later_plays_in_the_half_are_untrusted(self):
        rows = self.derive()
        after = [r for r in rows if r["batting_team"] == 0][2:]
        self.assertTrue(after)
        for row in after:
            self.assertEqual(row["parse_status"], "state_untrusted",
                             f"{row['event_raw']} inherited a poisoned state")

    def test_the_play_before_the_defect_is_untouched(self):
        rows = self.derive()
        self.assertEqual(rows[0]["parse_status"], "ok")

    def test_contamination_stops_at_the_half_inning(self):
        rows = self.derive()
        other = [r for r in rows if r["batting_team"] == 1]
        self.assertEqual(len(other), 3)
        for row in other:
            self.assertEqual(row["parse_status"], "ok",
                             "state resets at the boundary; doubt must not cross")

class LineupRecordFields(unittest.TestCase):
    """Splitting a `start`/`sub` record (rsse/database/secondary.py).

    Both failure modes here were found in the corpus, and neither raises: a
    lineup record this parser cannot read becomes a player who appears never
    to have played, which is a silent hole in `.pitcher()`.
    """

    def split(self, line):
        from rsse.database.secondary import _lineup_fields
        return _lineup_fields(tuple(line.split(",")))

    def test_ordinary_start_record(self):
        self.assertEqual(
            self.split('start,nichs101,"Simon Nicholls",0,1,6'),
            ("nichs101", "Simon Nicholls", 0, 1, 6))

    def test_a_trailing_space_after_the_position(self):
        # Three records in the corpus are written this way. Unstripped, the
        # position fails `isdigit()` and the record is reported unreadable.
        self.assertEqual(
            self.split('sub,hemsr101,"Rollie Hemsley",0,9,11 '),
            ("hemsr101", "Rollie Hemsley", 0, 9, 11))

    def test_a_comma_inside_the_quoted_name(self):
        # Read left to right, the name fragment lands where the position
        # belongs and `int()` raises on a well-formed row. Fields are read
        # from the end for this reason.
        self.assertEqual(
            self.split('sub,x001,"Griffey, Ken Jr.",1,3,8'),
            ("x001", "Griffey, Ken Jr.", 1, 3, 8))

    def test_single_quoted_names_are_accepted(self):
        self.assertEqual(
            self.split("sub,deinp101,'Pep Deininger',0,4,12")[1],
            "Pep Deininger")

    def test_an_unreadable_record_returns_none_rather_than_raising(self):
        for line in ("sub,x001,junk", 'sub,x001,"Name",1,3,left field'):
            self.assertIsNone(self.split(line), line)


if __name__ == "__main__":
    unittest.main()
