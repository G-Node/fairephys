"""DataCite record for the GIN DOI service, from odML.

This is the GIN exporter: datacite.yml follows GIN's DataCite template
(_resources/schemas/datacite.v1.yaml: its fields, reference types and
licence names), and the LICENSE file next to it names the same licence,
as GIN's DOI service checks. The licences a dataset can be shared under
are the registry licenses.yaml (SPDX id, name, URL).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from ..bids.structure import write_bidsignore
from ..metadata.identifiers import SECTION as PUBLICATIONS, parse_identifier
from ..registry import RESOURCES, load as load_registry

SCHEMA_PATH = RESOURCES / "schemas" / "datacite.v1.yaml"
# Official plain-text legal code of each licence of the registry (<SPDX id>.txt,
# downloaded from creativecommons.org/<licence path>/legalcode.txt)
LICENSES_DIR = RESOURCES / "licenses"

logger = logging.getLogger(__name__)
# GIN DataCite template: the id prefix of each identifier type it takes (a
# Handle as its URL); an ISBN has none
GIN_ID_PREFIXES = {"DOI": "doi:", "arXiv": "arxiv:", "PMID": "pmid:", "URL": "url:", "Handle": "url:"}

# SPDX id -> {name, url} (the dataset template's licence ids)
LICENSES: dict[str, dict] = load_registry("licenses.yaml")


def _load_schema() -> dict:
    """Load the DataCite output schema."""
    with open(SCHEMA_PATH) as f:
        return yaml.safe_load(f)


def license_entry(license_id: str) -> dict:
    """{name, url} of a licence id of the registry; an unknown id is an error."""
    if license_id not in LICENSES:
        raise ValueError(f"Unknown licence '{license_id}'; the licences registry (licenses.yaml) "
                         f"has: {', '.join(LICENSES)}")
    return LICENSES[license_id]


def _prop(sec: dict, name: str) -> Any:
    """Get property value from odML section."""
    return next((p.get("value", [None])[0] if len(p.get("value", [])) == 1 else p.get("value", [])
                 for p in sec.get("properties", []) if p.get("name") == name), None)


def _sec(parent: dict, name: str) -> dict | None:
    """Get subsection by name."""
    return next((s for s in parent.get("sections", []) if s.get("name") == name), None)


def _author(c: dict) -> dict:
    """Extract author from Contributor section."""
    a = {"firstname": _prop(c, "given name") or "", "lastname": _prop(c, "family name") or ""}
    # the README affiliation without the country
    if aff := _sec(c, "affiliation"):
        if parts := [v for f in ["lab", "department", "institution"] if (v := _prop(aff, f))]:
            a["affiliation"] = ", ".join(parts)
    if (d := _sec(c, "digital identifier")) and _prop(d, "type") == "ORCID" and (oid := _prop(d, "value")):
        a["id"] = f"ORCID:{oid}"
    return a


def _funding(f: dict) -> str | None:
    """Extract funding entry."""
    funder, award = _prop(f, "funder"), _prop(f, "award number")
    return f"{funder}; {award}" if funder and award else funder


def _reference(entry: dict) -> dict | None:
    """GIN reference of a Related Publications entry: the id with GIN's
    prefix of its type, the relation as reftype, the citation; an ISBN,
    which GIN's file cannot hold, raises; an entry without identifier is
    left out with a warning (None)."""
    given = _prop(entry, "identifier")
    if not given:
        logger.warning("Related publication without identifier, not in datacite.yml: %s", _prop(entry, "citation"))
        return None
    identifier = parse_identifier(given)
    if identifier.type not in GIN_ID_PREFIXES:
        raise ValueError(f"Related publication '{given}': GIN's datacite.yml takes no {identifier.type} "
                         f"(its id prefixes: {', '.join(sorted(set(GIN_ID_PREFIXES.values())))})")
    value = identifier.link if identifier.type == "Handle" else identifier.value
    return {"id": f"{GIN_ID_PREFIXES[identifier.type]}{value}", "reftype": _prop(entry, "relation"),
            "citation": str(_prop(entry, "citation") or "").strip()}


def _dataset_section(odml_path: Path) -> dict:
    """Dataset section of an odML YAML file."""
    with open(odml_path) as f:
        doc = yaml.safe_load(f)
    ds = next((s for s in doc.get("Document", {}).get("sections", []) if s.get("name") == "Dataset"), None)
    if not ds:
        raise ValueError("Dataset section not found")
    return ds


def odml_to_datacite(odml_path: Path) -> dict:
    """Convert odML to DataCite format."""
    ds = _dataset_section(odml_path)

    # Build in schema property order: authors, title, description, keywords,
    # license, funding, references, resourcetype, templateversion
    dc = {}
    if c := _sec(ds, "Contributors"):
        dc["authors"] = [_author(s) for s in c.get("sections", []) if s.get("name", "").startswith("Contributors_")]
    if v := _prop(ds, "full name"): dc["title"] = v
    if v := _prop(ds, "description"): dc["description"] = v.strip() if isinstance(v, str) else v
    if v := _prop(ds, "keywords"): dc["keywords"] = v if isinstance(v, list) else [v]
    if v := _prop(ds, "license"): dc["license"] = dict(license_entry(str(v)))
    if f := _sec(ds, "Fundings"):
        if items := [x for s in f.get("sections", []) if s.get("name", "").startswith("Fundings_") and (x := _funding(s))]:
            dc["funding"] = items
    if p := _sec(ds, PUBLICATIONS):
        if refs := [r for s in p.get("sections", [])
                    if s.get("name", "").startswith(f"{PUBLICATIONS}_") and (r := _reference(s))]:
            dc["references"] = refs
    schema = _load_schema()
    dc["resourcetype"] = "Dataset"
    dc["templateversion"] = schema["properties"]["templateversion"]["const"]
    return dc


def validate(data: dict) -> list[str]:
    """Validate against schema. Returns errors."""
    schema = _load_schema()
    return [f"{'.'.join(map(str, e.path))}: {e.message}" for e in jsonschema.Draft7Validator(schema).iter_errors(data)]


def write_datacite(odml_path: Path, output_dir: Path, validate_output: bool = True) -> Path:
    """Generate datacite.yml from odML."""
    dc = odml_to_datacite(odml_path)
    if validate_output and (errs := validate(dc)):
        raise ValueError("DataCite validation failed:\n" + "\n".join(f"  - {e}" for e in errs))

    (out := output_dir / "datacite.yml").write_text(yaml.dump(dc, default_flow_style=False, allow_unicode=True, sort_keys=False))
    # LICENSE next to datacite.yml: GIN's DOI service checks that both name the same licence
    if license_id := _prop(_dataset_section(odml_path), "license"):
        (license_file := output_dir / "LICENSE").unlink(missing_ok=True)  # may be an annexed release
        license_file.write_text(license_text(str(license_id)), encoding="utf-8")
    else:
        logger.warning("No licence in the odML, no LICENSE file written")
    write_bidsignore(output_dir)
    return out


def license_text(license_id: str) -> str:
    """LICENSE content: licence name and URL, then the official plain-text legal code.

    The first line is the datacite.yml licence name (GIN's DOI service matches
    it). A registry id without a stored legal code gives name and URL only.
    """
    lic = license_entry(license_id)
    head = f"{lic['name']}\n{lic['url']}\n"
    legal_code = LICENSES_DIR / f"{license_id}.txt"
    if not legal_code.is_file():
        logger.warning("No legal code for licence '%s': LICENSE holds its name and URL only", license_id)
        return head
    return f"{head}\n{legal_code.read_text(encoding='utf-8')}"
