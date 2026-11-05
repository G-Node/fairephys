"""BIDS physio sidecar and data generator (recording-*_physio.json / .tsv.gz)."""

import gzip
import io
import json
from pathlib import Path

import numpy as np


def generate_physio_json(
    columns: list[str],
    sampling_freq: float,
    start_time: float = 0.0,
    units: dict[str, str] = None,
    physio_type: str = None,
    task_name: str = None,
    extra_fields: dict = None,
    output_path: Path = None,
) -> dict | Path:
    """Write physio.json sidecar with column names and sampling info (the
    sampling frequency as given, e.g. 24414.0625)."""
    sidecar = {
        "Columns": columns,
        "SamplingFrequency": sampling_freq,
        "StartTime": start_time,
    }

    if task_name:
        sidecar["TaskName"] = task_name
    if physio_type:
        sidecar["PhysioType"] = physio_type
    if extra_fields:
        sidecar.update(extra_fields)

    for col in columns:
        sidecar[col] = {"Units": (units or {}).get(col, "n/a")}

    if output_path:
        output_path.write_text(json.dumps(sidecar, indent=2))
        return output_path
    return sidecar


def write_physio_data(
    data: dict[str, np.ndarray],
    output_path: Path,
) -> Path:
    """Write physio.tsv.gz: columns of continuous physiological data.

    BIDS compressed tabular files must NOT contain a header row.
    Column names are defined in the companion JSON sidecar "Columns" field.

    Args:
        data: Mapping of column name to 1-D array of values.
        output_path: Must end with .tsv.gz.
    """
    columns = list(data.keys())
    buf = io.StringIO()
    matrix = np.column_stack([data[c] for c in columns])
    for row in matrix:
        buf.write("\t".join(f"{v:.10g}" for v in row) + "\n")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)  # a released file is a read-only annex link
    # Use GzipFile with mtime=0 and empty filename to avoid BIDS gzip header warnings
    payload = buf.getvalue().encode("utf-8")
    with open(output_path, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as gz:
            gz.write(payload)
    return output_path
