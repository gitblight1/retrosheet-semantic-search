"""Earned runs derived from the play-by-play (rsse/model/earnedruns.py).

Every case here is driven from event strings through the real parser and the
real state machine, so a test that passes says the rule works on Retrosheet
notation rather than on a hand-built structure that happens to match what the
rule expects.

The corpus-scale check lives elsewhere, in `rsse earned-runs --check`: 1.8
million runs against the `(UR)` and `(TUR)` flags Retrosheet wrote. These are
the cases that pin the individual clauses of 9.16, where a corpus percentage
would move by too little to notice.
"""

import unittest

from rsse.model import earnedruns as ER
from rsse.model.state import HalfInningState, Runner, apply_play
from rsse.parser.parser import parse


def half(events, *, pitchers=None, batters=None, placed=(), trusted=None):
    """Build a half-inning from event strings.

    `pitchers` is one pitcher id per play, so a mid-inning change is written
    as a change in that list; `placed` seeds the bases for an extra-inning
    tiebreaker.
    """
    state = HalfInningState()
    for base in placed:
        state.bases[base] = Runner(f"placed{base}", placed=True)
    plays = []
    for index, text in enumerate(events):
        event = parse(text).event
        batter = (batters or [])[index] if batters else f"bat{index}"
        pitcher = (pitchers or [])[index] if pitchers else "pit"
        before = state.copy()
        state, outcome = apply_play(state, event, batter_id=batter)
        movements = []
        for seq, adv in enumerate(outcome.advances):
            aided = pb = recorded = None
            for written in event.advances:
                if written.origin == adv.origin:
                    aided, pb, recorded = ER.movement_flags(written)
                    break
            movements.append(ER.Movement(
                adv_seq=seq, origin=adv.origin, dest=adv.dest,
                marked_out=adv.marked_out, is_out=adv.is_out,
                scored=adv.scored, runner_id=(adv.runner.player_id
                                              if adv.runner else None),
                aided_by_error=bool(aided), aided_by_passed_ball=bool(pb),
                recorded=recorded or ""))
        plays.append(ER.play_facts(
            event, index, batter, before.outs, outcome.outs_recorded,
            pitcher, movements,
            True if trusted is None else trusted[index],
            before.code()))
    return plays


def verdicts(events, **kwargs):
    return ER.reconstruct(half(events, **kwargs))


class CleanInnings(unittest.TestCase):
    def test_a_run_with_no_aid_anywhere_is_earned_and_certain(self):
        [run] = verdicts(["D7", "S8.2-H", "K", "K", "8"])
        self.assertTrue(run.earned_team)
        self.assertTrue(run.earned_pitcher)
        self.assertEqual(run.certainty, ER.DERIVED)
        self.assertEqual(run.flag, "")

    def test_a_home_run_scores_everyone_earned(self):
        runs = verdicts(["W", "W.1-2", "HR.2-H;1-H;B-H"])
        self.assertEqual(len(runs), 3)
        self.assertTrue(all(r.earned_team and r.earned_pitcher for r in runs))

    def test_a_scoreless_inning_produces_no_verdicts(self):
        self.assertEqual(verdicts(["K", "43", "8"]), [])


class ReachingOnAnError(unittest.TestCase):
    def test_a_runner_who_reached_on_an_error_scores_unearned(self):
        runs = verdicts(["E6", "S8.1-3", "S8.3-H"])
        self.assertEqual(runs[-1].flag, "UR")
        self.assertFalse(runs[-1].earned_team)
        self.assertEqual(runs[-1].reason, "reached on an error")
        # Categorical in 9.16(a)(2)(iii): no reconstruction is consulted, so
        # the verdict claims the strong word.
        self.assertEqual(runs[-1].certainty, ER.DERIVED)

    def test_the_error_also_costs_the_defence_an_out(self):
        # `E6` means the batter would have been out. That out is what makes
        # the *third* run below unearned, not the taint on the first runner.
        runs = verdicts(["E6", "S8.1-2", "S8.2-H;1-2", "K", "K",
                         "S8.1-3", "S8.3-H;1-2"])
        self.assertEqual(runs[-1].reason, "the inning would already have ended")
        self.assertFalse(runs[-1].earned_team)

    def test_catchers_interference_taints_without_costing_an_out(self):
        runs = verdicts(["C/E2", "S8.1-3", "S8.3-H"])
        self.assertEqual(runs[-1].flag, "UR")
        self.assertEqual(runs[-1].reason,
                         "reached on interference or obstruction")

    def test_a_muffed_foul_fly_prolongs_the_at_bat(self):
        # 9.16(a)(2)(i). The muff is the missed chance; the hit that follows
        # only happened because the at bat did not end.
        runs = verdicts(["FLE5/FL", "S8", "S8.1-3", "S8.3-H"],
                        batters=["a", "a", "b", "c"])
        self.assertEqual(runs[-1].flag, "UR")
        self.assertEqual(runs[-1].reason,
                         "at bat prolonged by a muffed foul fly")


class TheReconstructedInning(unittest.TestCase):
    def test_three_reconstructed_outs_end_the_inning_for_everyone(self):
        # The `E6` batter is out in the reconstruction, so with the two
        # strikeouts the inning ends there. The runner scoring from second
        # reached cleanly, on a walk, two batters later -- and his run is
        # still unearned, because in the reconstruction he never batted.
        runs = verdicts(["E6", "K", "K", "W.1-2", "W.2-3;1-2",
                         "S8.3-H;2-H;1-3"])
        clean = runs[-1]
        self.assertEqual(clean.reason, "the inning would already have ended")
        self.assertFalse(clean.earned_team)
        self.assertEqual(clean.certainty, ER.DERIVED)
        # And the runner who did reach on the error is unearned for his own
        # reason, which outranks the inning's.
        self.assertEqual(runs[0].reason, "reached on an error")

    def test_an_out_on_a_runner_who_does_not_exist_does_not_count(self):
        # The runner reached on an error, so in the reconstruction he was
        # never on base to be caught stealing. Counting his out would end the
        # inning early and make later runs unearned for the wrong reason.
        runs = verdicts(["E6", "CS2(26)", "S8", "S8.1-3", "S8.3-H"])
        self.assertEqual(runs[-1].reason, "scored without aid")
        self.assertTrue(runs[-1].earned_team)


class WhereTheRuleDefers(unittest.TestCase):
    def test_an_advance_aided_by_an_error_is_not_decided(self):
        runs = verdicts(["W", "S8.1-3(E7/TH)", "8/SF.3-H"])
        self.assertIsNone(runs[-1].earned_team)
        self.assertIsNone(runs[-1].flag)
        self.assertEqual(runs[-1].certainty, ER.AMBIGUOUS)

    def test_a_passed_ball_is_aid_and_a_wild_pitch_is_not(self):
        # 9.16(a) lists the wild pitch among the things that make a run
        # earned and pointedly omits the passed ball: one is the pitcher's
        # own mistake, the other is the catcher's.
        [wild] = verdicts(["T9", "WP.3-H"])
        self.assertTrue(wild.earned_team)
        [passed] = verdicts(["T9", "PB.3-H"])
        self.assertIsNone(passed.earned_team)
        self.assertEqual(passed.reason, "advance aided by a passed ball")

    def test_an_undetermined_run_is_not_an_earned_one(self):
        # The distinction the whole design turns on: `flag` is None, not "".
        [run] = verdicts(["D7", "D7.2-H(E9/TH)"])
        self.assertIsNone(run.flag)
        self.assertNotEqual(run.flag, "")


class PitcherResponsibility(unittest.TestCase):
    def test_a_reliever_is_not_charged_with_inherited_runners(self):
        # 9.16(f). The runner reached against `starter`; the run is his even
        # though `reliever` threw the pitch it scored on.
        runs = verdicts(["D7", "S8.2-H"],
                        pitchers=["starter", "reliever"])
        self.assertEqual(runs[0].pitcher_id, "starter")

    def test_a_reliever_gets_no_benefit_from_an_earlier_missed_chance(self):
        # 9.16(g), and the only thing `(TUR)` exists to record: the team's
        # inning should have been over, but this pitcher's should not.
        runs = verdicts(["K", "K", "E6", "W", "S8.1-3;B-2", "S8.3-H;2-3"],
                        pitchers=["a", "a", "a", "b", "b", "b"])
        last = runs[-1]
        self.assertFalse(last.earned_team)
        self.assertTrue(last.earned_pitcher)
        self.assertEqual(last.flag, "TUR")

    def test_presadj_overrules_the_derivation(self):
        plays = half(["D7", "S8.2-H"])
        plays[1].responsibility = (("2", "stated"),)
        runs = ER.reconstruct(plays)
        self.assertEqual(runs[0].pitcher_id, "stated")


class PlacedRunners(unittest.TestCase):
    def test_the_tiebreaker_runner_is_nobody_s_earned_run(self):
        # spec/03-STATE.md §6.4: he reached second without facing a pitch.
        runs = verdicts(["8/SF.2-3", "S8.3-H"], placed=("2",))
        self.assertEqual(runs[-1].flag, "UR")
        self.assertEqual(runs[-1].reason, "extra-inning placed runner")

    def test_a_batter_who_follows_him_is_still_earned(self):
        runs = verdicts(["D7.2-H", "D7.2-H"], placed=("2",))
        self.assertEqual(runs[0].flag, "UR")
        self.assertTrue(runs[1].earned_team)


class UntrustedState(unittest.TestCase):
    def test_one_unparsed_play_grades_the_whole_half_inning(self):
        # The reconstruction cannot count an out it cannot see, and one
        # uncounted out is the difference between an inning that ended in the
        # reconstruction and one that did not.
        runs = verdicts(["D7", "S8.2-H"], trusted=[False, True])
        self.assertEqual(runs[0].certainty, ER.UNTRUSTED)


class RecordedFlags(unittest.TestCase):
    def test_the_flags_are_carried_through_and_never_read(self):
        # The whole check depends on this: `(UR)` is the answer key, so it
        # must reach the verdict untouched and must not reach the rule.
        [run] = verdicts(["D7", "S8.2-H(UR)"])
        self.assertEqual(run.recorded, "UR")
        self.assertTrue(run.earned_team)   # derived independently, and wrong
        self.assertNotEqual(run.flag, run.recorded)

    def test_tur_and_ur_are_read_apart(self):
        self.assertEqual(ER.movement_flags(parse("S8.3-H(TUR)").event
                                           .advances[0])[2], "TUR")
        self.assertEqual(ER.movement_flags(parse("S8.3-H(UR)").event
                                           .advances[0])[2], "UR")
        self.assertEqual(ER.movement_flags(parse("S8.3-H").event
                                           .advances[0])[2], "")


class TheLoader(unittest.TestCase):
    """Feeding the rule from the derived tables (rsse/database/earnedruns.py).

    Separate from the rule's own tests because the failure modes are
    different. The rule can be right and the loader still hand it the wrong
    runner, the wrong pitcher, or -- as happened -- the `(UR)` flag in the
    field next to the one it belongs in.
    """

    PLAYS = [
        # play_id, seq, inning, half, batting_team, batter, event, outs_before,
        # outs_recorded, bases_before
        (1, 1, 1, "top", 0, "bat1", "D7", 0, 0, "000"),
        (2, 2, 1, "top", 0, "bat2", "S8.2-H(UR)", 0, 0, "010"),
        (3, 3, 1, "top", 0, "bat3", "K", 0, 1, "100"),
    ]
    ADVANCES = [
        # play_id, seq, runner, origin, dest, marked_out, is_out, scored, raw
        (1, 0, "bat1", "B", "2", 0, 0, 0, "B-2"),
        (2, 0, "bat1", "2", "H", 0, 0, 1, "2-H(UR)"),
        (2, 1, "bat2", "B", "1", 0, 0, 0, "B-1"),
    ]

    def build(self, *, batting_team=0, lineup=(("start", 0, "ace"),)):
        import sqlite3

        from rsse.database.earnedruns import EARNED_DDL, _derive_batch
        from rsse.database.schema import DERIVED_DDL
        from rsse.database.secondary import SECONDARY_DDL

        conn = sqlite3.connect(":memory:")
        conn.executescript(DERIVED_DDL + SECONDARY_DDL + EARNED_DDL)
        conn.execute("INSERT INTO games (game_key, game_id, season)"
                     " VALUES (1, 'TST190001010', 1900)")
        for (pid, seq, inning, half, _team, batter, event, ob, orec,
             bases) in self.PLAYS:
            conn.execute(
                "INSERT INTO plays (play_id, game_key, game_id, seq,"
                " record_id, inning, half, batting_team, batter_id,"
                " event_raw, event_basic, event_modifiers, event_advances,"
                " annotations, outs_before, outs_recorded, bases_before,"
                " parser_version) VALUES (?,1,'TST190001010',?,?,?,?,?,?,?,"
                "'','','','',?,?,?,'t')",
                (pid, seq, pid, inning, half, batting_team, batter, event,
                 ob, orec, bases))
        conn.executemany(
            "INSERT INTO runner_advances (play_id, seq, runner_id, origin,"
            " destination, is_explicit, marked_out, is_out, scored, raw)"
            " VALUES (?,?,?,?,?,1,?,?,?,?)", self.ADVANCES)
        for seq, (kind, after, player) in enumerate(lineup):
            conn.execute(
                "INSERT INTO lineup_entries (game_key, seq, is_sub, play_id,"
                " player_id, player_name, team, batting_order, position)"
                " VALUES (1,?,?,?,?,'n',?,9,1)",
                (seq, int(kind == "sub"), after or None, player,
                 1 - batting_team))
        stats = __import__("rsse.database.earnedruns", fromlist=["x"]) \
            .BuildStats()
        return _derive_batch(conn, [1], [], stats)

    def rows(self, **kwargs):
        [(_gk, rows)] = self.build(**kwargs)
        return rows

    def test_the_recorded_flag_is_read_but_not_obeyed(self):
        # The loader must carry `(UR)` through from `runner_advances.raw` and
        # the rule must not see it. Both halves matter: a silent failure to
        # read it makes every game look like agreement, and that is exactly
        # what a positional-argument slip produced once.
        [row] = self.rows()
        self.assertEqual(row[-1], "UR")           # recorded
        self.assertEqual(row[8], 1)               # earned_team, derived anyway

    def test_the_batting_side_is_stored_not_inferred(self):
        # The same half-inning, batted by the home team. 51 games in the
        # corpus do this, and reading the side off `half` charges their
        # earned runs to the wrong pitchers while the totals still add up.
        [top] = self.rows(batting_team=0)
        [bottom] = self.rows(batting_team=1)
        self.assertEqual(top[4], "top")
        self.assertEqual(top[5], 0)
        self.assertEqual(bottom[5], 1)

    def test_the_charged_pitcher_comes_from_the_lineup_timeline(self):
        [row] = self.rows()
        self.assertEqual(row[7], "ace")

    def test_a_reliever_who_arrives_later_is_not_charged(self):
        # 9.16(f). He entered after play 1, where the runner was already on
        # second, so the run is the starter's.
        [row] = self.rows(lineup=(("start", 0, "ace"), ("sub", 1, "reliever")))
        self.assertEqual(row[7], "ace")


class Bounds(unittest.TestCase):
    """Checking a published total against an interval, not a number."""

    def score(self, low, high, published):
        from rsse.database.earnedruns import TotalLevel, _check
        level = TotalLevel("t")
        _check(level, low, high, published)
        return level

    def test_an_exact_bound_is_compared_exactly(self):
        level = self.score(3, 3, 3)
        self.assertEqual((level.exact, level.exact_agree, level.within),
                         (1, 1, 1))

    def test_a_published_total_inside_the_bound_passes(self):
        level = self.score(2, 4, 3)
        self.assertEqual(level.within, 1)
        # ...and is not counted among the exact comparisons, because the
        # derivation declined to name a number.
        self.assertEqual(level.exact, 0)

    def test_a_published_total_outside_the_bound_fails_with_its_distance(self):
        level = self.score(2, 4, 6)
        self.assertEqual(level.outside, 1)
        self.assertEqual(level.spread[2], 1)


class ProlongedAtBats(unittest.TestCase):
    """A muffed foul fly taints the batter until his at bat ends, and no
    longer (9.16(a)(2)(i)).

    The at bat can run several plays past the muff -- a steal, a wild pitch --
    and each of those is its own `play` record with the same batter on it.
    """

    def test_the_taint_survives_a_play_inside_the_at_bat(self):
        # `a`'s foul fly is muffed, a passed ball happens while he is still
        # at bat, and only then does he single. He is the runner who scores.
        runs = verdicts(["FLE5/FL", "PB", "S8", "S8.1-3", "S8.3-H"],
                        batters=["a", "a", "a", "b", "c"])
        self.assertEqual(runs[-1].runner_id, "a")
        self.assertEqual(runs[-1].reason,
                         "at bat prolonged by a muffed foul fly")

    def test_the_taint_does_not_outlive_the_at_bat(self):
        # The muffed foul fly is `a`'s. `b` reaching later is unaffected, and
        # scores earned -- against an inning the muff left one out further on.
        runs = verdicts(["FLE5/FL", "K", "D7", "S8.2-H"],
                        batters=["a", "a", "b", "c"])
        self.assertEqual(runs[-1].reason, "scored without aid")
        self.assertTrue(runs[-1].earned_team)


class MuffedFoulFlyLeavesTheBatterAtThePlate(unittest.TestCase):
    """`FLE$` does not put the batter on first (03-STATE §2 rule 2).

    The rule listed it among the events that do, and the corpus is unanimous
    the other way: all 8,562 `FLE` plays are followed by another play with the
    same batter. Placing him on first leaves a phantom runner for the rest of
    the half-inning. Nothing else in the project can see that -- outs still
    balance, and a runner no advance ever names never scores, so score
    reconciliation stays at 100%.
    """

    def test_the_batter_is_not_placed(self):
        from rsse.model.state import batter_destination
        self.assertIsNone(batter_destination(parse("FLE5/FL").event))

    def test_the_bases_are_unchanged(self):
        state = HalfInningState()
        after, outcome = apply_play(state, parse("FLE5/FL").event,
                                    batter_id="a")
        self.assertEqual(after.code(), "000")
        self.assertEqual(outcome.advances, [])
        self.assertEqual(outcome.outs_recorded, 0)

    def test_an_error_on_a_foul_fly_is_still_an_error(self):
        # The fix must not lose the error: it is the missed chance 9.16
        # counts, and the reconstruction owes an out for it.
        facts = half(["FLE5/FL"])[0]
        self.assertTrue(facts.foul_fly_error)
        self.assertTrue(facts.play_level_error)
