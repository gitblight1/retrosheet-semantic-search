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

from .model.game import replay_game
from .database import load as dbload
from .database import schema as dbschema
from .parser.parser import ParseError, parse
from .parser.records import detect_line_ending, iter_plays
from .util.download import available_years, fetch_season

VERSION = "0.1.0"
PARSER_VERSION = "0.1.0"
ARCHIVE_DEFAULT = "archive.db"     # verbatim source records (spec/05 §7)
QUERY_DEFAULT = "rsse.db"          # derived, queryable tables

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
EVENTS = DATA / "events"
KNOWN_DEFECTS = ROOT / "tests" / "known-source-defects.json"


def load_known_defects() -> set[str]:
    """Event strings that are malformed at source (spec/02-GRAMMAR.md §8.1)."""
    if not KNOWN_DEFECTS.exists():
        return set()
    doc = json.loads(KNOWN_DEFECTS.read_text())
    return {d["event"] for d in doc["defects"]}

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

    known = load_known_defects()
    failed_events: collections.Counter[str] = collections.Counter()
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
                failed_events[play.event] += 1
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

    unexpected = {e: n for e, n in failed_events.items() if e not in known}
    accounted = sum(n for e, n in failed_events.items() if e in known)
    if known:
        print(f"known source defects  {accounted} (see {KNOWN_DEFECTS.name})")
        print(f"unexplained failures  {sum(unexpected.values())}")
    if unexpected:
        print("\nUNEXPLAINED -- a grammar bug until shown otherwise:")
        for event, n in sorted(unexpected.items(), key=lambda kv: -kv[1])[:args.top]:
            print(f"  {n:7d}  {event}")

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
    # The seven known defects are a documented baseline, not a regression.
    # Only unexplained failures and any round-trip loss fail the sweep.
    return 0 if (not unexpected and nrt == 0) else 1


def cmd_ingest(args: argparse.Namespace) -> int:
    """Build the raw layer (spec/05-DATABASE.md §1)."""
    root = Path(args.path) if args.path else EVENTS
    db_path = (Path(args.archive) if args.archive
               else DATA / "database" / ARCHIVE_DEFAULT)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    files = dbload.event_files(root)
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"no event files under {root}; run `rsse fetch` first", file=sys.stderr)
        return 2

    conn = dbschema.connect(str(db_path))
    for pragma in dbschema.LOAD_PRAGMAS:
        conn.execute(pragma)
    dbschema.create(conn)
    corpus_id = dbload.open_corpus(conn, VERSION, PARSER_VERSION, args.notes or "")

    seasons_seen: set[str] = set()

    def progress(path, loaded, stats):
        season = path.parent.name
        if args.progress and season not in seasons_seen:
            seasons_seen.add(season)
            print(f"  {season} ... {stats.records:,} records",
                  file=sys.stderr, flush=True)

    try:
        stats = dbload.ingest(conn, files, corpus_id, verify=not args.no_verify,
                              progress=progress)
    except dbload.CorpusChanged as exc:
        print(f"\nCORPUS CHANGED: {exc}", file=sys.stderr)
        return 3

    if not args.no_index:
        print("building indexes ...", file=sys.stderr, flush=True)
        dbschema.create_indexes(conn)
    conn.execute("ANALYZE")
    conn.commit()

    print(stats.summary())
    known = load_known_defects()
    unexplained = [f for f in stats.parse_failures if f[1] not in known]
    if stats.parse_failures:
        print(f"known source defects  "
              f"{len(stats.parse_failures) - len(unexplained)}")
    for game, event, detail in unexplained[: args.top]:
        print(f"  UNEXPLAINED {game} {event!r}: {detail}")
    for game, event, emitted in stats.roundtrip_failures[: args.top]:
        print(f"  ROUNDTRIP    {game} {event!r} -> {emitted!r}")

    if stats.repeated_game_ids:
        print(f"repeated game ids     {len(stats.repeated_game_ids)} "
              f"(kept and numbered; spec/01-CORPUS.md §5.4)")
        for gid, n, rec in stats.repeated_game_ids[: args.top]:
            print(f"  {gid} occurrence {n} at record {rec}")

    size = db_path.stat().st_size
    print(f"archive               {db_path} ({size / 1e9:.2f} GB)")
    print(f"bytes per record      {size / max(stats.records, 1):.1f}")
    conn.close()
    print(f"\n{ATTRIBUTION}")
    return 0 if (not unexplained and not stats.roundtrip_failures) else 1


def cmd_verify(args: argparse.Namespace) -> int:
    """Corpus-wide integrity checks on the raw layer (spec/07-TESTING.md §4).

    Every check is a single ordered pass. The obvious formulation of the
    overlap check -- a self-join of game_spans on range overlap -- has no
    usable index and degenerates to ~40 billion comparisons at corpus scale;
    scanning spans in record order settles overlap, gaps and coverage at once.
    """
    db_path = (Path(args.archive) if args.archive
               else DATA / "database" / ARCHIVE_DEFAULT)
    if not db_path.exists():
        print(f"no archive at {db_path}; run `rsse ingest` first", file=sys.stderr)
        return 2
    conn = dbschema.connect(f"file:{db_path}?mode=ro")
    failures = []

    records = conn.execute("SELECT count(*) FROM raw_records").fetchone()[0]
    spanned = conn.execute("SELECT sum(record_count) FROM game_spans").fetchone()[0] or 0
    print(f"records              {records:,}")
    print(f"covered by spans     {spanned:,}")
    if records != spanned:
        failures.append(f"spans cover {spanned:,} of {records:,} records")

    # One ordered pass: contiguity implies both no overlap and no gap.
    prev_end = 0
    overlaps = gaps = 0
    for first, last, count in conn.execute(
            "SELECT first_record_id, last_record_id, record_count"
            " FROM game_spans ORDER BY first_record_id"):
        if first <= prev_end:
            overlaps += 1
        elif first != prev_end + 1:
            gaps += 1
        if last - first + 1 != count:
            failures.append(f"span {first}-{last} declares {count} records")
        prev_end = max(prev_end, last)
    print(f"overlapping spans    {overlaps}")
    print(f"gaps between spans   {gaps}")
    if overlaps:
        failures.append(f"{overlaps} overlapping spans")

    dupes = conn.execute(
        "SELECT game_id, count(*) FROM game_spans GROUP BY game_id"
        " HAVING count(*) > 1 ORDER BY game_id").fetchall()
    print(f"repeated game ids    {len(dupes)}"
          + (f"  {[d[0] for d in dupes]}" if dupes else ""))

    files, recorded = conn.execute(
        "SELECT count(*), sum(record_count) FROM source_files").fetchone()
    print(f"source files         {files}")
    if recorded != records:
        failures.append(f"source_files sums to {recorded:,}, raw_records has {records:,}")

    print(f"archive              {db_path} ({db_path.stat().st_size / 1e9:.2f} GB)")
    conn.close()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nall raw-layer invariants hold")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Replay every game and check the state machine (spec/07-TESTING.md §4).

    The strongest check available from event files alone: every half-inning
    must end with three outs, except the last of a game, which can end early on
    a walk-off or not be played at all. Final-score reconciliation needs
    Retrosheet's game logs, which are a separate download.
    """
    root = Path(args.path) if args.path else EVENTS
    files = _event_files(root)
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"no event files under {root}", file=sys.stderr)
        return 2

    known_defects = load_known_defects()
    games = plays = 0
    bad_games = 0
    explained = 0
    failing: list[dict] = []
    short = collections.Counter()
    culprits: collections.Counter[str] = collections.Counter()
    totals = collections.Counter()
    seasons_seen: set[str] = set()

    for path in files:
        season = path.parent.name
        if args.progress and season not in seasons_seen:
            seasons_seen.add(season)
            print(f"  {season} ...", file=sys.stderr, flush=True)
        for game_id, records in _games_in(path):
            games += 1
            rp = replay_game(records, game_id)
            plays += len(rp.plays)
            totals["parse_errors"] += rp.parse_errors
            totals["inconsistent"] += rp.inconsistent
            totals["ambiguous"] += rp.ambiguous
            totals["contradicts_rules"] += rp.contradicts_rules
            if rp.short_innings or rp.inconsistent:
                failing.append({
                    "game_id": game_id,
                    "file": str(path),
                    "short_innings": rp.short_innings,
                    "inconsistent": [
                        {"event": p.event, "inning": p.inning, "team": p.team,
                         "notes": p.outcome.notes}
                        for p in rp.plays
                        if p.outcome and p.outcome.parse_status == "state_inconsistent"
                    ],
                })
            if rp.short_innings:
                # A play that cannot be parsed cannot record its out, so a
                # known source defect explains a short inning (02-GRAMMAR §8.1).
                if any(p.error and p.event in known_defects for p in rp.plays):
                    explained += 1
                else:
                    bad_games += 1
                for inning, team, outs in rp.short_innings:
                    short[outs] += 1
                    last = [p for p in rp.plays
                            if p.inning == inning and p.team == team][-1:]
                    for p in last:
                        culprits[_shape(p.event)] += 1

    print(f"games                {games:,}")
    print(f"plays                {plays:,}")
    print(f"games with a short half-inning  {bad_games + explained:,} "
          f"({100 * (bad_games + explained) / max(games, 1):.2f}%)")
    print(f"  explained by a known source defect  {explained:,}")
    print(f"  unexplained                         {bad_games:,}")
    print(f"outs at close        {dict(sorted(short.items()))}")
    print(f"state inconsistent   {totals['inconsistent']:,}")
    print(f"state ambiguous      {totals['ambiguous']:,}")
    print(f"contradicts rulebook {totals['contradicts_rules']:,}")
    print(f"parse errors         {totals['parse_errors']:,}")
    if culprits:
        print("\nlast play of a short half-inning, by shape:")
        for shape, n in culprits.most_common(args.top):
            print(f"  {n:7d}  {shape}")
    if args.report:
        Path(args.report).write_text(json.dumps(
            {"games": games, "plays": plays, "bad_games": bad_games,
             "totals": dict(totals), "failing": failing}, indent=2))
        print(f"\nreport written to {args.report}")
    print(f"\n{ATTRIBUTION}")
    return 0 if bad_games == 0 else 1


def cmd_tags(args: argparse.Namespace) -> int:
    """Derive tags over the corpus and report a census (spec/04-ONTOLOGY.md).

    Unit tests prove each rule fires on the play it was written for. They
    cannot show that a rule fires on the *right number* of plays, and the
    project has already learned that lesson twice: the seven state-machine
    bugs were all invisible to tests written from the documentation, and the
    undocumented `U` modifier exists in four seasons out of 118.

    So this is the ontology's corpus-wide gate. Two findings fail it:

    * **A derivable tag that never fires.** Either the rule is wrong or the
      encoding does not exist; both need to be known, and neither shows up in
      a suite of positives.
    * **A tag that fires on everything.** A rule matching most of the corpus
      is not selecting anything, whatever its name says.

    Era coverage beats volume here for the same reason it did for the grammar,
    so `--seasons` samples across the range rather than taking a prefix.
    """
    from .semantic import ontology as O
    from .semantic.derive import derive

    root = Path(args.path) if args.path else EVENTS
    files = _event_files(root)
    if args.seasons:
        # Spread the sample across the corpus: a tag confined to four seasons
        # is invisible to any prefix, however many plays it contains.
        by_season: dict[str, list[Path]] = collections.defaultdict(list)
        for path in files:
            by_season[path.parent.name].append(path)
        seasons = sorted(by_season)
        # Span the range end to end. A `[::step]` slice silently drops the
        # tail -- 12 of 118 seasons steps by 9 and stops at 2007, missing
        # replay review (2014+) and placed runners (2020+) entirely, and then
        # reports their tags as "never fired". The newest seasons are where
        # the newest encodings are, so the last season is not optional.
        if args.seasons >= len(seasons):
            picked = seasons
        elif args.seasons == 1:
            picked = seasons[-1:]
        else:
            last = len(seasons) - 1
            picked = sorted({seasons[round(i * last / (args.seasons - 1))]
                             for i in range(args.seasons)})
        files = [f for s in picked for f in by_season[s]]
        if args.progress:
            print(f"sampling {len(picked)} seasons: {', '.join(picked)}",
                  file=sys.stderr)
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"no event files under {root}", file=sys.stderr)
        return 2

    counts: collections.Counter[str] = collections.Counter()
    uncertain: collections.Counter[str] = collections.Counter()
    first_seen: dict[str, str] = {}
    seasons_with: dict[str, set[str]] = collections.defaultdict(set)
    games = plays = tagged = skipped = 0
    seasons_seen: set[str] = set()

    for path in files:
        season = path.parent.name
        if args.progress and season not in seasons_seen:
            seasons_seen.add(season)
            print(f"  {season} ...", file=sys.stderr, flush=True)
        for game_id, records in _games_in(path):
            games += 1
            replay = replay_game(records, game_id)
            for play in replay.plays:
                plays += 1
                if play.outcome is None:
                    skipped += 1
                    continue
                tags = derive(play.parsed.event, play.outcome, play.context,
                              play.parsed.trivia)
                tagged += 1
                for tag in tags:
                    counts[tag.name] += 1
                    if tag.confidence != "certain":
                        uncertain[tag.name] += 1
                    seasons_with[tag.name].add(season)
                    first_seen.setdefault(
                        tag.name, f"{game_id} {play.inning} {play.event}")

    print(f"ontology version     {O.ONTOLOGY_VERSION}  ({O.ontology_hash()})")
    print(f"registered tags      {len(O.REGISTRY)}")
    print(f"games                {games:,}")
    print(f"plays                {plays:,}")
    print(f"plays tagged         {tagged:,}")
    print(f"plays skipped        {skipped:,}   (unparsed)")
    print(f"tag rows             {sum(counts.values()):,}")
    print(f"  uncertain          {sum(uncertain.values()):,}")

    print("\ntag census, by category:")
    for category, tag_names in O.categories().items():
        print(f"\n  {category}")
        for name in tag_names:
            n = counts[name]
            share = 100 * n / max(tagged, 1)
            unc = uncertain[name]
            note = f"  {unc:,} uncertain" if unc else ""
            print(f"    {name:28} {n:11,}  {share:6.2f}%  "
                  f"{len(seasons_with[name]):3d} seasons{note}")

    silent = [td.name for td in O.derivable() if not counts[td.name]]
    saturated = [n for n, c in counts.items() if c > 0.90 * max(tagged, 1)]

    if silent:
        print(f"\nnever fired ({len(silent)}):")
        for name in sorted(silent):
            print(f"  {name:28} {O.REGISTRY[name].spec}")
    if saturated:
        print(f"\nfired on over 90% of plays ({len(saturated)}) -- "
              "a rule this broad selects nothing:")
        for name in sorted(saturated):
            print(f"  {name:28} {100 * counts[name] / max(tagged, 1):.2f}%")

    if args.report:
        Path(args.report).write_text(json.dumps({
            "ontology_version": O.ONTOLOGY_VERSION,
            "ontology_hash": O.ontology_hash(),
            "games": games, "plays": plays, "tagged": tagged,
            "skipped": skipped,
            "tags": {name: {"count": counts[name],
                            "uncertain": uncertain[name],
                            "seasons": len(seasons_with[name]),
                            "first_seen": first_seen.get(name),
                            "rule_hash": O.REGISTRY[name].rule_hash}
                     for name in sorted(O.REGISTRY)},
            "never_fired": sorted(silent),
        }, indent=2))
        print(f"\nreport written to {args.report}")

    print(f"\n{ATTRIBUTION}")
    return 0 if not silent else 1


def _games_in(path: Path):
    """Yield (game_id, records) for each game in an event file."""
    from .parser.records import read_records
    current: list = []
    game_id = ""
    for rec in read_records(path):
        if rec.type == "id":
            if current:
                yield game_id, current
            game_id = rec.fields[0] if rec.fields else ""
            current = []
        current.append(rec)
    if current:
        yield game_id, current


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

    i = sub.add_parser("ingest", help="load the raw layer into SQLite")
    i.add_argument("--path", help="event-file root (default data/events)")
    i.add_argument("--archive", help="archive database path")
    i.add_argument("--limit", type=int, help="only the first N files")
    i.add_argument("--top", type=int, default=15)
    i.add_argument("--notes")
    i.add_argument("--progress", action="store_true")
    i.add_argument("--no-verify", action="store_true",
                   help="skip the round-trip gate (not recommended)")
    i.add_argument("--no-index", action="store_true")
    i.set_defaults(func=cmd_ingest)

    r = sub.add_parser("replay", help="replay every game and check the state machine")
    r.add_argument("--path")
    r.add_argument("--limit", type=int)
    r.add_argument("--top", type=int, default=20)
    r.add_argument("--progress", action="store_true")
    r.add_argument("--report", help="write failing games to this JSON path")
    r.set_defaults(func=cmd_replay)

    t = sub.add_parser("tags", help="derive tags over the corpus and "
                                    "report a census")
    t.add_argument("--path")
    t.add_argument("--limit", type=int, help="first N event files")
    t.add_argument("--seasons", type=int,
                   help="sample N seasons spread across the corpus, rather "
                        "than a prefix: a tag confined to a few seasons is "
                        "invisible to any prefix")
    t.add_argument("--progress", action="store_true")
    t.add_argument("--report", help="write a JSON census to this path")
    t.set_defaults(func=cmd_tags)

    v = sub.add_parser("verify", help="check raw-layer integrity")
    v.add_argument("--archive")
    v.set_defaults(func=cmd_verify)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
