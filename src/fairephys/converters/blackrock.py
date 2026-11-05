"""Blackrock NWB converter."""

from neuroconv.datainterfaces import BlackrockRecordingInterface

from .base import EphysConverter


class BlackrockConverter(EphysConverter):
    """Convert Blackrock recordings to NWB."""

    data_interface_classes = {"Recording": BlackrockRecordingInterface}
