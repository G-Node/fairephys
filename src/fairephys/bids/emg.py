"""BIDS EMG sidecar and data generators (emg.json, channels.tsv, emg.edf)."""

import json
from datetime import datetime
from pathlib import Path

import numpy as np


def generate_emg_json(
    sampling_freq: float,
    task_name: str,
    channel_count: int,
    power_line_freq: int,
    extra_fields: dict = None,
    output_path: Path = None,
) -> dict | Path:
    """Write emg.json sidecar with EMG-specific metadata (the sampling
    frequency as given; RecordingType and the other registry fields from
    `extra_fields`)."""
    sidecar = {
        "TaskName": task_name,
        "SamplingFrequency": sampling_freq,
        "PowerLineFrequency": power_line_freq,
        "EMGChannelCount": channel_count,
        "SoftwareFilters": "n/a",
    }
    if extra_fields:
        sidecar.update(extra_fields)

    if output_path:
        output_path.write_text(json.dumps(sidecar, indent=2))
        return output_path
    return sidecar


def generate_emg_channels_tsv(channels: list[dict], output_path: Path) -> Path:
    """Write EMG channels.tsv with type, units, and status columns."""
    columns = ["name", "type", "units", "sampling_frequency", "status"]
    lines = ["\t".join(columns)]
    lines.extend("\t".join(str(ch.get(c, "n/a")) for c in columns) for ch in channels)
    output_path.write_text("\n".join(lines) + "\n")
    return output_path


def write_emg_edf(
    channel_data: dict[str, np.ndarray],
    sampling_freq: float,
    output_path: Path,
    units: dict[str, str],
    start: datetime | None = None,
) -> Path:
    """Write EMG data in EDF+ format (BIDS-required for EMG).

    Args:
        channel_data: Mapping of channel name to 1-D array (physical units).
        sampling_freq: Sampling frequency in Hz.
        output_path: Must end with .edf.
        units: Physical dimension label per channel name.
        start: Time of the first sample, written as EDF's clock time (no
            time zone); None leaves pyedflib's default, the time of writing.
    """
    import pyedflib

    channels = list(channel_data.keys())
    n_channels = len(channels)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)  # a released file is a read-only annex link

    f = pyedflib.EdfWriter(str(output_path), n_channels, file_type=pyedflib.FILETYPE_EDFPLUS)
    try:
        if start is not None:
            f.setStartdatetime(start.replace(tzinfo=None))
        for i, name in enumerate(channels):
            signal = channel_data[name]
            pmin, pmax = float(signal.min()), float(signal.max())
            # Avoid zero-range (EDF requires physical_min < physical_max)
            if pmin == pmax:
                pmin, pmax = pmin - 1.0, pmax + 1.0
            f.setSignalHeader(i, {
                "label": name,
                "dimension": units[name],
                "sample_frequency": sampling_freq,
                "physical_min": pmin,
                "physical_max": pmax,
                "digital_min": -32768,
                "digital_max": 32767,
                "transducer": "",
                "prefilter": "",
            })
        f.writeSamples([channel_data[ch].astype(np.float64) for ch in channels])
    finally:
        f.close()
    return output_path
