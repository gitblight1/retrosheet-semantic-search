# 02 — Event Grammar

Normative grammar for the `event` field (field 6 of a `play` record) and the
`pitches` field (field 5). This layer applies **no baseball knowledge**: it
produces a faithful syntax tree and nothing more. Force plays, dropped third
strikes, and base state belong to [03-STATE](03-STATE.md) and
[04-ONTOLOGY](04-ONTOLOGY.md).

## 1. Shape

An event has three parts:

```
<basic play> { "/" <modifier> } [ "." <advances> ]
```

Two facts the earlier draft grammar got wrong, restated because they drive
everything below:

- **`/` introduces a modifier. `+` joins two basic events.** `K+WP` is a
  strikeout *and* a wild pitch; `K/DP` is a strikeout with a double-play
  modifier. They are different productions.
- **`;` appears in two different roles.** It separates advances after the `.`,
  and it separates basic events before it (`SB3;SB2`). Position disambiguates;
  a parser that splits the whole string on `;` is wrong.

## 2. EBNF

```ebnf
event            ::= basic_section { "/" modifier } [ "." advance_section ]

(* ---------- basic play ---------- *)

basic_section    ::= basic_group { ";" basic_group }
basic_group      ::= basic_event { "+" basic_event }

basic_event      ::= batter_event | running_event | "NP"

(* ---------- events made by the batter ---------- *)

batter_event     ::= out_on_ball
                   | hit
                   | strikeout
                   | walk
                   | hit_by_pitch
                   | reached_on_error
                   | fielders_choice
                   | foul_fly_error
                   | interference
                   | unknown_play

out_on_ball      ::= fielder_seq { "(" runner ")" [ fielder_seq ] }
hit              ::= ( "S" | "D" | "T" ) [ fielder_seq ]
                   | "DGR" [ fielder_seq ]
                   | ( "HR" | "H" ) [ fielder_seq ]
strikeout        ::= "K" [ fielder_seq { "(" runner ")" [ fielder_seq ] } ]
walk             ::= "IW" | "I" | "W"
hit_by_pitch     ::= "HP"
reached_on_error ::= [ fielder_seq ] "E" fielder
fielders_choice  ::= "FC" [ fielder_seq ]
foul_fly_error   ::= "FLE" fielder
interference     ::= "C"
unknown_play     ::= "99" { "(" runner ")" [ fielder_seq ] }

(* ---------- events not made by the batter ---------- *)

running_event    ::= "SB" base { "(" adv_param ")" }
                   | "CS" base [ "(" fielding_param ")" ]
                   | "POCS" base [ "(" fielding_param ")" ]
                   | "PO" base [ "(" fielding_param ")" ]
                   | "DI" | "OA" | "WP" | "PB" | "BK"

(* ---------- modifiers ---------- *)

modifier         ::= named_modifier
                   | "E" fielder
                   | "TH" [ base ]
                   | "R" fielder_seq
                   | trajectory [ location ]
                   | location

trajectory       ::= "BG" | "BP" | "BL" | "B" | "G" | "L" | "P" | "F"
location         ::= zone { loc_qualifier }
zone             ::= digit [ digit ]
loc_qualifier    ::= "L" | "M" | "R" | "S" | "D" | "X" | "F" | "W"

(* ---------- advances ---------- *)

advance_section  ::= advance { ";" advance }
advance          ::= base ( "-" | "X" ) base { "(" adv_param ")" }
adv_param        ::= adv_flag | fielding_param
adv_flag         ::= "UR" | "TUR" | "RBI" | "NR" | "NORBI" | "WP" | "PB"
fielding_param   ::= credit_seq { "/" modifier }
credit_seq       ::= credit_atom { credit_atom }
credit_atom      ::= fielder | "E" fielder

(* ---------- terminals ---------- *)

fielder_seq      ::= fielder { fielder }
fielder          ::= "1".."9" | "U"
runner           ::= "B" | "1" | "2" | "3"
base             ::= "B" | "1" | "2" | "3" | "H"
digit            ::= "0".."9"
```

### 2.1 Resolution notes

**`99` before `E`.** `99` is a reserved two-digit code for an unknown play,
including unknown force outs and double plays. It cannot arise as a real
fielder pair (no fielder 9 twice unassisted). Match `99` before attempting
`out_on_ball`, and emit no putout or assist credit for it. Extra fielders padded
into a double play with `9` likewise receive no credit.

**`reached_on_error` vs `out_on_ball`.** Both begin with digits. `3E1` is an
assist to the first baseman and an error on the pitcher; `31` is a ground out.
Scan for an `E` before committing to a production.

**`HP` and `HR` before `H`.** Longest match, and `HP` first of the three — a
parser that tries `H` first reads every hit batsman as a home run followed by a
stray `P`. Same discipline for `IW` before `I`, `POCS` before `PO`, `DGR` before
`D`, `FLE` before `FC`/`F`.

**`C` is contextual.** Bare `C` is interference, and the interfering fielder is
carried in the modifier: `C/E2` catcher, `C/E1` pitcher, `C/E3` first baseman.
The parser records the event as `Interference` with the fielder taken from the
`E$` modifier; it does **not** assume the catcher.

**`U` fielder.** A `U` in a credit sequence means the handling fielder is
unknown (`8U3`). Present in older files only. Emit the putout to the last atom
and no assist for the `U`.

**Bare location modifier.** `D8/78` — a modifier that is only a location, with
no trajectory letter. Try `trajectory location` first, then bare `location`.

**Location codes are retained verbatim.** Retrosheet publishes the zone
semantics only as a diagram image, so RSSE stores the raw location string plus
the structural decomposition above. Mapping zones to field regions is out of
scope for v1; the raw string is queryable as text so nothing is lost.

**`TH` vs `T`.** Inside a modifier, `TH` is a throw and `TH3` a throw to third;
`T` alone in the basic section is a triple. Different sections, no conflict.

## 3. Annotation characters

Retrosheet permits `#`, `!`, `?`, `+`, `-` inside an event. Its own
documentation says they "can be safely ignored". Principle 2.1 says nothing is
discarded. Both are satisfied by treating them as **trivia**: the lexer strips
them into a side-channel list of `(byte_offset, char)` attached to the play, so
the tree is clean and the original string can still be rebuilt byte-for-byte.

| Char | Meaning |
|---|---|
| `#` | uncertainty in the play; often followed by an explanatory `com` |
| `!` | exceptional play |
| `?` | uncertainty |
| `+` | hard hit ball |
| `-` | softly hit ball |

**Disambiguation.** `+` and `-` are overloaded.

- `+` is the event joiner when the next character starts a basic event;
  it is the hard-hit annotation when the next character is `/`, `.`, `;`, `)`,
  or end of string.
- `-` is the advance operator only when it sits between two `base` characters
  inside `advance_section`. Everywhere else it is the soft-hit annotation.

**"Next character" means the next *significant* character.** Annotation
characters cluster, and the lookahead must skip them: in `S8/L78+#.2-H` the `+`
is followed literally by `#`, but the next significant character is `.`, so the
`+` is a hard-hit marker rather than an event joiner. A single-character
lookahead reads it as a joiner and then fails to find an event after it. The
same applies to the lookbehind for `-`.

These two rules MUST have dedicated tests; they are the likeliest source of
silent mis-parses.

## 4. Modifier catalogue

Complete list excluding hit locations. `$` is a fielder, `%` a base.

| Code | Meaning | | Code | Meaning |
|---|---|---|---|---|
| `AP` | appeal play | | `INT` | interference |
| `BF` | **undocumented**; foul bunt strikeout — see §4.1 | | `IPHR` | inside-the-park home run |
| `BG` | ground ball bunt | | `L` | line drive |
| `BGDP` | bunt grounded into DP | | `LDP` | lined into double play |
| `BINT` | batter interference | | `LTP` | lined into triple play |
| `BL` | line drive bunt | | `MREV` | manager challenge |
| `BOOT` | batting out of turn | | `NDP` | no double play credited |
| `BP` | bunt pop up | | `OBS` | obstruction |
| `BPDP` | bunt popped into DP | | `P` | pop fly |
| `BR` | runner hit by batted ball | | `PASS` | runner passed another runner |
| `C` | called third strike | | `R$$` | relay to `$`, no out made; one or two fielders |
| `COUB` | courtesy batter | | `RINT` | runner interference |
| `COUF` | courtesy fielder | | `SF` | sacrifice fly |
| `COUR` | courtesy runner | | `SH` | sacrifice hit (bunt) |
| `DP` | unspecified double play | | `TH` | throw |
| `E$` | error on `$` | | `TH%` | throw to base `%` |
| `F` | fly | | `TP` | unspecified triple play |
| `FDP` | fly ball double play | | `UINT` | umpire interference |
| `FINT` | fan interference | | `UREV` | umpire review |
| `FL` | foul | |  |  |
| `FO` | force out | |  |  |
| `G` | ground ball | |  |  |
| `GDP` | ground ball double play | |  |  |
| `GTP` | ground ball triple play | |  |  |
| `IF` | infield fly rule | |  |  |

### 4.1 Undocumented modifiers

Two modifier codes occur in the corpus but appear nowhere in Retrosheet's
published list. Both attach only to a strikeout:

| Code | Reading | Evidence |
|---|---|---|
| `B` | bunt, type unspecified — a **trajectory**, so it may carry a location (`/B6S`) | ~550 occurrences, essentially always alongside `/SH`. Generalises the documented `BG`/`BP`/`BL`, and must be matched after them or it shadows all three. |
| `BF` | strikeout on a foul bunt with two strikes | 127 in 2000, 129 in 1965; always on `K` |
| `S` | swinging third strike | 5 in 1965; complement of the documented `C` (called third strike), which appears 4,701 times in the same season. One file carries `com,"MIN scorer: K/S"`, identifying it as a scorer's notation. |

Two published statements are also contradicted by the corpus:

- **`DGR` takes no fielder.** The documentation says a ground rule double names
  no fielding player. `DGR7` and `DGR9` both occur.
- **`R$` relays one fielder.** `R652`, `R862` and `R6524` occur, so the relay
  modifier takes a full fielder sequence.

They are flagged rather than quietly absorbed because they are the demonstrated
case of the published documentation being incomplete. **The documentation is
the starting point for the grammar; the corpus is the authority.** §8's failure
policy and §9's sweep are what turn that from a hazard into a process.

Any further undocumented code found by the corpus sweep is added here with its
evidence, never inferred from its spelling alone.

**`/FO` is not the definition of a force play.** It appears on batted-ball force
outs only. Forces arising on uncaught third strikes, and forces recorded in the
advance section, carry no marker. See [03-STATE](03-STATE.md) §4.

## 5. Advance parameters

Advances are listed lead-runner first: third, then second, then first, then the
batter. Order is a Retrosheet convention, **not** chronological out order — see
[03-STATE](03-STATE.md) §4.3.

- `2-3` successful advance; `1X2` out advancing.
- `3-3` explicit hold. A runner not listed stays put.
- Fielding: last atom gets the putout, earlier atoms get assists — `BX2(8434)`.
- An `E` in a credit sequence **negates the out**: `BX2(7E4)` is written with an
  `X` but the runner is safe, with an assist to 7 and an error on 4. The same
  applies in the basic section: `CS2(2E4)` and `PO1(E3)` are not outs. A parser
  that trusts the `X` will over-count outs.
- Nested modifier: `1-3(E5/TH)` throwing error on the third baseman;
  `2X3(5/INT)` interference.
- Flags: `(UR)` unearned, `(TUR)` team unearned, `(RBI)` force an RBI, `(NR)` /
  `(NORBI)` suppress one, `(WP)` / `(PB)` alternative encoding of a wild pitch or
  passed ball (`K.1-2(WP)` ≡ `K+WP.1-2`).
- Multiple parameters are each separately parenthesized:
  `2-H(E4/TH)(UR)(NR)`.

## 6. Round-trip requirement

For every parsed event the emitter MUST reproduce the source string byte for
byte:

```
emit(parse(s)) == s        for all s in the corpus
```

This is a hard gate, enforced over the whole corpus in CI, not a sampled check.
It is the operational form of "never lose information": any construct the
grammar drops shows up immediately as a round-trip failure. Annotation trivia
(§3) is part of the reproduced output.

## 7. Pitch sequence

Field 5 of a `play` record. Each character is one token; a `.` marks a break
where a non-batter play interrupted the plate appearance.

| | | | | | |
|---|---|---|---|---|---|
| `+` | pickoff throw by catcher follows | `A` | automatic strike (pitch timer) | `N` | no pitch (balk, interference) |
| `*` | next pitch blocked by catcher | `B` | ball | `O` | foul tip on bunt |
| `.` | play not involving the batter | `C` | called strike | `P` | pitchout |
| `1` | pickoff throw to first | `F` | foul | `Q` | swinging on pitchout |
| `2` | pickoff throw to second | `H` | hit batter | `R` | foul on pitchout |
| `3` | pickoff throw to third | `I` | intentional ball | `S` | swinging strike |
| `>` | runner going on the pitch | `K` | strike, type unknown | `T` | foul tip |
| | | `L` | foul bunt | `U` | unknown or missed pitch |
| | | `M` | missed bunt attempt | `V` | called ball, pitcher to mouth; or automatic ball |
| | | | | `X` | ball put into play |
| | | | | `Y` | ball in play on pitchout |

`*` and `>` are prefix markers modifying the pitch that follows; `+` refers to
the throw preceding it. The parser stores the sequence both verbatim and as a
token list with those markers attached to their pitch.

Most games carry no pitch data. `info,pitches` states whether the game has
`pitches`, `count` only, or `none`; predicates over pitch data MUST consult it
so that "no games matched" is distinguishable from "no games had the data"
([01-CORPUS](01-CORPUS.md) §5.2).

## 8. Failure policy

The parser never guesses and never silently drops a play.

1. On a construct the grammar rejects, emit a `Play` with `parse_status =
   'unparsed'`, the raw event string, and the error offset. Do not attempt
   partial interpretation.
2. On a construct that parses but has no ontology mapping, `parse_status =
   'parsed_untagged'`.
3. `parse_status` is exposed to queries and counted in the coverage statement.

Any parse failure is a spec bug. The corpus-wide expectation is zero
`unparsed` plays; CI fails if the count rises above the recorded baseline.

## 9. Validation status

The grammar is not a paper design. It is implemented as a recursive-descent
parser built directly from §2 and run over the corpus:

| | |
|---|---|
| seasons | 57 (1908–1961, plus 1965, 2000, 2023) |
| files | 998 |
| games | 73,131 |
| plays | **6,240,913** |
| parse failures | **0** |
| round-trip failures | **0** |

Seasons 1962–2025 are not yet swept; see the README build order.

### 9.1 What the sweep found

Three passes were needed. Every failure was a real gap in the grammar as first
written, and each is fixed above:

| Gap | Found by | Fix |
|---|---|---|
| `HP` parsed as `H` + stray `P` | 3-season probe | longest-match ordering, §2.1 |
| `/BF` unknown modifier | 3-season probe | §4.1 |
| Location codes ending `W` (`F78XDW`) | 3-season probe | `loc_qualifier` gains `W` |
| `SBH(UR)` — parameters on a stolen base | 3-season probe | `running_event` accepts `adv_param` |
| `99(1)/FO` — `99` taking a runner designator | 3-season probe | `unknown_play` accepts runner groups |
| `R62` — relay naming two fielders | 3-season probe | `"R" fielder_seq` |
| `/S` swinging third strike | 3 seasons, real parser | §4.1 |
| Adjacent trivia (`L78+#.`) defeating one-char lookahead | unit test | §3, look past annotation characters |
| `/B` plain bunt, and `/B6S` with a location | full sweep | `B` added to `trajectory` |
| `DGR7` — ground rule double naming a fielder | full sweep | `"DGR" [ fielder_seq ]` |
| `R6524` — relay naming four fielders | full sweep | `"R" fielder_seq` |

### 9.2 Conclusions for the implementation

1. **The structure is sound.** Every failure was a missing terminal, an
   ordering slip, or a lookahead that was one character too short. No
   production had to change, across 6.2 million plays and 54 seasons of
   notation drift.
2. **The published documentation is incomplete and in two places wrong.** Four
   modifier codes (`B`, `BF`, `S`, and the `W` location suffix) appear nowhere
   in it, and two of its explicit statements — that `DGR` names no fielder, and
   that `R$` takes one — are contradicted by the data. The documentation is the
   starting point for the grammar; **the corpus is the authority.**
3. **The round-trip gate earned its place.** It passed at every stage, including
   the passes where hundreds of plays failed to parse, which is what makes it
   trustworthy as the information-loss detector rather than a proxy for
   correctness. The two are independent gates and both are needed.
4. **Failures cluster.** 577 failures across 52 distinct shapes reduced to four
   underlying causes. Grouping by shape (digits collapsed to `#`) is what makes
   a sweep actionable rather than a wall of output.
