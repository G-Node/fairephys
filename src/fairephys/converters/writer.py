"""Shared NWB writing for the converters and the dataset-specific writers."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from neuroconv.tools.nwb_helpers import configure_and_write_nwbfile
from probeinterface import ProbeGroup, read_probeinterface
from pynwb import NWBFile


def write_nwbfile(nwbfile: NWBFile, nwbfile_path: str | Path) -> Path:
    """Write with neuroconv's default HDF5 chunking and compression."""
    path = Path(nwbfile_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    configure_and_write_nwbfile(nwbfile, nwbfile_path=path, backend="hdf5")
    return path


def channel_positions(probe_group: str | Path | ProbeGroup, n_channels: int) -> np.ndarray:
    """Contact (x, y) in µm per recording channel, local to the channel's
    probe: the offset the group builder added to space the probes apart
    (annotation x_offset, 0 when absent) is removed, as in BIDS
    electrodes.tsv.

    Channels are matched through the probes' device_channel_indices; channels
    without a contact are NaN.
    """
    if not isinstance(probe_group, ProbeGroup):
        probe_group = read_probeinterface(str(probe_group))
    positions = np.full((n_channels, 2), np.nan)
    for probe in probe_group.probes:
        if probe.device_channel_indices is None:
            continue
        x_offset = float(probe.annotations.get("x_offset", 0))
        for (x, y), ch in zip(probe.contact_positions[:, :2], probe.device_channel_indices):
            if 0 <= ch < n_channels:
                positions[ch] = (x - x_offset, y)
    return positions
