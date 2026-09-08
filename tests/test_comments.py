"""`com` record classification (rsse/model/comments.py).

The structured sub-records nested inside `com` bodies are undocumented, so the
cases here are drawn from the corpus and the negatives matter more than the
positives: a prose comment misread as a structured record does not fail, it
invents a fact.
"""

import unittest

from rsse.model.comments import (EJECTION, REPLAY, SUSPEND, TEXT, UMPCHANGE,
                                 body_of, classify)


class Bodies(unittest.TestCase):
    def test_quotes_are_stripped(self):
        self.assertEqual(body_of('com,"hello"'), "hello")
        self.assertEqual(body_of("com,'hello'"), "hello")
        self.assertEqual(body_of("com,hello"), "hello")

    def test_a_comma_in_the_body_is_kept(self):
        self.assertEqual(body_of('com,"one, two"'), "one, two")


class Structured(unittest.TestCase):
    def test_ejection(self):
        c = classify('com,"ej,mcgud101,M,sherj901,Call at 2B"')
        self.assertEqual(c.kind, EJECTION)
        self.assertEqual(c.payload["person_id"], "mcgud101")
        self.assertEqual(c.payload["role"], "M")
        self.assertEqual(c.payload["umpire_id"], "sherj901")
        self.assertEqual(c.payload["reason"], "Call at 2B")

    def test_replay_overturned_and_upheld(self):
        rev = classify('com,"replay,6,molib001,SFN,welkb901,SFO03,O,Y,I,,H"')
        up = classify('com,"replay,6,pench001,HOU,welkt901,HOU03,O,N,I,,H"')
        self.assertEqual(rev.kind, REPLAY)
        self.assertIs(rev.replay_reversed, True)
        self.assertIs(up.replay_reversed, False)
        self.assertEqual(rev.payload["inning"], 6)
        self.assertEqual(rev.payload["player_id"], "molib001")

    def test_umpchange_with_a_vacancy(self):
        c = classify('com,"umpchange,8,ump1b,(none)"')
        self.assertEqual(c.kind, UMPCHANGE)
        self.assertEqual(c.payload["inning"], 8)
        self.assertEqual(c.payload["position"], "ump1b")
        self.assertIsNone(c.payload["umpire_id"],
                          "(none) is a vacancy, not an umpire named '(none)'")

    def test_umpchange_case_insensitive_vacancy(self):
        self.assertIsNone(classify('com,"umpchange,2,ump1b,(None)"')
                          .payload["umpire_id"])

    def test_suspend(self):
        c = classify('com,"suspended,19131002,NYC14,fans in bleachers"')
        self.assertEqual(c.kind, SUSPEND)
        self.assertEqual(c.payload["date"], "1913-10-02")
        self.assertEqual(c.payload["site"], "NYC14")


class ProseIsNotStructured(unittest.TestCase):
    """The negatives. Each of these occurs in the corpus."""

    #: Four prose comments begin "replay, ...". A prefix test alone reads them
    #: as structured records and fabricates a verdict.
    PROSE = [
        'com,"replay, scoring two runs"',
        'com,"replay, putting a run back on the board"',
        'com,"replay, saying that the catcher did not block the plate"',
        'com,"Catcher threw to pitcher at home on dropped third strike"',
        'com,"play, and the runner was safe"',
        'com,"however, the umpire disagreed"',
    ]

    def test_prose_stays_text(self):
        for raw in self.PROSE:
            with self.subTest(raw=raw):
                c = classify(raw)
                self.assertEqual(c.kind, TEXT)
                self.assertIsNone(c.payload)
                self.assertIsNone(c.replay_reversed)

    def test_a_replay_record_without_a_verdict_flag_is_text(self):
        # The Y/N field is what the tag depends on; without it there is no
        # verdict to report and claiming one would be the whole bug.
        self.assertEqual(classify('com,"replay,6,x001,HOU,u901,HOU03,O"').kind,
                         TEXT)

    def test_a_replay_record_with_a_junk_flag_is_text(self):
        self.assertEqual(
            classify('com,"replay,6,x001,HOU,u901,HOU03,O,maybe,I,,H"').kind,
            TEXT)


class Marker(unittest.TestCase):
    def test_the_dollar_marker_is_recorded_not_interpreted(self):
        c = classify('com,"$Game called on account of darkness"')
        self.assertTrue(c.marker)
        self.assertEqual(c.text, "Game called on account of darkness",
                         "the marker is stripped from the text but kept as a flag")
        self.assertEqual(c.kind, TEXT)

    def test_a_marked_structured_record_still_parses(self):
        c = classify('com,"$ej,wintg101,P,connt901,Balls and strikes"')
        self.assertTrue(c.marker)
        self.assertEqual(c.kind, EJECTION)


if __name__ == "__main__":
    unittest.main()
