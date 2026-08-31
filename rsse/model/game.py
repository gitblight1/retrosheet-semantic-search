"""Whole-game replay (spec/03-STATE.md §6).

Drives the half-inning state machine across a game's records, handling the
records that are not plays: `NP`/`sub`, `radj` placed runners, and the
`htbf` case where the home team bats first.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..parser.parser import ParseError, parse
from ..parser.records import PlayRecord, Record
from .state import HalfInningState, ParseStatus, PlayOutcome, Runner, apply_play


@dataclass
class ReplayedPlay:
    seq: int
    inning: int
    team: int
    batter_id: str
    event: str
    outcome: PlayOutcome | None
    error: str | None = None


@dataclass
class GameReplay:
    game_id: str
    plays: list[ReplayedPlay] = field(default_factory=list)
    runs: dict[int, int] = field(default_factory=lambda: {0: 0, 1: 0})
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
    seq = 0
    half_history: list[tuple[int, int, int]] = []   # inning, team, outs at close

    for rec in records:
        if rec.type == "radj" and len(rec.fields) >= 2:
            pending_runners.append((rec.fields[0], rec.fields[1]))
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

        seq += 1
        try:
            event = parse(play.event).event
        except ParseError as exc:
            out.parse_errors += 1
            out.plays.append(ReplayedPlay(seq, play.inning, play.team,
                                          play.batter_id, play.event, None,
                                          exc.message))
            continue

        state, outcome = apply_play(state, event)
        out.runs[play.team] = out.runs.get(play.team, 0) + outcome.runs_on_play
        if outcome.parse_status == ParseStatus.INCONSISTENT:
            out.inconsistent += 1
        elif outcome.parse_status == ParseStatus.AMBIGUOUS:
            out.ambiguous += 1
        elif outcome.parse_status == ParseStatus.CONTRADICTS_RULES:
            out.contradicts_rules += 1
        out.plays.append(ReplayedPlay(seq, play.inning, play.team,
                                      play.batter_id, play.event, outcome))

    if current is not None:
        half_history.append((current[0], current[1], state.outs))

    # Every half-inning must end with three outs, except the game's last: it
    # can end early on a walk-off, or not be played at all if the home team
    # leads. Only the final entry is exempt.
    for inning, team, outs in half_history[:-1]:
        if outs != 3:
            out.short_innings.append((inning, team, outs))
    return out
