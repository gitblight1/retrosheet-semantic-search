# 09 — Local Web UI

A browser front end for the query API, served from the machine that built the
database. `rsse serve` starts it; nothing about it is hosted anywhere.

## 1. Why local, and what that settles

The query database is **11.97 GB** ([05-DATABASE](05-DATABASE.md) §7). Hosting
it publicly would make storage and query compute the dominant cost of the
project, and a static host such as GitHub Pages cannot run SQLite over a file
that size at all. The build pipeline already puts the database on the user's
disk, so the UI goes where the database is.

Being local settles several things that a public service would have to argue
about:

- **One user, trusted.** No accounts, no rate limits, no multi-tenant
  isolation. `.event_matches()` takes an arbitrary regex, and that is fine
  here and nowhere else (§10.1).
- **Read-only, enforced by the connection.** Every request opens the database
  `mode=ro` through `rsse.query.connect`. The UI cannot modify the database
  even if a bug in it tries to.
- **Standard library only.** `http.server.ThreadingHTTPServer`, `json`,
  `sqlite3`. The project has no third-party dependencies
  ([README](../README.md)) and a UI is not a reason to add one.
- **No network beyond loopback.** The page loads no CDN script, font or
  stylesheet. It works offline, and the server makes no outbound request.

## 2. The UI is not a second query language

The web form builds **exactly** the `Search` the CLI builds from the same
inputs. There is one table of query parameters, and three things are
generated from it: the CLI flags, the `/api/schema` document, and the form.

Today that table is `_QUERY_FLAGS` / `_QUERY_SWITCHES` in `rsse/cli.py`, plus
a block of hand-written special cases in `_search_from_args` (`--seasons`,
`--tag`, `--force-play*`, `--putout-sequence`, ...). Those move to
`rsse/query/params.py` as one declarative list, `QUERY_PARAMS`, together with
`search_from_params(dict) -> Search`. The CLI then parses argv into that dict,
and the web server parses JSON into it. After the move:

- a new predicate added to `QUERY_PARAMS` appears in the CLI and the form with
  no other change;
- `rsse query --outs 2 --bases-loaded` and a form with the same two fields
  produce byte-identical SQL, which is asserted by test (§9).

Each entry carries its name (the flag without dashes, so `outs`, `bases_loaded`,
`force_play_at`), a type (`int`, `str`, `switch`, `list`, `range`, `choice`),
a group used to lay out the form (context, players, play, fielding, quality,
game type), and a one-line help string.

**An unknown parameter is an error, not something to ignore.** A misspelled
filter that is silently dropped widens the query, and a count that is too
large reads as a finding in the same way that a false zero does. `/api/query/<id>`
returns 400 naming the parameter.

**`order_by` is not in `QUERY_PARAMS`, and nothing that takes SQL text ever
is.** `Search.order_by(expr)` puts its argument into `ORDER BY` verbatim. That
is reasonable for a Python caller, who can already run any SQL they like, and
it is SQL injection for anything that takes input from a request. The CLI does
not expose it either. Ordering in the UI is limited to a fixed set of named
orderings (chronological, which is the default, and reverse chronological),
each mapped to a SQL fragment on the server. A test asserts that no
`QUERY_PARAMS` entry maps to `order_by`.

## 3. Pages

The page is a single HTML document. Routes are URL fragments, so every view,
including a fully specified query, is a URL that can be bookmarked and pasted
back in.

| Route | Shows |
|---|---|
| `#/` | Name search box, the query builder, a few example queries |
| `#/query?<params>` | Query builder, results, disclosure panel (§4), explain (§5) |
| `#/play/<play_id>` | One play in full (§3.1) |
| `#/game/<game_key>` | One game: header, line score, play-by-play (§3.2) |
| `#/player/<person_id>` | Biography, roster lines, appearances, seasons (§3.3) |
| `#/team/<team_id>` | Franchise summary and seasons |
| `#/team/<team_id>/<season>` | Season: roster, games in the corpus, games missing (§3.4) |
| `#/park/<park_id>` | Park record, games by season |
| `#/date/<yyyy-mm-dd>` | Every game in the corpus that day |
| `#/season/<yyyy>` | Teams and leagues that season, with coverage |
| `#/tags` | The ontology: every tag, its category, spec section and note |

Games are addressed by **`game_key`, not `game_id`**. Three ids repeat in the
corpus ([01-CORPUS](01-CORPUS.md) §5.4), and a route keyed on `game_id` would
have to pick one. `#/game/id/<game_id>` exists as a convenience; if more than
one game has that id, it lists them and does not redirect.

### 3.1 Play

Everything the derived layer says about one play, placed next to the source
text it was derived from:

- `event_raw` byte for byte, then `event_basic`, `event_modifiers`,
  `event_advances` and `annotations`;
- the base-out state before and after, the score before, `runs_on_play`, and
  `batter_ran`;
- tags, each with its `confidence` and `source` (`derived` / `curated`);
- `runner_advances`, one row per advance, with `is_out`, `is_force`,
  `force_certainty`, and the raw advance text;
- `fielding_credits` and `credit_sequences`;
- comments attached to this play;
- **the lineup in effect at this play** for both sides, rebuilt by the
  timeline rule of [06-QUERY](06-QUERY.md) §2.1 (the last entry that had
  taken effect), and not by joining `fielding_credits`;
- `parse_status` and `parse_error`, and `record_id` as provenance.

A play whose `parse_status` is not `ok` shows a banner that names the status.
Its base-out state is shown as "unknown" where it is NULL
([03-STATE](03-STATE.md) §7), and never as bases empty.

### 3.2 Game

- **Header.** Teams, date, park, and game type, where "not stated" is shown
  instead of an inferred `regular` ([06-QUERY](06-QUERY.md) §5). Also every
  `game_info` record, since the table is lossless and a game page should be
  too.
- **Line score.** Runs per half-inning, summed from `plays.runs_on_play`, with
  the stored `final_home` / `final_away` shown alongside. When the two
  disagree, the page says so and does not pick one. Where `game_logs` holds
  the game, its score is a third column.
- **Play-by-play** in `seq` order. Substitutions (`lineup_entries.play_id`)
  and comments (`comments.play_id`) are interleaved after the play they
  follow, and comments before the first play go at the top. Each play links
  to §3.1.
- **Earned runs**, when `earned_runs` is present: per pitcher, with the
  `derived` / `likely` / `ambiguous` / `untrusted` split. A NULL
  `earned_pitcher` is shown as "scorer's judgement" and does not count as 0
  ([05-DATABASE](05-DATABASE.md) §5.4).

### 3.3 Player

- `people` biography. Rows with `source = 'observed'` say that no reference
  file describes this person.
- `roster_entries` by season: team, position, bats/throws.
- `appearances`, with `g` next to `games_in_corpus`. This comparison is the
  reason the table exists ([05-DATABASE](05-DATABASE.md) §9), and a player page
  that shows one number without the other overstates what the corpus holds.
- Seasons with plays in the corpus, each linking to a query prefilled with
  `batter=<id>&season=<y>`.

**Tag counts, not statistics.** The page may show, per season, how many plays
the player batted in that carry a given tag. It labels these as tag counts
("plays tagged `HomeRun`"), never as a batting line ("HR"). A season the
corpus holds 43% of ([05-DATABASE](05-DATABASE.md) §9) produces 43% of a
statistic, and a column headed "HR" would make that look like the whole
number. Computing statistics is outside this spec.

### 3.4 Team season

- The `teams` row, and every `roster_entries` line for the club that season.
- Games in the corpus, with scores and a won-lost record *over those games*,
  labelled as such.
- **Games the game logs list that the corpus has no event file for**, when
  `game_logs` is loaded. This is the per-team view of the `games_missing`
  count in `CoverageReport`. When the game logs are not loaded, the page says
  completeness is unknown and does not show zero.
- **Only regular-season log rows count as missing.** Postseason and all-star
  rows are listed separately, because the corpus holds no event files for
  them by design (06-QUERY §5). This is the same rule `coverage` uses.
- **Repeated log rows are collapsed and counted.** On the current archive,
  the postseason and all-star files were loaded both from `gamelogs/` and
  from `gamelogs/postseason/`. Every row appears twice except the four `ct`
  rows, whose file exists only in the second directory: 3,950 rows for
  1,977 games, so 1,973 repeats. The 233,634 regular-season rows have no
  repeats, so coverage is unaffected. The page
  shows each game once and states how many rows it collapsed, so the
  duplication stays visible until the load is fixed.

### 3.5 Name search

One search box finds people, teams and parks. It is a **prefix** search:
`/api/names?q=` matches the typed text as a prefix of the surname, the team
city or nickname, or the park name. Every hit carries the disambiguating label
`names.py` already builds (birth year, debut; park city and years).

The surname arm is a range test, `last >= :q AND last < :q_next`, so that it
can use `ix_people_name`. Measured on the full corpus with a warm cache, that
takes **23 ms** against **138 ms** for `LIKE 'Rob%'`, which SQLite runs as a
scan because `LIKE` is case-insensitive and the index is not.

## 4. Results and disclosure

`/api/query/<id>` returns the JSON of `rsse query --format json`: rows, `total`,
`coverage`, `excluded`, `force`, versions, SQL, attribution. The page renders
all of it.

**The disclosure panel cannot be hidden.** It sits beside the count, not below
the table, and it has no collapse control. It states:

- what was searched, from `CoverageReport.describe()` and its notes;
- what was excluded, from `ExcludedCounts`, by reason, with a link that
  re-runs the query with the matching `include_*` switch;
- for a force query, the `ForceReport` line. That includes both the
  `batter_ran = 'unknown'` count (included) and the `state_untrusted` count
  (excluded), worded so that the opposite treatment of the two is visible
  ([06-QUERY](06-QUERY.md) §3.3);
- corpus and ontology version.

**An empty result is never rendered as "no results".** It is rendered as
"No matching plays in 202,650 games, 1908–2025", with the numbers taken from
the coverage report. A UI is where a bare zero is most tempting to display and
least likely to be questioned, so this rule is enforced in the one render
function every result passes through, not in each page.

**Paging.** `Search` gains `.offset(n)`, which is compiled after `LIMIT` and
is only valid with it. The page size is 25, 100 or 500. `total` comes from
`ExcludedCounts.matched` as it does now, so moving to another page does not
recount.

**Export.** CSV and JSON of the current page, generated by the server with the
same writer the CLI uses. Every export carries the attribution notice and the
corpus version ([06-QUERY](06-QUERY.md) §9). An export of more than one page
is done with `rsse query`, and the export dialog shows the equivalent command
line. Because of §2, that command is exact.

## 5. Explain

Every query view has an Explain tab showing the SQL, its parameters, the
`EXPLAIN QUERY PLAN` rows, and any full-scan warning. Explain does not run the
query, so it answers immediately even for a query that would take minutes.
When a query times out (§6), the error links here.

## 6. Timeouts, cancellation and a cold cache

Some legitimate queries are slow: `.hit_located()` takes 28.3 s warm
([06-QUERY](06-QUERY.md) §3.4). So can the first query after boot. Measured
on the full corpus:

| | cold | warm |
|---|---|---|
| `games` scan by team | **20.0 s** | 30 ms |
| `plays` by `batter_id`, 10k rows | 2.87 s | 14 ms |

A 12 GB file does not stay in the page cache, and the UI cannot fix that. It
can make the wait visible and bounded:

- **Deadline.** Each query connection installs `set_progress_handler` with a
  check against a deadline, `--timeout` seconds (default 60). When it fires,
  SQLite aborts the statement and the API returns 504 with the elapsed time
  and a link to Explain. **It never returns partial rows.** A truncated result
  presented as a result is a wrong answer that looks right.
- **Cancel.** The server issues the request id, not the client. The page
  first calls `POST /api/query/start`, which returns an id from
  `secrets.token_urlsafe(16)`, and then `POST /api/query/<id>` runs the
  query. `POST /api/cancel/<id>` calls `conn.interrupt()` on that request's
  connection. An id the client chose could be guessed or reused, and on any
  server with more than one user that would let one user cancel another's
  query. The page's Cancel button and navigating away both send the cancel.
- **Concurrency cap.** At most `--max-queries` searches (default 2) run at
  once. A further search waits for a slot; after 30 s of waiting it gets 503
  with the number of searches ahead of it. A per-query deadline bounds how
  long one query runs but not how many run together, and a few overlapping
  `.hit_located()` calls starve every other page of disk bandwidth even
  locally. Browse pages (§3) do not count against the cap: they are
  index-backed lookups and should not queue behind a scan.
- **The deadline cannot interrupt a regex.** `.event_matches()` runs Python's
  `re.search` inside a SQLite user function. SQLite calls the progress
  handler between VDBE opcodes, not during a user function, so a pattern with
  catastrophic backtracking holds its thread until the regex finishes,
  whatever the deadline says. Locally this is accepted (§1): the only person
  it can hurt is the one who typed the pattern, and they can restart the
  server. §10.1 says what a hosted server would have to do instead.
- **Elapsed time** is shown while a query runs and next to every result, so
  it is obvious when a slow answer comes from a cold cache and not from the
  query itself.

Browse pages (§3) use only index-backed lookups, and each is measured in §9.
A page that would need a new index to be fast is scoped until it does not:
the team-season page goes through `ix_games_season` (10 ms warm) and does not
scan `games` by team. **The UI adds no indexes.** The database is opened
read-only, and a UI that needed its own indexes would mean the index plan of
[05-DATABASE](05-DATABASE.md) was wrong, which should be fixed there.

## 7. Names that resolve to more than one person

`.batter_named()` refuses an ambiguous name ([06-QUERY](06-QUERY.md) §8). The
CLI prints the candidates; the UI offers them as a choice. For that to work,
the refusal has to carry the candidates as data and not only as message text,
so `QueryError` gains a `candidates` attribute, a list of `(kind, id, label)`,
set by `check_names`. The API returns 409 with that list. The page shows "2
people named *Jack Robinson*", lists both with their labels, and on a choice
re-runs the query with `batter=<id>` in place of `batter_named`.

Choosing from the name search box (§3.5) puts the **id** into the form and not
the name. A query built from the box is therefore never ambiguous, and its URL
does not change meaning when a later `rsse reference` adds another person with
the same name.

## 8. Server

```
rsse serve [--host 127.0.0.1] [--port 8000] [--database PATH]
           [--timeout 60] [--max-queries 2]
           [--allowed-host NAME ...] [--no-browser]
```

- **Loopback by default.** A `--host` other than a loopback address prints a
  warning that the server has no authentication and exposes the whole
  database to anyone who can reach the port.
- **Host header check.** Requests whose `Host` header (without the port) is
  not on the allowed list are refused with 421. The list defaults to
  `localhost`, `127.0.0.1`, `::1` and the bound address; each
  `--allowed-host` adds a name. A loopback server is still reachable from a
  malicious page through DNS rebinding, and this check costs one comparison.
  The list is an option rather than built in because the same check is needed
  under any other hostname, whether that is a LAN name or a proxy.
- **Only GET and POST.** POST is used only for `/api/query/*`,
  `/api/explain` and `/api/cancel`, and none of them writes anything.
- **One connection per request**, opened and closed by the handler thread.
  `sqlite3` connections are not shared across threads, and a per-request
  connection is also what lets §6 interrupt one query without affecting the
  others.
- **The archive is attached, read-only, when present.** `game_logs` and the
  raw source lines live in `archive.db`, not the query database
  ([05-DATABASE](05-DATABASE.md) §1, §5.3). Each connection attaches it as
  `arc` with `mode=ro` (`--archive` overrides the path), and a missing
  archive is one more optional table.
- **Missing tables degrade and say so.** A database without `lineup_entries`,
  the reference tables, `earned_runs` or `game_logs` still serves every page.
  The section that needs the missing table is replaced by a note naming the
  command that builds it (`rsse secondary`, `rsse reference`, ...). This is
  the same behaviour as `Search.check_schema`.
- **Source text is escaped.** `event_raw`, comments and `game_info` values
  come from files, and they are inserted into the page with `textContent`,
  never `innerHTML`.

Layout under `rsse/web/`:

| File | Role |
|---|---|
| `server.py` | `ThreadingHTTPServer`, routing, Host check, concurrency cap, request registry for cancel |
| `api.py` | One function per endpoint, `(conn, params) -> dict`. No HTTP in it, so it is testable without a socket, and so a different server can mount it (§10.1) |
| `static/index.html`, `app.js`, `app.css` | The page. No build step, no framework |

The `static/` files are package data (`pyproject.toml`), for the same reason
`curated_tags.json` is: a wheel without them installs a server with no page to
serve.

### 8.1 Endpoints

| Method | Path | Returns |
|---|---|---|
| GET | `/api/schema` | `QUERY_PARAMS`, the tag list, and which optional tables are present |
| POST | `/api/query/start` | A server-issued request id (§6) |
| POST | `/api/query/<id>` | §4. 400 unknown parameter, 404 unknown id, 409 ambiguous name, 503 queue timeout, 504 timeout |
| POST | `/api/explain` | §5 |
| POST | `/api/cancel/<id>` | 204 |
| GET | `/api/names?q=` | §3.5, at most 20 hits per kind |
| GET | `/api/play/<play_id>` | §3.1 |
| GET | `/api/game/<game_key>` | §3.2 |
| GET | `/api/player/<person_id>` | §3.3 |
| GET | `/api/team/<team_id>[/<season>]` | §3.4 |
| GET | `/api/park/<park_id>` | park record and games per season |
| GET | `/api/park/<park_id>/<season>` | that season's games at the park |
| GET | `/api/date/<yyyy-mm-dd>` | games |
| GET | `/api/season/<yyyy>` | teams, leagues, coverage rows |
| GET | `/api/seasons` | every season, for the browse index |
| GET | `/api/tags` | the ontology (§3) |
| GET | `/api/about` | corpus and ontology versions, attribution |
| GET | `/api/export?<params>&format=csv\|json` | §4 |

## 9. Testing

Against the two-game fixture database that
[tests/test_query.py](../tests/test_query.py) already builds, and never
against a stub:

- **CLI and form agree.** For every entry in `QUERY_PARAMS`,
  `search_from_params` and the argv path compile to identical SQL and
  parameters.
- **Unknown parameters are refused**, including a near-miss such as
  `bases_load`.
- **An empty result carries coverage.** A query that matches nothing returns
  `total = 0` together with a non-empty coverage report, and the one render
  function produces the "No matching plays in N games" text from it.
- **A timeout returns no rows.** The deadline is set to zero and the test
  asserts a 504 with no `rows` key, not a short list.
- **Ambiguous names return candidates** as data, and choosing one gives the
  same result as passing the id directly.
- **No SQL text is accepted.** No `QUERY_PARAMS` entry maps to `order_by`,
  and an `order` value outside the named set is refused.
- **Cancel ids come from the server.** Running or cancelling with an id that
  `/api/query/start` did not issue returns 404.
- **The concurrency cap holds.** With `--max-queries 1`, a second search
  waits while the first runs and gets 503 when its wait times out; a browse
  request made at the same time is answered at once.
- **The Host check refuses** a foreign `Host` header and accepts one added
  with `--allowed-host`.
- **Missing tables degrade.** Every `/api/*` endpoint answers on a database
  built without `rsse secondary` and `rsse reference`, and names the missing
  command.
- **The page is in the wheel.** `static/index.html` is found through
  `importlib.resources`, as `curated_tags.json` is.
- **Browse lookups use an index.** For each §3 page query, `EXPLAIN QUERY
  PLAN` contains no `SCAN` of `plays` or `games`. The one exception is
  `#/park/<id>`, which scans `games` (26 ms warm, 203,285 rows) because
  `games.site` has no index, and the test names that exception so any
  second one fails.

One smoke test starts the real server on port 0, fetches `/`, `/api/schema`
and one query, and shuts it down.

## 10. Non-goals

- **Public hosting.** §1, and §10.1 for what it would take.
- **Statistics.** §3.3. Tag counts are shown and labelled as tag counts.
- **Charts.** Not in the first version. When they come, they are drawn from
  the same API responses and carry the same disclosure panel.
- **Editing.** Curated tags ([04-ONTOLOGY](04-ONTOLOGY.md) §8) are edited in
  `curated_tags.json` and loaded by `derive`. A write path from the browser
  would mean a read-write connection, and §1 rules that out.

### 10.1 If this is ever hosted

Not built and not planned. This section records what hosting would take, so
that the local design does not close it off by accident and so that nobody
mistakes `--host 0.0.0.0` for a deployment.

**Already compatible.** Nothing in this list needs to change for hosting:

- `api.py` has no HTTP in it (§8), so a WSGI or ASGI adapter can mount it.
- The page is static and loads nothing from outside, so any static host or
  CDN can serve it.
- Connections are read-only, SQL text is never accepted (§2), unknown
  parameters are refused, and source text is escaped.
- Request ids come from the server, the allowed-host list is configurable,
  and concurrency is capped (§6, §8).

**Would have to change:**

1. **The server.** `http.server` is not meant to face the internet. It
   would go behind a reverse proxy or be replaced by a production WSGI/ASGI
   server running the same `api.py`. This is a new file, not a rewrite.
2. **`.event_matches()`.** The deadline cannot interrupt it (§6). A hosted
   server must do one of three things: drop the predicate, replace `re` with
   an engine that guarantees linear time, or run each search in a worker
   process that can be killed at the deadline. The third is the only one
   that keeps the escape hatch whole.
3. **Abuse limits.** The concurrency cap bounds load but not fairness. One
   client submitting `.hit_located()` in a loop holds every slot. A hosted
   server needs per-client rate limits, and probably a cap on how many
   full-scan queries (as flagged by `explain()`'s warning) run at once.
4. **Caching.** Results depend only on the parameters and the corpus
   version, so they can be cached on `(params, corpus_version)`. The
   disclosure panel is part of the cached response, never recomputed apart
   from it.
5. **Cost.** None of the above changes the premise of §1. Acceptable
   latency needs the 11.97 GB database on fast disk and enough RAM to keep
   its hot pages cached; the cold-cache measurements in §6 are what a
   hosted server with too little RAM would serve all the time. That remains
   the largest cost and the reason this spec is local.
6. **Attribution.** The attribution notice is already on every page and
   export ([01-CORPUS](01-CORPUS.md) §1). Serving the data publicly would
   need Retrosheet's terms checked against a hosted service specifically,
   not only against redistribution of files.

## 11. Validation status

Built and tested: `rsse serve`, `rsse/web/`, and `rsse/query/params.py`,
which is now the only place query parameters are defined.
[tests/test_web.py](../tests/test_web.py) covers every item in §9.

Every `rsse query` flag compiles to the same SQL through the table as it did
through the hand-written code it replaced. This was checked on eight flag
combinations covering every flag before the old code was removed. Four flags
(`--half`, `--runner-on`, `--out-at`, `--batter-ran`) now reject a bad value
at argument parsing, where before `Search` rejected it.

Measured on the full corpus, warm: the motivating query in 1.8 s end to end
through the API, including its coverage and force reports; a game page
(81 plays) 0.6 s; a player page 0.2–0.3 s; name search 10–33 ms. Cold, the
same player page took 3–9 s. That is the page cache, which is the reason
elapsed time is shown (§6).

Building it found three things:

- **The postseason game logs are loaded twice** (§3.4). It is a load
  problem, not a UI one, and it is left for the loader to fix. It does not
  affect `games_missing`, which counts regular-season rows only.
- **`appearances` is Negro Leagues only** ([05-DATABASE](05-DATABASE.md)
  §9). A player page that labelled the table as a career would show Babe
  Ruth with two barnstorming games. The section is headed "Negro Leagues
  appearances" and is hidden when empty. Its 207 rows where the corpus holds
  more games than the file counts are marked, not capped.
- **A marker key collided with data.** The note that replaces a section for
  a missing table was first `{"missing": ...}`, and the team-season payload
  also had a `missing` list of games. The page then read "game logs not
  loaded" on a database that had them. The marker is now `missing_table`,
  which no payload uses for anything else.
