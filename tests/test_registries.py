"""The registries against the template schemas and their own conventions."""

import json
from functools import lru_cache

import yaml

from fairephys.bids.builder import load_registry as load_bids_registry
from fairephys.bids.ecephys import generate_electrodes_json
from fairephys.converters.metadata import load_nwb_registry
from fairephys.export.datacite import LICENSES, LICENSES_DIR
from fairephys.registry import RESOURCES, load

CONFLICT_TYPES = {"unit", "type", "vocabulary", "structural", "cardinality", "coverage"}

# Where the template schemas list the properties the registry paths read (Recording Defaults and
# Recordings entries are merged into every session)
SCHEMA_PROPERTIES = {
    "Electrode Placements/*": [
        ("recordings.v1", "Recordings/additionalProperties/properties/Electrode Placements/items/properties"),
        ("recordings.v1", "Recording Defaults/properties/Electrode Placements/items/properties")],
    "Hardware/Probes/*": [("hardware.v1", "Hardware/properties/Probes/items/properties")],
    "Hardware/Recording System": [("hardware.v1", "Hardware/properties/Recording System/properties")],
    "Auxiliary Device": [("hardware.v1", "Hardware/properties/Auxiliary Devices/items/properties")],
    "Sessions/*": [("sessions.v1", "Sessions/items/properties"), ("recordings.v1", "Recording Defaults/properties"),
                   ("recordings.v1", "Recordings/additionalProperties/properties")],
    "Dataset": [("dataset.v1", "Dataset/properties")],
    "Behavioral Protocol": [("protocol.v1", "Behavioral Protocol/properties")],
}


@lru_cache(maxsize=None)
def _schema_properties(schema: str, location: str) -> set[str]:
    node = yaml.safe_load((RESOURCES / "schemas" / f"{schema}.yaml").read_text())["properties"]
    for part in location.split("/"):
        node = node[part]
    return set(node)


def test_registry_paths_name_fields_of_the_schemas():
    """Every odML field a BIDS registry path reads is a field of a template schema."""
    paths = []
    for section in load_bids_registry().values():
        if isinstance(section, dict):
            for spec in {**section.get("columns", {}), **section.get("fields", {})}.values():
                path = spec.get("path")
                paths += path if isinstance(path, list) else [path] if path else []
    missing = []
    for path in paths:
        if "." not in path:  # a section handed to an extractor, not a property
            continue
        section, prop = path.rsplit(".", 1)
        locations = SCHEMA_PROPERTIES.get(section.removeprefix("Sessions/*/") if "Electrode Placements" in section
                                          else section)
        if locations and not any(prop in _schema_properties(s, loc) for s, loc in locations):
            missing.append(path)
    assert missing == []


def test_every_mapping_declares_its_conflict_types():
    """NWB fields, BIDS columns and fields and openMINDS schemas each name their conflict types."""
    nwb, bids = load_nwb_registry(), load_bids_registry()
    specs = {f"nwb:{s}.{f}": spec for s in ("NWBFile", "Subject") for f, spec in nwb[s].items()
             if not f.startswith("_")}
    for key in ("probes_tsv", "electrodes_tsv", "channels_tsv", "participants_tsv"):
        specs |= {f"{key}.{c}": spec for c, spec in bids[key]["columns"].items()}
    for key in ("dataset_description_json", "ecephys_json", "coordsystem_json", "events_json", "motion_json",
                "emg_json", "eyetrack_json"):
        specs |= {f"{key}.{f}": spec for f, spec in bids[key].get("fields", {}).items()}
    specs |= {f"nwb_electrode_columns.{c}": spec for c, spec in bids["nwb_electrode_columns"].items()}
    specs |= {f"openminds:{n}": spec for n, spec in load("openminds_schemas.yaml")["schemas"].items()}
    assert [k for k, spec in specs.items() if not set(spec.get("conflict_types", [None])) <= CONFLICT_TYPES] == []


def test_bids_and_openminds_conventions(tmp_path):
    """BEP032 anatomical_location columns with their ontology; organizations looked up in common only."""
    bids = load_bids_registry()
    for tsv in ("probes_tsv", "electrodes_tsv"):
        assert "anatomical_location" in bids[tsv]["columns"]
        assert not {"location", "associated_brain_region"} & set(bids[tsv]["columns"])
    sidecar = json.loads(generate_electrodes_json(tmp_path / "electrodes.json").read_text())
    assert sidecar["anatomical_location"]["TermURL"].startswith("http")

    lookups = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("fairgraph_class") == "core.Organization" and "kg_lookup" in node:
                lookups.append(node["kg_lookup"].get("search_space"))
            for value in node.values():
                walk(value)
    walk(load("openminds_schemas.yaml"))
    assert lookups and set(lookups) == {"common"}


def test_licences_registry_matches_the_dataset_schema():
    """Every licence the schema allows has name, https URL and legal code; one shared registry."""
    schema = yaml.safe_load((RESOURCES / "schemas" / "dataset.v1.yaml").read_text())
    allowed = schema["properties"]["Dataset"]["properties"]["license"]["enum"]
    assert sorted(allowed) == sorted(LICENSES) and load("licenses.yaml") is LICENSES
    assert all(set(v) == {"name", "url"} and v["url"].startswith("https://") for v in LICENSES.values())
    assert all((LICENSES_DIR / f"{spdx}.txt").is_file() for spdx in allowed)
