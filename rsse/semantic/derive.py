"""Tag derivation: run the ontology over a play (spec/04-ONTOLOGY.md).

:mod:`rsse.semantic.ontology` holds the rules. This module runs them, and owns
the three things a rule deliberately does not know about:

* **Implication and aliasing.** `GroundRuleDouble` implies `Double`, and
  `DroppedThirdStrike` is a deprecated alias of `UncaughtThirdStrike`. Both are
  declared on the ``TagDef`` and closed over here, so no rule restates another.
* **Confidence** (§1.4). A tag is `uncertain` only from a stated ambiguity:
  an `ambiguous` force determination, a `#` annotation, or a `99` unknown play.
  Nothing else may downgrade a tag, and nothing may upgrade one.
* **Curated tags** (§8). Human judgement, quarantined in a version-controlled
  file, loaded alongside derived tags and distinguishable from them in every
  result. The deriver never produces one and a re-derive never overwrites one.

Derivation is a pure function of the parsed play plus state: dropping and
rebuilding `play_tags` changes nothing (§1.2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..model.state import FORCE_AMBIGUOUS, PlayContext, PlayOutcome
from ..parser import grammar as G
from . import ontology as O
from .ontology import CERTAIN, UNCERTAIN, REGISTRY, PlayFacts

#: The curated tag set, keyed by (game_id, play_seq). Version-controlled
#: because it is an editorial artefact, not data (§8).
CURATED_PATH = Path(__file__).with_name("curated_tags.json")


@dataclass(frozen=True)
class DerivedTag:
    name: str
    confidence: str = CERTAIN
    source: str = "derived"

    def __str__(self) -> str:  # pragma: no cover - convenience
        mark = "" if self.confidence == CERTAIN else "?"
        return self.name + mark


# ---------------------------------------------------------------------------
# confidence (§1.4)
# ---------------------------------------------------------------------------

#: Tags whose confidence depends on a force determination, as
#: (must_be_forced, destination or None).
_FORCE_DEPENDENT = {
    "ForceOut": (True, None),
    "ForceOutAtHome": (True, "H"),
    "ForceOutAtThird": (True, "3"),
    "ForceOutAtSecond": (True, "2"),
    "ForceOutAtFirst": (True, "1"),
    "TagOut": (False, None),
}


def _play_is_uncertain(facts: PlayFacts) -> bool:
    """The two play-wide sources of uncertainty in §1.4.

    A `#` is Retrosheet's own marker that the play as recorded is questionable,
    and `99` says the play is not known. Either makes every reading of the play
    uncertain, so it is applied to all of its tags rather than to some.
    """
    if any(ch == "#" for _offset, ch in facts.trivia):
        return True
    return REGISTRY["UnknownPlay"].rule(facts)


def _confidence(facts: PlayFacts, name: str, play_uncertain: bool) -> str:
    """Confidence for one tag on one play."""
    td = REGISTRY[name]
    if td.always_uncertain:
        return UNCERTAIN
    # A `#` or a `99` says this play's record is questionable. It says nothing
    # about the base/out state the batter walked into, which earlier plays
    # established, nor about whether the game ended here. Applying play-wide
    # uncertainty to those tags marked ~35k `BasesEmpty` tags uncertain in a
    # 12-season sample for no reason, and they are excluded from default query
    # results (spec/06-QUERY.md §4).
    if play_uncertain and not td.prior_state:
        return UNCERTAIN
    spec = _FORCE_DEPENDENT.get(name)
    if spec is not None:
        forced, at = spec
        supporting = facts.force_outs(at) if forced else facts.tag_outs(at)
        # Only `ambiguous` downgrades a tag. `derived` and `likely` are both
        # `certain` here, because tag confidence is binary and `uncertain`
        # means excluded from default results (spec/06-QUERY.md §4) -- and
        # `likely` is how the pre-1970s corpus records an ordinary ground out,
        # so downgrading it would drop about half the force outs at first.
        # The three-way distinction survives on the advance, which is where a
        # query that needs the strict population reads it.
        if supporting and all(a.force_certainty == FORCE_AMBIGUOUS
                              for a in supporting):
            return UNCERTAIN
    return CERTAIN


# ---------------------------------------------------------------------------
# derivation
# ---------------------------------------------------------------------------

def derive_from_facts(facts: PlayFacts) -> list[DerivedTag]:
    """Every tag whose rule fires, plus its implications and aliases.

    Returned sorted by name so a tag set is directly comparable between runs;
    tags are a set, not a sequence, and nothing downstream may depend on order.
    """
    fired: set[str] = set()
    for name, td in REGISTRY.items():
        if td.curated_only:
            continue
        if td.rule(facts):
            fired.add(name)

    # Close over implications: a tag's implications fire even if their own rule
    # did not, which is what lets `Double` be stated once (§2).
    pending = list(fired)
    while pending:
        for implied in REGISTRY[pending.pop()].implies:
            if implied not in fired:
                fired.add(implied)
                pending.append(implied)

    play_uncertain = _play_is_uncertain(facts)
    return sorted(
        (DerivedTag(name, _confidence(facts, name, play_uncertain))
         for name in fired),
        key=lambda t: t.name,
    )


def derive(event: G.Event, outcome: PlayOutcome,
           context: PlayContext | None = None,
           trivia: tuple = ()) -> list[DerivedTag]:
    """Derive tags for one play from its parse tree and replayed state."""
    return derive_from_facts(PlayFacts(event, outcome, context, trivia))


def tag_names(tags: list[DerivedTag], certain_only: bool = False) -> list[str]:
    """Just the names -- what a gold file asserts against."""
    return [t.name for t in tags
            if not certain_only or t.confidence == CERTAIN]


# ---------------------------------------------------------------------------
# curated tags (§8)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CuratedEntry:
    game_id: str
    play_seq: int
    tags: tuple[str, ...]
    note: str = ""
    citation: str = ""


def load_curated(path: Path | None = None) -> dict[tuple[str, int], CuratedEntry]:
    """Load the curated tag set, validating every name against the registry.

    A curated tag must still be a *named* tag: §8 quarantines human judgement
    about which plays qualify, not about what the vocabulary is. An unknown
    name is an error, not a new tag, because a typo would otherwise create one
    silently and no query would ever match it.
    """
    path = path or CURATED_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing. The curated set ships with the package; "
            "returning an empty one instead would make a derive quietly "
            "disagree with a derive from the source tree, which is exactly "
            "the difference §8 exists to keep visible.")
    doc = json.loads(path.read_text())
    out: dict[tuple[str, int], CuratedEntry] = {}
    for entry in doc.get("entries", []):
        names = tuple(entry["tags"])
        unknown = [n for n in names if n not in REGISTRY]
        if unknown:
            raise ValueError(
                f"{path.name}: {entry['game_id']} play {entry['play_seq']} "
                f"names tags that are not in the ontology: {unknown}")
        key = (entry["game_id"], int(entry["play_seq"]))
        if key in out:
            raise ValueError(f"{path.name}: duplicate entry for {key}")
        out[key] = CuratedEntry(entry["game_id"], int(entry["play_seq"]), names,
                                entry.get("note", ""), entry.get("citation", ""))
    return out


def curated_for(game_id: str, play_seq: int,
                curated: dict[tuple[str, int], CuratedEntry] | None = None
                ) -> list[DerivedTag]:
    entry = (curated if curated is not None else load_curated()).get(
        (game_id, play_seq))
    if entry is None:
        return []
    return [DerivedTag(n, CERTAIN, source="curated") for n in entry.tags]


def tags_for_play(event: G.Event, outcome: PlayOutcome,
                  context: PlayContext | None = None,
                  trivia: tuple = (),
                  game_id: str = "", play_seq: int = 0,
                  curated: dict[tuple[str, int], CuratedEntry] | None = None
                  ) -> list[DerivedTag]:
    """Every tag on one play, derived and curated together (§8).

    This is what a `play_tags` load writes. It exists so §8's two guarantees
    are executable rather than described:

    * **A curated tag survives a re-derive.** Derivation is a pure function of
      the play, so re-running it can only reproduce the derived set; the
      curated entries are merged in afterwards from the version-controlled
      file and cannot be computed away.
    * **A curated tag is distinguishable in every result.** It carries
      ``source='curated'``, and on the one collision that can occur -- a human
      asserting a name the deriver also fired -- the curated row wins, because
      `play_tags` is keyed ``(play_id, tag_id)`` and can hold only one.

    The collision is not hypothetical and not a mistake: §3.1.1 names it as the
    intended escape hatch. The 2000 record is an uncaught third strike that the
    event string cannot show, so a curator may assert `UncaughtThirdStrike`
    there. Letting the derived row win would silently discard exactly the
    judgement §8 exists to preserve.
    """
    derived = derive(event, outcome, context, trivia)
    entries = curated_for(game_id, play_seq, curated)
    if not entries:
        return derived
    curated_names = {t.name for t in entries}
    merged = [t for t in derived if t.name not in curated_names] + entries
    return sorted(merged, key=lambda t: t.name)


# ---------------------------------------------------------------------------
# the tags table (spec/05-DATABASE.md §4)
# ---------------------------------------------------------------------------

def tag_rows() -> list[dict]:
    """Rows for the `tags` table: name, category, version, rule hash, alias.

    The rule hash is per tag rather than per ontology, so a version bump that
    touched one derivation is distinguishable from one that touched forty.
    """
    return [
        {
            "name": td.name,
            "category": td.category,
            "version": O.ONTOLOGY_VERSION,
            "rule_hash": td.rule_hash,
            "deprecated_alias_of": td.alias_of,
        }
        for td in sorted(REGISTRY.values(), key=lambda t: t.name)
    ]
