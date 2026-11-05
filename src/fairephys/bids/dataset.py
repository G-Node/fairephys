"""BIDS dataset-level files (dataset_description.json, participants.tsv)."""

import json
from pathlib import Path

from .builder import get_columns, load_registry
from .ecephys import _write_tsv


def generate_dataset_description(metadata: dict, output_path: Path) -> Path:
    """Write dataset_description.json from extracted metadata dict."""
    output_path.write_text(json.dumps(metadata, indent=2))
    return output_path


def generate_participants_tsv(participants: list[dict], output_path: Path) -> Path:
    """Write participants.tsv using registry-defined columns."""
    return _write_tsv(participants, get_columns("participants_tsv"), output_path)


def generate_participants_json(output_path: Path) -> Path:
    """Write participants.json with column definitions from registry."""
    registry = load_registry()
    sidecar = {}

    # Add column definitions for additional columns
    for col, spec in registry.get("participants_tsv", {}).get("columns", {}).items():
        if desc := spec.get("sidecar_description"):
            sidecar[col] = {"Description": desc}

    output_path.write_text(json.dumps(sidecar, indent=2))
    return output_path
