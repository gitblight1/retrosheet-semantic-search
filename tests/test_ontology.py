"""Ontology tests (spec/07-TESTING.md §1).

> The negative case per tag is not optional. `K.1X2(26)` must not be tagged
> `UncaughtThirdStrike` -- a rule that only ever sees positives will happily
> over-fire.

So the table below carries **both** for every tag, and
:meth:`Coverage.test_every_tag_has_both_cases` fails if a tag is added without
them. That check is the point of the file: a rule with no negative is the
failure mode this project has already been bitten by, and the ontology is the
layer where over-firing is cheapest to introduce and hardest to notice.
"""

import unittest

from rsse.model.state import HalfInningState, PlayContext, Runner, apply_play
from rsse.parser.parser import parse
from rsse.semantic import ontology as O
from rsse.semantic.derive import derive, load_curated, tag_rows


def case(event, bases="", outs=0, **context):
    return (event, bases, outs, context)


def tags_for(event, bases="", outs=0, **context):
    """Derive tags for one event against a stated base/out state."""
    state = HalfInningState(outs=outs)
    for base in bases:
        state.bases[base] = Runner("runner_" + base)
    parsed = parse(event)
    _after, outcome = apply_play(state, parsed.event)
    return derive(parsed.event, outcome, PlayContext(**context), parsed.trivia)


def names(event, bases="", outs=0, **context):
    return {t.name for t in tags_for(event, bases, outs, **context)}


#: One positive and one negative per tag. The negative is chosen to be the
#: nearest plausible miss rather than an unrelated play: `D7` for `Single`,
#: `K` for `UncaughtThirdStrike`, `CS2(2E6)` for `CaughtStealing`.
CASES = {
    # -- §2 batting ------------------------------------------------------
    "Single":               ([case("S7")], [case("D7")]),
    "Double":               ([case("D7"), case("DGR")], [case("S7")]),
    "Triple":               ([case("T9")], [case("D7")]),
    "GroundRuleDouble":     ([case("DGR"), case("DGR7")], [case("D7")]),
    "HomeRun":              ([case("HR/F78"), case("H7")], [case("T9")]),
    "InsideTheParkHomeRun": ([case("H7"), case("HR/IPHR")], [case("HR/F78")]),
    "Walk":                 ([case("W"), case("IW")], [case("HP")]),
    "IntentionalWalk":      ([case("IW"), case("I")], [case("W")]),
    "HitByPitch":           ([case("HP")], [case("W")]),
    "Strikeout":            ([case("K"), case("K+WP")], [case("W")]),
    "ReachedOnError":       ([case("E6/G6")], [case("63/G6")]),
    "FieldersChoice":       ([case("FC6/G6.1-2", "1")], [case("63/G6")]),
    "SacrificeFly":         ([case("8/SF.3-H", "3")], [case("8/F8")]),
    "SacrificeHit":         ([case("13/SH.1-2", "1")], [case("13/G1", "1")]),
    # Bare forms included deliberately: `/G` and `/G6` are the same fact, and
    # a classification bug once made only the located form fire.
    "GroundOut":            ([case("63/G6"), case("63/G")], [case("8/F8")]),
    "FlyOut":               ([case("8/F8"), case("8/F")], [case("63/G6")]),
    "LineOut":              ([case("7/L7"), case("7/L")], [case("8/F8")]),
    "PopOut":               ([case("3/P3"), case("3/P")], [case("8/F8")]),
    "Bunt":                 ([case("13/BG1.1-2", "1"), case("13/B1/SH.1-2", "1"),
                              case("13/BG.1-2", "1"), case("13/B.1-2", "1")],
                             [case("63/G6"), case("63/G")]),
    "InfieldFly":           ([case("4/IF/P4")], [case("8/F8")]),

    # -- §3 strikeout family ---------------------------------------------
    "UncaughtThirdStrike":  ([case("K23"), case("K+WP"), case("K+PB"),
                              case("K+E2"), case("K.B-1"), case("K.1-2(WP)", "1")],
                             [case("K"), case("K/DP.1X2(26)", "1"),
                              case("K/NDP.3XH(21)", "13", 1)]),
    "BatterReachedOnK":     ([case("K.B-1"), case("K+WP.B-1")],
                             [case("K23"), case("K")]),
    "DroppedThirdStrike":   ([case("K23")], [case("K")]),
    "StrikeoutDoublePlay":  ([case("K/DP.1X2(26)", "1")],
                             [case("K"), case("K/NDP.3XH(21)", "13", 1)]),
    "CalledThirdStrike":    ([case("K/C")], [case("K/S")]),
    "StrikeoutThrowOut":    ([case("K.1X2(26)", "1")], [case("K")]),

    # -- §4 base running -------------------------------------------------
    "StolenBase":           ([case("SB2", "1")], [case("CS2(26)", "1")]),
    "DoubleSteal":          ([case("SB3;SB2", "12")], [case("SB2", "1")]),
    "CaughtStealing":       ([case("CS2(26)", "1")], [case("CS2(2E6)", "1"),
                                                      case("SB2", "1")]),
    "CaughtStealingSafeOnError": ([case("CS2(2E6)", "1")], [case("CS2(26)", "1")]),
    "Pickoff":              ([case("PO1(13)", "1")], [case("PO1(E3)", "1")]),
    "PickoffError":         ([case("PO1(E3)", "1")], [case("PO1(13)", "1")]),
    "PickoffCaughtStealing": ([case("POCS2(1361)", "1")], [case("CS2(26)", "1")]),
    "DefensiveIndifference": ([case("DI.1-2", "1")], [case("SB2", "1")]),
    "WildPitch":            ([case("WP.2-3", "2"), case("K+WP"),
                              case("BK.2-3(WP)", "2")],
                             [case("PB.2-3", "2")]),
    "PassedBall":           ([case("PB.2-3", "2"), case("K+PB")],
                             [case("WP.2-3", "2")]),
    "Balk":                 ([case("BK.1-2", "1")], [case("WP.1-2", "1")]),
    "OtherAdvance":         ([case("OA.1-2", "1")], [case("WP.1-2", "1")]),
    "RunnerPassedRunner":   ([case("S8/PASS.1-2", "1")], [case("S8.1-2", "1")]),
    "RunnerHitByBattedBall": ([case("4/BR")], [case("4/G4")]),

    # -- §5 outs and force plays -----------------------------------------
    "ForceOut":             ([case("64(1)3/GDP/G6", "1"),
                              case("K.B-1;3XH(21)", "123", 2)],
                             [case("8/F8"), case("K/NDP.3XH(21)", "13", 1),
                              case("8(B)84(2)/LDP/L8", "2")]),
    "ForceOutAtHome":       ([case("K.B-1;3XH(21)", "123", 2)],
                             [case("K.2XH(21);B-1", "2", 1),
                              case("K/NDP.3XH(21)", "13", 1)]),
    "ForceOutAtThird":      ([case("54(2)/FO/G5", "12")],
                             [case("64(1)3/GDP/G6", "1")]),
    "ForceOutAtSecond":     ([case("64(1)3/GDP/G6", "1")], [case("8/F8")]),
    "ForceOutAtFirst":      ([case("63/G6")], [case("8/F8")]),
    "TagOut":               ([case("K/NDP.3XH(21)", "13", 1),
                              case("8(B)84(2)/LDP/L8", "2")],
                             [case("63/G6"), case("8/F8")]),
    "AppealOut":            ([case("64(1)/AP/G6", "1")],
                             [case("64(1)3/GDP/G6", "1")]),
    "DoublePlay":           ([case("64(1)3/GDP/G6", "1"), case("K/DP.1X2(26)", "1")],
                             [case("63/G6"), case("K/NDP.3XH(21)", "13", 1)]),
    "TriplePlay":           ([case("1(B)16(2)63(1)/LTP/L1", "12")],
                             [case("64(1)3/GDP/G6", "1")]),
    "UnassistedOut":        ([case("8/F8")], [case("63/G6")]),
    "Rundown":              ([case("CS2(2626)", "1")], [case("CS2(26)", "1")]),
    "RelayThrow":           ([case("D7/R6.1-H", "1")], [case("D7.1-H", "1")]),

    # -- §6 special ------------------------------------------------------
    "CatcherInterference":  ([case("C/E2")], [case("C/E1")]),
    "PitcherInterference":  ([case("C/E1")], [case("C/E2")]),
    "FirstBaseInterference": ([case("C/E3")], [case("C/E2")]),
    "BatterInterference":   ([case("2/BINT")], [case("2/F2")]),
    "RunnerInterference":   ([case("4(1)/RINT", "1")], [case("4(1)/G4", "1")]),
    "UmpireInterference":   ([case("2/UINT")], [case("2/F2")]),
    "FanInterference":      ([case("D7/FINT")], [case("D7")]),
    "Obstruction":          ([case("S7/OBS.1-3", "1")], [case("S7.1-3", "1")]),
    "BattingOutOfTurn":     ([case("63/G6/BOOT"), case("63/G6", after_ladj=True)],
                             [case("63/G6")]),
    "CourtesyRunner":       ([case("63/G6/COUR")], [case("63/G6")]),
    "CourtesyBatter":       ([case("63/G6/COUB")], [case("63/G6")]),
    "CourtesyFielder":      ([case("63/G6/COUF")], [case("63/G6")]),
    "FoulFlyError":         ([case("FLE5")], [case("E5/G5")]),
    "ErrorOnThrow":         ([case("S7.1XH(E5/TH)", "1")], [case("S7.1X3(65)", "1")]),
    "UnknownPlay":          ([case("99")], [case("63/G6")]),
    "ReplayReviewed":       ([case("63/G6/UREV"), case("63/G6/MREV")],
                             [case("63/G6")]),
    "ReplayOverturned":     ([case("63/G6/UREV", replay_reversed=True)],
                             [case("63/G6/UREV"),
                              case("63/G6/UREV", replay_reversed=False),
                              case("63/G6", replay_reversed=True)]),
    "PlacedRunner":         ([case("63/G6", "2", has_placed_runner=True)],
                             [case("63/G6", "2")]),

    # -- §7 context ------------------------------------------------------
    "BasesLoaded":          ([case("63/G6", "123")], [case("63/G6", "12")]),
    "BasesEmpty":           ([case("63/G6")], [case("63/G6", "1")]),
    "RunnerOnThird":        ([case("63/G6", "3")], [case("63/G6", "1")]),
    "ScoringPosition":      ([case("63/G6", "2"), case("63/G6", "3")],
                             [case("63/G6", "1"), case("63/G6")]),
    "NoOuts":               ([case("63/G6", "", 0)], [case("63/G6", "", 1)]),
    "OneOut":               ([case("63/G6", "", 1)], [case("63/G6", "", 0)]),
    "TwoOuts":              ([case("63/G6", "", 2)], [case("63/G6", "", 0)]),
    "InningEnding":         ([case("63/G6", "", 2)], [case("63/G6", "", 0)]),
    "GoAheadRun":           ([case("S7.3-H", "3", is_go_ahead=True)],
                             [case("S7.3-H", "3")]),
    "WalkOff":              ([case("S7.3-H", "3", is_walkoff=True)],
                             [case("S7.3-H", "3")]),
    "FinalPlay":            ([case("63/G6", is_final_play=True)], [case("63/G6")]),
    "ExtraInnings":         ([case("63/G6", inning=10)],
                             [case("63/G6", inning=9)]),
    "LateAndClose":         ([case("63/G6", inning=8),
                              case("63/G6", inning=9, score_batting_before=1),
                              case("63/G6", "1", inning=9, score_fielding_before=2)],
                             [case("63/G6", inning=3),
                              case("63/G6", inning=9, score_batting_before=5),
                              case("63/G6", inning=9, score_fielding_before=6)]),
}


class TagCases(unittest.TestCase):
    def test_positives_fire(self):
        for name, (positives, _negatives) in CASES.items():
            for event, bases, outs, context in positives:
                with self.subTest(tag=name, event=event, bases=bases, outs=outs):
                    self.assertIn(name, names(event, bases, outs, **context))

    def test_negatives_do_not_fire(self):
        for name, (_positives, negatives) in CASES.items():
            for event, bases, outs, context in negatives:
                with self.subTest(tag=name, event=event, bases=bases, outs=outs):
                    self.assertNotIn(name, names(event, bases, outs, **context))


class Coverage(unittest.TestCase):
    """The gate: no derivable tag may exist without both kinds of case."""

    def test_every_tag_has_both_cases(self):
        for td in O.derivable():
            with self.subTest(tag=td.name):
                self.assertIn(td.name, CASES, "tag has no test cases")
                positives, negatives = CASES[td.name]
                self.assertTrue(positives, "tag has no positive case")
                self.assertTrue(negatives, "tag has no negative case")

    def test_no_stale_cases(self):
        for name in CASES:
            with self.subTest(tag=name):
                self.assertIn(name, O.REGISTRY)

    def test_curated_only_tags_never_derive(self):
        """§8: a curated tag is never produced by the deriver.

        `HiddenBallTrick` has no Retrosheet encoding at all, so it exists only
        as a name for a curated annotation to attach to.
        """
        curated = [td for td in O.REGISTRY.values() if td.curated_only]
        self.assertTrue(curated, "expected at least one curated-only tag")
        for event in ("63/G6", "K23", "PO1(13)", "S7.1-3", "99"):
            fired = names(event, "1", 0)
            for td in curated:
                with self.subTest(tag=td.name, event=event):
                    self.assertNotIn(td.name, fired)


class Structure(unittest.TestCase):
    """§1: the properties that make this an ontology rather than a word list."""

    def test_every_tag_has_a_rule(self):
        for name, td in O.REGISTRY.items():
            with self.subTest(tag=name):
                self.assertTrue(callable(td.rule))

    def test_every_tag_cites_a_spec_section(self):
        for name, td in O.REGISTRY.items():
            with self.subTest(tag=name):
                self.assertRegex(td.spec, r"^§\d")

    def test_rule_hashes_are_distinct_per_rule(self):
        """A shared hash would mean two tags share a derivation."""
        by_hash = {}
        for name, td in O.REGISTRY.items():
            by_hash.setdefault(td.rule_hash, []).append(name)
        collisions = {h: n for h, n in by_hash.items() if len(n) > 1}
        self.assertEqual(collisions, {})

    def test_rule_hash_tracks_the_rule(self):
        def rule_a(f):
            return True

        def rule_b(f):
            return False

        self.assertNotEqual(O.rule_hash(rule_a), O.rule_hash(rule_b))
        self.assertEqual(O.rule_hash(rule_a), O.rule_hash(rule_a))

    def test_implications_name_registered_tags(self):
        for name, td in O.REGISTRY.items():
            for implied in td.implies:
                with self.subTest(tag=name, implies=implied):
                    self.assertIn(implied, O.REGISTRY)

    def test_implications_do_not_cycle(self):
        for name in O.REGISTRY:
            seen, pending = {name}, list(O.REGISTRY[name].implies)
            while pending:
                nxt = pending.pop()
                self.assertNotIn(nxt, seen, f"{name} implication cycle at {nxt}")
                seen.add(nxt)
                pending.extend(O.REGISTRY[nxt].implies)

    def test_aliases_target_registered_tags(self):
        self.assertTrue(O.aliases())
        for alias, target in O.aliases().items():
            with self.subTest(alias=alias):
                self.assertIn(target, O.REGISTRY)

    def test_deprecated_alias_fires_with_its_target(self):
        """`DroppedThirdStrike` must track `UncaughtThirdStrike` exactly."""
        for event, bases, outs in (("K23", "", 0), ("K", "", 0),
                                   ("K+WP.B-1", "", 0),
                                   ("K/DP.1X2(26)", "1", 0)):
            with self.subTest(event=event):
                fired = names(event, bases, outs)
                self.assertEqual("DroppedThirdStrike" in fired,
                                 "UncaughtThirdStrike" in fired)

    def test_derivation_is_deterministic(self):
        for event, bases, outs in (("K.B-1;3XH(21)", "123", 2),
                                   ("64(1)3/GDP/G6", "1", 0)):
            with self.subTest(event=event):
                first = names(event, bases, outs)
                self.assertEqual(first, names(event, bases, outs))

    def test_tag_rows_cover_the_registry(self):
        rows = tag_rows()
        self.assertEqual(len(rows), len(O.REGISTRY))
        self.assertEqual({r["name"] for r in rows}, set(O.REGISTRY))
        for row in rows:
            self.assertEqual(row["version"], O.ONTOLOGY_VERSION)
            self.assertTrue(row["rule_hash"])


class Confidence(unittest.TestCase):
    """§1.4: the three sources of uncertainty, and nothing else."""

    def _confidence(self, tag, event, bases="", outs=0, **context):
        by_name = {t.name: t for t in tags_for(event, bases, outs, **context)}
        return by_name[tag].confidence

    def test_default_is_certain(self):
        self.assertEqual(self._confidence("ForceOutAtHome",
                                          "K.B-1;3XH(21)", "123", 2), "certain")

    def test_a_likely_force_is_still_returned_by_default(self):
        """`64(1)3` gives no trajectory, so rule 5 reads the force off the throw.

        That is `likely`, not `ambiguous`, and a `likely` force is `certain` at
        the tag level: tag confidence is binary and `uncertain` means excluded
        from default results (spec/06-QUERY.md §4). Excluding these would drop
        about half the force outs at first in the pre-1970s corpus, which is
        the wrong answer to give silently. The three-way distinction lives on
        `runner_advances.force_certainty`, where it can be interrogated.
        """
        for event in ("64(1)3", "64(1)3/GDP/G6"):
            with self.subTest(event=event):
                self.assertEqual(
                    self._confidence("ForceOutAtSecond", event, "1"), "certain")

    def test_the_advance_still_distinguishes_likely_from_derived(self):
        from rsse.model.state import HalfInningState, Runner, apply_play
        from rsse.parser.parser import parse as _parse

        def force_certainty(event):
            state = HalfInningState()
            state.bases["1"] = Runner("r1")
            _after, outcome = apply_play(state, _parse(event).event)
            return {a.origin: a.force_certainty
                    for a in outcome.advances if a.is_force}

        self.assertEqual(force_certainty("64(1)3")["1"], "likely")
        self.assertEqual(force_certainty("64(1)3/GDP/G6")["1"], "derived")

    def test_hash_annotation_makes_the_play_uncertain(self):
        self.assertEqual(self._confidence("Single", "S7/L6#"), "uncertain")
        self.assertEqual(self._confidence("Single", "S7/L6"), "certain")

    def test_unknown_play_is_always_uncertain(self):
        self.assertEqual(self._confidence("UnknownPlay", "99"), "uncertain")

    def test_prior_state_tags_survive_a_questionable_record(self):
        """A `99` or `#` questions *this* play, not the state entering it.

        The base/out state a batter walked into was established by earlier
        plays. Marking `RunnerOnThird` uncertain because the record of what he
        then did is questionable would exclude it from default query results
        (spec/06-QUERY.md §4) for no reason.
        """
        for event in ("99/SH.3-H", "8/F8#.3-H"):
            with self.subTest(event=event):
                by_name = {t.name: t.confidence
                           for t in tags_for(event, "3", 1)}
                self.assertEqual(by_name["OneOut"], "certain")
                self.assertEqual(by_name["RunnerOnThird"], "certain")
                self.assertEqual(by_name["ScoringPosition"], "certain")

    def test_this_plays_own_tags_do_not_survive_it(self):
        """The complement: what happened *is* in doubt, and says so."""
        by_name = {t.name: t.confidence for t in tags_for("99/SH.3-H", "3", 1)}
        self.assertEqual(by_name["SacrificeHit"], "uncertain")
        self.assertEqual(by_name["UnknownPlay"], "uncertain")

    def test_prior_state_flag_is_only_on_prior_state_tags(self):
        """`InningEnding`, `GoAheadRun` and `WalkOff` depend on *this* play."""
        for name in ("InningEnding", "GoAheadRun", "WalkOff"):
            with self.subTest(tag=name):
                self.assertFalse(O.REGISTRY[name].prior_state)
        for td in O.REGISTRY.values():
            if td.prior_state:
                with self.subTest(tag=td.name):
                    self.assertEqual(td.category, "context")

    def test_certain_tags_are_the_default_population(self):
        """A tag out on an unambiguous play is certain, not 'not applicable'.

        The 2000 record is the anchor for the force derivation, so its TagOut
        must be a positive determination rather than an absence of one.
        """
        self.assertEqual(self._confidence("TagOut", "K/NDP.3XH(21)", "13", 1),
                         "certain")


class Curated(unittest.TestCase):
    """§8: human judgement, quarantined and validated."""

    def test_shipped_file_loads(self):
        load_curated()   # raises on an unknown or duplicated tag name

    def test_unknown_tag_name_is_rejected(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "curated.json"
            path.write_text(json.dumps({"entries": [
                {"game_id": "KCA200009270", "play_seq": 4,
                 "tags": ["HiddenBallTrik"]},
            ]}))
            with self.assertRaises(ValueError):
                load_curated(path)

    def test_a_curated_tag_survives_a_re_derive(self):
        """§8's central guarantee, made executable.

        Derivation is a pure function of the play, so re-running it can only
        reproduce the derived set. The curated entry is merged from the
        version-controlled file afterwards and cannot be computed away.
        """
        import json
        import tempfile
        from pathlib import Path
        from rsse.semantic.derive import load_curated, tags_for_play

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "curated.json"
            path.write_text(json.dumps({"entries": [
                {"game_id": "KCA200009270", "play_seq": 4,
                 "tags": ["HiddenBallTrick"], "citation": "test"},
            ]}))
            curated = load_curated(path)
            state = HalfInningState()
            parsed = parse("63/G6")
            _after, outcome = apply_play(state, parsed.event)

            first = tags_for_play(parsed.event, outcome, PlayContext(),
                                  parsed.trivia, "KCA200009270", 4, curated)
            again = tags_for_play(parsed.event, outcome, PlayContext(),
                                  parsed.trivia, "KCA200009270", 4, curated)
            self.assertEqual([(t.name, t.source) for t in first],
                             [(t.name, t.source) for t in again])
            by_name = {t.name: t for t in first}
            self.assertEqual(by_name["HiddenBallTrick"].source, "curated")
            # ...and the derived tags are still all there.
            self.assertEqual(by_name["GroundOut"].source, "derived")

    def test_a_curated_tag_wins_a_collision(self):
        """The escape hatch of §3.1.1: a human asserting what the string cannot.

        `play_tags` is keyed (play_id, tag_id) and holds one row, so a name
        that is both derived and curated must resolve to the curated one --
        otherwise the judgement §8 exists to preserve is silently discarded.
        """
        import json
        import tempfile
        from pathlib import Path
        from rsse.semantic.derive import load_curated, tags_for_play

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "curated.json"
            path.write_text(json.dumps({"entries": [
                {"game_id": "KCA200009270", "play_seq": 4,
                 "tags": ["UncaughtThirdStrike"], "citation": "test"},
            ]}))
            curated = load_curated(path)
            state = HalfInningState(outs=1)
            state.bases["1"] = Runner("damoj001")
            state.bases["3"] = Runner("ortih001")
            parsed = parse("K/NDP.3XH(21)")
            _after, outcome = apply_play(state, parsed.event)
            tags = tags_for_play(parsed.event, outcome, PlayContext(),
                                 parsed.trivia, "KCA200009270", 4, curated)
            names = [t.name for t in tags]
            self.assertEqual(names.count("UncaughtThirdStrike"), 1)
            by_name = {t.name: t for t in tags}
            self.assertEqual(by_name["UncaughtThirdStrike"].source, "curated")
            self.assertEqual(by_name["TagOut"].source, "derived")

    def test_no_curated_entry_changes_nothing(self):
        from rsse.semantic.derive import tags_for_play
        state = HalfInningState()
        parsed = parse("63/G6")
        _after, outcome = apply_play(state, parsed.event)
        merged = tags_for_play(parsed.event, outcome, PlayContext(),
                               parsed.trivia, "XXX000000000", 1, {})
        self.assertEqual([t.name for t in merged],
                         [t.name for t in derive(parsed.event, outcome,
                                                 PlayContext(), parsed.trivia)])

    def test_curated_tags_are_marked_as_such(self):
        import json
        import tempfile
        from pathlib import Path
        from rsse.semantic.derive import curated_for

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "curated.json"
            path.write_text(json.dumps({"entries": [
                {"game_id": "KCA200009270", "play_seq": 4,
                 "tags": ["HiddenBallTrick"], "citation": "test"},
            ]}))
            loaded = load_curated(path)
            tags = curated_for("KCA200009270", 4, loaded)
            self.assertEqual([t.name for t in tags], ["HiddenBallTrick"])
            self.assertEqual([t.source for t in tags], ["curated"])
            self.assertEqual(curated_for("KCA200009270", 5, loaded), [])


if __name__ == "__main__":
    unittest.main()
