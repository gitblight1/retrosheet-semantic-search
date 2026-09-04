"""Gold corpus runner (spec/07-TESTING.md §2).

Each gold file asserts against four layers -- parse, state, tags, and the SQL
fields a play promotes into. The first three exist, so they are checked here.
`expected_sql_fields` is still reported as pending rather than passed silently:
a gold file that quietly checks nothing is worse than no gold file.

Two assertions carry most of the weight, and neither is a snapshot:

* `forbidden_tags` -- every file must list at least one, because a rule that
  only ever sees positives will happily over-fire. `strikeout_tag_home_2000`
  and `dropped_third_force_home` differ by one modifier and one base state,
  and each is the other's negative.
* `equivalents` -- encodings of the *same* play must carry the same tags. This
  is what catches a rule keyed on the written form rather than the derived
  state.
"""

import json
import unittest
from pathlib import Path

from rsse.model.state import HalfInningState, PlayContext, Runner, apply_play
from rsse.parser.parser import parse
from rsse.semantic.derive import derive

GOLD_DIR = Path(__file__).parent / "gold"

#: Layers not yet implemented. Assertions naming these are reported, not run.
PENDING_LAYERS = ("expected_sql_fields",)

#: Tags that legitimately differ between encodings of the same play: an
#: encoding that charges a wild pitch or a passed ball records something the
#: others do not, and must say so. Everything else must agree (§2.1).
ENCODING_SPECIFIC = frozenset({"WildPitch", "PassedBall"})


def load_gold():
    return [(p.stem, json.loads(p.read_text()))
            for p in sorted(GOLD_DIR.glob("*.json"))]


def replay(gold, event_string=None):
    """Replay a gold entry's subject event against its stated state."""
    before = gold["input"]["state_before"]
    state = HalfInningState(outs=before["outs"])
    for base, runner in before["bases"].items():
        state.bases[base] = Runner(runner) if runner else None
    parsed = parse(event_string or gold["input"]["subject"])
    _after, outcome = apply_play(state, parsed.event)
    context = PlayContext(inning=before["inning"], half=before["half"])
    return parsed, outcome, derive(parsed.event, outcome, context, parsed.trivia)


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


class GoldState(unittest.TestCase):
    """spec/03-STATE.md, asserted per play against a stated base/out state."""

    SCALARS = ("batter_dest", "batter_is_out", "outs_recorded", "outs_after",
               "runs_on_play")

    def test_expected_state(self):
        for name, gold in load_gold():
            if "expected_state" not in gold:
                continue
            with self.subTest(gold=name):
                _parsed, outcome, _tags = replay(gold)
                want = gold["expected_state"]
                for field in self.SCALARS:
                    if field in want:
                        self.assertEqual(getattr(outcome, field), want[field],
                                         f"{name}: {field}")
                for spec in want.get("advances", []):
                    self._check_advance(name, outcome, spec)

    def _check_advance(self, name, outcome, spec):
        matches = [a for a in outcome.advances
                   if a.origin == spec["origin"] and a.dest == spec["dest"]]
        self.assertEqual(len(matches), 1,
                         f"{name}: expected one {spec['origin']}->{spec['dest']}")
        advance = matches[0]
        for field in ("is_out", "is_force", "force_certainty"):
            if field in spec:
                self.assertEqual(getattr(advance, field), spec[field],
                                 f"{name}: advance {field}")

    def test_replays_are_consistent(self):
        """No gold entry may itself be state-inconsistent."""
        for name, gold in load_gold():
            with self.subTest(gold=name):
                _parsed, outcome, _tags = replay(gold)
                self.assertNotEqual(outcome.parse_status, "state_inconsistent",
                                    f"{name}: {outcome.notes}")


class GoldTags(unittest.TestCase):
    """spec/04-ONTOLOGY.md.

    Tags are additive (§1.5), so `expected_tags` is a subset assertion and
    `forbidden_tags` a disjointness assertion. A file need not enumerate every
    tag a play carries; it must be right about the ones it names.
    """

    def test_expected_tags_all_fire(self):
        for name, gold in load_gold():
            if "expected_tags" not in gold:
                continue
            with self.subTest(gold=name):
                _p, _o, tags = replay(gold)
                fired = {t.name for t in tags}
                missing = sorted(set(gold["expected_tags"]) - fired)
                self.assertFalse(missing, f"{name}: not derived: {missing}")

    def test_forbidden_tags_never_fire(self):
        for name, gold in load_gold():
            with self.subTest(gold=name):
                _p, _o, tags = replay(gold)
                fired = {t.name for t in tags}
                wrong = sorted(fired & set(gold["forbidden_tags"]))
                self.assertFalse(wrong, f"{name}: wrongly derived: {wrong}")

    def test_expected_tags_are_certain(self):
        """A gold file asserts a determination, not a guess.

        Anything a file lists in `expected_tags` must come back `certain`,
        because an `uncertain` tag is excluded from default query results
        (spec/06-QUERY.md §4) -- so a gold file passing on an uncertain tag
        would assert a result no researcher would see.
        """
        for name, gold in load_gold():
            if "expected_tags" not in gold:
                continue
            _p, _o, tags = replay(gold)
            by_name = {t.name: t for t in tags}
            for want in gold["expected_tags"]:
                with self.subTest(gold=name, tag=want):
                    self.assertEqual(by_name[want].confidence, "certain")

    def test_equivalents_carry_the_same_tags(self):
        """§2.1: encodings of the same play must be found by the same query.

        This is the assertion that caught trigger (c) of `UncaughtThirdStrike`
        being keyed on a written `B-%` advance: `K.3XH(21)` derives the
        batter's advance from the out count instead of stating it, and so
        carried a different tag set from `K.B-1;3XH(21)`.
        """
        for name, gold in load_gold():
            equivalents = gold.get("equivalents", [])
            if not equivalents:
                continue
            _p, _o, base_tags = replay(gold)
            baseline = {t.name for t in base_tags} - ENCODING_SPECIFIC
            for event in equivalents:
                with self.subTest(gold=name, event=event):
                    _p2, _o2, tags = replay(gold, event)
                    actual = {t.name for t in tags} - ENCODING_SPECIFIC
                    self.assertEqual(actual, baseline,
                                     f"{name}: {event} differs by "
                                     f"{sorted(actual ^ baseline)}")


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
