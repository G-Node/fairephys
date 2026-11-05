"""NWB validation using nwbinspector."""

from collections import Counter
from dataclasses import replace
from pathlib import Path

from .report import ValidationReport


def validate_nwb(nwb_path: Path, report: ValidationReport) -> ValidationReport:
    """Inspect an NWB file with nwbinspector and add its findings to the report,
    under the layer "NWB" with the file named (a session may hold several
    files): CRITICAL findings are errors, BEST_PRACTICE_VIOLATION warnings;
    every finding is listed in the report's inspector section, by file name
    (no local path in a report that is published with the dataset)."""
    from nwbinspector import format_messages, inspect_nwbfile

    name = nwb_path.name
    if not nwb_path.exists():
        return report.add_error("NWB", f"File not found: {nwb_path}") or report

    messages = [replace(m, file_path=name) for m in inspect_nwbfile(nwbfile_path=str(nwb_path)) if m is not None]
    report.add_info(f"nwb_inspector_{nwb_path.stem}",
                    "\n".join(format_messages(messages, levels=["importance"])) if messages else "No issues")
    counts = Counter(m.importance.name for m in messages)
    if n_critical := counts["CRITICAL"]:
        report.add_error("NWB", f"{name}: {n_critical} critical issues")
    if n_violations := counts["BEST_PRACTICE_VIOLATION"]:
        report.add_warning("NWB", f"{name}: {n_violations} best practice violations")
    return report
