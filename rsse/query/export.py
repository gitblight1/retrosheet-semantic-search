"""Result serialisation shared by `rsse query` and the web UI.

One writer for both, so a CSV exported from the browser and one produced by
`rsse query --format csv` are the same file (spec/09-WEB.md §4). Every export
carries the attribution notice and the corpus version (spec/06-QUERY.md §9).
"""

from __future__ import annotations

import csv
import dataclasses

CSV_COLUMNS = ["game_id", "date", "inning", "half", "batter_id",
               "outs_before", "bases_before", "event_raw", "tags"]


def result_dict(result, attribution: str) -> dict:
    """The `--format json` document."""
    return {
        "rows": [dataclasses.asdict(r) for r in result.rows],
        "total": result.total,
        "coverage": dataclasses.asdict(result.coverage),
        "excluded": dataclasses.asdict(result.excluded),
        "force": dataclasses.asdict(result.force) if result.force else None,
        "corpus_version": result.corpus_version,
        "ontology_version": result.ontology_version,
        "sql": result.sql,
        "attribution": attribution,
    }


def write_csv(result, fp, attribution: str) -> None:
    writer = csv.writer(fp)
    writer.writerow(CSV_COLUMNS)
    for r in result.rows:
        writer.writerow([r.game_id, r.date, r.inning, r.half, r.batter_id,
                         r.outs_before, r.bases_before, r.event_raw,
                         " ".join(r.tags)])
    fp.write(f"# {attribution}\n")
