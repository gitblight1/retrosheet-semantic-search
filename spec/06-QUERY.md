# 06 — Query API

The builder from the original spec, given semantics. Every predicate below has
one defined SQL contract; a predicate without one is not in the API.

```python
Search() \
    .bases_loaded() \
    .outs(2) \
    .dropped_third() \
    .force_play(at="H") \
    .putout_sequence([2, 1])
```

## 1. Model

- `Search()` builds an immutable query; each method returns a new instance.
- Predicates combine with **AND**. `.any_of(...)` and `.none_of(...)` provide OR
  and negation.
- Nothing executes until `.run()`, `.count()`, `.explain()`, or iteration.
- `.explain()` returns the generated SQL and the query plan. It is part of the
  public API: a researcher must be able to audit what a "never happened" answer
  actually asked.

Compilation shape — tag predicates drive the plan, because
`ix_playtags_tag (tag_id, play_id)` is by far the most selective index:

```sql
SELECT p.* FROM plays p
  JOIN play_tags t1 ON t1.play_id = p.play_id AND t1.tag_id = :tag_dts
  JOIN play_tags t2 ON t2.play_id = p.play_id AND t2.tag_id = :tag_force
 WHERE p.bases_before = '111'
   AND p.outs_before  = 2
   AND p.parse_status = 'ok'
   AND EXISTS (SELECT 1 FROM credit_sequences cs
                WHERE cs.play_id = p.play_id AND cs.seq_text = '21')
 ORDER BY p.game_id, p.seq;
```

## 2. Context predicates

Straight column filters on `plays` ([05-DATABASE](05-DATABASE.md) §3).

| Method | SQL |
|---|---|
| `.outs(n)` | `outs_before = n` — **before** the play. `.outs_after(n)` for the other sense. |
| `.bases(state)` | `bases_before = state`, `'000'`–`'111'` |
| `.bases_loaded()` | `bases_before = '111'` |
| `.runner_on(base)` | corresponding bit set |
| `.scoring_position()` | 2 or 3 occupied |
| `.inning(n)` / `.inning_at_least(n)` | `inning` |
| `.half('top'\|'bottom')` | `half` |
| `.inning_ending()` | `is_inning_ending = 1` |
| `.walkoff()` | `is_walkoff = 1` |
| `.score_diff(lo, hi)` | batting-team differential, inclusive |
| `.season(y)` / `.seasons(lo, hi)` | via `games.season` |
| `.team(code)` / `.batting_team(code)` / `.fielding_team(code)` | via `games` |
| `.batter(id)` | `plays.batter_id` — ids, never names |
| `.pitcher(id)` / `.fielder(pos, id)` | via `lineup_entries`; see §2.1 |
| `.park(id)` | `games.site` |

Ambiguous names are given both forms rather than a default: `.outs()` /
`.outs_after()`. The original `.outs(2)` did not say which, and for the
motivating play the two differ (2 before, 3 after).

### 2.1 The lineup is a timeline, not a mapping

`plays` names the batter and nobody else, so "who was pitching?" has to be
reconstructed. `lineup_entries` holds one row per `start` and `sub` record with
`play_id` set to the play a substitute entered *after*, and the holder of a
position at a given play is **the last entry that had taken effect by then**.
So `.fielder(pos, id)` compiles to an `EXISTS` with a `NOT EXISTS` over later
entries, not an equality join — an equality join matches a replaced fielder as
well as his replacement.

`.pitcher(id)` is `.fielder(1, id)`.

Deliberately **not** answered from `fielding_credits`. Those record who touched
the ball, which is a different question: a pitcher who faced nine batters
without a putout or assist would vanish from the results entirely. The
credits question is `.putout_by()`.

The invariant that makes this testable: summing `.fielder(pos, id)` over every
player who ever held `pos` must equal the total number of plays, since exactly
one player holds a position on any given play. It is asserted in
[tests/test_query.py](../tests/test_query.py).

## 3. Play predicates

| Method | Meaning |
|---|---|
| `.tag(name)` | `play_tags` join. Every tag in [04-ONTOLOGY](04-ONTOLOGY.md) is reachable this way; the named methods below are sugar. |
| `.strikeout()` | tag `Strikeout` |
| `.uncaught_third_strike()` | tag `UncaughtThirdStrike` |
| `.dropped_third()` | alias of the above |
| `.batter_reached_on_k()` | tag `BatterReachedOnK` |
| `.force_play(at=None, include_tag_outs=False, certainty=None)` | `EXISTS` a `runner_advances` row with `is_out=1` and `is_force=1`, optionally `destination = at`. With `include_tag_outs=True` the `is_force` condition is dropped, widening to any out at that base. `certainty='derived'` narrows to force determinations a trajectory or modifier settled; the default admits `derived` and `likely` both. |
| `.out_at(base)` | `EXISTS` a `runner_advances` row with `is_out=1` and `destination = base`, regardless of force status |
| `.tag_out(at=None)` | `EXISTS` an out with `is_force=0` — the complement of `.force_play()` |
| `.batter_ran(value)` | `plays.batter_ran`, one of `'yes'`, `'no'`, `'unknown'` ([03-STATE](03-STATE.md) §4.2) |
| `.double_play()` / `.triple_play()` | tags |
| `.stolen_base(base=None)` / `.caught_stealing(base=None)` | tags, optional base |
| `.error(fielder=None)` | `fielding_credits.credit='error'`, optional position |
| `.hit_location(pattern)` | `LIKE` on the retained location string |
| `.event_matches(regex)` | regex over `event_raw` — the escape hatch for anything the ontology does not yet name |

### 3.1 Fielding sequences

Three distinct questions, three methods, because "putout sequence 2-1" is
ambiguous in the original spec:

| Method | Matches | SQL |
|---|---|---|
| `.putout_sequence([2,1])` | one throw sequence that **is** exactly 2 then 1 | `credit_sequences.seq_text = '21'` |
| `.contains_sequence([2,1])` | 2 then 1 contiguously **within** a longer sequence | `seq_text LIKE '%21%'` — unindexed, documented as slower |
| `.putout_by(1, assist_by=[2])` | putout to the pitcher with an assist from the catcher, regardless of the rest | join on `fielding_credits` |

Scope defaults to **either** the basic section or an advance, since Retrosheet
places the same physical play in different sections depending on where the out
occurred — `K23` in the basic section, `K.3XH(21)` in an advance.
`.putout_sequence([2,1], scope='advance')` narrows it.

A sequence is **one throw sequence, not the whole play**
([05-DATABASE](05-DATABASE.md) §3.1). A 6-4-3 double play is written `64(1)3`
and holds two of them, `64` and `3`, so `.putout_sequence([6,4,3])` matches
nothing — correctly, since no single throw sequence 6-4-3 occurred. Asking for
the fielders who took part in a play is `.putout_by()` or a `fielding_credits`
join; asking for a specific relay is `.putout_sequence()`. The motivating query
wants the latter: `[2,1]` is one throw, catcher to pitcher.

### 3.2 Force and tag outs must both be expressible

`.force_play()`, `.tag_out()`, and `.out_at()` form a deliberate trio.
`.out_at(base)` is the union; the other two partition it.

This is not redundancy. The force/tag distinction is *derived*
([03-STATE](03-STATE.md) §4), so it is exactly the part of the pipeline most
likely to be wrong, and the only way to test a derived predicate is to run the
query with and without it over the same population. A search API offering only
`.force_play()` cannot express its own control case.

`is_out` is required in all three, not just `destination`: an advance written
with an `X` whose credit sequence contains an error is **not** an out
([02-GRAMMAR](02-GRAMMAR.md) §5). Predicates that match on the `X` as written
over-count.

### 3.3 Force certainty is a query parameter, not a filter applied for you

`.force_play()` admits both `derived` and `likely` force determinations
([03-STATE](03-STATE.md) §4.2 rule 5). This is deliberate and it is the one
place where the "default queries return `certain` only" rule of §4 would give
the wrong answer if applied naively: `likely` is how the pre-1970s corpus
records an ordinary ground out, so a strict default would omit roughly half the
force outs at first and report it as a smaller number rather than as missing
coverage.

`certainty='derived'` gives the strict population, and the two run together are
the control pair that §3.2 argues for — the same reason `.tag_out()` exists
alongside `.force_play()`. A `CoverageReport` on any force query states the
`derived`/`likely` split, so a count is never quoted without it.

A force query's `CoverageReport` MUST also state how many plays in the
population have `batter_ran = 'unknown'`. Those plays claim no force and are
**not** excluded from the query — the doubt is about the force, not about the
play — so without the report a force count would quietly omit them. 2.97% of
the corpus is `unknown`, and the rate is 7.70% in 1920 against 0.00% in 2020,
which is exactly the shape of error that makes an era comparison wrong while
looking fine.

The report MUST likewise state the `state_untrusted` count in the population
([03-STATE](03-STATE.md) §7.1) — plays that parsed cleanly on a base-out state
known to be wrong, because an unparsed play earlier in the half-inning was
never applied. These are excluded by default, since the default is
`parse_status = 'ok'`, and that is the right default: a force derivation reads
the base state, so on these plays it is reading fiction.

The two counts are reported together but mean opposite things, and the report
must not blur them:

| | `batter_ran = 'unknown'` | `state_untrusted` |
|---|---|---|
| What is doubted | one field of this play | the state this play inherited |
| In default results | **yes** | **no** |
| Why | the play is sound; only the force claim is withheld | every derived field may be wrong |
| Corpus scale | 530,888 (2.97%) | 15 (0.0001%) |

The scale difference is the point of keeping them apart. `unknown` is a
structural property of early-era scoring and must stay in results or a home-run
count comes back 3% short; `state_untrusted` is a handful of plays downstream
of seven malformed records and must stay out or a force count is quietly
wrong.

What no parameter can offer is a filter on whether the out was executed by
touching the base or by tagging the runner. That is not recorded for any play
([03-STATE](03-STATE.md) §4.2.1), and a predicate implying otherwise would be
the most misleading thing in this API.

The concrete use is in [08-WORKED-EXAMPLE](08-WORKED-EXAMPLE.md): the same
plays, queried with `.force_play(at="H")` and with
`.force_play(at="H", include_tag_outs=True)`, must differ by exactly the
tag-out cases.

## 4. Certainty and quality

| Method | Effect |
|---|---|
| *(default)* | `parse_status = 'ok'` and `confidence = 'certain'` |
| `.include_uncertain()` | also `confidence = 'uncertain'` — ambiguous force ordering, `#`-annotated plays, `99` |
| `.include_untrusted()` | also `parse_status = 'state_untrusted'` — plays on a base state known to be wrong ([03-STATE](03-STATE.md) §7.1) |
| `.include_unparsed()` | also the remaining non-`ok` plays; a research affordance for finding grammar gaps |
| `.curated_only()` / `.exclude_curated()` | filter on `play_tags.source` |

Defaults exclude uncertain rows so that a result set is defensible. The
excluded counts are always reported (§6), so exclusion is never silent.

## 5. Game-type defaults

Default scope is `game_type IN ('regular','playoff','worldseries','lcs',
'divisionseries','wildcard','championship')` — i.e. games that count, with
`playoff` included as the tiebreaker subset of `regular`.

`.include_exhibition()`, `.include_allstar()`, `.only_postseason()` adjust it,
and `.all_game_types()` removes the filter.

**The inference for games with no `gametype` record is applied at query time,
not stored.** `info,gametype` only appears from 2023: 9,476 games in the corpus
state one and 193,809 do not, so `games.game_type` is NULL for 95% of it. An
earlier draft of this section said the inference belonged on the game row. It
does not, for the same reason `scheduled_innings` is NULL below 2020
([05-DATABASE](05-DATABASE.md) §3): backfilling `'regular'` would erase the
distinction between *the file told us this was a regular-season game* and
*nothing said otherwise*, which is a distinction this project keeps everywhere
else. The default therefore compiles to `coalesce(game_type,'regular') IN
(...)`, and a query wanting the strict population can ask for
`game_type IS NOT NULL` through the raw column.

Getting this wrong is not a subtle failure: without the `coalesce` the default
scope excludes everything before 2023 and reports it as zero matches rather
than as missing coverage.

**The corpus holds no postseason event files.** Every file is a regular-season
team file (`####TEAM.EVA/EVN/EVF/EVR`); there are no `.EVE` files. So
`.only_postseason()` can match only the games labelled `playoff`,
`championship`, `lcs` or `divisionseries` *inside* team files — the tiebreakers
and the Negro Leagues championship games — and never a World Series. A
`CoverageReport` on a postseason-scoped query states this, because otherwise an
empty result reads as "it never happened in the postseason".

## 6. Results

`.run()` returns a `ResultSet`:

```python
class ResultSet:
    rows: list[PlayResult]
    total: int                # matches before limit
    coverage: CoverageReport  # what was actually searched
    excluded: ExcludedCounts  # uncertain / unparsed / inconsistent, by reason
    corpus_version: str
    ontology_version: str
    sql: str
```

Each `PlayResult` carries the game, the play, the byte-exact `event_raw`, the
derived tags, and a rendered English description.

Default ordering is chronological (`games.date`, `game_id`, `plays.seq`) —
the right order for "has this happened before". `.order_by()` and `.limit()`
are available; ordering is always total, never left to SQLite.

**`CoverageReport` is mandatory and cannot be suppressed.** It states seasons,
leagues, game count, and date range actually searched, from the `coverage`
table. An empty `rows` with a coverage report reads "no such play in the
N games from YYYY to YYYY"; without one it reads "never happened", which the
data does not support ([01-CORPUS](01-CORPUS.md) §5.2).

### 6.1 Coverage describes what was searched, never what matched

`CoverageReport` is built from the query's own **game-level** predicates —
season, league, team, park, game type — evaluated against `games`, and never
from the rows the query returned.

The distinction is the whole value of the report. Coverage derived from results
says "we looked exactly where we found something": a query matching in two
seasons of a 118-season corpus would report a two-season search and a reader
would conclude the other 116 were checked and clean. The first implementation
here did exactly that, and it is caught by a test asserting that a rare tag and
a common one report the *same* coverage.

Predicates on `plays` never narrow coverage — narrowing results is their job.
A team or park filter does narrow the games matched but cannot be expressed in
the `(season, league)` key the `coverage` table uses, so a query carrying one
gets a note saying the totals are for the whole seasons and leagues searched.

## 7. Performance

Target: <100 ms for a typical query on a warm cache.

- Tag joins first, then column filters, then `EXISTS` subqueries for advances
  and sequences — most selective to least.
- `.count()` compiles to `COUNT(*)` without materializing rows.
- Any query whose plan contains a full scan of `plays` emits a warning through
  `.explain()`.
- The benchmark suite ([07-TESTING](07-TESTING.md) §5) pins the motivating query
  and a dozen others; a regression beyond the target fails CI.

## 8. Names

`.batter_named()`, `.park_named()` and `.team_named()` resolve a typed name
against the reference tables ([05-DATABASE](05-DATABASE.md) §8). `.batter()`,
`.park()` and `.team()` keep taking ids and work without those tables.

Two measured facts shape the resolution.

**The name people use is not `people.first`.** `biofile.csv` records the legal
name — Ruth is `George Herman` — and the playing name lives in `nickname` and
in `roster_entries.first`. **19,598 of 21,993 people (89%) differ between the
two.** So a name is matched against four spellings: nickname + surname, first
+ surname, surname alone, and the roster's own. `Jackie Robinson`, `Jack
Robinson` and `John Edward Robinson` all have to reach `robij101`, and only
the first two would without the roster arm.

**Names are not unique, and an ambiguous one is refused.** 207 names are
shared by 442 people, 30 of them among players who appear in the corpus, and
26 park names are shared — including *Wrigley Field*, which is Chicago's and
the Los Angeles park that hosted Negro Leagues games and the 1961 Angels.
`.batter_named("Jack Robinson")` raises, listing both candidates with their
birth years, rather than answering for whichever sorts first. Guessing would
answer a different question than the one asked and say nothing about it.

The check happens at `run()` or `count()`, not when the predicate is built:
the fluent API has no connection until then. That is the same division of
labour as tag names (§9), and it means an unresolvable name costs an error
rather than a silent empty result.

`PlayResult.batter_name` is filled from `people` in **one** query per result
set, not one per row. It is None when the reference tables are absent — a
result without a name is still a result.

### 8.1 Two league vocabularies

`games.league` is the event file's own code and has four values: `AL`, `NL`,
`FL`, `NGL`. The last collapses **seven** Negro Leagues into one.
`teams.league` is Retrosheet's per-season code, kept verbatim, and
distinguishes `NN1`, `NN2`, `NAL`, `ECL`, `ANL`, `NSL` and `EW` — while
spelling the majors `A`/`N` outside 1920–1949 and `AL`/`NL` within it.

`.league(code)` matches **either**, so `NGL` still works and `NN2` now does
too. The two overlap only on `AL` and `NL`, where they agree, so there is no
code whose meaning depends on which table answered. The corpus's 2,193 Negro
Leagues games resolve as NN2 1,064 / NAL 780 / NN1 49 / ECL 19, with 281 at
clubs whose league field is genuinely empty.

It compiles to a **row-value `IN`**, not a correlated `EXISTS`. The `EXISTS`
form probes `teams` once per game and cost 42% over the plain `g.league = ?`
it replaced; the `IN` form builds the subquery once and is indistinguishable
from it. This is the fourth time that distinction has cost real time on this
project ([07-TESTING](07-TESTING.md) §5.1), and it is now asserted by test.

## 9. CLI

```
rsse query --bases-loaded --outs 2 --tag UncaughtThirdStrike \
           --force-play-at H --putout-sequence 2,1 --format table
rsse explain <same flags>
rsse coverage --season-range 1901 2026
```

`--format` is `table`, `json`, or `csv`. Every non-`table` export carries the
Retrosheet attribution notice ([01-CORPUS](01-CORPUS.md) §1) and the corpus
version.

`rsse coverage --rebuild` populates the `coverage` table
([05-DATABASE](05-DATABASE.md) §5) from `games` and `plays`. It is a GROUP BY,
so it is rebuilt in seconds after a derive rather than carried through the load.

## 10. Validation status

Built and tested. `Search`, the compiler, `CoverageReport`, `ExcludedCounts`,
`ForceReport`, and the `query` / `explain` / `coverage` commands.

`.pitcher()` and `.fielder()` are implemented (§2.1) against
`lineup_entries`, which `rsse secondary` builds. Until that table existed both
raised `NotImplementedError` naming it rather than returning nothing — silently
matching zero rows is the one behaviour this API must never have, since zero is
a meaningful answer everywhere else in it.

Building it found two defects in the API's own contract:

- **Quality flags were order-dependent.** `.tag()` read `_include_uncertain`
  when the predicate was built, so `.strikeout().include_uncertain()` compiled
  strictly and `.include_uncertain().strikeout()` did not — same methods, same
  arguments, different answer, no error. The confidence filter is now resolved
  at compile time. An immutable builder whose result depends on call order is
  worse than a mutable one, because nothing about the API suggests it could.
- **Coverage was derived from the result rows** (§6.1).

Both were found by tests written from this document rather than from the code,
which is the argument for writing the spec first.
