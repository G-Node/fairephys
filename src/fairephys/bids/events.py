"""BIDS events sidecar generators (events.tsv, events.json)."""

import json
from pathlib import Path


def generate_events_tsv(events: list[dict], output_path: Path, columns: list[str] | None = None) -> Path:
    """Write events.tsv from event list with onset, duration, and optional
    columns (the header alone for no events). `columns`, when given, is the
    column order and the header of an empty file; it starts with onset and
    duration, and an event key outside it is an error."""
    required = ["onset", "duration"]
    if columns is None:
        extra_cols = {k for e in events for k in e if k not in required}
        columns = required + sorted(extra_cols)
    elif columns[:2] != required:
        raise ValueError(f"events.tsv columns {columns}: must start with {required}")
    elif unknown := {k for e in events for k in e} - set(columns):
        raise ValueError(f"events.tsv: event keys {sorted(unknown)} are not among the columns {columns}")

    lines = ["\t".join(columns)]
    lines.extend("\t".join(str(e.get(c, "n/a")) for c in columns) for e in events)

    output_path.write_text("\n".join(lines) + "\n")
    return output_path


def generate_events_json(column_descriptions: dict, output_path: Path) -> Path:
    """Write events.json sidecar with column descriptions."""
    output_path.write_text(json.dumps(column_descriptions, indent=2))
    return output_path
