"""Earned runs, derived from the play-by-play (spec/03-STATE.md §9).

Retrosheet records *that* a run was unearned -- the `(UR)` and `(TUR)` advance
flags -- but never why, and never the reconstruction that produced the verdict.
This module derives the verdict from the events themselves, so the flags become
an answer key rather than an input. They cover all 118 seasons at the rate the
published unearned-run percentages predict, which makes them the densest check
this project has: 1.8 million individually adjudicated runs, against the
203,285 game totals that `data,er` and the game logs offer.

The rule is OBR 9.16. Its mechanism is a **reconstructed inning**: replay the
half-inning as if no error, passed ball, or obstruction had occurred, and count
the outs that *would* have been made. Once three of them accumulate, every
later run is unearned, because the inning would already have ended.

9.16 is not fully mechanical, and the parts that are not say so in the text --
"in the scorer's judgment", "benefit of the doubt shall be given to the
pitcher". Those clauses are where a derivation has to stop claiming and start
grading, so every verdict carries a `certainty` the way a force does
(§4.4): `derived` where the rule decides, `likely` where one reading is
strongly indicated, `ambiguous` where the rule itself defers to a human.

Two ledgers, not one. 9.16(g) denies a relief pitcher "the benefit of previous
chances for outs not accepted", so a run can be unearned for the team and
earned for the pitcher who allowed it. That is exactly what `(TUR)` marks, and
it is why this returns `earned_team` and `earned_pitcher` separately rather
than one flag: `data,er` counts the pitcher ledger and the game logs count
both.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..parser import grammar as G
from .state import OUT, batter_destination

#: Base names as distances from home, so "did he advance far enough" is
#: arithmetic rather than a table. The batter starts at 0.
_BASE_NUM = {"B": 0, "1": 1, "2": 2, "3": 3, "H": 4}

#: What a verdict may claim. Mirrors `force_certainty` (§4.4) deliberately:
#: the two derivations face the same problem, which is that the record states
#: an outcome and leaves the reasoning out.
DERIVED = "derived"
LIKELY = "likely"
AMBIGUOUS = "ambiguous"
UNTRUSTED = "untrusted"



@dataclass(frozen=True, kw_only=True)
class Movement:
    """One runner movement, with the 9.16 facts about it.

    A subset of `ResolvedAdvance` plus the aid attribution, kept separate so
    the rule can be fed from the derived tables without a replay.
    """

    #: `runner_advances.seq` -- provenance, so a verdict names the row it
    #: judged rather than being matched back by position.
    adv_seq: int
    origin: str
    dest: str
    marked_out: bool
    is_out: bool
    scored: bool
    runner_id: str | None
    #: An error charged inside *this advance's* parameters -- `2-H(E4/TH)`.
    #: Aid to this runner specifically, as against an error elsewhere on the
    #: play, which 9.16(c) treats differently.
    aided_by_error: bool = False
    #: A `(PB)` flag on this advance. A passed ball is the catcher's fault and
    #: so is aid; a wild pitch is the pitcher's own and is not (9.16(a) note).
    aided_by_passed_ball: bool = False
    #: Set by the reconstruction, not the caller: the play carried aid
    #: *outside* the advance section, so every advance on it happened
    #: downstream of that aid. `E6/G.3-H` scored because the shortstop booted
    #: the ball and `PO1(E1).2-3` because the pickoff throw got away, yet
    #: neither advance carries an error parameter of its own.
    on_an_error_play: bool = False
    #: What Retrosheet wrote: '', 'UR' or 'TUR'. Carried through untouched --
    #: this module never reads it, and the reconciliation compares it.
    recorded: str = ""


@dataclass
class HalfPlay:
    """One play, reduced to what 9.16 needs to know about it."""

    seq: int
    batter_id: str
    outs_before: int
    outs_recorded: int
    pitcher_id: str | None
    movements: list[Movement] = field(default_factory=list)

    #: Basic-event facts. Each names a distinct clause of 9.16 and they are
    #: kept apart rather than folded into "the batter is tainted", because the
    #: reconstruction owes an out for some of them and not for others.
    reached_on_error: bool = False        # `E$`      -- 9.16(a)(2)(iii)
    foul_fly_error: bool = False          # `FLE$`    -- 9.16(a)(2)(i)
    interference: bool = False            # `C`       -- 9.16(a)(2)(ii)
    fielders_choice: bool = False         # `FC`      -- 9.16(a)(1)
    passed_ball: bool = False             # `PB`
    #: An error charged *outside* the advance section -- in the basic event,
    #: in a base-running event's parameters, or as a `/E$` modifier. Every
    #: advance on such a play moved downstream of it, and the advance section
    #: records nothing to say so.
    play_level_error: bool = False
    #: Whether this play resolved the plate appearance. A muffed foul fly
    #: taints the batter for the *rest of his at bat*, which can run to
    #: several plays -- a steal, a wild pitch -- before he reaches or is out.
    at_bat_ended: bool = True
    #: Where the basic event alone puts the batter (§2 rule 2). The
    #: reconstruction needs it as his baseline: a single puts him on first
    #: whatever else went wrong, so holding him at the plate would be a
    #: benefit of the doubt the rule never offers.
    batter_implicit_dest: str | None = None
    #: Bases occupied entering the play, as `plays.bases_before`. Only the
    #: first play of a half-inning is consulted, and only to spot the
    #: extra-inning placed runner, who is on base with no play to explain him.
    bases_before: str | None = None
    #: `(base, pitcher_id)` from any `presadj` records standing between the
    #: previous play and this one: Retrosheet stating outright who is
    #: responsible for a runner, which overrules this module's bookkeeping.
    responsibility: tuple[tuple[str, str], ...] = ()
    #: False when the play could not be parsed or its state is not trusted.
    #: The reconstruction cannot count outs it cannot see, so this poisons the
    #: rest of the half-inning rather than being silently skipped.
    trusted: bool = True


@dataclass(frozen=True)
class RunVerdict:
    """One run, and what the rule says about it."""

    seq: int
    adv_seq: int
    runner_id: str | None
    pitcher_id: str | None
    #: True, False, or **None where 9.16 defers to the scorer**. The rule says
    #: "in the scorer's judgment" and "benefit of the doubt", and a derivation
    #: that answers anyway is inventing a fact. None is the honest value, and
    #: it is reported rather than hidden: an unearned verdict has to be earned
    #: the same way a zero does.
    earned_team: bool | None
    earned_pitcher: bool | None
    certainty: str
    #: The clause that settled it, in words. Short and stable enough to group
    #: by: a census of reasons is how a disagreement gets localised.
    reason: str
    recorded: str

    @property
    def flag(self) -> str | None:
        """The verdict in Retrosheet's own vocabulary, for comparison.

        None where the derivation makes no claim -- which is not the same as
        claiming the run was earned, and must not be compared as if it were.
        """
        if self.earned_pitcher is None or self.earned_team is None:
            return None
        if not self.earned_pitcher:
            return "UR"
        return "" if self.earned_team else "TUR"


# ---------------------------------------------------------------------------
# reading the facts off a parsed event
# ---------------------------------------------------------------------------

def play_facts(event: G.Event, seq: int, batter_id: str, outs_before: int,
               outs_recorded: int, pitcher_id: str | None,
               movements: list[Movement], trusted: bool = True,
               bases_before: str | None = None,
               responsibility: tuple[tuple[str, str], ...] = ()) -> HalfPlay:
    """Read the 9.16 facts off a parsed event.

    Takes the parse tree rather than the raw string so this cannot drift from
    the grammar, and takes the movements from the caller because the state
    machine has already resolved them -- error negation, implicit batter
    placement and force derivation included (§3).
    """
    basics = event.basics
    codes = event.modifier_codes()
    play_level = (
        any(isinstance(b, (G.ReachedOnError, G.FoulFlyError)) for b in basics)
        or any(m.kind == "error" for m in event.modifiers)
        or _error_in_basic_params(event)
    )
    implicit = batter_destination(event)
    return HalfPlay(
        seq=seq,
        batter_id=batter_id,
        outs_before=outs_before,
        outs_recorded=outs_recorded,
        pitcher_id=pitcher_id,
        movements=movements,
        # `E$` only. Interference is a separate clause with a separate
        # answer: 9.16(a)(2)(iii) says the batter would have been out and so
        # owes the reconstruction an out, while 9.16(a)(2)(ii) says he was
        # still at the plate and owes it nothing. Folding the two together
        # ends innings that should still be going.
        reached_on_error=any(isinstance(b, G.ReachedOnError) for b in basics),
        foul_fly_error=any(isinstance(b, G.FoulFlyError) for b in basics),
        interference=(any(isinstance(b, G.Interference) for b in basics)
                      or "INT" in codes),
        fielders_choice=any(isinstance(b, G.FieldersChoice) for b in basics),
        passed_ball=any(isinstance(b, G.BaseRunning) and b.code == "PB"
                        for b in basics),
        play_level_error=play_level,
        at_bat_ended=implicit is not None,
        bases_before=bases_before,
        responsibility=responsibility,
        batter_implicit_dest=None if implicit in (None, OUT) else implicit,
        trusted=trusted,
    )


def _error_in_basic_params(event: G.Event) -> bool:
    """An error inside a base-running event's parameters -- `PO1(E1)`.

    Kept apart from the advance section because of where the aid lands: a
    pickoff throw that gets away moves every runner on the play, and the
    advances it produces (`2-3;1-2`) carry no error of their own.
    """
    for basic in event.basics:
        if isinstance(basic, G.BaseRunning):
            for param in basic.params:
                if isinstance(param, G.CreditSequence) and param.has_error:
                    return True
    return False



def movement_flags(adv: G.Advance) -> tuple[bool, bool, str]:
    """`(aided_by_error, aided_by_passed_ball, recorded)` for one advance."""
    aided = any(isinstance(p, G.CreditSequence) and p.has_error
                for p in adv.params)
    flags = {p.text for p in adv.params if isinstance(p, G.Flag)}
    recorded = "TUR" if "TUR" in flags else ("UR" if "UR" in flags else "")
    return aided, "PB" in flags, recorded


# ---------------------------------------------------------------------------
# the reconstructed inning (9.16)
# ---------------------------------------------------------------------------

#: Why a runner is absent from the reconstruction. Each names the clause, so a
#: census of reasons localises a disagreement to a rule rather than to a game.
_REACHED_ON_ERROR = "reached on an error"
_INTERFERENCE = "reached on interference or obstruction"
_PROLONGED = "at bat prolonged by a muffed foul fly"
_FC_ON_ERROR_RUNNER = "fielder's choice retiring a runner who reached on an error"
_ERASED = "erased in the reconstruction"
#: 9.16(b) -- written as an out, safe only because of the error.
_LIFE_PROLONGED = "life prolonged by an error"
#: The 2020+ extra-inning tiebreaker runner. He reached second without facing
#: a pitch, so no pitcher put him there and his run is nobody's to earn
#: (spec/03-STATE.md §6.4).
_PLACED = "extra-inning placed runner"


@dataclass
class _Reconstruction:
    """Mutable state carried across the plays of one half-inning."""

    #: Outs the defence would have made with errorless play. The whole rule
    #: turns on this reaching three.
    outs: int = 0
    #: Real outs, tracked alongside so divergence is detectable rather than
    #: assumed. While the two agree and nobody is tainted, the reconstruction
    #: *is* the half-inning and a verdict needs no judgment.
    real_outs: int = 0
    #: Actual base -> the base that runner occupies in the reconstruction.
    #: A base missing from this map holds a runner who is not in the
    #: reconstruction at all.
    at: dict[str, int] = field(default_factory=dict)
    #: Actual base -> why that runner is absent, for the ones that are.
    absent: dict[str, str] = field(default_factory=dict)
    #: Actual base -> the pitcher charged with that runner (9.16(f)).
    charged: dict[str, str | None] = field(default_factory=dict)
    #: Pitcher -> the out count *his* reconstruction stands at (9.16(g)).
    pitcher_outs: dict[str, int] = field(default_factory=dict)
    #: The batter whose at bat a muffed foul fly extended (9.16(a)(2)(i)).
    prolonged: str | None = None
    #: True once the reconstruction stops matching the half-inning that was
    #: actually played. Until then every verdict follows from the record.
    diverged: bool = False
    untrusted: bool = False


def reconstruct(plays: list[HalfPlay],
                placed: tuple[str, ...] = ()) -> list[RunVerdict]:
    """Adjudicate every run in one half-inning (9.16).

    `plays` must be the whole half-inning in order, including the plays on
    which nothing scored: the rule is decided by the *outs* the defence would
    have made, so a scoreless play at the top of the inning can be the reason
    a run at the bottom of it is unearned.

    `placed` names bases already occupied when the half-inning opened, which
    only happens under the extra-inning tiebreaker. Such a runner is outside
    the reconstruction from the start: the rule denies the pitcher the charge
    because no pitcher put him there.
    """
    rec = _Reconstruction()
    for base in placed or opening_runners(plays):
        rec.absent[base] = _PLACED
    verdicts: list[RunVerdict] = []

    for play in plays:
        if not play.trusted:
            # The reconstruction cannot count outs it cannot see. One unparsed
            # play makes every later verdict in the half-inning a guess, so it
            # is marked rather than quietly absorbed (§7.1).
            rec.untrusted = True
        for base, pitcher in play.responsibility:
            # `presadj` is Retrosheet saying who is responsible, which beats
            # working it out. It arrives between plays, so it is applied
            # before the play it precedes rather than after.
            rec.charged[base] = pitcher
        _enter_pitcher(rec, play)
        verdicts.extend(_apply(rec, play))

    return verdicts


def _enter_pitcher(rec: _Reconstruction, play: HalfPlay) -> None:
    """Open a ledger for a pitcher the half-inning has not seen yet (9.16(g)).

    A reliever does not inherit the reconstruction's out count: 9.16(g) denies
    him "the benefit of previous chances for outs not accepted", and that
    benefit is exactly what a high reconstructed count is. He starts from the
    outs actually on the board -- and never from more than the team's own
    count, so a real out made on a runner who does not exist in the
    reconstruction cannot be charged to him twice.
    """
    pid = play.pitcher_id
    if pid is not None and pid not in rec.pitcher_outs:
        rec.pitcher_outs[pid] = min(rec.outs, play.outs_before)


def _apply(rec: _Reconstruction, play: HalfPlay) -> list[RunVerdict]:
    """Advance the reconstruction over one play, judging any runs on it."""
    inning_was_over = rec.outs >= 3
    movements = play.movements

    def present(origin: str) -> bool:
        return origin in rec.at

    # A batter reaching while a runner is retired is a fielder's choice in the
    # sense 9.16(a)(1) and 9.16(f) mean, whether or not Retrosheet wrote `FC`:
    # a force out at second is `54(1)/FO`, a plain `Out`, and the rule does not
    # turn on the notation.
    retired = [m for m in movements if m.origin != "B" and m.is_out]
    batter_safe = any(m.origin == "B" and not m.is_out for m in movements)
    fc_like = batter_safe and bool(retired)
    retired_absent = [m for m in retired if not present(m.origin)]

    # 9.16(e) reaches past the advance that carries the error. When the aid is
    # written in the basic event -- `E6/G.3-H`, `PO1(E1).2-3` -- every runner
    # on the play moved because of it and the advance section records nothing
    # to say so. Reading only each advance's own parameters calls those runs
    # unaided, which is how this was found.
    #
    # A passed ball belongs here too. 9.16(a) lists a wild pitch among the
    # things that produce an *earned* run and pointedly omits the passed ball,
    # because one is the pitcher's own mistake and the other is the catcher's.
    if play.play_level_error:
        movements = [replace(m, on_an_error_play=True) for m in movements]
    if play.passed_ball:
        movements = [replace(m, aided_by_passed_ball=True) for m in movements]

    # --- the batter: which clause, if any, keeps him out of the reconstruction
    batter_absent: str | None = None
    batter_owes_out = False
    if rec.prolonged == play.batter_id:
        # His at bat only reached this play because a foul fly was muffed. The
        # out that should have ended it was counted at the muff, so none is
        # owed here -- counting it again would end the inning early.
        batter_absent = _PROLONGED
    elif play.reached_on_error:
        batter_absent, batter_owes_out = _REACHED_ON_ERROR, True
    elif play.interference:
        # 9.16(a)(2)(ii). No out is owed: the batter was still at the plate.
        batter_absent = _INTERFERENCE
    elif fc_like and retired_absent:
        # 9.16(a)(1): the out was made on a runner who was not there, so it
        # would have been made on the batter instead.
        batter_absent, batter_owes_out = _FC_ON_ERROR_RUNNER, True

    # --- outs the reconstruction records
    delta = 1 if batter_owes_out else 0
    moved_outs = sum(1 for m in movements if m.is_out)
    # Outs with no movement row: a strikeout or a caught fly, where nobody ran
    # (§3 rule 3). They are never error-dependent, so they always count --
    # unless the batter is not in the reconstruction at all, in which case the
    # reconstruction already retired him.
    at_plate = max(0, play.outs_recorded - moved_outs)
    if batter_absent is None:
        delta += at_plate
    if play.foul_fly_error:
        # The muff itself is the missed chance, counted here so that a runner
        # who scores later in the at bat is judged against an inning that
        # should already have been one out further along.
        delta += 1
        rec.prolonged = play.batter_id
        rec.diverged = True
    elif rec.prolonged == play.batter_id and play.at_bat_ended:
        # The taint lasts exactly as long as the plate appearance does.
        # Clearing it when the batter fails to reach drops it on a stolen
        # base mid-at-bat; never clearing it leaves it set for whoever
        # happens to bat with the same id later in the inning.
        rec.prolonged = None

    # --- movements
    new_at: dict[str, int] = dict(rec.at)
    new_absent: dict[str, str] = dict(rec.absent)
    new_charged: dict[str, str | None] = dict(rec.charged)
    landing: list[tuple[str, int, str | None, str | None]] = []
    runs: list[tuple[Movement, int, str | None, str | None]] = []

    for mv in movements:
        origin = mv.origin
        if origin == "B":
            here = batter_absent is None
            # Held back, the batter still gets what the basic event alone
            # gives him: a single is a single however the throw was muffed.
            src = _BASE_NUM.get(play.batter_implicit_dest or "B", 0)
            why = batter_absent
            owner = _charge_for_batter(rec, play, fc_like, retired)
        else:
            here = present(origin)
            src = rec.at.get(origin, 0)
            why = rec.absent.get(origin, _ERASED)
            owner = rec.charged.get(origin)
        new_at.pop(origin, None)
        new_absent.pop(origin, None)
        new_charged.pop(origin, None)

        if not here:
            # Invisible to the reconstruction: his movements did not happen,
            # and neither did any out made on him.
            if mv.scored:
                runs.append((mv, -1, why, owner))
            elif not mv.is_out:
                landing.append((mv.dest, -1, why, owner))
            continue

        # Only an out Retrosheet *wrote* counts here. Reading an error on an
        # advance as a chance not accepted -- `FC5.1-3(E5/TH)`, the third
        # baseman throwing at the runner taking third and missing -- is the
        # obvious extension of 9.16(b) and it was tried. Over 211,000 runs it
        # made agreement worse, not better (spec/07-TESTING.md §4.5): the
        # cases it fixes are outnumbered by the runners it retires who were
        # never going to be out.
        if mv.is_out or mv.marked_out:
            # Either a real out, or one an error negated -- `2XH(7E2)` is
            # written as an out and is only safe because of the error, so the
            # reconstruction records it either way.
            delta += 1
            if mv.marked_out and not mv.is_out:
                # 9.16(b): his life was prolonged by an error. He is out in
                # the reconstruction but still standing on a base in the game,
                # so the absence is filed where he actually ends up -- filing
                # it against the base he left would leave the base he reached
                # looking like an untainted runner.
                rec.diverged = True
                if mv.scored:
                    runs.append((mv, -1, _LIFE_PROLONGED, owner))
                else:
                    landing.append((mv.dest, -1, _LIFE_PROLONGED, owner))
            continue

        if mv.aided_by_error or mv.aided_by_passed_ball or mv.on_an_error_play:
            # 9.16(e): benefit of the doubt to the pitcher. Absent the aid he
            # is assumed to have held the base he was on.
            dest = src
            rec.diverged = True
        else:
            dest = src + (_BASE_NUM[mv.dest] - _BASE_NUM[origin])
            if origin == "B":
                dest = min(dest, _BASE_NUM[mv.dest])

        if mv.scored:
            runs.append((mv, dest, None, owner))
        else:
            landing.append((mv.dest, dest, None, owner))

    for base, dest, why, owner in landing:
        if dest < 0 or dest >= 4:
            # Absent from the reconstruction, or past the base he really holds
            # -- either way there is no reconstruction runner at this base.
            new_absent[base] = why or _ERASED
            new_at.pop(base, None)
        else:
            new_at[base] = dest
            new_absent.pop(base, None)
        new_charged[base] = owner

    verdicts = [_judge(rec, play, mv, dest, why, owner, inning_was_over, delta)
                for mv, dest, why, owner in runs]

    rec.at, rec.absent, rec.charged = new_at, new_absent, new_charged
    rec.outs += delta
    rec.real_outs += play.outs_recorded
    if rec.outs != rec.real_outs or rec.absent:
        rec.diverged = True
    for pid in rec.pitcher_outs:
        rec.pitcher_outs[pid] += delta
    return verdicts


def opening_runners(plays: list[HalfPlay]) -> tuple[str, ...]:
    """Bases occupied when the half-inning opened.

    A half-inning starts empty, so anyone standing on a base before the first
    play got there without facing a pitch: the extra-inning tiebreaker runner
    (§6.4). Read off the state the loader already stored rather than asked for
    separately, so the two cannot disagree.
    """
    if not plays or not plays[0].bases_before:
        return ()
    code = plays[0].bases_before
    return tuple(b for b, bit in zip(("1", "2", "3"), code) if bit == "1")


def _charge_for_batter(rec: _Reconstruction, play: HalfPlay, fc_like: bool,
                       retired: list[Movement]) -> str | None:
    """Which pitcher owns the batter as a baserunner (9.16(f)).

    Normally the pitcher who put him there. On a fielder's choice the charge
    follows the runner erased instead, so that a reliever is not handed a run
    for a baserunner he inherited and merely swapped.
    """
    if fc_like and retired:
        return rec.charged.get(retired[0].origin, play.pitcher_id)
    return play.pitcher_id


#: Why a run was judged as it was. Stable strings: the reconciliation groups
#: disagreements by reason, which is what turns "1,400 runs differ" into "one
#: clause is wrong".
_INNING_OVER = "the inning would already have ended"
_INNING_OVER_HERE = "the inning would have ended on this play"
_AIDED_ERROR = "advance aided by an error"
_AIDED_PASSED_BALL = "advance aided by a passed ball"
_ON_ERROR_PLAY = "scored on a play that turned on an error"
_AIDED_EARLIER = "held short of home by an earlier error"
_CLEAN = "scored without aid"


def _judge(rec: _Reconstruction, play: HalfPlay, mv: Movement, dest: int,
           why: str | None, owner: str | None, inning_was_over: bool,
           delta: int) -> RunVerdict:
    """Decide one run, and say how firmly -- or decline to.

    The order of the tests is the order 9.16 decides in, and it matters: a
    runner who reached on an error is unearned whatever the reconstruction's
    out count says, and an inning that should already have been over settles
    every run after it without asking how the runner got around.

    Where the rule defers, so does this. `earned_team` comes back None and the
    certainty says `ambiguous`, which is a different statement from "earned"
    and is counted separately by everything downstream.
    """
    pitcher = owner if owner is not None else play.pitcher_id
    before = rec.pitcher_outs.get(pitcher, 0) if pitcher else rec.outs

    if why is not None:
        # 9.16(a)(1), (a)(2), (b). Categorical: the rule names the situation
        # and gives the answer, with no reconstruction to second-guess.
        earned_team = earned_pitcher = False
        reason, certainty = why, DERIVED
    elif inning_was_over:
        earned_team = False
        # 9.16(g). The team's inning was over; this pitcher's may not have
        # been, and that difference is precisely what `(TUR)` records.
        earned_pitcher = before < 3
        reason, certainty = _INNING_OVER, DERIVED
    elif rec.outs + delta >= 3:
        # The third reconstructed out falls on this very play. Whether it
        # would have come before or after the run is not in the record.
        earned_team = earned_pitcher = None
        reason, certainty = _INNING_OVER_HERE, AMBIGUOUS
    elif dest < 4:
        # He is in the reconstruction but does not get home in it. How far a
        # runner would have gone without the error is the one thing 9.16(e)
        # explicitly refuses to settle, so neither does this.
        earned_team = earned_pitcher = None
        certainty = AMBIGUOUS
        reason = (_ON_ERROR_PLAY if mv.on_an_error_play
                  else _AIDED_ERROR if mv.aided_by_error
                  else _AIDED_PASSED_BALL if mv.aided_by_passed_ball
                  else _AIDED_EARLIER)
    else:
        earned_team = earned_pitcher = True
        reason = _CLEAN
        # While the reconstruction has not diverged from the half-inning that
        # was played, "earned" is a reading of the record rather than a
        # hypothetical, and claims the stronger word.
        certainty = LIKELY if rec.diverged else DERIVED

    return RunVerdict(
        seq=play.seq,
        adv_seq=mv.adv_seq,
        runner_id=mv.runner_id,
        pitcher_id=pitcher,
        earned_team=earned_team,
        earned_pitcher=earned_pitcher,
        certainty=UNTRUSTED if rec.untrusted else certainty,
        reason=reason,
        recorded=mv.recorded,
    )
