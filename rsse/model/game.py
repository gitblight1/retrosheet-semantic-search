"""Whole-game replay (spec/03-STATE.md §6).

Drives the half-inning state machine across a game's records, handling the
records that are not plays: `NP`/`sub`, `radj` placed runners, and the
`htbf` case where the home team bats first.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..parser.parser import ParseError, ParsedEvent, parse
from ..parser.grammar import NoPlay
from ..parser.records import PlayRecord, Record
from .comments import AFTER, NO_PLAY, Comment, classify, replay_target
from .state import (HalfInningState, ParseStatus, PlayContext, PlayOutcome,
                    Runner, apply_play)


@dataclass
class ReplayedPlay:
    seq: int
    inning: int
    team: int
    batter_id: str
    event: str
    outcome: PlayOutcome | None
    error: str | None = None
    #: Line in the source file, which is how a derived row finds the archive
    #: record it came from.
    line_no: int = 0
    balls: int | None = None
    strikes: int | None = None
    pitches: str = ""
    #: Game context (§7). None for a play that could not be parsed.
    context: PlayContext | None = None
    #: The parse tree and its trivia. Retained so a consumer -- the tag
    #: deriver, the derived-table loader -- does not re-parse an event the
    #: replay has already parsed; over the corpus that is 17.9M parses saved.
    parsed: ParsedEvent | None = None


@dataclass
class GameReplay:
    game_id: str
    plays: list[ReplayedPlay] = field(default_factory=list)
    #: `(index into plays, line_no, Comment)` for every `com` record, in file
    #: order. The index is the count of plays seen so far, so 0 means the
    #: comment precedes the first play -- a game-level note, not a play note.
    comments: list[tuple[int, int, "Comment"]] = field(default_factory=list)
    runs: dict[int, int] = field(default_factory=lambda: {0: 0, 1: 0})
    #: From `info,innings`, present only from 2020 on. 518 games in the corpus
    #: are scheduled for 7 innings, so defaulting to 9 misreports every one of
    #: their 8th innings as regulation.
    scheduled_innings: int = 9
    #: From `info,htbf`. 51 games in the corpus; in them the home team bats in
    #: the *top* half (§6.5).
    home_bats_first: bool = False
    #: Half-innings that ended with other than three outs, and were not last.
    short_innings: list[tuple[int, int, int]] = field(default_factory=list)
    parse_errors: int = 0
    inconsistent: int = 0
    ambiguous: int = 0
    contradicts_rules: int = 0

    @property
    def ok(self) -> bool:
        return not (self.short_innings or self.parse_errors or self.inconsistent)


def replay_game(records: list[Record], game_id: str = "") -> GameReplay:
    """Replay one game's records into per-play state."""
    out = GameReplay(game_id=game_id)
    state = HalfInningState()
    current: tuple[int, int] | None = None
    pending_runners: list[tuple[str, str]] = []   # (runner_id, base) from radj
    after_ladj = False
    #: Whether the current half-inning has had a play that changed state. A
    #: `radj` arriving before that can be applied straight away; one arriving
    #: before the boundary has to wait for it.
    half_started = False
    seq = 0
    half_history: list[tuple[int, int, int]] = []   # inning, team, outs at close

    for rec in records:
        if rec.type == "info" and len(rec.fields) >= 2:
            # `info` records precede the plays, so one forward pass suffices.
            if rec.fields[0] == "innings" and rec.fields[1].isdigit():
                out.scheduled_innings = int(rec.fields[1])
            elif rec.fields[0] == "htbf" and rec.fields[1].lower() == "true":
                out.home_bats_first = True
            continue
        if rec.type == "radj" and len(rec.fields) >= 2:
            runner_id, base = rec.fields[0], rec.fields[1]
            if current is not None and not half_started and base in state.bases:
                # `radj` is *not* reliably the first record of the half-inning:
                # it commonly follows the leading `NP`/`sub` block, so the
                # boundary has already been crossed by the time it arrives.
                #
                #   play,10,0,biggc002,00,,NP     <- boundary here
                #   radj,shawt001,2               <- runner declared only now
                #   play,10,0,biggc002,31,...,W
                #
                # Deferring this to the *next* boundary left 2020+ extra
                # innings with no placed runner at all, and then leaked him
                # into the following half-inning. `NP` changes no state, so
                # applying it here is exact rather than a nudge.
                state.bases[base] = Runner(runner_id, placed=True)
            else:
                pending_runners.append((runner_id, base))
            continue
        if rec.type == "ladj":
            # The team batted out of order; the next plate appearance carries
            # the tag, since the record that says so is a different record.
            after_ladj = True
            continue
        if rec.type == "com":
            # Recorded here, linked in a second pass. A `com` record usually
            # describes the play before it, but 51 of the corpus's replay
            # records describe the play *after* -- and which one it is cannot
            # be decided going forwards, because the deciding evidence is the
            # next play's batter and it has not been read yet.
            #
            # Only a structured `replay` record carries a verdict; prose
            # saying "call was overturned by replay" is not read, because
            # parsing English into a derived fact is exactly the guess this
            # project does not make.
            out.comments.append((len(out.plays), rec.line_no,
                                 classify(rec.raw)))
            continue
        if rec.type != "play":
            continue
        try:
            play = PlayRecord.from_record(rec)
        except ValueError:
            out.parse_errors += 1
            continue

        half = (play.inning, play.team)
        if half != current:
            if current is not None:
                half_history.append((current[0], current[1], state.outs))
            state = HalfInningState()
            # An extra inning may begin with a placed runner (2020+).
            for runner_id, base in pending_runners:
                if base in state.bases:
                    state.bases[base] = Runner(runner_id, placed=True)
            pending_runners.clear()
            current = half
            half_started = False

        seq += 1
        try:
            parsed = parse(play.event)
        except ParseError as exc:
            out.parse_errors += 1
            out.plays.append(ReplayedPlay(
                seq, play.inning, play.team, play.batter_id, play.event, None,
                exc.message, line_no=play.line_no, balls=play.balls,
                strikes=play.strikes, pitches=play.pitches))
            continue

        # Built before the play is applied: the context is the situation the
        # batter walked into.
        batting_before = out.runs.get(play.team, 0)
        fielding_before = out.runs.get(1 - play.team, 0)
        placed = any(r is not None and r.placed for r in state.bases.values())

        is_no_play = (len(parsed.event.basics) == 1
                      and isinstance(parsed.event.basics[0], NoPlay))
        state, outcome = apply_play(state, parsed.event,
                                    batter_id=play.batter_id)
        if not is_no_play:
            half_started = True
        out.runs[play.team] = out.runs.get(play.team, 0) + outcome.runs_on_play

        context = PlayContext(
            inning=play.inning,
            # Keyed off the `team` field and the game's own `htbf`, never off an
            # assumption that the visitor bats first (§6.5). In an `htbf` game
            # team 1 -- the home team -- bats in the top half.
            half=half_for(play.team, out.home_bats_first),
            team=play.team,
            scheduled_innings=out.scheduled_innings,
            score_batting_before=batting_before,
            score_fielding_before=fielding_before,
            is_go_ahead=(batting_before <= fielding_before
                         and batting_before + outcome.runs_on_play > fielding_before),
            has_placed_runner=placed,
            after_ladj=after_ladj,
        )
        after_ladj = False
        if outcome.parse_status == ParseStatus.INCONSISTENT:
            out.inconsistent += 1
        elif outcome.parse_status == ParseStatus.AMBIGUOUS:
            out.ambiguous += 1
        elif outcome.parse_status == ParseStatus.CONTRADICTS_RULES:
            out.contradicts_rules += 1
        out.plays.append(ReplayedPlay(
            seq, play.inning, play.team, play.batter_id, play.event, outcome,
            line_no=play.line_no, balls=play.balls, strikes=play.strikes,
            pitches=play.pitches, context=context, parsed=parsed))

    if current is not None:
        half_history.append((current[0], current[1], state.outs))

    # Every half-inning must end with three outs, except the game's last: it
    # can end early on a walk-off, or not be played at all if the home team
    # leads. Only the final entry is exempt.
    for inning, team, outs in half_history[:-1]:
        if outs != 3:
            out.short_innings.append((inning, team, outs))

    _link_replay_verdicts(out)
    _stamp_final_play(out)
    return out


def half_for(team: int, home_bats_first: bool) -> str:
    """Which half of the inning ``team`` bats in (§6.5).

    Normally the visitor (team 0) bats in the top. `info,htbf,true` reverses
    it, and 51 games in the corpus say so.
    """
    bats_first = 1 if home_bats_first else 0
    return "top" if team == bats_first else "bottom"


def _real_play_before(plays: list[ReplayedPlay], index: int):
    """The nearest actual play before `index`, skipping `NP` (comments.py)."""
    for i in range(index - 1, -1, -1):
        if plays[i].event != NO_PLAY:
            return plays[i]
    return None


def _real_play_from(plays: list[ReplayedPlay], index: int):
    """The nearest actual play at or after `index`, skipping `NP`."""
    for i in range(index, len(plays)):
        if plays[i].event != NO_PLAY:
            return plays[i]
    return None


def _link_replay_verdicts(out: GameReplay) -> None:
    """Attach each replay verdict to the play it actually describes (§6.7).

    A second pass, because the rule needs the play on *both* sides of the
    comment and the forward pass has only seen one of them. `comments` stores
    the number of plays seen when the record arrived, so entry `i` sits
    between `plays[i - 1]` and `plays[i]` -- and the neighbours are the nearest
    *real* plays from there, because a substitution record is not a play and
    cannot be the subject of a review (`NO_PLAY` in comments.py).

    **A reversal is never overwritten by a later upheld verdict.** A play may
    be reviewed twice -- a `MREV` and a `UREV` on the same event -- and 33
    plays carry one comment saying the call was reversed and a second saying a
    call was upheld. Taking the last one written lost all 33 reversals. The
    tag means *a* call on this play was overturned, so the verdicts are ORed:
    once True it stays True.
    """
    for index, _line_no, comment in out.comments:
        if comment.replay_reversed is None:
            continue
        before = _real_play_before(out.plays, index)
        after = _real_play_from(out.plays, index)
        who = (comment.payload or {}).get("player_id")
        target = (after if replay_target(
            who, before.batter_id if before else None,
            after.batter_id if after else None) == AFTER else before)
        if target is None or target.context is None:
            continue
        if target.context.replay_reversed is True and not comment.replay_reversed:
            continue
        target.context = replace(
            target.context, replay_reversed=comment.replay_reversed)


def _stamp_final_play(out: GameReplay) -> None:
    """Mark the last play, and a walk-off, in a second pass (§7).

    `is_walkoff` cannot be decided during the replay: it requires knowing the
    game ended here, which the play itself does not say.

    Once the final play is known, a walk-off is just a go-ahead run on it: the
    team batting on the last play is by definition the team that bats last, so
    no test on the half is needed -- and none may be made, because the home
    team bats in the *top* half of an `htbf` game (§6.5).
    """
    playable = [p for p in out.plays if p.context is not None]
    if not playable:
        return
    last = playable[-1]
    last.context = replace(last.context, is_final_play=True,
                           is_walkoff=last.context.is_go_ahead)
