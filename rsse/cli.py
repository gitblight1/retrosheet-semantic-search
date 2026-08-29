"""RSSE command line.

`fetch` acquires the corpus; `sweep` runs the grammar over every event string
in it. The sweep is the first build step (see README build order): the grammar
must be proven against the whole corpus before any semantic layer depends on
it.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

from .parser.parser import ParseError, parse
from .parser.records import detect_line_ending, iter_plays
from .util.download import available_years, fetch_season

DATA = Path(__file__).resolve().parent.parent / "data"
EVENTS = DATA / "events"

ATTRIBUTION = (
    "The information used here was obtained free of charge from and is "
    "copyrighted by Retrosheet. Interested parties may contact Retrosheet "
    'at "www.retrosheet.org".'
)

EVENT_FILE = re.compile(r"\.EV[ANFR]$", re.IGNORECASE)


def _event_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if EVENT_FILE.search(p.name))


def _shape(event: str) -> str:
    """Collapse an event to its structural shape, so one gap is one finding.

    Fielder digits and player-specific detail are noise when grouping parse
    failures; the shape is what identifies the missing production.
    """
    return re.sub(r"\d", "#", event)


def cmd_fetch(args: argparse.Namespace) -> int:
    years = available_years()
    if args.since:
        years = [y for y in years if y >= args.since]
    if args.until:
        years = [y for y in years if y <= args.until]
    print(f"fetching {len(years)} seasons ({years[0]}-{years[-1]}) into {EVENTS}")
    ok = 0
    for year in years:
        if (EVENTS / str(year)).exists() and not args.force:
            ok += 1
            continue
        if fetch_season(year, EVENTS):
            ok += 1
            print(f"  {year} ok", flush=True)
    print(f"done: {ok}/{len(years)} seasons available")
    return 0 if ok == len(years) else 1


def cmd_sweep(args: argparse.Namespace) -> int:
    root = Path(args.path) if args.path else EVENTS
    files = _event_files(root)
    if not files:
        print(f"no event files under {root}; run `rsse fetch` first", file=sys.stderr)
        return 2

    total = 0
    parse_failures: collections.Counter[str] = collections.Counter()
    roundtrip_failures: collections.Counter[str] = collections.Counter()
    examples: dict[str, list[str]] = {}
    per_season: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0, 0])
    line_endings: collections.Counter[str] = collections.Counter()
    games: set[str] = set()

    seasons_seen: set[str] = set()
    for path in files:
        season = path.parent.name
        if season not in seasons_seen:
            seasons_seen.add(season)
            if args.progress:
                print(f"  {season} ...", file=sys.stderr, flush=True)
        line_endings[detect_line_ending(path)] += 1
        for game_id, play in iter_plays(path):
            total += 1
            games.add(game_id)
            per_season[season][0] += 1
            try:
                parsed = parse(play.event)
            except ParseError as exc:
                key = f"{exc.message} | {_shape(play.event)}"
                parse_failures[key] += 1
                per_season[season][1] += 1
                examples.setdefault(key, [])
                if len(examples[key]) < 3:
                    examples[key].append(f"{game_id} {play.event}")
                continue
            except Exception as exc:  # noqa: BLE001
                key = f"{type(exc).__name__}: {exc} | {_shape(play.event)}"
                parse_failures[key] += 1
                per_season[season][1] += 1
                continue
            if not parsed.roundtrips():
                key = _shape(play.event)
                roundtrip_failures[key] += 1
                per_season[season][2] += 1
                examples.setdefault("rt:" + key, [])
                if len(examples["rt:" + key]) < 3:
                    examples["rt:" + key].append(
                        f"{game_id} {play.event!r} -> {parsed.emit()!r}"
                    )

    nparse = sum(parse_failures.values())
    nrt = sum(roundtrip_failures.values())
    print(f"files            {len(files)}")
    print(f"games            {len(games)}")
    print(f"plays            {total}")
    print(f"parse failures   {nparse}  ({100 * nparse / max(total, 1):.4f}%)")
    print(f"roundtrip fails  {nrt}  ({100 * nrt / max(total, 1):.4f}%)")
    print(f"line endings     {dict(line_endings)}")

    if parse_failures:
        print(f"\ndistinct parse-failure shapes: {len(parse_failures)}")
        for key, count in parse_failures.most_common(args.top):
            print(f"  {count:7d}  {key}")
            for ex in examples.get(key, []):
                print(f"           e.g. {ex}")
    if roundtrip_failures:
        print(f"\ndistinct roundtrip-failure shapes: {len(roundtrip_failures)}")
        for key, count in roundtrip_failures.most_common(args.top):
            print(f"  {count:7d}  {key}")
            for ex in examples.get("rt:" + key, []):
                print(f"           e.g. {ex}")

    if args.by_season:
        print("\nseason    plays   parse_fail  rt_fail")
        for season in sorted(per_season):
            p, f, r = per_season[season]
            print(f"{season:8s} {p:8d} {f:10d} {r:8d}")

    if args.report:
        report = {
            "files": len(files),
            "games": len(games),
            "plays": total,
            "parse_failures": nparse,
            "roundtrip_failures": nrt,
            "line_endings": dict(line_endings),
            "parse_failure_shapes": dict(parse_failures),
            "roundtrip_failure_shapes": dict(roundtrip_failures),
            "examples": examples,
            "per_season": {k: dict(zip(("plays", "parse_fail", "rt_fail"), v))
                           for k, v in per_season.items()},
        }
        Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True))
        print(f"\nreport written to {args.report}")

    print(f"\n{ATTRIBUTION}")
    return 0 if (nparse == 0 and nrt == 0) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rsse")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download Retrosheet season archives")
    f.add_argument("--since", type=int)
    f.add_argument("--until", type=int)
    f.add_argument("--force", action="store_true")
    f.set_defaults(func=cmd_fetch)

    s = sub.add_parser("sweep", help="parse every event string in the corpus")
    s.add_argument("--path")
    s.add_argument("--top", type=int, default=25)
    s.add_argument("--by-season", action="store_true")
    s.add_argument("--progress", action="store_true",
                   help="report each season to stderr as it is swept")
    s.add_argument("--report", help="write a JSON report to this path")
    s.set_defaults(func=cmd_sweep)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
