"""Base NWB converters with ProbeInterface integration.

EphysConverter subclasses set ``data_interface_classes`` (a neuroconv data
interface reads the raw files) and optionally override ``__init__`` for
format-specific preparation (channel name fixes, block selection, etc.).
RecordingConverter writes any SpikeInterface recording, for raw files no
Neo reader opens (TdtSevConverter, a dataset's own lazy recording). The
shared steps (probe attachment, electrode groups and locations, the test
file rule, the first-sample anchoring) are module functions on the
recording, so both write the same NWB.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from neuroconv import NWBConverter
from neuroconv.datainterfaces.ecephys.baserecordingextractorinterface import BaseRecordingExtractorInterface
from probeinterface import ProbeGroup, read_probeinterface

from ..placements import UNPLACED_GROUP, channel_group_names
from .devices import add_devices, without_device_models
from .writer import channel_positions, write_nwbfile

logger = logging.getLogger(__name__)


def attach_probes(recording, probe_group: ProbeGroup):
    """The recording with its channels grouped by the session's probes.

    group_name is the probe's id (UNPLACED_GROUP for a channel on no probe);
    location is the channel's contact position local to its probe (NaN on no
    probe), so NWB rel_x/rel_y match BIDS electrodes.tsv. When every channel
    is on a probe the group itself is attached too (set_probegroup drops the
    channels on no probe); a group that does not fit the channels is an error.
    """
    channel_ids = recording.get_channel_ids()
    names = channel_group_names(probe_group, len(channel_ids))
    if UNPLACED_GROUP in names:
        logger.warning(f"{names.count(UNPLACED_GROUP)} of {len(names)} channels are on no placed "
                       f"probe: kept in the electrode group '{UNPLACED_GROUP}' without probe geometry")
    else:
        try:
            recording = recording.set_probegroup(probe_group, group_mode="by_probe")
        except (AssertionError, ValueError) as exc:
            raise ValueError(f"the probe group does not fit the recording's {len(channel_ids)} "
                             f"channels: {exc}") from exc
    contacts = [""] * len(channel_ids)  # NWB electrode_name: the contact id ("" on no probe)
    for probe in probe_group.probes:
        for contact, index in zip(probe.contact_ids, probe.device_channel_indices):
            if 0 <= index < len(contacts):
                contacts[int(index)] = str(contact)
    recording.set_property("group_name", names, ids=channel_ids)
    recording.set_property("electrode_name", contacts, ids=channel_ids)
    recording.set_property("location", channel_positions(probe_group, len(channel_ids)), ids=channel_ids)
    return recording


def neo_channel_names(recording) -> list[str]:
    """The name in the raw header (Neo's) of every channel of a Neo-based
    SpikeInterface recording."""
    channels = recording.neo_reader.header["signal_channels"]
    channels = channels[channels["stream_id"] == recording.stream_id]
    names = dict(zip(channels["id"], channels["name"]))
    return [str(names[c]) for c in recording.get_channel_ids()]


def add_unplaced_group(recording, metadata: dict):
    """The electrode group of the channels on no placed probe, on the
    recording system's device (the first Device), when there are such
    channels."""
    names = recording.get_property("group_name")
    ecephys = metadata.setdefault("Ecephys", {})
    groups = ecephys.setdefault("ElectrodeGroup", [])
    if names is None or UNPLACED_GROUP not in names or any(g["name"] == UNPLACED_GROUP for g in groups):
        return
    if not ecephys.get("Device"):
        raise ValueError(f"no device for the '{UNPLACED_GROUP}' electrode group: the metadata "
                         "names no recording system (Hardware/Recording System)")
    groups.append({"name": UNPLACED_GROUP, "location": "no electrode placed",
                   "description": "channels recorded with no electrode placed in this session",
                   "device": ecephys["Device"][0]["name"]})


def set_electrode_locations(recording, metadata: dict):
    """brain_area of every channel: the location of its electrode group in
    the metadata. Channels without a group, or a group without metadata,
    are an error (neuroconv would write placeholder groups)."""
    names = recording.get_property("group_name")
    if names is None:
        raise ValueError("the channels are in no electrode group: attach the session's probe group")
    groups = {g["name"]: g.get("location") for g in metadata.get("Ecephys", {}).get("ElectrodeGroup", [])}
    if missing := sorted(str(g) for g in set(names) if groups.get(g) in (None, "")):
        raise ValueError(f"electrode groups {missing} have no ElectrodeGroup location in the metadata")
    recording.set_property("brain_area", [groups[g] for g in names])


STUB_SAMPLES = 100


def stub_recording(recording):
    """The recording of a test file: the first samples of each segment, at
    least 100 and more than the channel count (fewer samples than channels
    read as a transposed series to NWB readers and the NWB Inspector)."""
    from neuroconv.tools.spikeinterface.spikeinterface import _stub_recording

    return _stub_recording(recording, stub_samples=max(STUB_SAMPLES, recording.get_num_channels() + 1))


def _anchor_first_sample(metadata: dict, t_start: float) -> dict:
    """metadata whose session_start_time is moved back by t_start (seconds),
    so a series written at t_start starts at the given first-sample time."""
    nwbfile = metadata.get("NWBFile", {})
    start = nwbfile.get("session_start_time")
    if not isinstance(start, datetime) or not t_start:
        return metadata
    return {**metadata, "NWBFile": {**nwbfile, "session_start_time": start - timedelta(seconds=t_start)}}


def prepare_recording(recording, metadata: dict) -> dict:
    """The recording's electrode groups and locations set from the metadata;
    the metadata anchored on the recording's first sample. The metadata's
    session_start_time is the time of the first sample (build_nwb_metadata:
    the run's start_time); neuroconv writes the series at the recording's
    own t_start, so the file's reference time is moved back by it."""
    add_unplaced_group(recording, metadata)
    set_electrode_locations(recording, metadata)
    return _anchor_first_sample(metadata, recording.get_start_time(segment_index=0))


class RecordingConverter:
    """Converter of a SpikeInterface recording read without neuroconv: the
    recording as given, its channels grouped by the session's probes when a
    probe group is given.

    neuroconv writes the recording's samples as stored (int16 counts stay
    int16, chunked); the recording's gains and offsets (gain_to_uV,
    offset_to_uV) give the ElectricalSeries `conversion` (`channel_conversion`
    for channels of unequal gain) and `offset` in volts. A recording without
    them is an error (its samples would read as volts in every NWB reader);
    a dataset sets the gain from the odML, never a literal, and a float
    recording in volts sets gain_to_uV=1e6, offset_to_uV=0.
    """

    def __init__(self, recording, probe_group_path: str | Path = None):
        self.recording = recording
        if probe_group_path:
            self.recording = attach_probes(self.recording, read_probeinterface(str(probe_group_path)))

    def run_conversion(self, nwbfile_path: str | Path, metadata: dict, stub_test: bool = False,
                       add_to_nwbfile=None):
        """Write the NWB (a test file holds the first samples only, see
        stub_recording). `add_to_nwbfile(nwbfile)` adds a dataset's further
        content (behavioural series, intervals) to the file before it is
        written; nothing reopens a written file."""
        from neuroconv.tools.nwb_helpers import make_nwbfile_from_metadata
        from neuroconv.tools.spikeinterface import add_recording_to_nwbfile

        if not self.recording.has_scaleable_traces():
            raise ValueError("the recording has no gain_to_uV/offset_to_uV: set the scale from the odML")
        metadata = prepare_recording(self.recording, metadata)
        recording = stub_recording(self.recording) if stub_test else self.recording
        nwbfile = make_nwbfile_from_metadata(metadata)
        add_devices(nwbfile, metadata)
        add_recording_to_nwbfile(recording=recording, nwbfile=nwbfile, metadata=without_device_models(metadata))
        if add_to_nwbfile is not None:
            add_to_nwbfile(nwbfile)
        write_nwbfile(nwbfile, nwbfile_path)


class EphysConverter(NWBConverter):
    """Base converter: probes, electrode locations, and NWB write. The
    "Recording" interface is the recording; a subclass may add further
    streams of the same channels (SpikeGLXConverter's LF), written as their
    own ElectricalSeries with the recording's electrode groups."""

    def __init__(self, source_data: dict, probe_group_path: str | Path = None):
        super().__init__(source_data=source_data)
        if probe_group_path:
            self._attach_probes(probe_group_path)

    @property
    def recording(self):
        return self.data_interface_objects["Recording"].recording_extractor

    @recording.setter
    def recording(self, recording):
        self.data_interface_objects["Recording"].recording_extractor = recording

    @property
    def recording_interfaces(self) -> list[BaseRecordingExtractorInterface]:
        """The recording interfaces, "Recording" first."""
        others = [i for name, i in self.data_interface_objects.items()
                  if name != "Recording" and isinstance(i, BaseRecordingExtractorInterface)]
        return [self.data_interface_objects["Recording"], *others]

    def _attach_probes(self, probe_group_path: str | Path):
        self.recording = attach_probes(self.recording, read_probeinterface(str(probe_group_path)))

    def add_to_nwbfile(self, nwbfile, metadata: dict = None, conversion_options: dict = None):
        if metadata:
            add_devices(nwbfile, metadata)
            metadata = without_device_models(metadata)
        if not getattr(self, "_stub_test", False):
            return super().add_to_nwbfile(nwbfile=nwbfile, metadata=metadata, conversion_options=conversion_options)
        # a test file: the cut recordings only while the data are written (the
        # interfaces' own extractors stay in place for their metadata)
        full = [(interface, interface.recording_extractor) for interface in self.recording_interfaces]
        for interface, recording in full:
            interface.recording_extractor = stub_recording(recording)
        try:
            super().add_to_nwbfile(nwbfile=nwbfile, metadata=metadata, conversion_options=conversion_options)
        finally:
            for interface, recording in full:
                interface.recording_extractor = recording

    def run_conversion(self, nwbfile_path: str | Path, metadata: dict, stub_test: bool = False,
                       add_to_nwbfile=None):
        """Write the NWB (see prepare_recording for the first-sample anchoring;
        a test file holds the first samples of every stream, see
        stub_recording). `add_to_nwbfile(nwbfile)` adds a dataset's further
        content (behavioural series, intervals, units) to the file after the
        recording and before it is written; nothing reopens a written file."""
        metadata = prepare_recording(self.recording, metadata)
        for interface in self.recording_interfaces[1:]:
            set_electrode_locations(interface.recording_extractor, metadata)
        self.validate_metadata(metadata)
        self._stub_test = stub_test
        nwbfile = self.create_nwbfile(metadata=metadata)
        if add_to_nwbfile is not None:
            add_to_nwbfile(nwbfile)
        write_nwbfile(nwbfile, nwbfile_path)
