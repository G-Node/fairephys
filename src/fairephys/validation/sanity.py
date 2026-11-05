"""Per-session sanity checks: the orchestration of the integrity checks (raw
versus NWB, `integrity`) and the cross-format geometry checks (probes,
electrodes and channels, `geometry`), and the report they fill.

A check that crashes adds an error naming itself to the report (see
report.guarded); a given file that is missing is an error too.
"""

import logging
from pathlib import Path

from .geometry import (
    check_bids_electrode_mapping, check_electrode_consistency,
    check_probe_geometry, check_probe_to_nwb_geometry,
)
from .integrity import check_channel_distinctness, check_data_integrity, check_recording_metadata
from .report import ValidationReport

logger = logging.getLogger(__name__)

__all__ = [
    "run_sanity_checks",
    "check_bids_electrode_mapping", "check_channel_distinctness", "check_data_integrity",
    "check_electrode_consistency", "check_probe_geometry", "check_probe_to_nwb_geometry",
    "check_recording_metadata",
]


# The session files the checks read from the paths dict (other keys are ignored)
SANITY_FILES = ("nwb", "channels_tsv", "electrodes_tsv", "probes_tsv", "probe_group")


def run_sanity_checks(
    raw_path: Path | None,
    paths: dict[str, Path],
    report_label: str,
    validation_dir: Path,
    recording_format: str = None,
    raw_recording=None,
    **raw_kwargs,
) -> ValidationReport:
    """Run per-session sanity checks and save markdown report.

    Args:
        raw_path: Path to raw recording file/folder (a Neo format), or None.
        paths: Session paths dict with keys: nwb, channels_tsv,
            electrodes_tsv, probes_tsv, probe_group. A path given but
            missing is an error; a key left out skips its checks.
        report_label: Identifier for the report (e.g. session ID or
            "session_run-01").
        validation_dir: Directory to save the report markdown.
        recording_format: a key of fairephys.formats.RAW_FORMATS, required
            with a raw path.
        raw_recording: the raw side as a SpikeInterface recording (a format
            without a Neo reader), instead of a raw path. Without either
            the raw checks are skipped.
        **raw_kwargs: Extra keyword arguments passed to
            check_channel_distinctness and check_data_integrity
            (e.g. segment_index, stream_id for TDT).

    Returns:
        The completed ValidationReport.
    """
    if raw_path is not None and raw_recording is not None:
        raise ValueError("give the raw side as a path or as a recording, not both")
    if raw_path is not None and not recording_format:
        raise ValueError("recording_format is required with a raw path")
    report = ValidationReport(report_label)
    have = _existing(report, {"raw": raw_path, **{key: paths.get(key) for key in SANITY_FILES}})
    raw, nwb = have.get("raw"), have.get("nwb")
    channels_tsv, electrodes_tsv = have.get("channels_tsv"), have.get("electrodes_tsv")
    probes_tsv, probe_group = have.get("probes_tsv"), have.get("probe_group")
    raw_kwargs.update(recording_format=recording_format, raw_recording=raw_recording)

    if raw or raw_recording is not None:
        check_channel_distinctness(raw, report, **raw_kwargs)
    if nwb:
        check_recording_metadata(nwb, report)
    if (raw or raw_recording is not None) and nwb:
        check_data_integrity(raw, nwb, report, **raw_kwargs)
    if channels_tsv and nwb:
        check_electrode_consistency(channels_tsv, nwb, report)
    if probe_group and nwb:
        check_probe_geometry(probe_group, nwb, report)
    if channels_tsv and electrodes_tsv and probes_tsv:
        check_bids_electrode_mapping(channels_tsv, electrodes_tsv, probes_tsv, report)
    if probe_group and electrodes_tsv and nwb:
        check_probe_to_nwb_geometry(probe_group, electrodes_tsv, nwb, report)

    validation_dir.mkdir(parents=True, exist_ok=True)
    report_path = validation_dir / f"{report_label}.md"
    report_path.write_text(report.to_markdown())
    logger.info("Sanity: %s (%d errors, %d warnings)", report_path, len(report.errors), len(report.warnings))
    return report


def _existing(report: ValidationReport, paths: dict) -> dict[str, Path]:
    """The given paths that exist; each one missing is an error of the report."""
    found = {}
    for key, path in paths.items():
        if path is None:
            continue
        if Path(path).exists():
            found[key] = Path(path)
        else:
            report.add_error("Sanity", f"{key} not found: {path}")
    return found
