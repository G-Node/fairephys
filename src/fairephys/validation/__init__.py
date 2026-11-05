"""Validation framework for metadata, BIDS and NWB datasets."""

import json
from pathlib import Path

from .bids import validate_bids
from .nwb import validate_nwb
from .report import ValidationReport
from .schema import validate_template, validate_templates, validate_document

__all__ = [
    "ValidationReport",
    "run_validation",
    "validate_bids",
    "validate_nwb",
    "validate_template",
    "validate_templates",
    "validate_document",
]


def run_validation(session_id: str, bids_dir: Path,
                   validation_dir: Path = None,
                   scope: str = "all") -> ValidationReport:
    """Run dataset validation at the given scope.

    scope "all" runs everything; "metadata" runs the BIDS validator and
    surfaces persisted openMINDS warnings (fast, no data files opened);
    "nwb" runs only the per-file NWB inspection (slow).

    validation_dir is where pipeline steps persist openMINDS QA artifacts
    (enrichment warnings from compile, collection failures from
    openminds-collection); when given, their contents are surfaced in
    the report. No openMINDS checks are re-run here.
    """
    report = ValidationReport(session_id)
    if not bids_dir or not Path(bids_dir).exists():
        report.add_error("BIDS", f"Directory not found: {bids_dir}")
        return report

    if scope in ("all", "metadata"):
        validate_bids(bids_dir, report)
        if validation_dir is not None:
            _surface_openminds_warnings(validation_dir, report)

    if scope in ("all", "nwb"):
        for nwb in sorted(bids_dir.glob("sub-*/ses-*/ecephys/*_ecephys.nwb")):
            validate_nwb(nwb, report)

    return report


def _load_persisted_json(path: Path, report: ValidationReport):
    """Load a persisted warnings JSON file, reporting read failures."""
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        report.add_warning("openMINDS", f"Could not read {path}")
        return None


def _surface_openminds_warnings(validation_dir: Path,
                                report: ValidationReport) -> None:
    """Surface enrichment warnings persisted by finalize_compilation."""
    warnings_path = validation_dir / "_openminds_warnings.json"
    if not warnings_path.exists():
        return
    warnings = _load_persisted_json(warnings_path, report)
    for w in warnings or []:
        report.add_warning("openMINDS", w)
