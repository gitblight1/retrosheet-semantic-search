"""Query API (spec/06-QUERY.md)."""

import re
import sqlite3

from .results import (CoverageReport, ExcludedCounts, ForceReport, PlayResult,
                      ResultSet)
from .search import QueryError, Search

__all__ = ["Search", "QueryError", "ResultSet", "PlayResult",
           "CoverageReport", "ExcludedCounts", "ForceReport", "connect"]


def connect(path: str, read_only: bool = True) -> sqlite3.Connection:
    """Open a query database with the functions the API's SQL needs.

    SQLite has no built-in `REGEXP`, so `.event_matches()` -- the documented
    escape hatch for anything the ontology does not yet name -- fails against a
    bare connection. Registering it here means every path into the API gets it,
    rather than the predicate working from the CLI and not from a script.
    """
    uri = f"file:{path}?mode=ro" if read_only else path
    conn = sqlite3.connect(uri, uri=read_only)
    conn.create_function(
        "regexp", 2,
        lambda pattern, value: (0 if value is None
                                else 1 if re.search(pattern, value) else 0),
        deterministic=True)
    return conn
