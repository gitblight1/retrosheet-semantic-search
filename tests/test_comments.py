"""`com` record classification (rsse/model/comments.py).

The structured sub-records nested inside `com` bodies are undocumented, so the
cases here are drawn from the corpus and the negatives matter more than the
positives: a prose comment misread as a structured record does not fail, it
invents a fact.
"""

import unittest

from rsse.model.comments import (AFTER, BEFORE, EJECTION, REPLAY, SUSPEND,
                                 TEXT, UMPCHANGE, body_of, classify,
                                 replay_target)


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


class ReplayLinking(unittest.TestCase):
    """Which play a `replay` comment describes (rsse/model/comments.py).

    A `com` record usually describes the play before it, and 4,241 of the
    corpus's 5,102 replay records do. 51 describe the play *after*: the
    umpires confer, the comment is written, and the reviewed play follows.
    Linking those backwards puts the verdict on a play that was never
    reviewed.
    """

    def test_the_default_is_the_play_before(self):
        self.assertEqual(replay_target("a", "a", "b"), BEFORE)

    def test_a_player_who_bats_only_after_moves_the_link(self):
        # `SEA200809240`: the comment names Guerrero, the play before it is
        # Teixeira's, and the play after carries `/UREV` and is Guerrero's.
        self.assertEqual(replay_target("guerv001", "teixm001", "guerv001"),
                         AFTER)

    def test_the_same_batter_on_both_sides_keeps_the_default(self):
        # 804 records. A muffed foul fly or a pickoff leaves the same batter
        # at the plate across the boundary, so the name settles nothing and
        # the convention stands.
        self.assertEqual(replay_target("a", "a", "a"), BEFORE)

    def test_a_name_matching_neither_play_changes_nothing(self):
        # 6 records. The field names the player involved in the reviewed
        # call, which for a review of a runner's advance is the runner rather
        # than the batter -- so a name that matches nothing is not evidence
        # that the default is wrong.
        self.assertEqual(replay_target("vottj001", "brucj001", "duvaa001"),
                         BEFORE)

    def test_a_record_naming_nobody_keeps_the_default(self):
        self.assertEqual(replay_target(None, "a", "b"), BEFORE)

    def test_a_padded_player_id_still_matches(self):
        # Four records pad the id with a trailing space. An id that will not
        # compare equal to the batter it names fails every link silently.
        c = classify('com,"replay,5,jacka001 ,CHA,welkt901,CHI12,F,Y,I,,H"')
        self.assertEqual(c.payload["player_id"], "jacka001")


class ReplayLinkingInAGame(unittest.TestCase):
    """The rule, driven through `replay_game` (§6.7)."""

    GAME = [
        "id,TST200809240",
        "version,2",
        "info,visteam,ANA",
        "info,hometeam,SEA",
        "play,5,0,teixm001,01,FX,5/P5F",
        'com,"replay,5,guerv001,ANA,welkt901,SEA03,F,Y,I,,H"',
        "play,5,0,guerv001,12,SFFBX,S8/UREV/G6M+",
        "play,5,0,huntt001,30,BBBB,W.1-2",
    ]

    def replay(self):
        from rsse.model.game import replay_game
        from rsse.parser.records import parse_line
        records = [parse_line(i, line) for i, line in enumerate(self.GAME, 1)]
        return replay_game([r for r in records if r is not None],
                           "TST200809240")

    def test_the_verdict_lands_on_the_reviewed_play(self):
        out = self.replay()
        by_batter = {p.batter_id: p for p in out.plays}
        self.assertIs(by_batter["guerv001"].context.replay_reversed, True)

    def test_the_preceding_play_is_left_alone(self):
        out = self.replay()
        by_batter = {p.batter_id: p for p in out.plays}
        self.assertIsNone(by_batter["teixm001"].context.replay_reversed)

class ReplayVerdictsAreOred(unittest.TestCase):
    """A second review on the same play must not erase the first (§6.7).

    33 plays in the corpus carry one `replay` record saying a call was
    reversed and another saying a call was upheld. Writing each verdict as it
    arrives let the upheld one win and lost every one of those reversals.
    """

    GAME = [
        "id,TST201606040",
        "version,2",
        "info,visteam,BOS",
        "info,hometeam,TOR",
        "play,3,0,bettm001,01,FX,3/G3S",
        'com,"replay,3,bettm001,BOS,welkt901,TOR02,F,Y,I,,H"',
        'com,"replay,3,bettm001,BOS,welkt901,TOR02,F,N,I,,H"',
        "play,3,0,pedrd001,12,SFFBX,S8/G6M+",
    ]

    def replay(self):
        from rsse.model.game import replay_game
        from rsse.parser.records import parse_line
        records = [parse_line(i, line) for i, line in enumerate(self.GAME, 1)]
        return replay_game([r for r in records if r is not None],
                           "TST201606040")

    def test_a_later_upheld_does_not_erase_a_reversal(self):
        out = self.replay()
        by_batter = {p.batter_id: p for p in out.plays}
        self.assertIs(by_batter["bettm001"].context.replay_reversed, True)

    def test_a_later_reversal_does_upgrade_an_upheld(self):
        self.GAME[5], self.GAME[6] = self.GAME[6], self.GAME[5]
        try:
            out = self.replay()
        finally:
            self.GAME[5], self.GAME[6] = self.GAME[6], self.GAME[5]
        by_batter = {p.batter_id: p for p in out.plays}
        self.assertIs(by_batter["bettm001"].context.replay_reversed, True)


class NoPlayIsNotAReviewablePlay(unittest.TestCase):
    """`NP` must be skipped when choosing a replay comment's neighbours.

    An `NP` carries the batter due up, who is normally the batter of the next
    real play, so `replay_target`'s test -- named player bats after but not
    before -- can never fire and the verdict lands on the substitution. 141
    comments in the corpus did exactly this; where the event strings settle
    it, the review modifier is on the play after the `NP` 47 times and on the
    play before it none.
    """

    GAME = [
        "id,TST201404120",
        "version,2",
        "info,visteam,CHN",
        "info,hometeam,CHA",
        "play,7,0,rizza001,01,FX,8/F8",
        "sub,abreb003,Bobby Abreu,0,4,7",
        "play,7,0,castw001,,,NP",
        'com,"replay,7,castw001,CHN,welkt901,CHA01,F,Y,I,,H"',
        "play,7,0,castw001,12,SFFBX,K/UREV",
    ]

    def replay(self):
        from rsse.model.game import replay_game
        from rsse.parser.records import parse_line
        records = [parse_line(i, line) for i, line in enumerate(self.GAME, 1)]
        return replay_game([r for r in records if r is not None],
                           "TST201404120")

    def test_the_verdict_skips_the_substitution(self):
        out = self.replay()
        np_play = [p for p in out.plays if p.event == "NP"][0]
        self.assertIsNone(np_play.context.replay_reversed)

    def test_the_verdict_lands_on_the_reviewed_play(self):
        out = self.replay()
        reviewed = [p for p in out.plays if "UREV" in p.event][0]
        self.assertIs(reviewed.context.replay_reversed, True)
