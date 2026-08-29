"""Trivia handling, especially the `+` / `-` overloads (spec/02-GRAMMAR.md §3).

These two characters are the likeliest source of a silent mis-parse, so they
get dedicated tests as the spec requires.
"""

import unittest

from rsse.parser.lexer import reinsert_trivia, strip_trivia


class StripTrivia(unittest.TestCase):
    def assert_clean(self, raw, expected_clean):
        clean, trivia = strip_trivia(raw)
        self.assertEqual(clean, expected_clean)
        self.assertEqual(reinsert_trivia(clean, trivia), raw)

    def test_always_trivia(self):
        self.assert_clean("PB.2-3#", "PB.2-3")
        self.assert_clean("S8/L!", "S8/L")
        self.assert_clean("K?", "K")

    def test_plus_as_event_joiner_is_kept(self):
        self.assert_clean("K+WP.B-1", "K+WP.B-1")
        self.assert_clean("W+PB.2-3", "W+PB.2-3")
        self.assert_clean("SB2+E2", "SB2+E2")

    def test_plus_as_hard_hit_marker_is_trivia(self):
        self.assert_clean("S7/G6+", "S7/G6")
        self.assert_clean("D7/L78S+", "D7/L78S")
        self.assert_clean("S8/L+.1-2", "S8/L.1-2")

    def test_minus_as_advance_operator_is_kept(self):
        self.assert_clean("S8.3-H;1-2", "S8.3-H;1-2")
        self.assert_clean("K.B-1", "K.B-1")

    def test_minus_as_soft_hit_marker_is_trivia(self):
        self.assert_clean("S7/G6-", "S7/G6")
        self.assert_clean("S7/G6-.1-2", "S7/G6.1-2")

    def test_trivia_positions_survive_multiple_marks(self):
        raw = "S8/L78+#.2-H!;1-3"
        clean, trivia = strip_trivia(raw)
        self.assertEqual(clean, "S8/L78.2-H;1-3")
        self.assertEqual(reinsert_trivia(clean, trivia), raw)


if __name__ == "__main__":
    unittest.main()
