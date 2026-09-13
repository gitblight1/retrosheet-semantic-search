"""Resolving names to ids against the reference tables (spec/06-QUERY.md §8).

A researcher types "Babe Ruth", not `ruthb101`. Turning one into the other is
less obvious than it looks, for two reasons that were measured rather than
assumed.

**The name people use is not the name `people.first` holds.** `biofile.csv`
records the legal name -- Ruth is `George Herman` -- and the playing name
lives in `nickname` and in `roster_entries.first`. **19,598 of 21,993 people
(89%) differ between the two.** A lookup against `people.first || last` alone
would miss almost everyone worth searching for.

**Names are not unique, and the collisions are the interesting ones.** 207
names are shared by 442 people, 30 of those names among players who actually
appear in the corpus, and 26 park names are shared -- including *Wrigley
Field*, which is both Chicago's and the Los Angeles park the Negro Leagues
and the 1961 Angels used. So an ambiguous name raises with the candidates
listed rather than picking one. Guessing here would answer a different
question than the one asked and say nothing about it.
"""

from __future__ import annotations

from dataclasses import dataclass


class AmbiguousName(ValueError):
    """A name matches more than one id, or none."""


@dataclass(frozen=True)
class Candidate:
    """One resolution of a name, with enough context to tell them apart."""

    id: str
    label: str

    def __str__(self) -> str:
        return f"{self.id} ({self.label})"


#: Matched case-insensitively against four spellings: the playing name, the
#: legal name, the surname alone, and whatever the rosters called them. The
#: roster arm is what makes `Jack Robinson` find `robij101`, whose biography
#: says `John Edward`.
_PEOPLE_SQL = """
SELECT p.person_id,
       coalesce(p.nickname, p.first, '') || ' ' || coalesce(p.last, '')
         || coalesce(' b.' || substr(p.birthdate, -4), '')
         || coalesce(' debut ' || substr(p.play_debut, -4), '')
  FROM people p
 WHERE lower(trim(coalesce(p.nickname, '') || ' ' || coalesce(p.last, ''))) = :n
    OR lower(trim(coalesce(p.first, '') || ' ' || coalesce(p.last, ''))) = :n
    OR lower(trim(coalesce(p.last, ''))) = :n
    OR p.person_id IN (
         SELECT r.person_id FROM roster_entries r
          WHERE lower(trim(coalesce(r.first, '') || ' '
                           || coalesce(r.last, ''))) = :n)
 ORDER BY p.person_id
"""

_PARKS_SQL = """
SELECT park_id,
       coalesce(name, park_id) || coalesce(', ' || city, '')
         || coalesce(' ' || substr(start_iso, 1, 4), '')
         || coalesce('-' || substr(end_iso, 1, 4), '')
  FROM parks
 WHERE lower(coalesce(name, '')) = :n OR lower(coalesce(aka, '')) = :n
 ORDER BY park_id
"""

#: Teams are keyed per season, so a name can resolve to one id across many
#: seasons. Distinct on the id: "the Dodgers" is one team however many years
#: it spans.
_TEAMS_SQL = """
SELECT team_id,
       min(coalesce(city, '') || ' ' || coalesce(nickname, ''))
         || ' ' || cast(min(season) AS TEXT)
         || '-' || cast(max(season) AS TEXT)
  FROM teams
 WHERE lower(trim(coalesce(city, '') || ' ' || coalesce(nickname, ''))) = :n
    OR lower(coalesce(nickname, '')) = :n
    OR lower(coalesce(city, '')) = :n
 GROUP BY team_id ORDER BY team_id
"""


def _lookup(conn, sql: str, name: str) -> list[Candidate]:
    key = " ".join(name.split()).lower()
    return [Candidate(row[0], row[1].strip())
            for row in conn.execute(sql, {"n": key})]


def people_named(conn, name: str) -> list[Candidate]:
    return _lookup(conn, _PEOPLE_SQL, name)


def parks_named(conn, name: str) -> list[Candidate]:
    return _lookup(conn, _PARKS_SQL, name)


def teams_named(conn, name: str) -> list[Candidate]:
    return _lookup(conn, _TEAMS_SQL, name)


def resolve(candidates: list[Candidate], what: str, name: str) -> str:
    """Exactly one candidate, or an error that names the alternatives.

    The error carries the candidates because the caller cannot act on
    "ambiguous" alone -- they need the ids to choose between, and the
    disambiguating detail (birth year, park city and dates) is there so the
    choice can be made without a second query.
    """
    if not candidates:
        raise AmbiguousName(
            f"no {what} named {name!r}. Reference tables are built by "
            f"`rsse reference`; without them this cannot resolve anything.")
    if len(candidates) > 1:
        listed = "\n  ".join(str(c) for c in candidates)
        raise AmbiguousName(
            f"{len(candidates)} {what}s named {name!r}:\n  {listed}\n"
            f"Pass the id instead.")
    return candidates[0].id


def names_for(conn, person_ids) -> dict:
    """`person_id -> display name` for a batch of ids.

    One query, not one per row: a result set of 500 plays would otherwise be
    500 round trips for what a single `IN` answers
    ([07-TESTING](../../spec/07-TESTING.md) §5.1).
    """
    ids = [i for i in dict.fromkeys(person_ids) if i]
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT person_id, coalesce(nickname, first), last FROM people"
        " WHERE person_id IN (%s)" % ",".join("?" * len(ids)), ids)
    out = {}
    for person_id, first, last in rows:
        label = " ".join(p for p in (first, last) if p)
        # A person the corpus names and no file describes has a row with null
        # attributes (spec/05-DATABASE.md §8.2). The id is the only name there
        # is, and it is better than an empty string.
        out[person_id] = label or person_id
    return out
