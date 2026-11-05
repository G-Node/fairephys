"""Plexon2 NWB converter.

Plexon .pl2 files require PL2FileReader.dll, accessed via Wine + zugbruecke
(wine on PATH, see README.md). The converter sets the DLL architecture on
instantiation.
"""

import os

from neuroconv.datainterfaces import Plexon2RecordingInterface

from .base import EphysConverter


def configure_plexon_env():
    """ZUGBRUECKE_ARCH=win64 for the 64-bit PL2FileReader.dll (unless set). Idempotent."""
    os.environ.setdefault("ZUGBRUECKE_ARCH", "win64")


class PlexonConverter(EphysConverter):
    """Convert Plexon2 (.pl2) recordings to NWB."""

    data_interface_classes = {"Recording": Plexon2RecordingInterface}

    def __init__(self, source_data: dict, probe_group_path=None):
        configure_plexon_env()
        super().__init__(source_data=source_data, probe_group_path=probe_group_path)
