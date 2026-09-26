"""The one table of query parameters (spec/09-WEB.md §2).

`rsse query`, `rsse explain` and the web form all build their `Search` through
`search_from_params`, from a plain dict keyed by the names below. The CLI
flags and the form's fields are both generated from `QUERY_PARAMS`, so the
two cannot drift apart: a predicate added here appears in both, and the same
inputs compile to the same SQL.

Two rules the table enforces rather than documents:

- **An unknown key is an error.** A misspelled filter that is quietly dropped
  widens the query, and a count that is too large reads as a finding exactly
  as a false zero does.
- **Nothing here takes SQL text.** `Search.order_by()` splices its argument
  into `ORDER BY`, which is fine for a Python caller and injection for anything
  fed by a request. Ordering is `ORDERS`, a fixed set of names.
"""

from __future__ import annotations

from dataclasses import dataclass

from .search import QueryError, Search


@dataclass(frozen=True)
class Param:
    """One query input.

    `kind` is one of:

    - `int`, `str` -- a value passed to `method`;
    - `switch` -- `method` called with no argument when the value is true;
    - `list` -- repeatable; `method` called once per value;
    - `range` -- `LO,HI`, passed to `method` as two ints;
    - `seq` -- `2,1`, a fielder sequence, passed as a list;
    - `choice` -- a `str` restricted to `choices`;
    - `option` -- a modifier read by a composite (`method` is None).
    """

    name: str
    kind: str
    group: str
    method: str | None = None
    help: str = ""
    choices: tuple[str, ...] = ()

    @property
    def flag(self) -> str:
        return "--" + self.name.replace("_", "-")

    def as_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "group": self.group,
                "help": self.help, "choices": list(self.choices),
                "flag": self.flag}


def _p(name, kind, group, method=None, help="", choices=()):
    return Param(name, kind, group, method, help, tuple(choices))


#: Order matters: it is the order predicates are applied in, and so the order
#: their terms appear in the compiled SQL. It is the order the CLI used before
#: this table existed, kept so that no existing query's SQL changed.
QUERY_PARAMS: tuple[Param, ...] = (
    # -- values mapping one-to-one onto a Search method
    _p("bases", "str", "context", "bases", "base state before the play, 000-111"),
    _p("outs", "int", "context", "outs", "outs before the play"),
    _p("outs_after", "int", "context", "outs_after", "outs after the play"),
    _p("inning", "int", "context", "inning"),
    _p("inning_at_least", "int", "context", "inning_at_least"),
    _p("half", "choice", "context", "half", choices=("top", "bottom")),
    _p("season", "int", "games", "season"),
    _p("league", "str", "games", "league", "AL, NL, FL, NGL, or a teams.league code"),
    _p("team", "str", "games", "team", "team id, either side"),
    _p("batting_team", "str", "games", "batting_team", "team id"),
    _p("fielding_team", "str", "games", "fielding_team", "team id"),
    _p("batter", "str", "players", "batter", "person id, e.g. ruthb101"),
    _p("park", "str", "games", "park", "park id"),
    _p("runner_on", "choice", "context", "runner_on", choices=("1", "2", "3")),
    # Names, resolved against the reference tables. Separate from the id
    # parameters: `ruthb101` and `Babe Ruth` are never confusable, but a
    # parameter that silently accepts both hides which one failed.
    _p("batter_named", "str", "players", "batter_named", "e.g. Babe Ruth"),
    _p("park_named", "str", "games", "park_named", "e.g. Wrigley Field"),
    _p("team_named", "str", "games", "team_named", "e.g. Yankees"),
    _p("out_at", "choice", "play", "out_at", "any out at this base",
       choices=("1", "2", "3", "H")),
    _p("batter_ran", "choice", "play", "batter_ran",
       choices=("yes", "no", "unknown")),
    _p("error", "int", "fielding", "error", "error by this position"),
    _p("hit_location", "str", "play", "hit_location",
       "zone as written, e.g. 7 or 78D; a trailing * is a prefix"),
    _p("position_played", "str", "players", "position_played",
       "batter's roster position that season, e.g. SS"),
    _p("event_matches", "str", "play", "event_matches",
       "regex over the raw event text"),
    # -- switches
    _p("bases_loaded", "switch", "context", "bases_loaded"),
    _p("bases_empty", "switch", "context", "bases_empty"),
    _p("scoring_position", "switch", "context", "scoring_position"),
    _p("hit_located", "switch", "play", "hit_located",
       "the play records any location (a denominator; slow alone)"),
    _p("inning_ending", "switch", "context", "inning_ending"),
    _p("walkoff", "switch", "context", "walkoff"),
    _p("strikeout", "switch", "play", "strikeout"),
    _p("dropped_third", "switch", "play", "dropped_third",
       "uncaught third strike"),
    _p("batter_reached_on_k", "switch", "play", "batter_reached_on_k"),
    _p("double_play", "switch", "play", "double_play"),
    _p("triple_play", "switch", "play", "triple_play"),
    _p("include_uncertain", "switch", "quality", "include_uncertain"),
    _p("include_untrusted", "switch", "quality", "include_untrusted"),
    _p("include_unparsed", "switch", "quality", "include_unparsed"),
    _p("curated_only", "switch", "quality", "curated_only"),
    _p("exclude_curated", "switch", "quality", "exclude_curated"),
    _p("include_exhibition", "switch", "game_type", "include_exhibition"),
    _p("include_allstar", "switch", "game_type", "include_allstar"),
    _p("only_postseason", "switch", "game_type", "only_postseason"),
    # -- composites
    _p("seasons", "range", "games", "seasons", "LO,HI inclusive"),
    _p("tag", "list", "play", "tag", "an ontology tag; repeatable"),
    _p("force_play", "switch", "fielding", None, "any force out"),
    _p("force_play_at", "choice", "fielding", None, "force out at this base",
       choices=("1", "2", "3", "H")),
    _p("force_certainty", "choice", "fielding", None,
       "narrow the force determination", choices=("derived", "likely",
                                                  "ambiguous")),
    _p("include_tag_outs", "switch", "fielding", None,
       "widen a force query to any out at that base"),
    _p("tag_out_at", "choice", "fielding", None, "tag out at this base",
       choices=("1", "2", "3", "H")),
    _p("putout_sequence", "seq", "fielding", "putout_sequence",
       "one throw sequence exactly, e.g. 2,1"),
    _p("contains_sequence", "seq", "fielding", "contains_sequence",
       "contiguous within a sequence, e.g. 2,1 (slower)"),
    _p("putout_by", "int", "fielding", None, "putout credited to this position"),
    _p("assist_by", "seq", "fielding", None,
       "with assists from these positions, e.g. 2"),
)

#: Keys read by a composite rather than applied on their own.
_OPTIONS = {"force_certainty", "include_tag_outs", "assist_by"}

BY_NAME = {p.name: p for p in QUERY_PARAMS}

#: Named orderings, each a fixed SQL fragment. The only way to order a query
#: from outside Python. Chronological is the default (spec/06-QUERY.md §6).
ORDERS = {
    "chronological": None,
    "reverse": "g.date DESC, p.game_id DESC, p.game_key DESC, p.seq DESC",
}


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _int(param: Param, value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise QueryError(f"{param.name} must be a whole number,"
                         f" not {value!r}") from None


def _seq(param: Param, value) -> list[str]:
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    items = [str(x).strip() for x in items if str(x).strip()]
    if not items:
        raise QueryError(f"{param.name} is empty")
    return items


def _present(value) -> bool:
    return value is not None and value != "" and value != [] and value is not False


def normalize(params: dict) -> dict:
    """Drop empty values and refuse unknown keys.

    Empty is how a blank form field arrives, so it means "not set" rather than
    "set to nothing". Unknown is a typo, and is an error (module docstring).
    """
    unknown = sorted(k for k in params if k not in BY_NAME)
    if unknown:
        near = {k: [n for n in BY_NAME if n.startswith(k[:4])] for k in unknown}
        hint = "; ".join(f"{k} (did you mean {', '.join(v)}?)" if v else k
                         for k, v in near.items())
        raise QueryError(f"unknown query parameter(s): {hint}")
    return {k: v for k, v in params.items() if _present(v)}


def search_from_params(params: dict) -> Search:
    """Build the `Search` a dict of query parameters describes."""
    params = normalize(params)
    for name, needs in (("force_certainty", ("force_play", "force_play_at")),
                        ("include_tag_outs", ("force_play", "force_play_at")),
                        ("assist_by", ("putout_by",))):
        if name in params and not any(n in params for n in needs):
            raise QueryError(f"{name} modifies {' or '.join(needs)},"
                             " which is not set")
    s = Search()
    for p in QUERY_PARAMS:
        # Composites are applied at their anchor's place in the table, so the
        # SQL keeps the table's order.
        if p.name == "force_play":
            s = _force(s, params)
            continue
        if p.name == "tag_out_at" and p.name in params:
            s = s.tag_out(at=_choice(p, params[p.name]))
            continue
        if p.name == "putout_by" and p.name in params:
            assists = (_seq(BY_NAME["assist_by"], params["assist_by"])
                       if "assist_by" in params else ())
            s = s.putout_by(_int(p, params[p.name]),
                            assist_by=[_int(BY_NAME["assist_by"], a)
                                       for a in assists])
            continue
        if p.name not in params or p.method is None:
            continue
        value = params[p.name]
        method = getattr(s, p.method)
        if p.kind == "switch":
            if _truthy(value):
                s = method()
        elif p.kind == "int":
            s = method(_int(p, value))
        elif p.kind in ("str", "choice"):
            s = method(_choice(p, value))
        elif p.kind == "list":
            for v in (value if isinstance(value, (list, tuple)) else [value]):
                if str(v).strip():
                    s = getattr(s, p.method)(str(v).strip())
        elif p.kind == "range":
            parts = _seq(p, value)
            if len(parts) != 2:
                raise QueryError(f"{p.name} is LO,HI, not {value!r}")
            s = method(*(_int(p, x) for x in parts))
        elif p.kind == "seq":
            s = method(_seq(p, value))
    return s


def _choice(p: Param, value) -> str:
    value = str(value).strip()
    if p.choices and value not in p.choices:
        raise QueryError(f"{p.name} must be one of {', '.join(p.choices)},"
                         f" not {value!r}")
    return value


def _force(s: Search, params: dict) -> Search:
    if not (_truthy(params.get("force_play", False))
            or "force_play_at" in params):
        return s
    at = params.get("force_play_at")
    certainty = params.get("force_certainty")
    return s.force_play(
        at=None if at is None else _choice(BY_NAME["force_play_at"], at),
        certainty=(None if certainty is None
                   else _choice(BY_NAME["force_certainty"], certainty)),
        include_tag_outs=_truthy(params.get("include_tag_outs", False)))


def apply_page(s: Search, order: str | None = None, limit: int | None = None,
               offset: int | None = None) -> Search:
    """Ordering and paging, from names rather than SQL."""
    if order:
        if order not in ORDERS:
            raise QueryError(f"order must be one of {', '.join(ORDERS)},"
                             f" not {order!r}")
        if ORDERS[order]:
            s = s.order_by(ORDERS[order])
    if limit is not None:
        s = s.limit(limit)
    if offset:
        s = s.offset(offset)
    return s


def params_from_namespace(args) -> dict:
    """The query parameters an argparse namespace carries."""
    return {p.name: getattr(args, p.name) for p in QUERY_PARAMS
            if _present(getattr(args, p.name, None))}


def add_arguments(parser) -> None:
    """Add one CLI flag per `QUERY_PARAMS` entry."""
    for p in QUERY_PARAMS:
        kw = {"help": p.help or None}
        if p.kind == "switch":
            kw["action"] = "store_true"
        elif p.kind == "list":
            kw["action"] = "append"
        elif p.kind == "choice":
            kw["choices"] = p.choices
        parser.add_argument(p.flag, **kw)
