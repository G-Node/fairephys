"""NWB conversion using neuroconv.

Provides converters for different recording systems and metadata builders
for extracting NWB-compatible metadata from odML documents.
"""

from .metadata import build_nwb_metadata, recording_scale
from .base import EphysConverter, RecordingConverter
from .devices import add_devices
from .extras import add_time_intervals, add_timeseries, add_trials, add_units, recording_span
from .writer import channel_positions, write_nwbfile
from .blackrock import BlackrockConverter
from .plexon import PlexonConverter
from .spikeglx import SpikeGLXConverter, nidq_times_on_imec_clock, read_nidq_channels
from .tdt import TdtConverter
from .tdt_sev import TdtSevConverter

__all__ = [
    "build_nwb_metadata",
    "recording_scale",
    "EphysConverter",
    "RecordingConverter",
    "add_devices",
    "add_time_intervals",
    "add_timeseries",
    "add_trials",
    "add_units",
    "recording_span",
    "channel_positions",
    "write_nwbfile",
    "BlackrockConverter",
    "PlexonConverter",
    "SpikeGLXConverter",
    "read_nidq_channels",
    "nidq_times_on_imec_clock",
    "TdtConverter",
    "TdtSevConverter",
]
