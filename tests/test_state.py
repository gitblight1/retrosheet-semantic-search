"""State machine tests (spec/03-STATE.md).

The force derivation is the reason this layer exists, so most of these are
about the distinction between a forced runner and a tagged one -- a
distinction Retrosheet never records.
"""

import unittest

from rsse.model.state import (HalfInningState, ParseStatus, Runner, apply_play,
                              batter_became_runner, batter_destination,
                              forced_bases)
from rsse.parser.parser import parse


def state(bases="", outs=0):
    return HalfInningState(
        outs=outs,
        bases={b: (Runner(f"r{b}") if b in bases else None) for b in "123"})


def run(event, bases="", outs=0):
    return apply_play(state(bases, outs), parse(event).event)[1]


class BatterDestination(unittest.TestCase):
    def test_implicit_destinations(self):
        cases = {"S8": "1", "D7": "2", "T9": "3", "HR": "H", "DGR": "2",
                 "W": "1", "HP": "1", "E3": "1", "FC5": "1", "C": "1",
                 "K": "out", "63": "out"}
        for event, want in cases.items():
            with self.subTest(event=event):
                self.assertEqual(batter_destination(parse(event).event), want)

    def test_only_the_first_event_describes_the_batter(self):
        """`K+E2` charges an error for a runner; the batter is still out.

        Retrosheet documents `K+event` with event one of SB/CS/OA/PO/PB/WP/E$ --
        all base-running. Letting the `E2` set the destination puts a phantom
        runner on first and corrupts every later play in the inning, which is
        what 18 flagged plays across the corpus turned out to be.
        """
        for event in ("K+E2.1-2", "K+PB.1-2", "K+WP.2-3", "K+SB2",
                      "K+E2/TH.1-2", "K+E3.1-2"):
            with self.subTest(event=event):
                self.assertEqual(batter_destination(parse(event).event), "out")

    def test_a_batter_reaching_on_a_third_strike_needs_an_explicit_advance(self):
        self.assertEqual(run("K+WP.B-1", "", 0).batter_dest, "1")
        self.assertEqual(run("K+E2.1-2", "1", 1).batter_dest, "out")

    def test_k_plus_error_does_not_invent_a_runner(self):
        outcome = run("K+E2.1-2", "1", 1)
        self.assertEqual(outcome.outs_after, 2)
        self.assertEqual(outcome.bases_after, "010")

    def test_walk_plus_event_still_puts_the_batter_on_first(self):
        self.assertEqual(batter_destination(parse("W+WP.2-3").event), "1")

    def test_events_not_involving_the_batter(self):
        for event in ("SB2", "CS2(26)", "WP", "PB", "BK", "PO1(13)", "NP"):
            with self.subTest(event=event):
                self.assertIsNone(batter_destination(parse(event).event))

    def test_explicit_batter_advance_wins(self):
        """`K.B-1` -- the batter reached on an uncaught third strike."""
        self.assertEqual(run("K.B-1;3XH(21)", "123", 2).batter_dest, "1")

    def test_force_out_leaves_the_batter_safe(self):
        """`64(1)` retires only the runner; `64(1)3` goes on to first."""
        self.assertEqual(run("64(1)/FO/G6", "1").batter_dest, "1")
        self.assertEqual(run("64(1)3/GDP/G6", "1").batter_dest, "out")


class BatterBecameRunner(unittest.TestCase):
    """Not the same as reaching safely -- this is what decides a force."""

    def test_ground_out_batter_ran(self):
        self.assertEqual(batter_became_runner(parse("64(1)3/GDP/G6").event, "out"),
                         (True, True))

    def test_caught_liner_batter_did_not_run(self):
        self.assertEqual(batter_became_runner(parse("8(B)84(2)/LDP/L8").event, "out"),
                         (False, True))

    def test_strikeout_batter_did_not_run(self):
        self.assertEqual(batter_became_runner(parse("K").event, "out"), (False, True))

    def test_no_trajectory_falls_back_and_is_uncertain(self):
        self.assertEqual(batter_became_runner(parse("63").event, "out"), (True, False))
        self.assertEqual(batter_became_runner(parse("8").event, "out"), (False, False))


class ForceDerivation(unittest.TestCase):
    def test_nothing_is_forced_when_the_batter_is_not_a_runner(self):
        self.assertEqual(forced_bases(state("123"), batter_live=False), set())

    def test_force_chain_requires_the_bases_behind(self):
        self.assertEqual(forced_bases(state(""), True), {"1"})
        self.assertEqual(forced_bases(state("1"), True), {"1", "2"})
        self.assertEqual(forced_bases(state("2"), True), {"1"})
        self.assertEqual(forced_bases(state("12"), True), {"1", "2", "3"})
        self.assertEqual(forced_bases(state("123"), True), {"1", "2", "3"})

    def test_the_two_motivating_plays_differ_only_by_state(self):
        """The whole project in one assertion.

        `K.B-1;3XH(21)` and `K/NDP.3XH(21)` put a runner out at home 2-1 on an
        uncaught third strike. One is a force and one is a tag, and nothing in
        the event strings says which.
        """
        force = run("K.B-1;3XH(21)", "123", 2)
        tag = run("K/NDP.3XH(21)", "13", 1)
        for outcome, expected in ((force, True), (tag, False)):
            home = [a for a in outcome.advances if a.dest == "H" and a.is_out]
            self.assertEqual(len(home), 1)
            self.assertEqual(home[0].is_force, expected)
        self.assertFalse(force.batter_is_out)
        self.assertTrue(tag.batter_is_out)
        self.assertTrue(force.is_inning_ending)
        self.assertTrue(tag.is_inning_ending)

    def test_lined_into_double_play_is_a_tag_not_a_force(self):
        outcome = run("8(B)84(2)/LDP/L8", "2")
        out = [a for a in outcome.advances if a.is_out][0]
        self.assertFalse(out.is_force)
        self.assertEqual(outcome.outs_recorded, 2)


class OutAccounting(unittest.TestCase):
    def test_designator_outs_are_counted(self):
        self.assertEqual(run("64(1)3/GDP/G6", "1").outs_recorded, 2)
        self.assertEqual(run("1(B)16(2)63(1)/LTP/L1", "12").outs_recorded, 3)

    def test_base_running_outs_are_counted(self):
        for event, bases in (("CS2(26)", "1"), ("PO1(13)", "1"),
                             ("POCS2(1361)", "1")):
            with self.subTest(event=event):
                self.assertEqual(run(event, bases).outs_recorded, 1)

    def test_an_error_negates_a_base_running_out(self):
        self.assertEqual(run("CS2(2E4).1-3", "1").outs_recorded, 0)
        self.assertEqual(run("PO1(E3).1-2", "1").outs_recorded, 0)

    def test_an_error_beside_a_clean_putout_does_not_negate(self):
        """`1X3(E1)(35)`: an error is charged and the runner is still out."""
        self.assertEqual(run("OA.1X3(E1)(35)", "1").outs_recorded, 1)

    def test_error_in_the_only_credit_sequence_negates(self):
        self.assertEqual(run("S7/L7LD.3-H;2-H;BX2(7E4)", "23").outs_recorded, 0)

    def test_a_non_negated_caught_stealing_beats_a_safe_advance(self):
        """`CS2(25).1-2` records the out; the advance does not cancel it."""
        self.assertEqual(run("CS2(25).1-2", "1", 1).outs_recorded, 1)
        self.assertEqual(run("PO2(165).2-2", "12", 1).outs_recorded, 1)

    def test_an_error_still_negates_a_caught_stealing(self):
        outcome = run("CS2(2E4).1-3", "1")
        self.assertEqual(outcome.outs_recorded, 0)
        self.assertEqual(outcome.bases_after, "001")

    def test_a_bare_throw_parameter_does_not_preserve_an_out(self):
        """`BXH(TH)(E2/TH)(8E2)` names no putout: the runner scored."""
        outcome = run("S6/L6D.2-H;BXH(TH)(E2/TH)(8E2)(NR)(UR)", "2", 2)
        self.assertEqual(outcome.outs_recorded, 0)
        self.assertEqual(outcome.runs_on_play, 3)

    def test_rulebook_contradiction_is_not_a_state_error(self):
        """Six pre-1947 plays contradict the uncaught-third-strike rule.

        The replay is self-consistent, so this is a statement about the data,
        recorded separately from a state-machine failure.
        """
        outcome = run("K+PB.1-2;B-1", "1", 0)
        self.assertEqual(outcome.parse_status, ParseStatus.CONTRADICTS_RULES)
        self.assertNotEqual(outcome.parse_status, ParseStatus.INCONSISTENT)

    def test_more_than_three_outs_is_flagged(self):
        self.assertEqual(run("64(1)3/GDP/G6", "1", outs=2).parse_status,
                         ParseStatus.INCONSISTENT)


class Advances(unittest.TestCase):
    def test_stolen_base_moves_the_runner(self):
        self.assertEqual(run("SB2", "1").bases_after, "010")
        self.assertEqual(run("SB3;SB2", "12").bases_after, "011")

    def test_steal_of_home_scores(self):
        self.assertEqual(run("SBH", "3").runs_on_play, 1)

    def test_unmentioned_runners_hold(self):
        self.assertEqual(run("K", "13").bases_after, "101")

    def test_runs_are_counted(self):
        self.assertEqual(run("S8.3-H;1-2", "13").runs_on_play, 1)
        self.assertEqual(run("HR/F78XD.2-H;1-H", "12").runs_on_play, 3)

    def test_no_play_changes_nothing(self):
        outcome = run("NP", "13", outs=1)
        self.assertEqual((outcome.bases_after, outcome.outs_after), ("101", 1))


if __name__ == "__main__":
    unittest.main()
