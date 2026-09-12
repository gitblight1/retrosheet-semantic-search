"""Archiving the source files that belong to no game (rsse/database/auxiliary.py).

The archive's promise is bytes, so most of these are about bytes: a file that
goes in must come back out identical, and a file that is already in must not
go in twice.
"""

import tempfile
import unittest
from pathlib import Path

from rsse.database import auxiliary as aux, schema as dbschema
from rsse.database.load import CorpusChanged


def archive():
    # `dbschema.connect`, not `sqlite3.connect`: the loader manages its own
    # transactions, which the driver's implicit ones collide with.
    conn = dbschema.connect(":memory:")
    conn.executescript(dbschema.ARCHIVE_DDL)
    conn.execute("INSERT INTO corpus (ingested_at, rsse_version, parser_version)"
                 " VALUES ('now', 't', 't')")
    # Committed, so the loader's own `BEGIN` is not nested inside the driver's
    # implicit transaction from this insert.
    conn.commit()
    return conn, 1


class RoundTrip(unittest.TestCase):
    """`_read` and `rebuild` must be exact inverses.

    Not "close enough": an archive that cannot reproduce its input is a
    transcription, and the whole point of this layer is that it is not one.
    """

    CASES = {
        "crlf": b"beckr001,Beck,Rod,R,R,BOS,P\r\nbufod001,Buford,Damon,R,R,BOS,OF\r\n",
        "lf": b"ALS,A,American League,All Stars(A)\nANA,A,Anaheim,Angels\n",
        # One file in 3,435 ends without a terminator. Without the
        # `final_newline` column this rebuilds one byte short and the digest
        # check is the only thing that would ever notice.
        "no final newline": b"PARKID,NAME\r\nACY01,Inlet Park",
        # latin-1 is a total byte mapping, which is why it is used here: these
        # bytes are not valid UTF-8 and must still survive.
        "non-utf8 bytes": b"munoz001,Mu\xf1oz,Jos\xe9,R,R,SDN,P\r\n",
        "empty": b"",
        "blank lines kept": b"a\r\n\r\nb\r\n",
    }

    def test_every_shape_rebuilds_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, blob in self.CASES.items():
                path = Path(tmp) / "f.txt"
                path.write_bytes(blob)
                lines, ending, final = aux._read(path)
                self.assertEqual(aux.rebuild(lines, ending, final), blob, name)


class Ingest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "BOS1998.ROS"
        self.path.write_bytes(b"beckr001,Beck,Rod,R,R,BOS,P\r\n")
        self.conn, self.corpus = archive()

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, path=None, stats=None):
        stats = stats or aux.AuxStats()
        aux.ingest_file(self.conn, self.corpus, "roster", path or self.path,
                        1998, stats)
        return stats

    def test_records_and_declared_count_agree(self):
        # The invariant `rsse verify` asserts, at the scale of one file.
        self.load()
        declared = self.conn.execute(
            "SELECT sum(record_count) FROM aux_files").fetchone()[0]
        actual = self.conn.execute(
            "SELECT count(*) FROM aux_records").fetchone()[0]
        self.assertEqual(declared, actual)

    def test_loading_the_same_file_twice_loads_it_once(self):
        self.load()
        stats = self.load()
        self.assertEqual(stats.skipped_files, 1)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM aux_records").fetchone()[0], 1)

    def test_a_relative_and_an_absolute_path_are_the_same_file(self):
        # This is not hypothetical. Ingesting `data/parks` and then the
        # absolute form of the same directory loaded 153,010 records twice
        # without violating `UNIQUE (corpus_id, path)`, because the two
        # spellings are different strings.
        self.load()
        import os
        cwd = os.getcwd()
        try:
            os.chdir(self.dir)
            stats = self.load(Path("BOS1998.ROS"))
        finally:
            os.chdir(cwd)
        self.assertEqual(stats.skipped_files, 1)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM aux_files").fetchone()[0], 1)

    def test_a_reissued_file_is_fatal_rather_than_mixed_in(self):
        self.load()
        self.path.write_bytes(b"beckr001,Beck,Rodney,R,R,BOS,P\r\n")
        with self.assertRaises(CorpusChanged):
            self.load()

    def test_the_stored_file_rebuilds_from_the_archive(self):
        self.load()
        self.assertIsNone(aux.check_roundtrip(self.conn, 1))

    def test_a_lost_record_is_caught_by_the_gate(self):
        # The failure this gate exists for: records present, bytes wrong.
        self.load()
        self.conn.execute("DELETE FROM aux_records WHERE line_no = 1")
        self.assertIsNotNone(aux.check_roundtrip(self.conn, 1))


class Discovery(unittest.TestCase):
    def test_kinds_are_read_off_the_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            events, parks = Path(tmp) / "events" / "1998", Path(tmp) / "parks"
            events.mkdir(parents=True)
            parks.mkdir()
            for name in ("BOS1998.ROS", "TEAM1998", "1998BOS.EVA"):
                (events / name).write_bytes(b"x\r\n")
            for name in ("ballparks.csv", "biofile.csv", "teams.csv",
                         "parkcode.txt", "allplayers.csv"):
                (parks / name).write_bytes(b"x\r\n")
            found = aux.discover(events.parent, parks)
        kinds = {kind: path.name for kind, path, _season in found}
        self.assertEqual(kinds, {"roster": "BOS1998.ROS", "team": "TEAM1998",
                                 "park": "ballparks.csv", "bio": "biofile.csv",
                                 "teamlist": "teams.csv"})
        # Event files belong to `source_files`, not here.
        self.assertNotIn("1998BOS.EVA", {p.name for _k, p, _s in found})
        # `parkcode.txt` is a strict subset of `ballparks.csv` -- same nine
        # columns, no park id it lacks -- and `allplayers.csv` adds no person
        # that a roster or biography does not already carry. Both are left out
        # deliberately, so this asserts the decision rather than the accident.
        self.assertNotIn("parkcode.txt", {p.name for _k, p, _s in found})
        self.assertNotIn("allplayers.csv", {p.name for _k, p, _s in found})

    def test_the_season_comes_from_the_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "1998"
            events.mkdir(parents=True)
            (events / "BOS1998.ROS").write_bytes(b"x\r\n")
            (events / "TEAM1998").write_bytes(b"x\r\n")
            found = aux.discover(Path(tmp), Path(tmp) / "none")
        self.assertEqual({s for _k, _p, s in found}, {1998})
