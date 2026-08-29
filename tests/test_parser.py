"""Parser tests: every example in the Retrosheet documentation, plus the
resolution rules that the grammar spec calls out as ambiguous.
"""

import unittest

from rsse.parser import grammar as G
from rsse.parser.parser import ParseError, parse

#: Every event string appearing as an example in the Retrosheet event file
#: documentation. Each must parse and round-trip.
DOC_EXAMPLES = [
    "S8.3-H;1-2", "8/F78", "9/SF.3-H", "3/G.2-3", "63/G6M", "143/G1",
    "54(B)/BG25/SH.1-2", "54(1)/FO/G5.3-H;B-1", "23/SH.1-2",
    "64(1)3/GDP/G6", "4(1)3/G4/GDP", "8(B)84(2)/LDP/L8", "3(B)3(1)/LDP",
    "1(B)16(2)63(1)/LTP/L1", "C/E2.1-2", "S7", "D7/G5.3-H;2-H;1-H",
    "T9/F9LD.2-H", "DGR/L9LS.2-H", "E1/TH/BG15.1-3", "E3.1-2;B-1",
    "FC5/G5.3XH(52)", "FC3/G3S.3-H;1-2", "FLE5/P5F", "H/L7D",
    "HR/F78XD.2-H;1-H", "HR9/F9LS.3-H;1-H", "HP.1-2", "K", "K23",
    "K+PB.1-2", "K+WP.B-1", "K23+WP.2-3", "NP", "W.1-2", "IW",
    "W+WP.2-3", "BK.3-H;1-2", "CSH(12)", "CS2(24).2-3", "CS2(2E4).1-3",
    "DI.1-2", "OA.2X3(25)", "WP.2-3;1-2", "PB.2-3", "PO2(14)",
    "PO1(E3).1-2", "POCS2(1361)", "SB2", "SB3;SB2", "SBH;SB2",
    "W.2-3;1-2", "S7/F7S.2-H;B-2", "K/DP.1X2(26)", "9/F9LS/FDP.3XH(92)",
    "S8/L78.BX2(8434)", "S7/L7LD.3-H;2-H;BX2(7E4)", "S5/G5.1-3(E5/TH)",
    "W+PB.3-H(NR);1-3", "S4/G34.2-H(E4/TH)(UR)(NR);1-3;B-2",
    "E6/G6.3-H(RBI);2-3;B-1", "S/L9S.3-H;2X3(5/INT);1-2",
    "S9.3-H(TUR);2-H(TUR);1-3;BX2(93)", "S8.2-H;BX2(8U3)", "K.1-2(WP)",
    "D8/78",
]

#: Constructs found in the corpus that the published documentation omits.
CORPUS_EXAMPLES = [
    "K/BF", "K/S", "K/NDP.3XH(21)", "SBH(UR);SB2", "99(1)/FO",
    "D7/F7D/R62.1-H", "7/F78XDW", "9/F9DW", "S7/L7DW", "D8/78XDW.2-H",
    "3E1", "S/78XDW",
    # Found by the full-corpus sweep; all absent from the documentation.
    "34/SH/B.1-2", "FC3/B/SH.1-2;B-1", "63/B6S/SH.3-H(UR)",
    "1/G/SH/B.1-2", "E1/TH/SH/B.1-3;B-2",
    "13/R6524", "HR9/F9LS/IPHR/R652", "PO2(E2)/R862.2-H(UR)",
    "DGR9.2-H", "DGR7.2-H;1-3",
]


class RoundTrip(unittest.TestCase):
    def test_documentation_examples(self):
        for event in DOC_EXAMPLES:
            with self.subTest(event=event):
                self.assertEqual(parse(event).emit(), event)

    def test_corpus_examples(self):
        for event in CORPUS_EXAMPLES:
            with self.subTest(event=event):
                self.assertEqual(parse(event).emit(), event)


class Resolution(unittest.TestCase):
    """The longest-match and lookahead rules of spec/02-GRAMMAR.md §2.1."""

    def test_hp_is_not_a_home_run(self):
        ev = parse("HP.1-2").event
        self.assertIsInstance(ev.basics[0], G.HitByPitch)

    def test_hr_before_h(self):
        self.assertEqual(parse("HR/F78XD").event.basics[0].code, "HR")
        self.assertEqual(parse("H/L7D").event.basics[0].code, "H")

    def test_inside_the_park_home_run_names_a_fielder(self):
        self.assertTrue(parse("HR9/F9LS").event.basics[0].inside_the_park)
        self.assertFalse(parse("HR/F9LS").event.basics[0].inside_the_park)

    def test_error_distinguished_from_ground_out(self):
        reached = parse("3E1").event.basics[0]
        self.assertIsInstance(reached, G.ReachedOnError)
        self.assertEqual((reached.assists, reached.fielder), ("3", "1"))
        self.assertIsInstance(parse("31").event.basics[0], G.Out)

    def test_iw_before_i_before_w(self):
        self.assertEqual(parse("IW").event.basics[0].code, "IW")
        self.assertTrue(parse("I").event.basics[0].intentional)
        self.assertFalse(parse("W").event.basics[0].intentional)

    def test_pocs_before_po(self):
        self.assertEqual(parse("POCS2(1361)").event.basics[0].code, "POCS")
        self.assertEqual(parse("PO2(14)").event.basics[0].code, "PO")

    def test_bunt_trajectory_does_not_shadow_bg_bp_bl(self):
        """`B` is a trajectory; it must be matched after the longer codes."""
        self.assertEqual(parse("34/SH/B").event.modifiers[1].trajectory, "B")
        self.assertEqual(parse("54(B)/BG25").event.modifiers[0].trajectory, "BG")
        self.assertEqual(parse("63/B6S").event.modifiers[0].location, "6S")

    def test_ground_rule_double_may_name_a_fielder(self):
        """The documentation says it never does; the corpus disagrees."""
        self.assertEqual(parse("DGR9.2-H").event.basics[0].fielders, "9")
        self.assertEqual(parse("DGR/L9LS").event.basics[0].fielders, "")

    def test_relay_takes_any_number_of_fielders(self):
        self.assertEqual(parse("13/R6524").event.modifiers[0].fielders, "6524")

    def test_unknown_play_code(self):
        out = parse("99(1)/FO").event.basics[0]
        self.assertTrue(out.is_unknown)


class Structure(unittest.TestCase):
    def test_semicolon_separates_basic_events_before_the_dot(self):
        """`SB3;SB2` is two events, not an advance list (§1)."""
        ev = parse("SB3;SB2").event
        self.assertEqual(len(ev.groups), 2)
        self.assertEqual(ev.advances, ())
        self.assertEqual([b.base for b in ev.basics], ["3", "2"])

    def test_plus_joins_events_slash_introduces_modifiers(self):
        ev = parse("K+WP.B-1").event
        self.assertEqual(len(ev.groups), 1)
        self.assertEqual(len(ev.groups[0]), 2)
        self.assertEqual(parse("K/DP.1X2(26)").event.modifier_codes(), {"DP"})

    def test_putout_sequence_with_runner_designators(self):
        groups = parse("64(1)3/GDP").event.basics[0].groups
        self.assertEqual(
            [(g.fielders, g.runner) for g in groups], [("64", "1"), ("3", None)]
        )

    def test_multiple_advance_parameters_each_bracketed(self):
        adv = parse("S4/G34.2-H(E4/TH)(UR)(NR);1-3;B-2").event.advances[0]
        self.assertEqual(len(adv.params), 3)
        self.assertTrue(adv.params[0].has_error)
        self.assertEqual([p.text for p in adv.params[1:]], ["UR", "NR"])

    def test_credit_sequence_assigns_putout_to_last_atom(self):
        adv = parse("K.3XH(21)").event.advances[0]
        seq = adv.params[0]
        self.assertEqual([c.fielder for c in seq.credits], ["2", "1"])

    def test_unknown_fielder_u(self):
        seq = parse("S8.2-H;BX2(8U3)").event.advances[1].params[0]
        self.assertEqual([c.fielder for c in seq.credits], ["8", "U", "3"])


class ErrorNegatesOut(unittest.TestCase):
    """An `E` in a credit sequence negates the out, however it is written (§5)."""

    def test_advance_marked_x_with_error_is_not_an_out(self):
        adv = parse("S7/L7LD.3-H;2-H;BX2(7E4)").event.advances[2]
        self.assertTrue(adv.marked_out)
        self.assertFalse(adv.is_out)

    def test_advance_marked_x_without_error_is_an_out(self):
        adv = parse("S8/L78.BX2(8434)").event.advances[0]
        self.assertTrue(adv.is_out)

    def test_caught_stealing_negated_by_error(self):
        seq = parse("CS2(2E4).1-3").event.basics[0].params[0]
        self.assertTrue(seq.has_error)

    def test_pickoff_negated_by_error(self):
        seq = parse("PO1(E3).1-2").event.basics[0].params[0]
        self.assertTrue(seq.has_error)


class Failure(unittest.TestCase):
    """The parser never guesses; it reports an offset (§8)."""

    def test_rejects_unknown_modifier(self):
        with self.assertRaises(ParseError):
            parse("K/ZZZ")

    def test_rejects_garbage(self):
        with self.assertRaises(ParseError):
            parse("!!!")

    def test_rejects_trailing_input(self):
        with self.assertRaises(ParseError):
            parse("K.3XH(21)qqq")


if __name__ == "__main__":
    unittest.main()
