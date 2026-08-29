"""Record-level reading of Retrosheet event files (spec/01-CORPUS.md §3).

Splits a line into its type and fields. Quoted fields may contain commas, so
this uses the csv module rather than str.split -- the naive split is a defect
the spec calls out explicitly.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class Record:
    line_no: int
    type: str
    fields: tuple[str, ...]
    raw: str


@dataclass(frozen=True)
class PlayRecord:
    """The six fields of a `play` record (spec/01-CORPUS.md §3.2)."""

    line_no: int
    inning: int
    team: int
    batter_id: str
    balls: int | None
    strikes: int | None
    pitches: str
    event: str
    raw: str

    @staticmethod
    def from_record(rec: Record) -> "PlayRecord":
        f = rec.fields
        if len(f) < 6:
            raise ValueError(f"play record has {len(f)} fields, expected 6")
        count = f[3]
        balls = strikes = None
        if len(count) == 2 and count.isdigit():
            balls, strikes = int(count[0]), int(count[1])
        return PlayRecord(
            line_no=rec.line_no,
            inning=int(f[0]),
            team=int(f[1]),
            batter_id=f[2],
            balls=balls,
            strikes=strikes,
            pitches=f[4],
            event=",".join(f[5:]),
            raw=rec.raw,
        )


def read_records(path: Path) -> Iterator[Record]:
    """Yield every record in an event file, verbatim.

    Files are latin-1: a handful carry non-ASCII bytes in name fields, and
    latin-1 round-trips every byte rather than failing or substituting.
    """
    with open(path, encoding="latin-1", newline="") as fh:
        for n, line in enumerate(fh, start=1):
            raw = line.rstrip("\r\n")
            if not raw:
                continue
            # csv parsing is only needed when a field is quoted, and quoted
            # fields are a small minority of records. Constructing a reader per
            # line dominates the runtime of a corpus sweep, so take the fast
            # path when the line provably has no quoting to interpret.
            if '"' in raw:
                row = next(csv.reader(io.StringIO(raw)), None)
                if not row:
                    continue
            else:
                row = raw.split(",")
            yield Record(n, row[0], tuple(row[1:]), raw)


def detect_line_ending(path: Path) -> str:
    """Retrosheet files are CRLF; mirrors sometimes normalise (§2.1)."""
    with open(path, "rb") as fh:
        blob = fh.read(65536)
    crlf = blob.count(b"\r\n")
    lf = blob.count(b"\n")
    if crlf and crlf == lf:
        return "crlf"
    if crlf:
        return "mixed"
    return "lf"


def iter_plays(path: Path) -> Iterator[tuple[str, PlayRecord]]:
    """Yield ``(game_id, play)`` for every play record in a file."""
    game_id = ""
    for rec in read_records(path):
        if rec.type == "id":
            game_id = rec.fields[0] if rec.fields else ""
        elif rec.type == "play":
            yield game_id, PlayRecord.from_record(rec)
