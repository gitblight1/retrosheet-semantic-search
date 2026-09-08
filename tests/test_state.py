"""State machine tests (spec/03-STATE.md).

The force derivation is the reason this layer exists, so most of these are
about the distinction between a forced runner and a tagged one -- a
distinction Retrosheet never records.
"""

import unittest

from rsse.model.state import (HalfInningState, ParseStatus, Runner, apply_play,
                              batter_became_runner, batter_destination,
                              credit_sequences,
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

    def ran(self, event):
        return batter_became_runner(parse(event).event, "out")

    def test_ground_out_batter_ran(self):
        self.assertEqual(self.ran("64(1)3/GDP/G6"), (True, "derived"))

    def test_caught_liner_batter_did_not_run(self):
        self.assertEqual(self.ran("8(B)84(2)/LDP/L8"), (False, "derived"))

    def test_strikeout_batter_did_not_run(self):
        self.assertEqual(self.ran("K"), (False, "derived"))

    def test_a_throw_makes_it_likely_he_ran(self):
        """§4.2 rule 5. A throw is only necessary if the batter was running."""
        for event in ("63", "43", "53", "13", "31", "23", "143"):
            with self.subTest(event=event):
                self.assertEqual(self.ran(event), (True, "likely"))

    def test_an_unassisted_putout_away_from_first_is_deductively_a_catch(self):
        """Retiring a batter at first needs the ball at first.

        A shortstop who fields a grounder has to throw it, so an unassisted
        putout by 4-9 is a catch. This is geometry, not a tendency: of bare
        single-fielder putouts that do carry a trajectory, zero of 20,494 by an
        outfielder are ground balls.
        """
        for event in ("4", "5", "6", "7", "8", "9"):
            with self.subTest(event=event):
                self.assertEqual(self.ran(event), (False, "derived"))

    def test_unassisted_by_pitcher_catcher_or_first_base_is_unresolved(self):
        """The one case the fielding cannot settle.

        He either fielded a grounder and beat the batter to the bag, or caught
        it in the air, and the record does not say. 53% of bare first-baseman
        putouts carrying a trajectory are grounders, and 45% of the pitcher's.
        """
        for event in ("1", "2", "3"):
            with self.subTest(event=event):
                self.assertEqual(self.ran(event), (False, "unresolved"))

    def test_a_throw_elsewhere_on_the_play_settles_it(self):
        """`64(1)3`: the batter's own putout is unassisted, the `64` is not."""
        self.assertEqual(self.ran("64(1)3"), (True, "likely"))

    def test_an_unknown_play_resolves_nothing(self):
        self.assertEqual(self.ran("99"), (False, "unresolved"))


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
        """`BXH(TH)(E2/TH)(8E2)` names no putout: the runner scored.

        Two runs, not three. This assertion read `3` until the run accounting
        was checked against the base state -- a runner on second plus the
        batter cannot produce three runs, and the third was the batter being
        counted once by the advance loop and again by the batter placement.
        The test had been written against the observed value while the bug was
        live, which is the whole hazard of asserting what the code prints.
        """
        outcome = run("S6/L6D.2-H;BXH(TH)(E2/TH)(8E2)(NR)(UR)", "2", 2)
        self.assertEqual(outcome.outs_recorded, 0)
        self.assertEqual(outcome.runs_on_play, 2)

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


class FieldingCredits(unittest.TestCase):
    """spec/03-STATE.md §5, and the key it has to be safe to store under."""

    def seqs(self, event):
        return credit_sequences(parse(event).event)

    def test_scope_seq_is_unique_per_scope(self):
        """`credit_sequences` is keyed (play_id, scope, scope_seq) in §3.1 of
        spec/05-DATABASE.md, so the index must not be the advance index.

        `BXH(TH)(E2/TH)(8E2)` hangs three parameters off one advance, two of
        which carry credits. Numbering by advance gives both `(advance, 0)`
        and the load fails on a constraint -- or worse, silently keeps one.
        """
        for event in ("S9.BXH(TH)(E2/TH)(8E2)", "64(1)3/GDP/G6",
                      "1(B)16(2)63(1)/LTP/L1", "OA.1X3(E1)(35)"):
            with self.subTest(event=event):
                keys = [(s.scope, s.scope_seq) for s in self.seqs(event)]
                self.assertEqual(len(keys), len(set(keys)), keys)

    def test_one_sequence_per_putout_group(self):
        """`64(1)3` is two throws and two putouts, not one sequence `643`."""
        seqs = self.seqs("64(1)3/GDP/G6")
        self.assertEqual([s.seq_text for s in seqs], ["64", "3"])
        self.assertEqual([s.putout for s in seqs], ["4", "3"])
        self.assertEqual([list(s.assists) for s in seqs], [["6"], []])

    def test_credits_are_collected_from_either_section(self):
        """`K23` and `K.3XH(21)` are the same kind of play, written apart."""
        self.assertEqual([(s.scope, s.seq_text) for s in self.seqs("K23")],
                         [("basic", "23")])
        self.assertEqual([(s.scope, s.seq_text) for s in self.seqs("K.3XH(21)")],
                         [("advance", "21")])

    def test_an_error_takes_the_putout_and_negates_the_out(self):
        seq = self.seqs("CS2(2E6)")[0]
        self.assertTrue(seq.has_error)
        self.assertIsNone(seq.putout)
        self.assertFalse(seq.records_out)

    def test_99_earns_no_credit(self):
        """`99` is an unknown play, not the right fielder twice (§2.1)."""
        seq = self.seqs("99")[0]
        self.assertEqual(seq.credited, ())
        self.assertIsNone(seq.putout)

    def test_u_earns_no_credit(self):
        """`U` is undocumented; RSSE derives no credit from it (02-GRAMMAR §4.1)."""
        seqs = self.seqs("CS2(U6)")
        self.assertEqual([f for f, _e in seqs[0].credited], ["6"])


class BareTrajectorySettlesTheForce(unittest.TestCase):
    """§4.2 rule 3 must see a bare `/G` as well as a located `/G6`.

    When it did not, the force determination fell through to rule 5 -- the
    one inference in the chain -- and came back `ambiguous` on plays where
    the event string says plainly that the ball was on the ground.
    """

    def force_at_first(self, event, bases=""):
        outcome = run(event, bases)
        at_first = [a for a in outcome.advances
                    if a.origin == "B" and a.dest == "1" and a.is_out]
        self.assertEqual(len(at_first), 1, event)
        return at_first[0]

    def test_bare_g_gives_a_derived_force(self):
        for event in ("63/G", "43/G", "3/G"):
            with self.subTest(event=event):
                advance = self.force_at_first(event)
                self.assertTrue(advance.is_force)
                self.assertEqual(advance.force_certainty, "derived")

    def test_bare_and_located_agree(self):
        for bare, located in (("63/G", "63/G6"), ("43/G", "43/G4")):
            with self.subTest(event=bare):
                self.assertEqual(self.force_at_first(bare).force_certainty,
                                 self.force_at_first(located).force_certainty)

    def test_no_modifier_at_all_is_likely_not_derived(self):
        """The rule 5 case: a throw, but no trajectory to confirm it."""
        advance = self.force_at_first("63")
        self.assertTrue(advance.is_force)
        self.assertEqual(advance.force_certainty, "likely")

    def test_bare_caught_trajectory_makes_no_force(self):
        outcome = run("8/F", "1")
        self.assertEqual([a for a in outcome.advances if a.is_force], [])


class UnresolvedForceIsRecordedNotEscalated(unittest.TestCase):
    """§4.2 rule 5, the unresolved branch: claim nothing, record the doubt.

    The doubt is a queryable fact on the play, not a `parse_status`. Escalating
    would exclude the play from every default query (spec/06-QUERY.md §4) --
    3% of the corpus, 7.7% of 1920 -- so a count of home runs would come back
    short for reasons about ground balls.
    """

    def test_no_force_is_claimed(self):
        for event in ("3", "1", "2", "99"):
            with self.subTest(event=event):
                outcome = run(event, "12")
                self.assertEqual([a for a in outcome.advances if a.is_force], [])

    def test_batter_ran_is_unknown_and_the_reason_is_recorded(self):
        for event in ("3", "1", "2", "99"):
            with self.subTest(event=event):
                outcome = run(event, "12")
                self.assertEqual(outcome.batter_ran, "unknown")
                self.assertTrue(outcome.notes)

    def test_the_play_stays_in_default_results(self):
        """The whole point of the field: nothing else about the play is in doubt."""
        for event in ("3", "1", "2"):
            with self.subTest(event=event):
                self.assertEqual(run(event, "12").parse_status, "ok")

    def test_settled_plays_say_so(self):
        for event, ran in (("3/G", "yes"), ("8", "no"), ("63", "yes"),
                           ("63/G", "yes"), ("K", "no"), ("S7", "yes")):
            with self.subTest(event=event):
                outcome = run(event, "12")
                self.assertEqual(outcome.batter_ran, ran)
                self.assertEqual(outcome.parse_status, "ok")

    def test_outs_are_still_counted(self):
        """The doubt is about the force, not about the out."""
        for event in ("3", "1", "2", "99"):
            with self.subTest(event=event):
                self.assertEqual(run(event, "12").outs_recorded, 1)

    def test_a_no_play_is_not_unknown(self):
        """`NP` precedes every substitution, so it must not default to unknown.

        Left at the dataclass default it would file tens of thousands of
        substitution markers under 'unknown' and swamp the one population the
        field exists to count.
        """
        self.assertEqual(run("NP").batter_ran, "no")

    def test_every_replayed_play_states_batter_ran(self):
        """No path may leave the field at its default by omission."""
        for event in ("NP", "S7", "K", "63", "8/F", "SB2", "WP.2-3", "BK",
                      "W", "HR", "C/E2", "DI.1-2", "OA.1-2", "PB", "99"):
            with self.subTest(event=event):
                self.assertIn(run(event, "12").batter_ran,
                              ("yes", "no", "unknown"))


class GameShapeFromInfoRecords(unittest.TestCase):
    """§6.5 and §7: two facts that come from `info`, not from the plays."""

    def replay(self, *lines):
        from rsse.model.game import replay_game
        from rsse.parser.records import parse_line
        records = [parse_line(i, line) for i, line in enumerate(lines, 1)]
        return replay_game([r for r in records if r], "TST190001010")

    PLAY = "play,{inning},{team},bat{team}{inning}0,,,63/G6"

    def half_innings(self, *info):
        lines = ["id,TST190001010", *info]
        for inning in (1, 8):
            for team in (0, 1):
                for _ in range(3):
                    lines.append(self.PLAY.format(inning=inning, team=team))
        replay = self.replay(*lines)
        return replay, {(p.team, p.context.half) for p in replay.plays}

    def test_visitor_bats_top_by_default(self):
        _replay, halves = self.half_innings()
        self.assertEqual(halves, {(0, "top"), (1, "bottom")})

    def test_htbf_puts_the_home_team_in_the_top_half(self):
        """51 games in the corpus. Keying `half` off the team alone gets these
        backwards, which §6.5 forbids in as many words."""
        _replay, halves = self.half_innings("info,htbf,true")
        self.assertEqual(halves, {(1, "top"), (0, "bottom")})

    def test_scheduled_innings_defaults_to_nine(self):
        replay, _halves = self.half_innings()
        self.assertEqual(replay.scheduled_innings, 9)
        eighth = [p for p in replay.plays if p.inning == 8]
        self.assertTrue(eighth)
        self.assertFalse(any(p.context.extra_innings for p in eighth))

    def test_a_seven_inning_game_makes_the_eighth_extra(self):
        """518 games in the corpus are scheduled for 7 innings, and `info`
        says so on every one -- the key exists from 2020, which is exactly
        when the short doubleheader did."""
        replay, _halves = self.half_innings("info,innings,7")
        self.assertEqual(replay.scheduled_innings, 7)
        eighth = [p for p in replay.plays if p.inning == 8]
        self.assertTrue(eighth)
        self.assertTrue(all(p.context.extra_innings for p in eighth))

    def test_info_records_do_not_become_plays(self):
        replay, _halves = self.half_innings("info,innings,7", "info,htbf,true")
        self.assertEqual(len(replay.plays), 12)


class RunnersHaveIdentity(unittest.TestCase):
    """A batter who reaches goes onto the bases as himself.

    Without it the base *state* is still correct, so nothing fails -- but
    `runner_advances.runner_id` and `plays.runner_N_before` are NULL forever
    and "who was forced" is unanswerable.
    """

    def test_the_batter_reaches_as_himself(self):
        from rsse.model.state import HalfInningState, apply_play
        state = HalfInningState()
        after, _out = apply_play(state, parse("S7").event, batter_id="ortih001")
        self.assertEqual(after.bases["1"].player_id, "ortih001")

    def test_the_2000_anchor_names_its_runners(self):
        """The record 07-TESTING §2.2 walks through: Ortiz on third from a
        double plus a single, Damon on first, Ortiz retired at the plate."""
        from rsse.model.game import replay_game
        from rsse.parser.records import parse_line

        lines = ["id,KCA200009270",
                 "play,3,1,ortih001,22,BCCBX,D7/L78S+",
                 "play,3,1,feblc001,12,FFBS,K",
                 "play,3,1,damoj001,22,BFFBX,S9/L34D.2-3",
                 "play,3,1,sancr001,12,CBFS,K/NDP.3XH(21)"]
        replay = replay_game(
            [parse_line(i, line) for i, line in enumerate(lines, 1)],
            "KCA200009270")
        final = replay.plays[-1].outcome
        self.assertEqual(final.runners_before, ("damoj001", None, "ortih001"))
        out_at_home = [a for a in final.advances
                       if a.dest == "H" and a.is_out]
        self.assertEqual(len(out_at_home), 1)
        self.assertEqual(out_at_home[0].runner.player_id, "ortih001")
        self.assertFalse(out_at_home[0].is_force)

    def test_a_placed_runner_keeps_its_own_id(self):
        from rsse.model.game import replay_game
        from rsse.parser.records import parse_line

        lines = ["id,TST202004010", "info,innings,9",
                 "radj,speedy001,2",
                 "play,10,1,bat001,,,63/G6"]
        replay = replay_game(
            [parse_line(i, line) for i, line in enumerate(lines, 1)],
            "TST202004010")
        outcome = replay.plays[0].outcome
        self.assertEqual(outcome.runners_before, (None, "speedy001", None))
        self.assertTrue(replay.plays[0].context.has_placed_runner)


class RunAccounting(unittest.TestCase):
    """A run needs a runner, and every run needs exactly one.

    Nothing checked this until the derived tables made it a SQL query: the
    out-accounting invariant of §8 does not see runs, and final-score
    reconciliation needs game logs the project does not hold. It found the
    batter being double-counted on every home run written with an explicit
    `B-H` advance -- a solo shot scoring two.
    """

    def outcome(self, event, bases="", outs=0):
        return run(event, bases, outs)

    def test_runs_never_exceed_runners_on_base_plus_the_batter(self):
        for event, bases in (
                ("HR/F9", ""),
                ("HR/F7D+.B-H(UR)", ""),
                ("HR/F7D+.3-H;B-H", "3"),
                ("HR/F78XD.3-H;2-H;1-H", "123"),
                ("HR/F78XD.3-H;2-H;1-H;B-H", "123"),
                ("S6/L6D.2-H;BXH(TH)(E2/TH)(8E2)(NR)(UR)", "2"),
                ("D9.2-H;1-H", "12"),
                ("T9.1-H", "1")):
            with self.subTest(event=event, bases=bases):
                outcome = self.outcome(event, bases)
                self.assertLessEqual(outcome.runs_on_play, len(bases) + 1)

    def test_every_run_has_exactly_one_scoring_advance(self):
        for event, bases in (
                ("HR/F9", ""),
                ("HR/F7D+.B-H(UR)", ""),
                ("HR/F7D+.3-H;B-H", "3"),
                ("HR/F78XD.3-H;2-H;1-H", "123"),
                ("SBH", "3"),
                ("WP.3-H", "3"),
                ("D9.2-H", "2")):
            with self.subTest(event=event, bases=bases):
                outcome = self.outcome(event, bases)
                scored = [a for a in outcome.advances if a.scored]
                self.assertEqual(outcome.runs_on_play, len(scored))

    def test_a_solo_home_run_scores_one_however_it_is_written(self):
        """Retrosheet writes both forms; they must agree."""
        for event in ("HR/F9", "HR/F7D+.B-H", "HR/F7D+.B-H(UR)"):
            with self.subTest(event=event):
                self.assertEqual(self.outcome(event).runs_on_play, 1)

    def test_a_grand_slam_scores_four_however_it_is_written(self):
        for event in ("HR/F78XD.3-H;2-H;1-H", "HR/F78XD.3-H;2-H;1-H;B-H"):
            with self.subTest(event=event):
                self.assertEqual(self.outcome(event, "123").runs_on_play, 4)


class PlacedRunnerTiming(unittest.TestCase):
    """`radj` is not reliably the first record of its half-inning (§6.4).

    It commonly follows the leading `NP`/`sub` block, so a replay that applies
    it only at the half-inning boundary has already crossed that boundary and
    places nobody -- then leaks the runner into the next half. 91 plays in the
    2020s scored a run from a base the state called empty because of it.
    """

    def replay(self, *lines):
        from rsse.model.game import replay_game
        from rsse.parser.records import parse_line
        records = [parse_line(i, line) for i, line in enumerate(lines, 1)]
        return replay_game([r for r in records if r], "TST202009030")

    def first_real(self, replay, team):
        return next(p for p in replay.plays
                    if p.team == team and p.event != "NP")

    def test_radj_after_the_leading_no_plays(self):
        """The shape the corpus actually uses."""
        replay = self.replay(
            "id,TST202009030", "info,innings,9",
            "play,10,0,bat001,00,,NP",
            "radj,shawt001,2",
            "play,10,0,bat001,00,,NP",
            "play,10,0,bat001,,,S7",
        )
        play = self.first_real(replay, 0)
        self.assertEqual(play.outcome.bases_before, "010")
        self.assertEqual(play.outcome.runners_before, (None, "shawt001", None))

    def test_radj_before_the_boundary(self):
        """The other ordering must keep working."""
        replay = self.replay(
            "id,TST202009030", "info,innings,9",
            "play,9,1,prev001,,,63/G6",
            "play,9,1,prev002,,,63/G6",
            "play,9,1,prev003,,,63/G6",
            "radj,shawt001,2",
            "play,10,0,bat001,,,S7",
        )
        play = self.first_real(replay, 0)
        self.assertEqual(play.outcome.bases_before, "010")

    def test_each_half_gets_its_own_runner(self):
        """The leak: one half's runner must not reappear in the next."""
        replay = self.replay(
            "id,TST202009030", "info,innings,9",
            "play,10,0,bat001,00,,NP", "radj,shawt001,2",
            "play,10,0,bat001,,,63/G6", "play,10,0,bat002,,,63/G6",
            "play,10,0,bat003,,,63/G6",
            "play,10,1,bat004,00,,NP", "radj,bogax001,2",
            "play,10,1,bat004,,,S7",
        )
        self.assertEqual(self.first_real(replay, 0).outcome.runners_before,
                         (None, "shawt001", None))
        self.assertEqual(self.first_real(replay, 1).outcome.runners_before,
                         (None, "bogax001", None))

    def test_the_grand_slam_that_exposed_it_reconciles(self):
        """BOS202009030, top of the 10th: placed runner, walk, advance, homer."""
        replay = self.replay(
            "id,TST202009030", "info,innings,9",
            "play,10,0,biggc002,00,,NP", "radj,shawt001,2",
            "play,10,0,biggc002,31,BBFBB,W",
            "play,10,0,gricr001,00,X,3/G4-.2-3;1-2",
            "play,10,0,hernt002,01,CX,HR/L89XD.3-H(UR);2-H",
        )
        homer = replay.plays[-1].outcome
        self.assertEqual(homer.bases_before, "011")
        self.assertEqual(homer.runs_on_play, 3)
        self.assertEqual(homer.parse_status, "ok")
        self.assertEqual(replay.inconsistent, 0)


class BatterHasIdentityWhereverWritten(unittest.TestCase):
    """A batter's advance names him whether or not the scorer wrote it.

    The implicit form already carried `Runner(batter_id)`; the explicit form
    did not, so `K.3XH(21);2-3;1-2;B-1` identified all three runners on base
    and left the batter anonymous. Same physical fact, present or absent
    depending on notation -- the failure mode that put 69,938 batters in the
    2000 season with no advance row at all.
    """

    def loaded(self):
        state = HalfInningState(outs=2)
        state.bases["1"] = Runner("streg101")
        state.bases["2"] = Runner("freej105")
        state.bases["3"] = Runner("clymo101")
        return state

    def batter_advance(self, event, state=None):
        _after, out = apply_play(state or self.loaded(), parse(event).event,
                                 batter_id="picko101")
        return [a for a in out.advances if a.origin == "B"]

    def test_explicit_batter_advance_names_the_batter(self):
        rows = self.batter_advance("K.3XH(21);2-3;1-2;B-1")
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0].runner)
        self.assertEqual(rows[0].runner.player_id, "picko101")

    def test_implicit_and_explicit_forms_agree(self):
        explicit = self.batter_advance("K.3XH(21);2-3;1-2;B-1")[0]
        implicit = self.batter_advance("K.3XH(21);2-3;1-2")[0]
        self.assertEqual(explicit.runner, implicit.runner)
        self.assertEqual(explicit.dest, implicit.dest)

    def test_a_retired_batter_is_named_too(self):
        rows = self.batter_advance("43/G4", HalfInningState(outs=0))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].runner.player_id, "picko101")
        self.assertTrue(rows[0].is_out)

