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
import os
import re
import sys
import time
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
GAMELOGS = DATA / "gamelogs"
KNOWN_DEFECTS = ROOT / "tests" / "known-source-defects.json"
ARCHIVE = DATA / "database" / ARCHIVE_DEFAULT
QUERY_DB = DATA / "database" / QUERY_DEFAULT


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


#: Integrity checks on the derived tables (spec/07-TESTING.md §4). Each is a
#: query that must return zero rows; a name, the SQL, and whether the check is
#: meaningful on a play whose base-out state is untrustworthy.
#:
#: The `trusted_only` flag is the point of `state_untrusted`. A play that
#: inherited a wrong state from an unparsed play earlier in its half-inning
#: parses cleanly and then fails the base-state checks -- correctly, because
#: the state really is wrong. Excluding those rows by *status* keeps the check
#: honest: the count of what was excluded is printed, so a growing exclusion
#: is visible rather than a silent whitelist.
DERIVED_CHECKS = [
    # --- referential -------------------------------------------------------
    ("orphan play -> games", False,
     "SELECT p.play_id FROM plays p LEFT JOIN games g USING (game_key)"
     " WHERE g.game_key IS NULL"),
    ("orphan advance -> plays", False,
     "SELECT a.play_id FROM runner_advances a LEFT JOIN plays p USING (play_id)"
     " WHERE p.play_id IS NULL"),
    ("orphan credit -> plays", False,
     "SELECT f.play_id FROM fielding_credits f LEFT JOIN plays p USING (play_id)"
     " WHERE p.play_id IS NULL"),
    ("orphan sequence -> plays", False,
     "SELECT s.play_id FROM credit_sequences s LEFT JOIN plays p USING (play_id)"
     " WHERE p.play_id IS NULL"),
    ("orphan play_tag -> plays", False,
     "SELECT t.play_id FROM play_tags t LEFT JOIN plays p USING (play_id)"
     " WHERE p.play_id IS NULL"),
    ("orphan play_tag -> tags", False,
     "SELECT t.play_id FROM play_tags t LEFT JOIN tags g USING (tag_id)"
     " WHERE g.tag_id IS NULL"),
    ("games.plays disagrees with plays rows", False,
     "SELECT g.game_key FROM games g WHERE g.plays <>"
     " (SELECT COUNT(*) FROM plays p WHERE p.game_key = g.game_key)"),

    # --- out accounting ----------------------------------------------------
    ("outs_before + outs_recorded <> outs_after", False,
     "SELECT play_id FROM plays"
     " WHERE outs_before + outs_recorded <> outs_after"),
    ("outs_after > 3", False,
     "SELECT play_id FROM plays WHERE outs_after > 3"),
    ("outs_before <> previous outs_after", False,
     "SELECT play_id FROM (SELECT play_id, outs_before, inning, half,"
     "   LAG(outs_after) OVER (PARTITION BY game_key ORDER BY seq) AS prev,"
     "   LAG(inning) OVER (PARTITION BY game_key ORDER BY seq) AS pi,"
     "   LAG(half) OVER (PARTITION BY game_key ORDER BY seq) AS ph"
     "  FROM plays) WHERE pi = inning AND ph = half AND outs_before <> prev"),

    # --- run accounting ----------------------------------------------------
    ("runs_on_play <> scoring advances", False,
     "SELECT play_id FROM plays p WHERE p.runs_on_play <>"
     " (SELECT COUNT(*) FROM runner_advances a"
     "   WHERE a.play_id = p.play_id AND a.scored = 1)"),
    # `trusted_only` checks are filtered on `p.parse_status`, so every one of
    # them must expose the plays row under the alias `p`.
    ("runs_on_play exceeds runners on base + batter", True,
     "SELECT p.play_id FROM plays p WHERE p.runs_on_play >"
     " (LENGTH(p.bases_before) - LENGTH(REPLACE(p.bases_before,'1',''))) + 1"),

    # --- base state --------------------------------------------------------
    ("advance from an unoccupied base", True,
     "SELECT a.play_id FROM runner_advances a JOIN plays p USING (play_id)"
     " WHERE a.origin <> 'B'"
     "   AND SUBSTR(p.bases_before, CAST(a.origin AS INTEGER), 1) <> '1'"),
    ("batter reached but has no advance row", False,
     "SELECT p.play_id FROM plays p WHERE p.batter_ran = 'yes'"
     "  AND NOT EXISTS (SELECT 1 FROM runner_advances a"
     "                   WHERE a.play_id = p.play_id AND a.origin = 'B')"),
    ("non-batter advance with no runner id", True,
     "SELECT a.play_id FROM runner_advances a JOIN plays p USING (play_id)"
     " WHERE a.origin <> 'B' AND a.runner_id IS NULL"),
    # The batter has an identity too. It was attached only when the advance
    # was implicit, so `K.3XH(21);2-3;1-2;B-1` named all three runners on base
    # and left the batter anonymous -- the same fact identified or not
    # depending on whether the scorer wrote the advance.
    ("batter advance with no runner id", True,
     "SELECT a.play_id FROM runner_advances a JOIN plays p USING (play_id)"
     " WHERE a.origin = 'B' AND a.runner_id IS NULL"),
    ("bad bases string", False,
     "SELECT play_id FROM plays WHERE LENGTH(bases_before) <> 3"
     "    OR LENGTH(bases_after) <> 3"),
    ("state stored for an unparsed play", False,
     "SELECT play_id FROM plays WHERE parse_status = 'unparsed'"
     "   AND (outs_before IS NOT NULL OR bases_before IS NOT NULL)"),

    # --- ontology / force --------------------------------------------------
    ("force flagged but certainty n/a", False,
     "SELECT play_id FROM runner_advances"
     " WHERE is_force = 1 AND force_certainty = 'n/a'"),
    ("advance both out and scored", False,
     "SELECT play_id FROM runner_advances WHERE is_out = 1 AND scored = 1"),
    ("tag on an unparsed play", False,
     "SELECT t.play_id FROM play_tags t JOIN plays p USING (play_id)"
     " WHERE p.parse_status = 'unparsed'"),
]

_TRUSTED = "p.parse_status NOT IN ('unparsed','state_untrusted')"


def cmd_verify_derived(args: argparse.Namespace) -> int:
    """Integrity checks on the derived tables (spec/07-TESTING.md §4).

    Complements `verify`, which checks the raw layer. The raw checks prove
    nothing was lost in ingest; these prove it was filed correctly.
    """
    db_path = Path(args.database) if args.database else QUERY_DB
    if not db_path.exists():
        print(f"no query database at {db_path}; run `rsse derive` first",
              file=sys.stderr)
        return 2
    conn = dbschema.connect(f"file:{db_path}?mode=ro")
    conn.execute("PRAGMA cache_size = -400000")

    untrusted = conn.execute(
        "SELECT COUNT(*) FROM plays WHERE parse_status = 'state_untrusted'"
    ).fetchone()[0]
    unparsed = conn.execute(
        "SELECT COUNT(*) FROM plays WHERE parse_status = 'unparsed'"
    ).fetchone()[0]
    print(f"unparsed plays       {unparsed:,}")
    print(f"untrusted state      {untrusted:,}   (excluded from base-state checks)")
    print()

    failed = 0
    for name, trusted_only, sql in DERIVED_CHECKS:
        query = f"{sql} AND {_TRUSTED}" if trusted_only else sql
        started = time.time()
        count = conn.execute(f"SELECT COUNT(*) FROM ({query})").fetchone()[0]
        mark = "ok  " if count == 0 else "FAIL"
        print(f"{mark} {name:44s} {count:>10,}   ({time.time() - started:5.1f}s)",
              flush=True)
        if count:
            failed += 1
            for row in conn.execute(query + " LIMIT 3"):
                print(f"       e.g. play_id {row[0]}")
    conn.close()

    print(f"\n{len(DERIVED_CHECKS) - failed}/{len(DERIVED_CHECKS)} checks pass")
    print(f"\n{ATTRIBUTION}")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# query (spec/06-QUERY.md §8)
# ---------------------------------------------------------------------------

#: Flags that map one-to-one onto a `Search` method. Kept as data so `query`
#: and `explain` cannot drift apart: both build the search through `_search`.
_QUERY_FLAGS = [
    ("--bases", "bases", str), ("--outs", "outs", int),
    ("--outs-after", "outs_after", int), ("--inning", "inning", int),
    ("--inning-at-least", "inning_at_least", int), ("--half", "half", str),
    ("--season", "season", int), ("--league", "league", str),
    ("--team", "team", str), ("--batting-team", "batting_team", str),
    ("--fielding-team", "fielding_team", str), ("--batter", "batter", str),
    ("--park", "park", str), ("--runner-on", "runner_on", str),
    ("--out-at", "out_at", str), ("--batter-ran", "batter_ran", str),
    ("--error", "error", int), ("--hit-location", "hit_location", str),
    ("--event-matches", "event_matches", str),
]
_QUERY_SWITCHES = [
    ("--bases-loaded", "bases_loaded"), ("--bases-empty", "bases_empty"),
    ("--scoring-position", "scoring_position"),
    ("--inning-ending", "inning_ending"), ("--walkoff", "walkoff"),
    ("--strikeout", "strikeout"), ("--dropped-third", "dropped_third"),
    ("--batter-reached-on-k", "batter_reached_on_k"),
    ("--double-play", "double_play"), ("--triple-play", "triple_play"),
    ("--include-uncertain", "include_uncertain"),
    ("--include-untrusted", "include_untrusted"),
    ("--include-unparsed", "include_unparsed"),
    ("--curated-only", "curated_only"), ("--exclude-curated", "exclude_curated"),
    ("--include-exhibition", "include_exhibition"),
    ("--include-allstar", "include_allstar"),
    ("--only-postseason", "only_postseason"),
]


def _search_from_args(args) -> "object":
    from .query import Search
    s = Search()
    for flag, method, cast in _QUERY_FLAGS:
        value = getattr(args, flag.lstrip("-").replace("-", "_"), None)
        if value is not None:
            s = getattr(s, method)(cast(value))
    for flag, method in _QUERY_SWITCHES:
        if getattr(args, flag.lstrip("-").replace("-", "_"), False):
            s = getattr(s, method)()
    if args.seasons:
        lo, hi = (int(x) for x in args.seasons.split(","))
        s = s.seasons(lo, hi)
    if args.tag:
        for name in args.tag:
            s = s.tag(name)
    if args.force_play_at or args.force_play:
        s = s.force_play(at=args.force_play_at,
                         include_tag_outs=args.include_tag_outs,
                         certainty=args.force_certainty)
    if args.tag_out_at:
        s = s.tag_out(at=args.tag_out_at)
    if args.putout_sequence:
        s = s.putout_sequence(args.putout_sequence.split(","))
    if args.contains_sequence:
        s = s.contains_sequence(args.contains_sequence.split(","))
    if args.putout_by:
        s = s.putout_by(int(args.putout_by),
                        assist_by=[int(x) for x in args.assist_by.split(",")]
                        if args.assist_by else ())
    return s


def _add_query_flags(sub) -> None:
    for flag, _method, _cast in _QUERY_FLAGS:
        sub.add_argument(flag)
    for flag, _method in _QUERY_SWITCHES:
        sub.add_argument(flag, action="store_true")
    sub.add_argument("--seasons", help="LO,HI inclusive")
    sub.add_argument("--tag", action="append", help="repeatable")
    sub.add_argument("--force-play", action="store_true")
    sub.add_argument("--force-play-at")
    sub.add_argument("--force-certainty", choices=["derived", "likely",
                                                   "ambiguous"])
    sub.add_argument("--include-tag-outs", action="store_true")
    sub.add_argument("--tag-out-at")
    sub.add_argument("--putout-sequence", help="e.g. 2,1")
    sub.add_argument("--contains-sequence")
    sub.add_argument("--putout-by")
    sub.add_argument("--assist-by")
    sub.add_argument("--database")
    sub.add_argument("--limit", type=int, default=25)


def cmd_query(args: argparse.Namespace) -> int:
    """Run a search and print it with its coverage (spec/06-QUERY.md §8)."""
    from .query import QueryError, connect

    db_path = Path(args.database) if args.database else QUERY_DB
    if not db_path.exists():
        print(f"no query database at {db_path}; run `rsse derive` first",
              file=sys.stderr)
        return 2
    conn = connect(str(db_path))
    try:
        result = _search_from_args(args).run(conn, limit=args.limit)
    except QueryError as exc:
        print(f"bad query: {exc}", file=sys.stderr)
        return 2
    except NotImplementedError as exc:
        print(f"not available yet: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        import dataclasses
        print(json.dumps({
            "rows": [dataclasses.asdict(r) for r in result.rows],
            "total": result.total,
            "coverage": dataclasses.asdict(result.coverage),
            "excluded": dataclasses.asdict(result.excluded),
            "force": dataclasses.asdict(result.force) if result.force else None,
            "corpus_version": result.corpus_version,
            "ontology_version": result.ontology_version,
            "sql": result.sql,
            "attribution": ATTRIBUTION,
        }, indent=2, default=str))
        return 0

    if args.format == "csv":
        import csv
        writer = csv.writer(sys.stdout)
        writer.writerow(["game_id", "date", "inning", "half", "batter_id",
                         "outs_before", "bases_before", "event_raw", "tags"])
        for r in result.rows:
            writer.writerow([r.game_id, r.date, r.inning, r.half, r.batter_id,
                             r.outs_before, r.bases_before, r.event_raw,
                             " ".join(r.tags)])
        print(f"# {ATTRIBUTION}")
        return 0

    for r in result.rows:
        print(r.describe())
    shown = len(result.rows)
    print(f"\n{result.total:,} matching plays"
          + (f" ({shown} shown)" if shown < result.total else ""))
    # Never a bare count: coverage says what was searched, so an empty result
    # reads "not in these games" rather than "never happened" (§6).
    print(f"searched: {result.coverage.describe()}")
    for note in result.coverage.notes:
        print(f"  note: {note}")
    print(result.excluded.describe())
    if result.force:
        print(result.force.describe())
    print(f"corpus: {result.corpus_version}; ontology {result.ontology_version}")
    print(f"\n{ATTRIBUTION}")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    """Print the SQL and query plan without running the search."""
    from .query import QueryError, connect

    db_path = Path(args.database) if args.database else QUERY_DB
    if not db_path.exists():
        print(f"no query database at {db_path}", file=sys.stderr)
        return 2
    conn = connect(str(db_path))
    try:
        info = _search_from_args(args).explain(conn)
    except (QueryError, NotImplementedError) as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    print(info["sql"])
    print(f"\nparams: {info['params']}")
    print("\nplan:")
    for row in info["plan"]:
        print("  ", row[-1] if isinstance(row, tuple) else row)
    for warning in info["warnings"]:
        print(f"\nWARNING: {warning}")
    return 0


def cmd_gamelogs(args: argparse.Namespace) -> int:
    """Download and/or load Retrosheet's game logs (spec/07-TESTING.md §4)."""
    from .database import gamelogs
    from .util import download

    root = Path(args.dir) if args.dir else GAMELOGS
    if args.fetch:
        print(f"fetching game logs into {root} ...", file=sys.stderr)
        download.fetch_gamelogs(root)

    files = sorted(p for p in root.glob("*")
                   if p.is_file() and p.suffix.lower() in (".txt", ".csv"))
    if not files:
        print(f"no game log files in {root}.\n"
              "Retrosheet publishes the game logs as gl*.zip archives under "
              "retrosheet.org; try `rsse gamelogs --fetch`, or download and "
              f"unzip them into {root} by hand -- loading needs no network "
              "access.", file=sys.stderr)
        return 2

    archive_path = Path(args.archive) if args.archive else ARCHIVE
    if not archive_path.exists():
        print(f"no archive at {archive_path}; run `rsse ingest` first",
              file=sys.stderr)
        return 2
    conn = dbschema.connect(str(archive_path))
    for pragma in dbschema.LOAD_PRAGMAS:
        conn.execute(pragma)

    def progress(path):
        print(f"  {path.name} ...", file=sys.stderr, flush=True)

    stats = gamelogs.load(conn, files,
                          progress=progress if args.progress else None)
    print(f"files                {stats.files}")
    print(f"lines                {stats.lines:,}")
    print(f"loaded               {stats.loaded:,}")
    print(f"malformed            {len(stats.malformed):,}")
    for path, line_no, why in stats.malformed[:5]:
        print(f"    {Path(path).name}:{line_no}  {why}")

    # The field offsets cannot be checked by a fixture -- a test file written
    # from them agrees with them however wrong they are. These check the
    # *values* against the shape each field is supposed to hold, which is a
    # property of baseball rather than of the file format.
    print("\nlayout checks (field offsets against real data):")
    bad = 0
    for finding in stats.layout:
        mark = "ok  " if finding.ok else "FAIL"
        bad += 0 if finding.ok else 1
        print(f"  {mark} {finding.description:44s}"
              f" {finding.failed:>7,}/{finding.checked:,} ({finding.rate:.3%})")
    if bad:
        print(f"\n{bad} layout check(s) failed. The field offsets in "
              "rsse/model/gamelog.py are far more likely wrong than the data "
              "is; do not reconcile against this load.")
    conn.close()
    print(f"\n{ATTRIBUTION}")
    return 1 if (stats.malformed or bad) else 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Check every replayed final score against the published game log.

    The strongest check available on the state machine, and the first one that
    compares against numbers produced independently of the event files
    (spec/07-TESTING.md §4).
    """
    from .database import gamelogs

    archive_path = Path(args.archive) if args.archive else ARCHIVE
    query_path = Path(args.database) if args.database else QUERY_DB
    for path, what in ((archive_path, "ingest"), (query_path, "derive")):
        if not path.exists():
            print(f"no database at {path}; run `rsse {what}` first",
                  file=sys.stderr)
            return 2

    archive = dbschema.connect(f"file:{archive_path}?mode=ro")
    have = archive.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table'"
        " AND name='game_logs'").fetchone()[0]
    if not have:
        print("no game logs in the archive; run `rsse gamelogs` first",
              file=sys.stderr)
        return 2
    query = dbschema.connect(f"file:{query_path}?mode=ro")

    seasons = [int(y) for y in args.season.split(",")] if args.season else None
    result = gamelogs.reconcile(query, archive, seasons=seasons)

    print(f"games compared       {result.compared:,}")
    print(f"scores agree         {result.agreed:,}  ({result.rate:.4%})")
    print(f"scores disagree      {len(result.mismatches):,}")
    # Coverage, not error: Retrosheet's logs are Major League games and the
    # corpus includes the Negro Leagues, so a game in one and not the other is
    # expected and must not read as a failure.
    print(f"in the logs only     {result.log_only:,}")
    print(f"in the replay only   {result.replay_only:,}")
    print(f"skipped, forfeit     {result.skipped_forfeit:,}")
    print(f"skipped, incomplete  {result.skipped_incomplete:,}")

    if result.mismatches:
        print("\nmismatches (replay away-home vs log away-home):")
        for game_id, ra, rh, la, lh in result.mismatches[:args.show]:
            print(f"  {game_id}  replay {ra}-{rh}   log {la}-{lh}"
                  f"   delta {ra - la:+d}/{rh - lh:+d}")
        if len(result.mismatches) > args.show:
            print(f"  ... and {len(result.mismatches) - args.show:,} more")

    if args.explain_er:
        print("\nwhich earned-run field is the team total?")
        for key, value in gamelogs.explain_earned_runs(query, archive).items():
            print(f"  {key:12s} {value:,}")

    if args.report:
        Path(args.report).write_text(json.dumps({
            "compared": result.compared, "agreed": result.agreed,
            "mismatches": result.mismatches,
            "log_only": result.log_only, "replay_only": result.replay_only,
            "skipped_forfeit": result.skipped_forfeit,
            "skipped_incomplete": result.skipped_incomplete,
        }, indent=2))
        print(f"\nreport written to {args.report}")

    archive.close()
    query.close()
    print(f"\n{ATTRIBUTION}")
    return 1 if result.mismatches else 0


def cmd_secondary(args: argparse.Namespace) -> int:
    """Build `comments` and `lineup_entries` (spec/05-DATABASE.md §2, §5).

    A separate pass, not part of `derive`: neither table needs the state
    machine, and `plays.record_id` already links each play to its archive
    record, so both can be added to an existing query database without
    re-deriving 17.9 million plays.
    """
    from .database import secondary

    archive_path = Path(args.archive) if args.archive else ARCHIVE
    query_path = Path(args.database) if args.database else QUERY_DB
    for path, what in ((archive_path, "ingest"), (query_path, "derive")):
        if not path.exists():
            print(f"no database at {path}; run `rsse {what}` first",
                  file=sys.stderr)
            return 2

    conn = dbschema.connect(str(query_path))
    for pragma in dbschema.LOAD_PRAGMAS:
        conn.execute(pragma)
    archive = dbschema.connect(f"file:{archive_path}?mode=ro")

    def progress(season, stats):
        print(f"  {season} ... ({stats.comments:,} comments,"
              f" {stats.lineup_entries:,} lineup entries)",
              file=sys.stderr, flush=True)

    started = time.time()
    stats = secondary.build(conn, archive,
                            progress=progress if args.progress else None)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.commit()

    print(f"games                {stats.games:,}")
    print(f"comments             {stats.comments:,}")
    print(f"  by kind            {dict(sorted(stats.kinds.items()))}")
    print(f"  replay verdicts    {stats.replay_verdicts:,}")
    print(f"lineup_entries       {stats.lineup_entries:,}")
    print(f"  unreadable         {len(stats.malformed):,}")
    for raw in stats.malformed[:5]:
        print(f"    {raw[:100]}")
    print(f"wall clock           {(time.time() - started) / 60:.1f} min")
    conn.close()
    print(f"\n{ATTRIBUTION}")
    return 1 if stats.malformed else 0


def cmd_coverage(args: argparse.Namespace) -> int:
    """Rebuild or print the coverage table (spec/05-DATABASE.md §5)."""
    from .database import coverage as cov

    db_path = Path(args.database) if args.database else QUERY_DB
    if not db_path.exists():
        print(f"no query database at {db_path}", file=sys.stderr)
        return 2
    if args.rebuild:
        conn = dbschema.connect(str(db_path))
        rows = cov.build(conn)
        print(f"coverage rebuilt: {rows} (season, league) rows")
        conn.close()

    conn = dbschema.connect(f"file:{db_path}?mode=ro")
    where, params = "", ()
    if args.season_range:
        lo, hi = (int(x) for x in args.season_range.split(","))
        where, params = " WHERE season BETWEEN ? AND ?", (lo, hi)
    print(f"{'season':>7} {'lg':<4} {'games':>7} {'plays':>10} "
          f"{'pitches':>8} {'unparsed':>9} {'untrusted':>10}  dates")
    total_games = total_plays = 0
    for row in conn.execute(
            "SELECT season, league, games, plays, games_with_pitches,"
            " plays_unparsed, plays_inconsistent, first_date, last_date"
            f" FROM coverage{where} ORDER BY season, league", params):
        print(f"{row[0]:>7} {row[1]:<4} {row[2]:>7,} {row[3]:>10,} "
              f"{row[4]:>8,} {row[5]:>9,} {row[6]:>10,}  {row[7]}..{row[8]}")
        total_games += row[2]
        total_plays += row[3]
    print(f"\n{total_games:,} games, {total_plays:,} plays")
    print(f"\n{ATTRIBUTION}")
    conn.close()
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


#: Tables in the query database that `derive` does not rebuild. Kept as data
#: so a new build step cannot be forgotten here: a table that `--rebuild`
#: destroys and nothing recreates is a gap nobody would notice.
NON_DERIVED_TABLES = ("comments", "lineup_entries", "players", "teams", "parks")


def _non_derived_tables(path: Path) -> list[tuple[str, int]]:
    """`(table, row count)` for non-derived tables present and non-empty."""
    try:
        conn = dbschema.connect(f"file:{path}?mode=ro")
    except Exception:
        return []
    found = []
    try:
        present = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in NON_DERIVED_TABLES:
            if table in present:
                rows = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                if rows:
                    found.append((table, rows))
    except Exception:
        return found
    finally:
        conn.close()
    return found


def cmd_derive(args: argparse.Namespace) -> int:
    """Build the derived tables from the archive (spec/05-DATABASE.md §2-§4).

    Reads the archive, never the event files: the archive is the byte-exact
    record of what was ingested, and Retrosheet reissues corrected files, so a
    re-derive against the source tree is not the same operation.

    Nothing written here is a source of truth, so `--rebuild` is safe by
    construction -- it drops every derived table and starts over.
    """
    from .database import derived

    archive_path = Path(args.archive) if args.archive else ARCHIVE
    query_path = Path(args.database) if args.database else QUERY_DB
    if not archive_path.exists():
        print(f"no archive at {archive_path}; run `rsse ingest` first",
              file=sys.stderr)
        return 2

    query_path.parent.mkdir(parents=True, exist_ok=True)
    if args.rebuild and query_path.exists() and not args.keep_file:
        # Replace the file rather than dropping tables inside it. `DROP TABLE`
        # on 69 million `play_tags` rows and their indexes has to free every
        # page of an 11 GB file, which reads and rewrites the whole thing
        # before a single row is loaded -- measured at ~12 MB/s on an
        # encrypted volume, entirely in `D` state. Unlinking is instant and
        # leaves no freelist behind.
        #
        # Safe for the same reason `drop_derived` is: nothing here is a source
        # of truth. Curated tags are not lost either -- they are re-read from
        # `curated_tags.json` on every derive, which is what makes that file
        # the source of truth rather than the table (04-ONTOLOGY §8).
        # Replacing the file also takes out anything in it that `derive` does
        # not rebuild -- `comments` and `lineup_entries` are built by a
        # separate pass (`rsse secondary`). Losing them silently would be
        # worse than the slow path, so they are counted first and named.
        casualties = _non_derived_tables(query_path)
        for suffix in ("", "-wal", "-shm"):
            sibling = query_path.with_name(query_path.name + suffix)
            sibling.unlink(missing_ok=True)
        for table, rows in casualties:
            print(f"note: dropped {table} ({rows:,} rows) with the file; "
                  f"rebuild it with `rsse secondary`", file=sys.stderr)
    conn = dbschema.connect(str(query_path))
    for pragma in dbschema.LOAD_PRAGMAS:
        conn.execute(pragma)
    dbschema.create_query_db(conn)
    if args.rebuild:
        dbschema.drop_derived(conn)
    dbschema.create_derived(conn)

    archive = dbschema.connect(f"file:{archive_path}?mode=ro")

    seasons = None
    if args.season:
        seasons = [int(y) for y in args.season.split(",")]
    elif args.seasons:
        available = [r[0] for r in archive.execute(
            "SELECT DISTINCT season FROM source_files"
            " WHERE season IS NOT NULL ORDER BY season")]
        seasons = _spread(available, args.seasons)
        print(f"deriving {len(seasons)} seasons: "
              f"{', '.join(str(s) for s in seasons)}", file=sys.stderr)

    def progress(season, stats):
        # Fires when a season is first *seen*, so the count is the total
        # through the previous ones. Reading it as "this season produced N"
        # makes the last line look ~200k plays short of the final total.
        print(f"  starting {season}  ({stats.plays:,} plays derived so far)",
              file=sys.stderr, flush=True)

    started = time.time()
    stats = derived.build(
        conn, archive, parser_version=PARSER_VERSION, seasons=seasons,
        limit=args.limit, progress=progress if args.progress else None)

    if not args.no_indexes:
        print("building indexes ...", file=sys.stderr, flush=True)
        dbschema.create_derived_indexes(conn)
        conn.execute("ANALYZE")
        conn.commit()

    # Checkpoint before measuring: in WAL mode the rows just written live in
    # the -wal file, and the main database reads as ~0 bytes. The first run
    # reported 1.0 bytes per play.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.commit()
    elapsed = time.time() - started
    size = query_path.stat().st_size

    print(f"games                {stats.games:,}")
    print(f"plays                {stats.plays:,}")
    print(f"  unparsed           {stats.unparsed:,}")
    print(f"runner_advances      {stats.advances:,}")
    print(f"fielding_credits     {stats.credits:,}")
    print(f"credit_sequences     {stats.sequences:,}")
    print(f"play_tags            {stats.tags:,}   (curated {stats.curated:,})")
    print(f"parse status         {dict(sorted(stats.status.items()))}")
    print(f"database             {size / 1e9:.2f} GB  ({query_path})")
    if stats.plays:
        print(f"bytes per play       {size / stats.plays:.1f}")
    print(f"wall clock           {elapsed / 60:.1f} min")

    if args.report:
        Path(args.report).write_text(json.dumps({
            "games": stats.games, "plays": stats.plays,
            "unparsed": stats.unparsed, "advances": stats.advances,
            "credits": stats.credits, "sequences": stats.sequences,
            "tags": stats.tags, "curated": stats.curated,
            "status": stats.status,
            "bytes": size, "seconds": round(elapsed, 1),
            "seasons": seasons, "limit": args.limit,
        }, indent=2))
        print(f"\nreport written to {args.report}")

    print(f"\n{ATTRIBUTION}")
    return 0


def _spread(values: list, n: int) -> list:
    """``n`` values spread end to end across ``values``, inclusive of both.

    A `[::step]` slice drops the tail, and the tail is where the newest
    encodings live -- see cmd_tags.
    """
    if n >= len(values):
        return list(values)
    if n == 1:
        return values[-1:]
    last = len(values) - 1
    return sorted({values[round(i * last / (n - 1))] for i in range(n)})


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

    d = sub.add_parser("derive", help="build the derived tables from the archive")
    d.add_argument("--archive", help=f"archive path (default {ARCHIVE})")
    d.add_argument("--database", help=f"query database path (default {QUERY_DB})")
    d.add_argument("--seasons", type=int,
                   help="sample N seasons spread across the corpus")
    d.add_argument("--season", help="explicit season(s), comma separated")
    d.add_argument("--limit", type=int, help="first N games")
    d.add_argument("--rebuild", action="store_true",
                   help="replace the query database; safe, nothing in it is a "
                        "source of truth")
    d.add_argument("--keep-file", action="store_true",
                   help="with --rebuild, drop the tables in place instead of "
                        "replacing the file (slower; keeps any non-derived "
                        "tables such as comments and lineup_entries)")
    d.add_argument("--no-indexes", action="store_true",
                   help="skip index creation, for a timing run")
    d.add_argument("--progress", action="store_true")
    d.add_argument("--report", help="write a JSON report to this path")
    d.set_defaults(func=cmd_derive)

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

    q = sub.add_parser("query", help="search the derived tables")
    _add_query_flags(q)
    q.add_argument("--format", choices=["table", "json", "csv"],
                   default="table")
    q.set_defaults(func=cmd_query)

    x = sub.add_parser("explain", help="show the SQL and plan for a search")
    _add_query_flags(x)
    x.set_defaults(func=cmd_explain)

    gl = sub.add_parser("gamelogs",
                        help="download/load Retrosheet game logs")
    gl.add_argument("--fetch", action="store_true", help="download first")
    gl.add_argument("--dir", help="where the game log files live")
    gl.add_argument("--archive")
    gl.add_argument("--progress", action="store_true")
    gl.set_defaults(func=cmd_gamelogs)

    rc = sub.add_parser("reconcile",
                        help="check replayed scores against the game logs")
    rc.add_argument("--archive")
    rc.add_argument("--database")
    rc.add_argument("--season", help="comma-separated seasons")
    rc.add_argument("--show", type=int, default=20,
                    help="how many mismatches to print")
    rc.add_argument("--explain-er", action="store_true",
                    help="test which earned-run field is the team total")
    rc.add_argument("--report", help="write a JSON report to this path")
    rc.set_defaults(func=cmd_reconcile)

    sc = sub.add_parser("secondary",
                        help="build comments and lineup_entries")
    sc.add_argument("--archive")
    sc.add_argument("--database")
    sc.add_argument("--progress", action="store_true")
    sc.set_defaults(func=cmd_secondary)

    cv = sub.add_parser("coverage", help="what the corpus actually covers")
    cv.add_argument("--database")
    cv.add_argument("--rebuild", action="store_true")
    cv.add_argument("--season-range", help="LO,HI inclusive")
    cv.set_defaults(func=cmd_coverage)

    v = sub.add_parser("verify", help="check raw-layer integrity")
    v.add_argument("--archive")
    v.add_argument("--derived", action="store_true",
                   help="check the derived tables instead of the archive")
    v.add_argument("--database")
    v.set_defaults(func=lambda a: (cmd_verify_derived(a) if a.derived
                                   else cmd_verify(a)))

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except BrokenPipeError:
        # `rsse coverage | head` is an ordinary thing to do, and every one of
        # these commands prints more than a screenful. Python's default is to
        # raise here and then complain again at shutdown when it flushes
        # stdout, so the fd is pointed at devnull before returning.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
