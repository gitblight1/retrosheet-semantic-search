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
from .util import download
from .util.download import available_years, fetch_season

VERSION = "0.1.0"
PARSER_VERSION = "0.1.0"
ARCHIVE_DEFAULT = "archive.db"     # verbatim source records (spec/05 §7)
QUERY_DEFAULT = "rsse.db"          # derived, queryable tables

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
EVENTS = DATA / "events"
GAMELOGS = DATA / "gamelogs"
PARKS = DATA / "parks"
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
    aux = getattr(args, "aux", False) or getattr(args, "aux_only", False)
    parks = Path(args.parks) if getattr(args, "parks", None) else PARKS
    if getattr(args, "aux_only", False):
        return _fetch_aux(args, parks)

    years = available_years()
    if args.since:
        years = [y for y in years if y >= args.since]
    if args.until:
        years = [y for y in years if y <= args.until]
    refresh = getattr(args, "refresh", False)
    verb = "checking" if refresh else "fetching"
    print(f"{verb} {len(years)} seasons ({years[0]}-{years[-1]}) into {EVENTS}")
    if refresh:
        # Said before the requests start, not after: this is 118 requests at
        # the deliberate 5s delay, against a volunteer-run server that has
        # stopped answering this project once already.
        print(f"  {len(years)} conditional requests, "
              f"~{len(years) * download.DELAY_SECONDS / 60:.0f} min", flush=True)

    state = download.load_state(EVENTS) if refresh else None
    counts: collections.Counter[str] = collections.Counter()
    changed_files: list[Path] = []
    try:
        for year in years:
            if (EVENTS / str(year)).exists() and not args.force and not refresh:
                counts["present"] += 1
                continue
            got = fetch_season(year, EVENTS, refresh=refresh, state=state)
            counts[got.status] += 1
            changed_files.extend(got.changed_files)
            if got.status == "changed":
                print(f"  {year} CHANGED: {len(got.changed_files)} files",
                      flush=True)
            elif got.status == "downloaded":
                print(f"  {year} ok", flush=True)
    finally:
        # Saved even if the run is interrupted: the validators already
        # collected are what make the *next* refresh cheap, and throwing them
        # away would mean re-requesting every season in full.
        if state is not None:
            download.save_state(EVENTS, state)

    for status in ("present", "downloaded", "unchanged", "changed", "failed"):
        if counts[status]:
            print(f"{status:12s} {counts[status]}")
    if changed_files:
        print(f"\n{len(changed_files)} files reissued by Retrosheet:")
        for path in changed_files[: args.top or 20]:
            print(f"  {path}")
        print("\nThe archive still holds the previous vintage. Load the new"
              " one with `rsse ingest --refresh`, which replaces exactly these"
              " files' records and leaves the rest of the corpus untouched.")

    # After the seasons, not before: the auxiliary archive is one 335 MB
    # request and the season pass is 118, so a failure here is worth reporting
    # against a corpus that is already on disk rather than one that is not.
    rc = _fetch_aux(args, parks) if aux else 0
    return rc or (0 if not counts["failed"] else 1)


def _fetch_aux(args: argparse.Namespace, parks: Path) -> int:
    """The Negro Leagues archive, for `fetch --aux` and `fetch --aux-only`."""
    refresh = getattr(args, "refresh", False)
    state = download.load_state(EVENTS) if refresh else None
    print(f"{'checking' if refresh else 'fetching'} the Negro Leagues archive"
          f" into {parks} and {EVENTS / download.NGL_ROSTER_DIR}")
    if not refresh:
        print("  one request, ~335 MB", flush=True)

    got = download.fetch_aux(EVENTS, parks, refresh=refresh, state=state)
    if state is not None:
        download.save_state(EVENTS, state)

    if not got.ok:
        return 1
    print(f"{got.status:12s} {len(got.csvs)} csv, {got.rosters:,} rosters")

    # Counted rather than assumed: an archive that no longer carries one of
    # these leaves `reference` or `appearances` to fail several steps later,
    # with nothing pointing back at the download.
    missing = sorted(set(download.aux_csv_names())
                     - {p.name.lower() for p in got.csvs})
    if missing:
        print(f"  missing from the archive: {', '.join(missing)}",
              file=sys.stderr)
        return 1
    if not got.rosters:
        print("  no .ROS files in the archive; 283 Negro Leagues team-seasons"
              " would have a lineup and no names behind it", file=sys.stderr)
        return 1
    return 0


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

    # `--aux-only` exists because `--aux` is additive on top of this pass, and
    # adding rosters and parks to an archive should not mean re-offering 2,646
    # event files. It did once, under a different path spelling, and loaded a
    # second complete copy of the corpus (spec/05-DATABASE.md §1.1). Resolving
    # the path made that re-offer a cheap skip rather than a hazard, so this
    # flag is now about not re-hashing 700 MB to touch a different directory.
    aux_only = getattr(args, "aux_only", False)
    files = [] if aux_only else dbload.event_files(root)
    if args.limit:
        files = files[: args.limit]
    if not files and not aux_only:
        print(f"no event files under {root}; run `rsse fetch` first", file=sys.stderr)
        return 2

    conn = dbschema.connect(str(db_path))
    for pragma in dbschema.LOAD_PRAGMAS:
        conn.execute(pragma)
    dbschema.create(conn)
    # Before anything is written. `CREATE TABLE IF NOT EXISTS` leaves an
    # existing archive on the schema it was built with, so an archive from
    # before `allplayers.csv` had a kind would take the new file half way
    # through an ingest and then fail its CHECK.
    for change in dbschema.migrate_archive(conn):
        print(f"migrated: {change}", file=sys.stderr)
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
                              progress=progress,
                              refresh=getattr(args, "refresh", False))
    except dbload.CorpusChanged as exc:
        print(f"\nCORPUS CHANGED: {exc}", file=sys.stderr)
        return 3

    aux_stats = None
    if args.aux or aux_only:
        # Rosters, team files, ballparks and biographies: the same archive,
        # its own tables, because a record belonging to no game cannot live in
        # one partitioned by game (spec/05-DATABASE.md §1.2).
        from .database import auxiliary
        aux_files = auxiliary.discover(root, Path(args.parks) if args.parks
                                       else PARKS)
        if not aux_files:
            print(f"no auxiliary files under {root}", file=sys.stderr)
        else:
            def aux_progress(done, total, st):
                if args.progress:
                    print(f"  aux {done}/{total} ... {st.records:,} records",
                          file=sys.stderr, flush=True)
            aux_stats = auxiliary.AuxStats()
            try:
                auxiliary.ingest(conn, corpus_id, aux_files, aux_stats,
                                 progress=aux_progress,
                                 verify=not args.no_verify)
            except dbload.CorpusChanged as exc:
                print(f"\nCORPUS CHANGED: {exc}", file=sys.stderr)
                return 3

    if not args.no_index:
        print("building indexes ...", file=sys.stderr, flush=True)
        dbschema.create_indexes(conn)
    conn.execute("ANALYZE")
    conn.commit()
    if aux_stats is not None:
        print(f"auxiliary            {aux_stats.summary()}")
        for path, why in aux_stats.roundtrip_failures[: args.top or 5]:
            print(f"  ROUND-TRIP FAILED  {why}", file=sys.stderr)

    if aux_only:
        size = db_path.stat().st_size
        print(f"archive               {db_path} ({size / 1e9:.2f} GB)")
        conn.close()
        print(f"\n{ATTRIBUTION}")
        return 0 if not (aux_stats and aux_stats.roundtrip_failures) else 1

    print(stats.summary())
    if stats.refreshed:
        total = sum(len(keys) for *_r, keys in stats.refreshed)
        print(f"refreshed files       {len(stats.refreshed)}"
              f"  ({total:,} games replaced)")
        for path, old_digest, new_digest, keys in stats.refreshed[: args.top or 20]:
            print(f"  {Path(path).name}  {old_digest[:12]} -> {new_digest[:12]}"
                  f"  {len(keys)} games")
        # The derived database still holds rows for game keys that no longer
        # exist in the archive. Naming the command is the whole point: saying
        # only "the derived tables are stale" is the half-answer that has sent
        # a user to run the wrong pass twice (BUILD-LOG §3.32, §3.40).
        print("\nThe derived database is now behind the archive. Bring it back"
              " into agreement with `rsse derive --refresh`, which rebuilds"
              " only the affected games.")
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
    # Three games are genuinely recorded twice; the list is short enough to
    # read, and stays readable only because the next check exists.
    print(f"repeated game ids    {len(dupes)}"
          + (f"  {[d[0] for d in dupes][:12]}" if dupes else ""))

    files, recorded = conn.execute(
        "SELECT count(*), sum(record_count) FROM source_files").fetchone()
    print(f"source files         {files}")
    if recorded != records:
        failures.append(f"source_files sums to {recorded:,}, raw_records has {records:,}")

    # Every check above is a *partition* check: the spans tile the records, the
    # files sum to the records, nothing overlaps. A corpus ingested twice
    # satisfies all of them. It did: `rsse ingest` followed by `rsse ingest
    # --path data/events` spelled the same 2,646 files two ways, and every
    # count above simply doubled -- 62,230,544 records tiled exactly by 406,570
    # spans, declared exactly by 5,292 files. Three green gates on a corpus
    # with two of everything, because a partition of a doubled corpus is still
    # a partition.
    #
    # This is the check shaped for that class: two files with identical content
    # in one corpus. It is not a statement about partitioning at all, which is
    # the point.
    same = conn.execute(
        "SELECT count(*) FROM (SELECT sha256 FROM source_files"
        " GROUP BY corpus_id, sha256 HAVING count(*) > 1)").fetchone()[0]
    print(f"files ingested twice {same}")
    if same:
        example = conn.execute(
            "SELECT group_concat(path, ' == ') FROM source_files WHERE sha256 ="
            " (SELECT sha256 FROM source_files GROUP BY corpus_id, sha256"
            "  HAVING count(*) > 1 LIMIT 1)").fetchone()[0]
        failures.append(f"{same} files ingested more than once, e.g. {example}")

    has_revisions = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        " AND name = 'file_revisions'").fetchone()[0]
    if has_revisions:
        revisions = conn.execute(
            "SELECT path, old_sha256, new_sha256, replaced_at, games_replaced"
            " FROM file_revisions ORDER BY replaced_at DESC").fetchall()
        # Printed whenever there are any: a corpus that has been refreshed is
        # not the corpus that was first ingested, and that is exactly the fact
        # a published answer may need to be re-checked against.
        if revisions:
            print(f"files reissued       {len(revisions)}")
            for path, old_sha, new_sha, when, games in revisions[:10]:
                print(f"  {Path(path).name}  {old_sha[:8]} -> {new_sha[:8]}"
                      f"  {when}  {games} games")

    # --- auxiliary records: the same two shapes, asserted separately.
    # Kept apart from the checks above on purpose. `source_files` and
    # `raw_records` still mean exactly what they meant -- files of game
    # records, partitioned by game -- and these say the same things about the
    # files that belong to no game (spec/05-DATABASE.md §1.2).
    has_aux = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        " AND name = 'aux_files'").fetchone()[0]
    if has_aux:
        aux_files, aux_declared = conn.execute(
            "SELECT count(*), coalesce(sum(record_count), 0) FROM aux_files"
        ).fetchone()
        aux_records = conn.execute(
            "SELECT count(*) FROM aux_records").fetchone()[0]
        print(f"auxiliary files      {aux_files}")
        print(f"auxiliary records    {aux_records:,}")
        if aux_declared != aux_records:
            failures.append(f"aux_files declares {aux_declared:,},"
                            f" aux_records has {aux_records:,}")
        orphans = conn.execute(
            "SELECT count(*) FROM aux_records r"
            " LEFT JOIN aux_files f USING (aux_file_id)"
            " WHERE f.aux_file_id IS NULL").fetchone()[0]
        if orphans:
            failures.append(f"{orphans:,} aux_records with no file")

        # The archive's promise is bytes. For event files that is enforced per
        # event string; here it is enforced per file, by rebuilding it.
        if not args.quick:
            from .database.auxiliary import check_roundtrip
            bad = []
            for (aux_id,) in conn.execute(
                    "SELECT aux_file_id FROM aux_files ORDER BY aux_file_id"):
                failure = check_roundtrip(conn, aux_id)
                if failure:
                    bad.append(failure)
            print(f"auxiliary round-trip {len(bad)} failures")
            failures.extend(bad[:5])

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

#: Run only when `people` is present, since the reference tables are built by
#: their own pass (spec/05-DATABASE.md §8). Every one of these is at zero only
#: because a row is written for every id the corpus names, with null
#: attributes where no file explains it -- omitting those would turn a person
#: nobody recorded into a broken join.
REFERENCE_CHECKS = [
    ("lineup player not in people", False,
     "SELECT l.player_id FROM (SELECT DISTINCT player_id FROM lineup_entries) l"
     " LEFT JOIN people p ON p.person_id = l.player_id"
     " WHERE p.person_id IS NULL"),
    ("batter not in people", False,
     "SELECT b.batter_id FROM (SELECT DISTINCT batter_id FROM plays) b"
     " LEFT JOIN people p ON p.person_id = b.batter_id"
     " WHERE p.person_id IS NULL"),
    ("roster entry not in people", False,
     "SELECT r.person_id FROM (SELECT DISTINCT person_id FROM roster_entries) r"
     " LEFT JOIN people p ON p.person_id = r.person_id"
     " WHERE p.person_id IS NULL"),
    ("game site not in parks", False,
     "SELECT g.site FROM (SELECT DISTINCT site FROM games"
     "                     WHERE site IS NOT NULL AND site <> '') g"
     " LEFT JOIN parks p ON p.park_id = g.site WHERE p.park_id IS NULL"),
    ("team-season not in teams", False,
     "SELECT x.t FROM (SELECT DISTINCT home_team AS t, season FROM games"
     "                  UNION SELECT DISTINCT away_team, season FROM games) x"
     " LEFT JOIN teams tm ON tm.team_id = x.t AND tm.season = x.season"
     " WHERE tm.team_id IS NULL"),
    # A park date that will not parse is worse than one that is absent: it
    # silently drops out of every range comparison instead of being counted.
    ("park date present but unparseable", False,
     "SELECT park_id FROM parks"
     " WHERE (start_date IS NOT NULL AND start_iso IS NULL)"
     "    OR (end_date IS NOT NULL AND end_iso IS NULL)"),
]

#: Run only when `earned_runs` is present, since it is built by its own pass
#: (spec/05-DATABASE.md §5.4) and a database without it is not broken.
EARNED_RUN_CHECKS = [
    ("orphan earned run -> plays", False,
     "SELECT e.play_id FROM earned_runs e LEFT JOIN plays p USING (play_id)"
     " WHERE p.play_id IS NULL"),
    # The one that matters. A run with no verdict is not a run called earned
    # -- it is a run nobody looked at, and it would sink into every total
    # here without moving a single agreement percentage.
    ("scored advance with no verdict", False,
     "SELECT a.play_id FROM runner_advances a"
     " LEFT JOIN earned_runs e ON e.play_id = a.play_id AND e.adv_seq = a.seq"
     " WHERE a.scored = 1 AND e.play_id IS NULL"),
    ("verdict on an advance that did not score", False,
     "SELECT e.play_id FROM earned_runs e"
     " JOIN runner_advances a ON a.play_id = e.play_id AND a.seq = e.adv_seq"
     " WHERE a.scored = 0"),
    # `ambiguous` and NULL are two spellings of one statement and must never
    # come apart: a certain verdict marked ambiguous is thrown away, and a
    # NULL without it reads as a missing value rather than a deliberate one.
    ("ambiguous verdict that claims an answer", False,
     "SELECT play_id FROM earned_runs"
     " WHERE certainty = 'ambiguous' AND earned_team IS NOT NULL"),
    # `untrusted` is not the same statement. It says the half-inning's state
    # is unreliable, not that the rule declined -- so a categorical verdict
    # about *this runner* ("he reached on an error") survives it, while one
    # that leans on the out count does not. Both spellings are legal there;
    # what is not legal is a NULL with a confident certainty on it.
    ("no verdict, and no reason given for having none", False,
     "SELECT play_id FROM earned_runs"
     " WHERE earned_team IS NULL"
     "   AND certainty NOT IN ('ambiguous','untrusted')"),
    ("earned for the team, unearned for the pitcher", False,
     "SELECT play_id FROM earned_runs"
     " WHERE earned_team = 1 AND earned_pitcher = 0"),
]

#: `ReplayOverturned` is the only tag whose deciding evidence is not in the
#: event string -- whether a call was reversed is in the linked `com` record
#: alone. That makes it the only tag checkable against something it is not
#: derived from, and until these ran, nothing was: the tag sat 85 short of the
#: comments for two rebuilds, hiding two separate bugs (03-STATE §6.7).
#:
#: Stated as *both conditions* rather than as a count, because 38 plays carry
#: a comment saying a call was reversed on an event string that never said the
#: play was reviewed -- a gap in Retrosheet's annotation, concentrated in
#: 2014-2017, not a gap in the derivation.
#:
#: Both drive from `comments` (5,102 replay rows) rather than `plays` (17.9M):
#: the natural phrasing scans every play to test the modifier.
_REVERSED_COMMENT = (
    "SELECT c.play_id FROM comments c WHERE c.kind = 'replay'"
    " AND c.play_id IS NOT NULL"
    " AND json_extract(c.payload, '$.reversed') = 1")
_OVERTURNED_TAG = (
    "SELECT t.play_id FROM play_tags t JOIN tags g USING (tag_id)"
    " WHERE g.name = 'ReplayOverturned'")

REPLAY_CHECKS = [
    ("reviewed and reversed but not tagged", False,
     f"SELECT c.play_id FROM ({_REVERSED_COMMENT}) c"
     f" JOIN plays p ON p.play_id = c.play_id"
     f" WHERE (p.event_raw LIKE '%MREV%' OR p.event_raw LIKE '%UREV%')"
     f" AND c.play_id NOT IN ({_OVERTURNED_TAG})"),
    ("tagged overturned with no reversal linked", False,
     f"SELECT play_id FROM ({_OVERTURNED_TAG})"
     f" WHERE play_id NOT IN ({_REVERSED_COMMENT})"),
]


#: `plays.event_location` is a *lifted copy* of something already in
#: `event_modifiers`, and a lifted copy is exactly the kind of column that
#: drifts from its source without anything noticing -- a misaligned positional
#: insert writes 38 plausible values into 38 columns and fails nothing.
#:
#: The first check is the content test: a location is emitted as the tail of
#: its `hit` modifier, so it must be a suffix of one of them. It is honestly a
#: weak gate for the short values -- a column wrongly reading `8` on a play
#: carrying `/E8` would pass it -- and it is a total one for the failure it is
#: actually shaped for, a column that stopped corresponding to its row at all.
#: The second is the shape test, straight off the grammar: `zone
#: { loc_qualifier }`, so the first character is a digit and an empty string
#: is not a location but a missing one, which is what NULL is for.
#:
#: It is spelled with `substr` and a range rather than a GLOB character class,
#: because the first version was `GLOB '[!0-9]*'` and **SQLite negates a GLOB
#: class with `^`, not `!`** -- `!` is read as a literal member of the class.
#: That check matched every string starting with `!` or a digit, which is to
#: say every valid location, and it reported all 4,999,462 located plays as
#: malformed on the first database that had any. A gate nothing has ever seen
#: fire is a gate that may be inverted, so both of these now have tests that
#: assert they fire on a broken row *and* stay silent on a good one.
LOCATION_CHECKS = [
    ("location not in its own modifier list", False,
     "SELECT p.play_id FROM plays p WHERE p.event_location IS NOT NULL"
     " AND NOT EXISTS (SELECT 1 FROM json_each(p.event_modifiers) j"
     "                  WHERE j.value LIKE '%' || p.event_location)"),
    ("location is not zone-shaped", False,
     "SELECT play_id FROM plays WHERE event_location IS NOT NULL"
     " AND (event_location = ''"
     "      OR substr(event_location, 1, 1) NOT BETWEEN '0' AND '9')"),
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

    checks = list(DERIVED_CHECKS)
    if conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'"
                    " AND name = 'earned_runs'").fetchone()[0]:
        checks += EARNED_RUN_CHECKS
    else:
        print("note: no earned_runs table; run `rsse earned-runs` to add"
              f" {len(EARNED_RUN_CHECKS)} more checks\n", file=sys.stderr)
    if conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'"
                    " AND name = 'people'").fetchone()[0]:
        checks += REFERENCE_CHECKS
    else:
        print("note: no reference tables; run `rsse reference` to add"
              f" {len(REFERENCE_CHECKS)} more checks\n", file=sys.stderr)
    if conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'"
                    " AND name = 'comments'").fetchone()[0]:
        checks += REPLAY_CHECKS
    else:
        print("note: no comments table; run `rsse secondary` to add"
              f" {len(REPLAY_CHECKS)} more checks\n", file=sys.stderr)
    if any(r[1] == "event_location"
           for r in conn.execute("PRAGMA table_info(plays)")):
        checks += LOCATION_CHECKS
    else:
        print("note: plays has no event_location column; it was derived before"
              f" the column existed. A rebuild adds {len(LOCATION_CHECKS)} more"
              " checks -- and `.hit_location()` needs it too.\n",
              file=sys.stderr)

    failed = 0
    for name, trusted_only, sql in checks:
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

    print(f"\n{len(checks) - failed}/{len(checks)} checks pass")
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
    # Names, resolved against the reference tables. Separate flags rather than
    # letting `--batter` take either: `ruthb101` and `Babe Ruth` are never
    # confusable, but a flag that silently accepts both hides which one
    # failed when neither resolves.
    ("--batter-named", "batter_named", str),
    ("--park-named", "park_named", str),
    ("--team-named", "team_named", str),
    ("--out-at", "out_at", str), ("--batter-ran", "batter_ran", str),
    ("--error", "error", int), ("--hit-location", "hit_location", str),
    ("--position-played", "position_played", str),
    ("--event-matches", "event_matches", str),
]
_QUERY_SWITCHES = [
    ("--bases-loaded", "bases_loaded"), ("--bases-empty", "bases_empty"),
    ("--scoring-position", "scoring_position"),
    ("--hit-located", "hit_located"),
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


def cmd_bench(args: argparse.Namespace) -> int:
    """Run the pinned query benchmarks (spec/07-TESTING.md §5)."""
    from . import bench as benchmod
    from .query import connect

    db_path = Path(args.database) if args.database else QUERY_DB
    if not db_path.exists():
        print(f"no query database at {db_path}; run `rsse derive` first",
              file=sys.stderr)
        return 2
    conn = connect(str(db_path))

    def progress(b):
        print(f"  {b.name} ...", file=sys.stderr, flush=True)

    results = benchmod.run_all(conn, repeats=args.repeats, only=args.only,
                               progress=progress if args.progress else None)

    print(f"{'benchmark':24s} {'median':>9} {'best':>9} {'budget':>9} "
          f"{'rows':>10}  plan")
    failed = 0
    for r in results:
        mark = "ok  " if r.ok else "OVER"
        failed += 0 if r.ok else 1
        plan = "SCANS plays" if r.scans_plays else "indexed"
        print(f"{mark} {r.name:19s} {r.median_ms:>8.1f}ms "
              f"{r.best_ms:>8.1f}ms {r.budget_ms:>8.0f}ms "
              f"{r.rows:>10,}  {plan}")
    print(f"\n{len(results) - failed}/{len(results)} within budget")

    if args.report:
        Path(args.report).write_text(json.dumps({
            "corpus": _corpus_id(conn),
            "results": [{"name": r.name, "budget_ms": r.budget_ms,
                         "median_ms": round(r.median_ms, 2),
                         "best_ms": round(r.best_ms, 2), "rows": r.rows,
                         "scans_plays": r.scans_plays, "note": r.note}
                        for r in results],
        }, indent=2))
        print(f"report written to {args.report}")
    conn.close()
    return 1 if failed else 0


def _corpus_id(conn) -> str:
    """Identify the data a benchmark ran against.

    A timing without one is not comparable with anything: these numbers move
    with the corpus, not just with the code.
    """
    row = conn.execute("SELECT derived_at, games, plays FROM derive_runs"
                       " ORDER BY derive_id DESC LIMIT 1").fetchone()
    return (f"derived {row[0]}, {row[1]:,} games, {row[2]:,} plays"
            if row else "unknown")


def cmd_gamelogs(args: argparse.Namespace) -> int:
    """Download and/or load Retrosheet's game logs (spec/07-TESTING.md §4)."""
    from .database import gamelogs
    from .util import download

    root = Path(args.dir) if args.dir else GAMELOGS
    if args.fetch:
        print(f"fetching game logs into {root} ...", file=sys.stderr)
        download.fetch_gamelogs(root)

    # Recursive: the postseason and all-star archives extract into their own
    # subdirectory, and leaving them out is what made the coverage gap a guess
    # from the calendar rather than a fact (05-DATABASE §5.3).
    files = sorted(p for p in root.rglob("*")
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
    from .database.gamelogs import has_series_column
    if not has_series_column(archive):
        print("note: this archive predates the `series` column, so postseason "
              "and all-star games cannot be told from missing event files. "
              "Re-run `rsse gamelogs` to reclassify.", file=sys.stderr)
    query = dbschema.connect(f"file:{query_path}?mode=ro")

    seasons = [int(y) for y in args.season.split(",")] if args.season else None
    result = gamelogs.reconcile(query, archive, seasons=seasons)

    print(f"games compared       {result.compared:,}")
    print(f"scores agree         {result.agreed:,}  ({result.rate:.4%})")
    print(f"scores disagree      {len(result.mismatches):,}")
    # Coverage, not error -- but only once it is broken down. A single
    # "34,393 games in the logs only" reads as a defect until you know that
    # 29,133 of them predate the corpus and 1,992 are postseason and all-star
    # games the event files never contained.
    from .database.gamelogs import SERIES_LABELS
    print(f"\nin the logs only     {result.log_only:,}")
    print(f"  before the corpus  {result.log_only_before_corpus:,}")
    for code, n in sorted(result.log_only_special.items(),
                          key=lambda kv: -kv[1]):
        print(f"  {SERIES_LABELS.get(code, code):18s} {n:,}")
    print(f"  NO EVENT FILE      {result.log_only_gap:,}"
          "   <- the real coverage gap")
    if result.gap_by_season:
        worst = sorted(result.gap_by_season.items(), key=lambda kv: -kv[1])[:5]
        print("    worst seasons:   "
              + ", ".join(f"{y} ({n})" for y, n in worst))
        modern = sum(n for y, n in result.gap_by_season.items() if y >= 1960)
        print(f"    1960 onward:     {modern:,}")
    print(f"in the replay only   {result.replay_only:,}"
          "   (Negro Leagues; the logs are Major League)")
    print(f"  log row skipped    {result.replay_only_skipped:,}")
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
            "log_only": result.log_only,
            "log_only_before_corpus": result.log_only_before_corpus,
            "log_only_special": result.log_only_special,
            "log_only_gap": result.log_only_gap,
            "gap_by_season": result.gap_by_season,
            "replay_only": result.replay_only,
            "replay_only_skipped": result.replay_only_skipped,
            "skipped_forfeit": result.skipped_forfeit,
            "skipped_incomplete": result.skipped_incomplete,
        }, indent=2))
        print(f"\nreport written to {args.report}")

    archive.close()
    query.close()
    print(f"\n{ATTRIBUTION}")
    return 1 if result.mismatches else 0


def cmd_earnedruns(args: argparse.Namespace) -> int:
    """Derive earned runs from the play-by-play, and check them.

    The last quantity in the corpus that Retrosheet states without saying how
    it got there. `--check` compares the derivation against all three of the
    figures Retrosheet publishes: the `(UR)`/`(TUR)` flag on every individual
    run, the per-pitcher `data,er` record, and the two per-team totals in the
    game logs (spec/07-TESTING.md §4.5).
    """
    from .database import earnedruns as er

    archive_path = Path(args.archive) if args.archive else ARCHIVE
    query_path = Path(args.database) if args.database else QUERY_DB
    for path, what in ((archive_path, "ingest"), (query_path, "derive")):
        if not path.exists():
            print(f"no database at {path}; run `rsse {what}` first",
                  file=sys.stderr)
            return 2

    archive = dbschema.connect(f"file:{archive_path}?mode=ro")
    seasons = [int(y) for y in args.season.split(",")] if args.season else None

    # `lineup_entries` is where the pitcher on the mound comes from (9.16(f)).
    # Without it this does not fail -- it writes every run with a NULL
    # pitcher, and since the per-run check barely depends on who was pitching,
    # the headline agreement would still come back near 99%. Only the
    # `data,er` comparison would collapse, and only if someone read it.
    probe = dbschema.connect(f"file:{query_path}?mode=ro")
    have_lineups = probe.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        " AND name = 'lineup_entries'").fetchone()[0]
    pitchers = probe.execute(
        "SELECT count(*) FROM lineup_entries WHERE position = 1"
    ).fetchone()[0] if have_lineups else 0
    probe.close()
    if not pitchers:
        print("no pitchers in lineup_entries; run `rsse secondary` first."
              " Without it every run would be charged to nobody, and the"
              " per-run check would not notice.", file=sys.stderr)
        return 2

    if not args.check_only:
        conn = dbschema.connect(str(query_path))
        for pragma in dbschema.LOAD_PRAGMAS:
            conn.execute(pragma)

        started = time.time()

        def progress(done, total):
            if done % 20000 < er.BATCH:
                print(f"  {done:,}/{total:,} games"
                      f"  ({time.time() - started:.0f}s)",
                      file=sys.stderr, flush=True)

        stats = er.build(conn, archive,
                         progress=progress if args.progress else None,
                         seasons=seasons)
        print(f"games                {stats.games:,}")
        print(f"  with a run in them {stats.scoring_games:,}")
        print(f"runs adjudicated     {stats.runs:,}")
        print(f"  settled by rule    {stats.by_certainty['derived']:,}")
        print(f"  graded likely      {stats.by_certainty['likely']:,}")
        # Not a failure. 9.16 hands these clauses to the scorer in its own
        # words, and a derivation that answered anyway would be inventing a
        # fact rather than deriving one (spec/03-STATE.md §9).
        print(f"  9.16 defers        {stats.undetermined:,}"
              f"  ({100 * stats.undetermined / max(stats.runs, 1):.2f}%)")
        print(f"elapsed              {time.time() - started:.0f}s")
        print("\nby rule:")
        for reason, n in stats.by_reason.most_common():
            print(f"  {n:>9,}  {reason}")
        conn.close()

    if args.check or args.check_only:
        query = dbschema.connect(f"file:{query_path}?mode=ro")
        result = er.reconcile(query, archive)
        _print_earned_check(result, args.show)
        if args.report:
            _write_earned_report(Path(args.report), result)
            print(f"\nreport written to {args.report}")
    return 0


def _print_earned_check(result, show: int) -> None:
    """Print the three-way comparison."""
    runs = result.runs
    print(f"\nruns compared        {runs.compared:,}")
    print(f"  agree              {runs.agree:,}  ({runs.rate:.4%})")
    print(f"  differ             {runs.differ:,}")
    print(f"9.16 defers          {runs.deferred:,}  -- no claim made")
    if runs.tur_before_notation:
        # `(TUR)` has 3 uses in 1911 and none again until 1969. A derived TUR
        # against a recorded UR in 1920 is a distinction the source could not
        # write down, and counting it as a disagreement would blame the rule
        # for a gap in the notation.
        print(f"TUR before 1969      {runs.tur_before_notation:,}"
              f"  -- notation not yet in use")

    print(f"\n{'decade':>7} {'compared':>9} {'agree':>8} {'deferred':>9}")
    for decade, (differ, agree, deferred) in runs.by_decade.items():
        total = differ + agree
        print(f"{decade:>7} {total:>9,} {100 * agree / max(total, 1):>7.2f}%"
              f" {100 * deferred / max(total + deferred, 1):>8.1f}%")

    print(f"\n{'source':<26} {'compared':>9} {'in bound':>9}"
          f" {'exact':>9} {'exact agree':>12}")
    for level in (result.pitchers, result.individual, result.team):
        print(f"{level.name:<26} {level.compared:>9,}"
              f" {level.within_rate:>8.2%} {level.exact:>9,}"
              f" {level.exact_rate:>11.2%}")

    if runs.by_reason:
        print("\nwhere the derivation and the flag disagree:")
        print(f"{'wrote':>6} {'derived':>8} {'certainty':>10} {'n':>8}  reason")
        for (wrote, derived, certainty, reason), n in \
                runs.by_reason.most_common(show):
            print(f"{wrote:>6} {derived:>8} {certainty:>10} {n:>8,}  {reason}")
    if result.skipped:
        print("\nnot compared:")
        for cause, n in result.skipped.most_common():
            print(f"  {n:>9,}  {cause}")


def _write_earned_report(path: Path, result) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    runs = result.runs
    path.write_text(json.dumps({
        "runs": {
            "compared": runs.compared, "agree": runs.agree,
            "differ": runs.differ, "rate": runs.rate,
            "deferred": runs.deferred,
            "tur_before_notation": runs.tur_before_notation,
            "by_decade": {str(k): v for k, v in runs.by_decade.items()},
            "disagreements": [
                {"recorded": k[0], "derived": k[1], "certainty": k[2],
                 "reason": k[3], "n": n}
                for k, n in runs.by_reason.most_common()],
        },
        "totals": {
            level.name: {
                "compared": level.compared, "within": level.within,
                "outside": level.outside, "exact": level.exact,
                "exact_agree": level.exact_agree,
            }
            for level in (result.pitchers, result.individual, result.team)},
        "skipped": dict(result.skipped),
    }, indent=2) + "\n")


def cmd_reference(args: argparse.Namespace) -> int:
    """Build `people`, `roster_entries`, `teams`, `franchises` and `parks`.

    Its own pass, from the archive's `aux_records` (spec/05-DATABASE.md §8).
    Needs `rsse ingest --aux` first, which is where the source files enter the
    archive at all.
    """
    from .database import reference as refdb

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
        " AND name='aux_files'").fetchone()[0]
    if not have or not archive.execute(
            "SELECT count(*) FROM aux_files").fetchone()[0]:
        print("no auxiliary files in the archive; run `rsse ingest --aux`"
              " first", file=sys.stderr)
        return 2

    # `reference` reads `lineup_entries` to find every person the corpus
    # names, so it has to follow `secondary`. Without it the build raises on a
    # missing table -- loud, but a traceback is a worse way to learn a build
    # order than a sentence.
    probe = dbschema.connect(f"file:{query_path}?mode=ro")
    have_lineups = probe.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        " AND name = 'lineup_entries'").fetchone()[0]
    probe.close()
    if not have_lineups:
        print("no lineup_entries; run `rsse secondary` first. Without it the"
              " people only the comments name -- 471 umpires who never bat --"
              " would be missing.", file=sys.stderr)
        return 2

    conn = dbschema.connect(str(query_path))
    for pragma in dbschema.LOAD_PRAGMAS:
        conn.execute(pragma)
    started = time.time()
    stats = refdb.build(conn, archive)

    print(f"people               {stats.people:,}")
    for source, n in sorted(stats.by_source.items(), key=lambda kv: -kv[1]):
        print(f"  from {source:<16} {n:,}")
    print(f"roster_entries       {stats.roster_entries:,}")
    if stats.team_field_conflicts:
        # 85 lines name a team other than the file they sit in. Kept, with the
        # stated value in `stated_team_id`, rather than silently resolved.
        print(f"  team field conflicts {stats.team_field_conflicts}"
              f"  (see roster_entries.stated_team_id)")
    print(f"teams                {stats.teams:,}")
    print(f"franchises           {stats.franchises:,}")
    print(f"parks                {stats.parks:,}")
    if stats.parks_observed_only:
        print(f"  observed only      {stats.parks_observed_only}"
              f"  (a game is played there and no file describes it)")
    outside = stats.games_outside_park_dates
    if outside:
        # Reported, deliberately not gated: `ballparks.csv` dates a park by
        # its Major League occupancy, not its lifetime.
        pct = 100 * stats.games_outside_park_dates_ngl / outside
        print(f"\ngames outside their park's stated dates  {outside:,}"
              f" of {stats.games_park_date_comparable:,}")
        print(f"  Negro Leagues                          "
              f"{stats.games_outside_park_dates_ngl:,}  ({pct:.0f}%)"
              f" -- the file dates Major League use only")
    print(f"\nelapsed              {time.time() - started:.1f}s")
    print(f"\n{ATTRIBUTION}")
    return 0


def cmd_appearances(args: argparse.Namespace) -> int:
    """Build `appearances` from `allplayers.csv` (spec/05-DATABASE.md §9).

    Its own pass for the reason the module docstring gives: every id in the
    file is already a person, so this is not identity data and does not belong
    to `reference`. Needs `rsse ingest --aux` for the source and `rsse
    secondary` for the lineups it measures the corpus with.
    """
    from .database import appearances as appdb

    archive_path = Path(args.archive) if args.archive else ARCHIVE
    query_path = Path(args.database) if args.database else QUERY_DB
    for path, what in ((archive_path, "ingest"), (query_path, "derive")):
        if not path.exists():
            print(f"no database at {path}; run `rsse {what}` first",
                  file=sys.stderr)
            return 2

    archive = dbschema.connect(f"file:{archive_path}?mode=ro")
    have = archive.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        " AND name = 'aux_files'").fetchone()[0]
    if have:
        have = archive.execute(
            "SELECT count(*) FROM aux_files WHERE kind = 'appearances'"
        ).fetchone()[0]
    if not have:
        # Distinguished from "no aux files at all" on purpose: an archive
        # ingested before `allplayers.csv` had a kind has every other
        # auxiliary file and none of this one, and "run `rsse ingest --aux`"
        # on its own would look like advice it had already taken.
        print("no allplayers.csv in the archive. If this archive predates the"
              " file having a kind, `rsse ingest --aux-only` migrates it and"
              " adds it in one pass.", file=sys.stderr)
        return 2

    probe = dbschema.connect(f"file:{query_path}?mode=ro")
    have_lineups = probe.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        " AND name = 'lineup_entries'").fetchone()[0]
    probe.close()
    if not have_lineups:
        print("no lineup_entries; run `rsse secondary` first. Without it every"
              " games_in_corpus would be zero, which reads as a finding rather"
              " than a missing table.", file=sys.stderr)
        return 2

    conn = dbschema.connect(str(query_path))
    for pragma in dbschema.LOAD_PRAGMAS:
        conn.execute(pragma)
    started = time.time()
    stats = appdb.build(conn, archive)
    conn.close()
    archive.close()

    lo, hi = stats.seasons
    print(f"appearances          {stats.rows:,}")
    print(f"  people             {stats.people:,}")
    print(f"  seasons            {lo}-{hi}")
    if stats.unknown_teams:
        print(f"  teams with no season file  "
              f"{', '.join(stats.unknown_teams)}")
    if stats.games_stated:
        pct = 100 * stats.games_held / stats.games_stated
        print(f"\ngames the file counts    {stats.games_stated:,}")
        print(f"games the corpus holds   {stats.games_held:,}  ({pct:.1f}%)")
        print(f"person-seasons with no surviving game  "
              f"{stats.absent_from_corpus:,} of {stats.rows:,}")
    if stats.more_in_corpus:
        # Not a gate. One of the two sources is wrong and this pass is not the
        # place to decide which -- but the number should be small and seen.
        print(f"rows where the corpus holds MORE than the file counts  "
              f"{stats.more_in_corpus:,}")
    print(f"\nelapsed              {time.time() - started:.1f}s")
    print(f"\n{ATTRIBUTION}")
    return 0


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
        archive_path = Path(args.archive) if args.archive else ARCHIVE
        if archive_path.exists():
            archive = dbschema.connect(f"file:{archive_path}?mode=ro")
            missing = cov.fill_missing_games(conn, archive)
            archive.close()
            if missing < 0:
                print("  games_missing left NULL: no game logs in the archive."
                      " Run `rsse gamelogs` to measure what the corpus lacks.")
            else:
                print(f"  games with no event file: {missing:,}")
        conn.close()

    conn = dbschema.connect(f"file:{db_path}?mode=ro")
    where, params = "", ()
    if args.season_range:
        lo, hi = (int(x) for x in args.season_range.split(","))
        where, params = " WHERE season BETWEEN ? AND ?", (lo, hi)
    print(f"{'season':>7} {'lg':<4} {'games':>7} {'missing':>8} "
          f"{'plays':>10} {'pitches':>8} {'unparsed':>9} {'untrusted':>10}"
          "  dates")
    total_games = total_plays = total_missing = 0
    #: Games in seasons the comparison could actually reach. The percentage
    #: below is over these only: counting a league whose missing games are
    #: unknown into the numerator would report better coverage than was
    #: measured.
    measured_games = 0
    unmeasured: list[tuple] = []
    for row in conn.execute(
            "SELECT season, league, games, plays, games_with_pitches,"
            " plays_unparsed, plays_inconsistent, first_date, last_date,"
            " games_missing"
            f" FROM coverage{where} ORDER BY season, league", params):
        missing = row[9]
        if missing is None:
            unmeasured.append((row[0], row[1]))
        else:
            total_missing += missing
            measured_games += row[2]
        print(f"{row[0]:>7} {row[1]:<4} {row[2]:>7,} "
              f"{('?' if missing is None else f'{missing:,}'):>8} "
              f"{row[3]:>10,} {row[4]:>8,} {row[5]:>9,} {row[6]:>10,}"
              f"  {row[7]}..{row[8]}")
        total_games += row[2]
        total_plays += row[3]
    print(f"\n{total_games:,} games, {total_plays:,} plays")
    # A NULL here has two causes and only one of them is fixable, which the
    # message has to say. When *nothing* is measured the game logs are simply
    # not loaded. When some rows are measured and others are not, the others
    # are leagues the game logs do not cover -- Retrosheet publishes Major
    # League logs only -- and no download will ever fill them. Telling the
    # user to run `rsse gamelogs` in that case sends them after a file that
    # exists and will not help, and suppresses the real total while doing it.
    if not measured_games:
        print("games with no event file: not measured -- no game logs in the"
              " archive.\n  Run `rsse gamelogs`, then `rsse coverage"
              " --rebuild`.")
    else:
        whole = measured_games + total_missing
        print(f"games with no event file: {total_missing:,} "
              f"({measured_games / whole:.2%} of known games held)")
        if unmeasured:
            leagues = sorted({lg for _season, lg in unmeasured})
            print(f"  not measurable for {len(unmeasured)} season-league rows"
                  f" ({', '.join(leagues)}): Retrosheet's game logs are Major"
                  "\n  League only, so these have no published game list to"
                  " compare against. The\n  column is NULL rather than 0"
                  " because nobody has counted, not because\n  nothing is"
                  " missing.")
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
#: Tables in the query database that `derive` does not build. `--rebuild`
#: replaces the file, so each of these is destroyed with it and has to be
#: named before that happens -- `coverage` included, which was missing from
#: this list and is the reason it silently vanished from the database.
#: Kept as table -> the command that rebuilds it, because naming the table
#: without naming its command is the half-answer that sent a user to run the
#: wrong pass: every casualty used to be reported as "rebuild it with `rsse
#: secondary`", which is true of two of the nine.
NON_DERIVED_TABLES = {
    "comments": "rsse secondary",
    "lineup_entries": "rsse secondary",
    "earned_runs": "rsse earned-runs --check",
    "coverage": "rsse coverage --rebuild",
    "people": "rsse reference",
    "roster_entries": "rsse reference",
    "teams": "rsse reference",
    "franchises": "rsse reference",
    "parks": "rsse reference",
    "appearances": "rsse appearances",
}


def _non_derived_tables(path: Path) -> list[tuple[str, int, str]]:
    """`(table, row count, rebuild command)` for those present and non-empty."""
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
                    found.append((table, rows, NON_DERIVED_TABLES[table]))
    except Exception:
        return found
    finally:
        conn.close()
    return found


def _derive_refresh(args: argparse.Namespace, archive_path: Path,
                    query_path: Path) -> int:
    """Bring the derived database back into agreement with the archive.

    Agreement is defined by comparing the two sets of game keys, not by
    anything remembered between runs: a file re-ingested with `ingest
    --refresh` loses its old `game_spans` rows and gains new ones with new
    keys, so its games appear here as stale keys to forget and missing keys to
    build. Nothing else in the corpus is touched.

    This is the half that makes `--refresh` worth having. Re-ingesting one
    reissued file takes seconds; without a targeted derive it still cost a
    79-minute rebuild, which is why the two halves had to be designed
    together.
    """
    from .database import derived

    if not query_path.exists():
        print(f"no derived database at {query_path}; run `rsse derive` first",
              file=sys.stderr)
        return 2
    conn = dbschema.connect(str(query_path))
    for pragma in dbschema.LOAD_PRAGMAS:
        conn.execute(pragma)
    archive = dbschema.connect(f"file:{archive_path}?mode=ro")

    try:
        stale, missing = derived.stale_and_missing(conn, archive)
    except derived.NoSourceDigests:
        if not getattr(args, "adopt", False):
            print(
                "this derived database has no recorded source digests, so"
                " there is nothing to compare the archive against. It was"
                " built before `games.source_sha256` existed.\n\n"
                "  rsse derive --rebuild            rebuilds from the archive"
                " (~80 min, always correct)\n"
                "  rsse derive --refresh --adopt    records the archive's"
                " current digests as this database's vintage (seconds)\n\n"
                "`--adopt` asserts that the derived tables already agree with"
                " the archive. That is true of a database built from it and"
                " not since re-ingested; it is false, silently, of one built"
                " from an earlier vintage of any file.",
                file=sys.stderr)
            return 2
        adopted = derived.adopt_digests(conn, archive)
        # Deliberately not followed by a comparison. Adopting *writes* the
        # archive's digests onto these games, so re-running the check here
        # reports zero stale and zero missing every time, on a database that
        # agrees with the archive and equally on one that does not. Printing
        # that as a result would dress a tautology up as a verification --
        # exactly the shape of the reconciler that agreed with itself
        # (BUILD-LOG §3.42). The first comparison that means anything is the
        # next one, after the next re-ingest.
        print(f"adopted source digests for {adopted:,} games\n\n"
              "these games are now on record as coming from the files the"
              " archive holds today.\nNothing was compared: an adopt is an"
              " assertion, and after one the two agree by\nconstruction."
              " `rsse derive --refresh` measures something from here on.")
        conn.close()
        print(f"\n{ATTRIBUTION}")
        return 0
    print(f"stale games          {len(stale):,}  (in the derived db, not the archive)")
    print(f"missing games        {len(missing):,}  (in the archive, not derived)")
    if not stale and not missing:
        print("\nalready in agreement; nothing to do")
        return 0

    if stale:
        removed = derived.forget_games(conn, stale)
        for table, rows in removed.items():
            if rows:
                print(f"  forgot {table:18s} {rows:,}")

    stats = None
    if missing:
        started = time.time()
        stats = derived.build(conn, archive, PARSER_VERSION,
                              game_keys=missing)
        print(f"  derived {stats.games:,} games, {stats.plays:,} plays"
              f" in {time.time() - started:.1f}s")
    dbschema.create_derived_indexes(conn)
    conn.commit()

    # `secondary` and `earned-runs` own rows that were deleted with the games
    # above; `reference` and `coverage` are computed from the whole corpus.
    # Naming each pass is the point -- "the derived tables are stale" is the
    # half-answer that has sent a user to run the wrong command twice.
    if stale or missing:
        print("\nrebuild what depends on these games:")
        print("  rsse secondary                 (full pass, ~3 min)")
        print("  rsse earned-runs --check       (--season limits it)")
        print("  rsse reference")
        print("  rsse coverage --rebuild")
    conn.close()
    print(f"\n{ATTRIBUTION}")
    return 0


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

    if getattr(args, "refresh", False):
        return _derive_refresh(args, archive_path, query_path)

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
        for table, rows, command in casualties:
            print(f"note: dropped {table} ({rows:,} rows) with the file; "
                  f"rebuild it with `{command}`", file=sys.stderr)
        if casualties:
            order = []
            for _t, _r, command in casualties:
                if command not in order:
                    order.append(command)
            print("note: in order -- " + ", ".join(order), file=sys.stderr)
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
    f.add_argument("--refresh", action="store_true",
                   help="ask the server whether each season archive has been"
                        " reissued (one conditional request per season)")
    f.add_argument("--top", type=int, default=20)
    f.add_argument("--aux", action="store_true",
                   help="also download the Negro Leagues archive: the"
                        " whole-corpus CSVs and the 704 roster files the"
                        " season archives do not ship")
    f.add_argument("--aux-only", action="store_true",
                   help="download only the Negro Leagues archive, skipping"
                        " the season pass entirely")
    f.add_argument("--parks", help="directory for the whole-corpus auxiliary"
                   f" files (default {PARKS})")
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
    i.add_argument("--aux", action="store_true",
                   help="also archive rosters, team files, parks and bios")
    i.add_argument("--aux-only", action="store_true",
                   help="archive only the auxiliary files, skipping the"
                        " event-file pass entirely")
    i.add_argument("--refresh", action="store_true",
                   help="replace the records of files Retrosheet has reissued"
                        " instead of refusing (see `rsse fetch --refresh`)")
    i.add_argument("--parks", help="directory holding the whole-corpus"
                   " auxiliary files (default data/parks)")
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
    d.add_argument("--adopt", action="store_true",
                   help="with --refresh, record the archive's current digests"
                        " as this database's vintage instead of rebuilding")
    d.add_argument("--refresh", action="store_true",
                   help="rebuild only the games the archive and the derived"
                        " database disagree about (see `rsse ingest --refresh`)")
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

    bn = sub.add_parser("bench", help="run the pinned query benchmarks")
    bn.add_argument("--database")
    bn.add_argument("--repeats", type=int, default=3)
    bn.add_argument("--only", help="substring of a benchmark name")
    bn.add_argument("--progress", action="store_true")
    bn.add_argument("--report", help="write a JSON report to this path")
    bn.set_defaults(func=cmd_bench)

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

    ern = sub.add_parser("earned-runs",
                         help="derive earned runs from the play-by-play")
    ern.add_argument("--archive")
    ern.add_argument("--database")
    ern.add_argument("--season", help="comma-separated seasons")
    ern.add_argument("--progress", action="store_true")
    ern.add_argument("--check", action="store_true",
                     help="reconcile against (UR), data,er and the game logs")
    ern.add_argument("--check-only", action="store_true",
                     help="reconcile what is already built, deriving nothing")
    ern.add_argument("--show", type=int, default=15,
                     help="how many disagreement classes to print")
    ern.add_argument("--report", help="write a JSON report to this path")
    ern.set_defaults(func=cmd_earnedruns)

    rf = sub.add_parser("reference",
                        help="build people, rosters, teams and parks")
    rf.add_argument("--archive")
    rf.add_argument("--database")
    rf.set_defaults(func=cmd_reference)

    apc = sub.add_parser("appearances",
                         help="build appearances from allplayers.csv")
    apc.add_argument("--archive")
    apc.add_argument("--database")
    apc.set_defaults(func=cmd_appearances)

    sc = sub.add_parser("secondary",
                        help="build comments and lineup_entries")
    sc.add_argument("--archive")
    sc.add_argument("--database")
    sc.add_argument("--progress", action="store_true")
    sc.set_defaults(func=cmd_secondary)

    cv = sub.add_parser("coverage", help="what the corpus actually covers")
    cv.add_argument("--database")
    cv.add_argument("--archive")
    cv.add_argument("--rebuild", action="store_true")
    cv.add_argument("--season-range", help="LO,HI inclusive")
    cv.set_defaults(func=cmd_coverage)

    v = sub.add_parser("verify", help="check raw-layer integrity")
    v.add_argument("--archive")
    v.add_argument("--quick", action="store_true",
                   help="skip rebuilding every auxiliary file from its records")
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
