"""TDT NWB converter.

TDT tanks contain multiple blocks (segments). Use block_index to select
a specific block for conversion (0-based: Block-1 = 0, Block-2 = 1, etc.).
"""

from __future__ import annotations

from pathlib import Path

from neuroconv import NWBConverter
from neuroconv.datainterfaces import TdtRecordingInterface
from spikeinterface import select_segment_recording

from .base import EphysConverter, neo_channel_names


class TdtConverter(EphysConverter):
    """Convert TDT recordings to NWB."""

    data_interface_classes = {"Recording": TdtRecordingInterface}

    def __init__(self, source_data: dict, probe_group_path: str | Path = None, block_index: int = None):
        # Order matters: fix names on the full multi-segment recording first,
        # then slice to a single block, then attach probes.
        NWBConverter.__init__(self, source_data=source_data)
        self._fix_channel_names()
        if block_index is not None:
            self._select_block(block_index)
        if probe_group_path:
            self._attach_probes(probe_group_path)

    def _fix_channel_names(self):
        """The channel names of the TDT header, as Neo reads them.

        neuroconv 0.10.2 strips their first character
        (``name.replace("'", "")[1:]``, tdtdatainterface.py:130: 'Broa 1'
        becomes 'roa 1'; to be reported upstream).
        """
        self.recording.set_property("channel_name", neo_channel_names(self.recording))

    def _select_block(self, block_index: int):
        """Select a single block (segment) from a multi-block TDT recording."""
        interface = self.data_interface_objects["Recording"]
        recording = interface.recording_extractor
        if recording.get_num_segments() > 1:
            interface.recording_extractor = select_segment_recording(recording, segment_indices=block_index)
