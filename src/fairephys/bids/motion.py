"""BIDS motion sidecar and data generators (motion.json, channels.tsv, motion.tsv)."""

import json
from pathlib import Path

import numpy as np


def generate_motion_json(
    sampling_freq: float,
    task_name: str,
    channels: list[dict],
    tracking_system: str = None,
    extra_fields: dict = None,
    output_path: Path = None,
) -> dict | Path:
    """Write motion.json sidecar with tracking system info (the sampling
    frequency as given)."""
    # Count channel types from the channel list
    type_counts = {}
    for c in channels:
        t = c.get("type", "")
        type_counts[t] = type_counts.get(t, 0) + 1

    sidecar = {
        "SamplingFrequency": sampling_freq,
        "TaskName": task_name,
        "MotionChannelCount": len(channels),
        "POSChannelCount": type_counts.get("POS", 0),
        "ORNTChannelCount": type_counts.get("ORNT", 0),
        "ACCELChannelCount": type_counts.get("ACCEL", 0),
        "ANGACCELChannelCount": type_counts.get("ANGACCEL", 0),
        "GYROChannelCount": type_counts.get("GYRO", 0),
        "JNTANGChannelCount": type_counts.get("JNTANG", 0),
        "LATENCYChannelCount": type_counts.get("LATENCY", 0),
        "MAGNChannelCount": type_counts.get("MAGN", 0),
        # MISCChannelCount is a deprecated alias in the BIDS schema
        "MiscChannelCount": type_counts.get("MISC", 0),
        "VELChannelCount": type_counts.get("VEL", 0),
        "TrackedPointsCount": len({c.get("tracked_point") for c in channels}),
    }

    if tracking_system:
        sidecar["TrackingSystemName"] = tracking_system

    if extra_fields:
        sidecar.update(extra_fields)

    if output_path:
        output_path.write_text(json.dumps(sidecar, indent=2))
        return output_path
    return sidecar


def generate_motion_channels_tsv(channels: list[dict], output_path: Path) -> Path:
    """Write motion channels.tsv with component and tracked_point columns."""
    columns = ["name", "component", "type", "tracked_point", "units"]
    lines = ["\t".join(columns)]
    lines.extend("\t".join(str(ch.get(c, "n/a")) for c in columns) for ch in channels)
    output_path.write_text("\n".join(lines) + "\n")
    return output_path


def write_motion_data(
    data: dict[str, np.ndarray],
    output_path: Path,
) -> Path:
    """Write motion.tsv: columns of motion tracking data.

    Args:
        data: Mapping of column name to 1-D array of values.
        output_path: Must end with .tsv.
    """
    columns = list(data.keys())
    matrix = np.column_stack([data[c] for c in columns])
    lines = ["\t".join(columns)]
    for row in matrix:
        lines.append("\t".join(f"{v:.10g}" for v in row))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n")
    return output_path
