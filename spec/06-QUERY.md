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
| `.batter(id)` / `.pitcher(id)` / `.fielder(pos, id)` | ids, never names |
| `.park(id)` | `games.site` |

Ambiguous names are given both forms rather than a default: `.outs()` /
`.outs_after()`. The original `.outs(2)` did not say which, and for the
motivating play the two differ (2 before, 3 after).

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
| `.include_unparsed()` | also non-`ok` plays; a research affordance for finding grammar gaps |
| `.curated_only()` / `.exclude_curated()` | filter on `play_tags.source` |

Defaults exclude uncertain rows so that a result set is defensible. The
excluded counts are always reported (§6), so exclusion is never silent.

## 5. Game-type defaults

Default scope is `game_type IN ('regular','playoff','worldseries','lcs',
'divisionseries','wildcard','championship')` — i.e. games that count, with
`playoff` included as the tiebreaker subset of `regular`.

`.include_exhibition()`, `.include_allstar()`, `.only_postseason()` adjust it.
Pre-2023 games have no `gametype` record and are treated as `regular` unless
the game id or schedule says otherwise; that inference is recorded on the game
row rather than applied at query time.

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

## 7. Performance

Target: <100 ms for a typical query on a warm cache.

- Tag joins first, then column filters, then `EXISTS` subqueries for advances
  and sequences — most selective to least.
- `.count()` compiles to `COUNT(*)` without materializing rows.
- Any query whose plan contains a full scan of `plays` emits a warning through
  `.explain()`.
- The benchmark suite ([07-TESTING](07-TESTING.md) §5) pins the motivating query
  and a dozen others; a regression beyond the target fails CI.

## 8. CLI

```
rsse query --bases-loaded --outs 2 --tag UncaughtThirdStrike \
           --force-play-at H --putout-sequence 2,1 --format table
rsse explain <same flags>
rsse coverage --season-range 1901 2026
```

`--format` is `table`, `json`, or `csv`. Every non-`table` export carries the
Retrosheet attribution notice ([01-CORPUS](01-CORPUS.md) §1) and the corpus
version.
