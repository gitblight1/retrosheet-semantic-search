"""Gold corpus runner (spec/07-TESTING.md §2).

Each gold file asserts against several layers. Only the parse layer exists so
far, so this runner checks `expected_parse` and `roundtrip` now and reports the
state and tag sections as pending rather than passing them silently -- a gold
file that quietly checks nothing is worse than no gold file.
"""

import json
import unittest
from pathlib import Path

from rsse.parser.parser import parse

GOLD_DIR = Path(__file__).parent / "gold"

#: Layers not yet implemented. Assertions naming these are reported, not run.
PENDING_LAYERS = ("expected_state", "expected_tags", "forbidden_tags",
                  "expected_sql_fields")


def load_gold():
    return [(p.stem, json.loads(p.read_text())) for p in sorted(GOLD_DIR.glob("*.json"))]


class GoldCorpus(unittest.TestCase):
    def test_corpus_is_not_empty(self):
        self.assertTrue(load_gold(), "no gold files found")

    def test_every_file_has_forbidden_tags(self):
        """Required by §2: a gold file without negatives is a snapshot."""
        for name, gold in load_gold():
            with self.subTest(gold=name):
                self.assertTrue(
                    gold.get("forbidden_tags"),
                    "gold files must list at least one forbidden tag",
                )

    def test_every_file_declares_verification_status(self):
        for name, gold in load_gold():
            with self.subTest(gold=name):
                self.assertIn("status", gold["source"])

    def test_roundtrip(self):
        for name, gold in load_gold():
            with self.subTest(gold=name):
                subject = gold["input"]["subject"]
                self.assertEqual(parse(subject).emit(), subject)

    def test_equivalents_roundtrip(self):
        for name, gold in load_gold():
            for event in gold.get("equivalents", []):
                with self.subTest(gold=name, event=event):
                    self.assertEqual(parse(event).emit(), event)

    def test_expected_parse(self):
        for name, gold in load_gold():
            with self.subTest(gold=name):
                self._check_parse(gold)

    def _check_parse(self, gold):
        ev = parse(gold["input"]["subject"]).event
        expected = gold["expected_parse"]

        basics = ev.basics
        self.assertEqual(len(basics), len(expected["basic"]))
        for actual, want in zip(basics, expected["basic"]):
            self.assertEqual(type(actual).__name__, want["type"])
            if "groups" in want:
                self.assertEqual(
                    [{"fielders": g.fielders, "runner": g.runner}
                     for g in getattr(actual, "groups", ())],
                    want["groups"],
                )

        self.assertEqual(len(ev.modifiers), len(expected["modifiers"]))
        for actual, want in zip(ev.modifiers, expected["modifiers"]):
            self.assertEqual(actual.kind, want["kind"])
            if "code" in want:
                self.assertEqual(actual.code, want["code"])

        self.assertEqual(len(ev.advances), len(expected["advances"]))
        for actual, want in zip(ev.advances, expected["advances"]):
            self.assertEqual(actual.origin, want["origin"])
            self.assertEqual(actual.dest, want["dest"])
            self.assertEqual(actual.marked_out, want["marked_out"])
            self.assertEqual(actual.is_out, want["is_out"])
            credits = [c for p in actual.params
                       for c in getattr(p, "credits", ())]
            self.assertEqual(
                [{"fielder": c.fielder, "is_error": c.is_error} for c in credits],
                want["credits"],
            )


class PendingLayers(unittest.TestCase):
    """Surfaces what the gold corpus asserts that cannot yet be checked."""

    def test_report_pending(self):
        pending = {}
        for name, gold in load_gold():
            layers = [k for k in PENDING_LAYERS if k in gold]
            if layers:
                pending[name] = layers
        if pending:
            lines = [f"  {n}: {', '.join(v)}" for n, v in sorted(pending.items())]
            print(
                "\ngold assertions pending later layers "
                f"({len(pending)} files):\n" + "\n".join(lines)
            )
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
