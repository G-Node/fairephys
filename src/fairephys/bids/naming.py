"""BIDS filename generation."""

import re
from pathlib import Path


def bids_filename(sub: str, suffix: str, ext: str, *, ses: str = None, task: str = None,
                  acq: str = None, run: int = None, recording: str = None,
                  tracksys: str = None, space: str = None) -> str:
    """Generate BIDS filename. Entities appear in standard order."""
    if task and not re.fullmatch(r'[a-zA-Z0-9]+', task):
        raise ValueError(
            f"Invalid BIDS task label '{task}': must be alphanumeric only (^[a-zA-Z0-9]+$)"
        )
    parts = [f"sub-{sub}"]
    if ses:
        parts.append(f"ses-{ses}")
    if task:
        parts.append(f"task-{task}")
    if acq:
        parts.append(f"acq-{acq}")
    if run is not None:
        parts.append(f"run-{run:02d}")
    if recording:
        parts.append(f"recording-{recording}")
    if tracksys:
        parts.append(f"tracksys-{tracksys}")
    if space:
        parts.append(f"space-{space}")
    parts.append(suffix)
    return "_".join(parts) + (ext if ext.startswith(".") else f".{ext}")


def session_path(bids_root: Path, sub: str, ses: str = None, datatype: str = None) -> Path:
    """Get path to session directory or datatype subdirectory."""
    path = Path(bids_root) / f"sub-{sub}"
    if ses:
        path = path / f"ses-{ses}"
    if datatype:
        path = path / datatype
    return path
