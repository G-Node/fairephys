"""BIDS ecephys sidecar generators (channels, electrodes, probes).

Uses bids_mappings.yaml registry for column definitions.
"""

import csv
import json
from pathlib import Path

from .builder import get_columns


def generate_channels_tsv(channels: list[dict], output_path: Path) -> Path:
    """Write channels.tsv from channel metadata list."""
    columns = get_columns("channels_tsv")
    return _write_tsv(channels, columns, output_path)


def generate_electrodes_tsv(electrodes: list[dict], output_path: Path) -> Path:
    """Write electrodes.tsv from electrode metadata list."""
    columns = get_columns("electrodes_tsv")
    return _write_tsv(electrodes, columns, output_path)


def generate_electrodes_json(output_path: Path) -> Path:
    """Write electrodes.json sidecar with column definitions."""
    return _write_column_sidecar("electrodes_tsv", output_path)


def generate_probes_tsv(probes: list[dict], output_path: Path) -> Path:
    """Write probes.tsv from probe metadata list."""
    columns = get_columns("probes_tsv")
    return _write_tsv(probes, columns, output_path)


def generate_probes_json(probes: list[dict], output_path: Path, probes_dir: str = "probes") -> Path:
    """Write probes.json sidecar with ProbeInterface model references and column definitions.

    probes_dir: where the ProbeInterface files of these probes are, relative to
    the dataset root (a per-session group: "probes/sub-A_ses-01").
    """
    from .builder import load_registry

    # Build levels for probe models
    levels = {}
    for p in probes:
        probe_id = p.get("probe_name", "unknown")
        probe_type = p.get("type", "probe")
        manufacturer = p.get("manufacturer", "")
        description = f"{manufacturer} {probe_type}".strip() if manufacturer else probe_type

        levels[probe_id] = {
            "Description": description,
            "TermURL": f"bids::{probes_dir}/{probe_id}.json"
        }

    sidecar = {}
    if levels:
        sidecar["probe_name"] = {"Levels": levels}

    # Add column definitions from registry for additional columns
    registry = load_registry()
    for col, spec in registry.get("probes_tsv", {}).get("columns", {}).items():
        if (definition := _column_definition(spec)) and col not in sidecar:
            sidecar[col] = definition  # don't overwrite existing definitions

    output_path.write_text(json.dumps(sidecar, indent=2))
    return output_path


def generate_channels_json(output_path: Path) -> Path:
    """Write channels.json sidecar with column definitions."""
    return _write_column_sidecar("channels_tsv", output_path)


def generate_ecephys_json(metadata: dict, output_path: Path) -> Path:
    """Write ecephys.json recording sidecar."""
    output_path.write_text(json.dumps(metadata, indent=2))
    return output_path


def generate_coordsystem_json(metadata: dict, output_path: Path) -> Path:
    """Write coordsystem.json: the coordinate system of the probe positions in
    probes.tsv (fields from extract_sidecar_fields(doc, session, "coordsystem_json"));
    the file name carries the BIDS space entity."""
    output_path.write_text(json.dumps(metadata, indent=2))
    return output_path


def _write_column_sidecar(registry_key: str, output_path: Path) -> Path:
    """Write JSON sidecar with column descriptions from registry ({} when it
    describes none)."""
    from .builder import load_registry

    sidecar = {}
    for col, spec in load_registry().get(registry_key, {}).get("columns", {}).items():
        if definition := _column_definition(spec):
            sidecar[col] = definition

    output_path.write_text(json.dumps(sidecar, indent=2))
    return output_path


def _column_definition(spec: dict) -> dict:
    """Column entry for a TSV sidecar: Description and/or TermURL from the registry."""
    definition = {}
    if desc := spec.get("sidecar_description"):
        definition["Description"] = desc
    if url := spec.get("sidecar_term_url"):
        definition["TermURL"] = url
    return definition


def _write_tsv(rows: list[dict], columns: list[str], output_path: Path) -> Path:
    """Write TSV file with specified columns, using n/a for missing values."""
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, delimiter="\t",
                                extrasaction="ignore", restval="n/a")
        writer.writeheader()
        writer.writerows(rows)
    return output_path
