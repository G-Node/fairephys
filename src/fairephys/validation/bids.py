"""BIDS validation using deno bids-validator with BEP032 schema."""

import json
import os
import re
import subprocess
from pathlib import Path

from .report import ValidationReport

SCHEMA_DIR = Path(__file__).parent.parent / "_resources" / "bids"
# The version a validator is pinned to ("jsr:@bids/validator@<version>")
_PINNED_VERSION_RE = re.compile(r"@(\d[^@/]*)$")
# A schema file named after its extension and source commit ("schema-bep032-<commit>.json")
_SCHEMA_FILE_RE = re.compile(r"schema-(.+)-([0-9a-f]{7,40})")
DENO_HINT = "deno not found: install it (https://docs.deno.com/runtime/) to run the BIDS validator"


def _spec() -> dict:
    from ..bids.builder import load_registry
    return load_registry().get("spec", {})


def bids_validator() -> str:
    """The validator validate_bids() runs: the registry spec's
    "jsr:@bids/validator@<version>"; one not pinned to a version is an error."""
    validator = _spec().get("validator", "")
    if not _PINNED_VERSION_RE.search(validator):
        raise ValueError(f"BIDS validator not pinned to a version in the registry spec: {validator!r}")
    return validator


def default_schema_path() -> Path:
    """Schema to validate against: $BIDS_SCHEMA, else the packaged file named in the registry."""
    if env := os.environ.get("BIDS_SCHEMA"):
        return Path(env)
    return SCHEMA_DIR / _spec().get("schema_file", "")


def validator_versions() -> dict:
    """Version of the validator validate_bids() runs and of the schema it uses.

    {"version": <validator pin>, "schema": "BEP032", "schema commit": <commit>}
    from the registry spec and the schema file (a file not named
    schema-<extension>-<commit>.json gives its file name as schema, no commit).
    """
    versions = {"version": _PINNED_VERSION_RE.search(bids_validator()).group(1)}
    schema_path = default_schema_path()
    if schema_path.is_file():
        if s := _SCHEMA_FILE_RE.fullmatch(schema_path.stem):
            versions.update({"schema": s.group(1).upper(), "schema commit": s.group(2)})
        else:
            versions["schema"] = schema_path.name
    return versions


def validate_bids(bids_dir: Path, report: ValidationReport, schema_path: Path | None = None) -> ValidationReport:
    """Run deno bids-validator and add issues to report."""
    if not bids_dir.exists():
        return report.add_error("BIDS", f"Directory not found: {bids_dir}") or report

    # The validator's bundled schema has no microelectrode datatypes, so a
    # missing schema must fail instead of silently validating against it.
    schema_path = Path(schema_path) if schema_path else default_schema_path()
    if not schema_path.is_file():
        return report.add_error("BIDS", f"BIDS schema not found: {schema_path}") or report

    cmd = ["deno", "run", "-ERWN", bids_validator(), str(bids_dir), "--json",
           "-s", f"file://{schema_path.resolve()}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        return report.add_error("BIDS", DENO_HINT) or report
    except subprocess.TimeoutExpired:
        return report.add_error("BIDS", "Validation timed out") or report

    if not result.stdout:
        return report.add_error("BIDS", f"Validator gave no output (exit code {result.returncode}): "
                                        f"{result.stderr[:500]}") or report
    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return report.add_error("BIDS", f"Validator output is not JSON ({exc}): "
                                        f"{(result.stderr or result.stdout)[:500]}") or report

    issues = output.get("issues", {}).get("issues", [])
    errors = [i for i in issues if i.get("severity") == "error"]
    warnings = [i for i in issues if i.get("severity") == "warning"]
    for issue in errors:
        report.add_error("BIDS", f"{issue.get('location', '')}: {issue.get('code', 'Unknown')}")
    for issue in warnings:
        report.add_warning("BIDS", f"{issue.get('location', '')}: {issue.get('code', 'Unknown')}")
    report.add_info("bids_issues", {"errors": len(errors), "warnings": len(warnings)})
    report.add_info("bids_validator_output", json.dumps(output, indent=2))
    return report
