"""SpikeGLX NWB converter.

SpikeGLX recordings from Neuropixels probes use per-session IMRO tables
to select which electrodes are active.  The converter reads the .meta file
via probeinterface.read_spikeglx() to build a session-specific probe
(typically 384 contacts) rather than attaching the static full-probe
ProbeGroup (which has all physical contacts, e.g. 4416 for NP1032).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from neo.rawio import SpikeGLXRawIO
from neo.rawio.spikeglxrawio import read_meta_file
from neuroconv import NWBConverter
from neuroconv.datainterfaces import SpikeGLXRecordingInterface
from neuroconv.tools.signal_processing import get_rising_frames_from_ttl
from probeinterface import ProbeGroup, read_spikeglx

from .base import EphysConverter, attach_probes, neo_channel_names


class SpikeGLXConverter(EphysConverter):
    """Convert SpikeGLX recordings to NWB.

    source_data["Recording"] names the folder and the AP stream ("imec0.ap");
    probe_id is the id of the session's device (placements.probe_ids), under
    which the probe read from the AP stream's .meta is attached. With `lf` the
    probe's LF stream ("imec0.lf") is written too, as a second
    ElectricalSeries of the same electrode group (neuroconv's names,
    ElectricalSeriesAP and ElectricalSeriesLF, with the probe's slot appended
    when the run enabled more than one probe). The LF stream records the same
    electrodes as the AP stream, so it takes the AP probe, as SpikeInterface's
    SpikeGLX extractor does (its own .meta names its channels 384 to 767 in
    snsSaveChanSubset, which probeinterface's read_spikeglx cannot slice; an
    LF stream of another channel count fails SpikeInterface's set_probe when
    the extractor is built). The channels of each stream
    keep their SpikeGLX names (the .meta snsChanMap, "AP0" and "LF0", as Neo
    reads them), which neuroconv 0.10.2 would join ("AP0,LF0"); so each
    stream's channels are their own rows of the electrode table, with the
    contact in electrode_name.
    """

    data_interface_classes = {"Recording": SpikeGLXRecordingInterface, "LF": SpikeGLXRecordingInterface}

    def __init__(self, source_data: dict, probe_id: str, lf: bool = False):
        if lf:
            recording = source_data["Recording"]
            device, _, kind = recording["stream_id"].rpartition(".")
            if kind != "ap":
                raise ValueError(f"the LF stream goes with an AP stream, not {recording['stream_id']!r}")
            source_data = {**source_data, "LF": {"folder_path": recording["folder_path"], "stream_id": f"{device}.lf"}}
        NWBConverter.__init__(self, source_data=source_data)
        ap = self.data_interface_objects["Recording"]
        probe = read_spikeglx(str(_find_meta(ap.folder_path, ap.stream_id)))
        probe.annotate(probe_id=probe_id)
        for interface in self.recording_interfaces:
            recording = interface.recording_extractor
            recording.set_property("channel_name", neo_channel_names(recording))
            own = probe.copy()  # one Probe object per group; the copy drops the wiring
            own.set_device_channel_indices(probe.device_channel_indices)
            group = ProbeGroup()
            group.add_probe(own)
            interface.recording_extractor = attach_probes(recording, group)


def _find_meta(folder: Path, stream_id: str) -> Path:
    """The one .meta file of the stream under the folder."""
    metas = sorted(folder.rglob(f"*{stream_id}.meta"))
    if len(metas) != 1:
        raise FileNotFoundError(f"{len(metas)} .meta files of stream {stream_id} under {folder}"
                                + (f": {[m.name for m in metas]}" if metas else ""))
    return metas[0]


def read_nidq_channels(folder: str | Path, channels: list[str],
                       max_seconds: float | None = None) -> tuple[np.ndarray, float]:
    """Read SpikeGLX NIDQ channels (e.g. ["XA0", "XA1"]) in volts.

    Neo applies the gain of each channel type (MN, MA, XA) from the .meta
    file. Reads the first segment. Returns (data of shape (n_samples,
    n_channels), sampling rate in Hz).
    """
    reader = SpikeGLXRawIO(dirname=str(folder))
    reader.parse_header()
    stream_index = list(reader.header["signal_streams"]["id"]).index("nidq")
    signal_channels = reader.header["signal_channels"]
    names = list(signal_channels[signal_channels["stream_id"] == "nidq"]["name"])
    indexes = [names.index(c) for c in channels]
    rate = reader.get_signal_sampling_rate(stream_index=stream_index)
    n_samples = reader.get_signal_size(0, 0, stream_index=stream_index)
    if max_seconds is not None:
        n_samples = min(n_samples, int(rate * max_seconds))
    raw = reader.get_analogsignal_chunk(0, 0, 0, n_samples, stream_index=stream_index,
                                        channel_indexes=indexes)
    data = reader.rescale_signal_raw_to_float(raw, dtype="float64", stream_index=stream_index,
                                              channel_indexes=indexes)
    return data, rate


# A NIDQ sync edge pairs with the imec edge nearest to it within this (s);
# at least this fraction of the edges must pair.
_SYNC_MATCH = 0.1
_SYNC_MIN_PAIRED = 0.95


def nidq_times_on_imec_clock(folder: str | Path, nidq_sync: str | int, imec_stream: str,
                             imec_sync_bit: int = 6, n_samples: int | None = None) -> np.ndarray:
    """Times of the NIDQ samples on the clock of an imec probe, in seconds.

    The NIDQ device and an imec probe run on separate clocks. Both record
    SpikeGLX's 1 Hz sync square wave: on the NIDQ the input the lab chose
    (nidq_sync: an analog channel name such as "XA5", thresholded halfway
    between its minimum and maximum, or a digital line number, bit line % 16
    of digital word line // 16), on the probe bit imec_sync_bit of the SY0
    channel of imec_stream ("imec0.lf" is cheaper to read than "imec0.ap" and
    starts with it). The rising edges of both, in acquisition seconds
    (firstSample and the sampling rate of each .meta), are paired: each NIDQ
    edge with the nearest imec edge within 0.1 s, and at least 95% must pair.
    The NIDQ times are then mapped piecewise linearly between the paired edges,
    as SpikeGLX's TPrime does, and with the slopes of the end segments before
    the first and after the last pair.

    Each sync input becomes a 0/1 line here (the analog channel above the
    halfway cut, the digital line or SY0 bit shifted out of its word), whose
    rising edges neuroconv's get_rising_frames_from_ttl finds (threshold 0.5).
    SpikeGLXNIDQInterface.get_event_times_from_ttl is not used: it cuts at the
    mean of the trace instead of halfway and reads a digital word whole, not
    one line. SpikeGLXSyncChannelInterface is not used: it gives the whole SY0
    word, with no bit selection. Neither gives times as (sample + firstSample)
    / rate: theirs add firstSample / rate to sample / rate, which differs in
    the last bits, so the edge samples are converted to seconds here.

    Reads the first segment of the folder, which holds the NIDQ .bin/.meta,
    the .bin/.meta of imec_stream and the one .meta of the probe's AP stream.
    Returns the times of the first n_samples NIDQ samples (all by default) in
    seconds from the first sample of the probe's AP file.
    """
    device, _, kind = imec_stream.rpartition(".")
    if not device or kind not in ("ap", "lf"):
        raise ValueError(f"imec_stream must be '<device>.ap' or '<device>.lf', not {imec_stream!r}")
    reader = SpikeGLXRawIO(dirname=str(folder))
    reader.parse_header()

    ni_index, ni_meta = _sync_stream(reader, folder, "nidq")
    names = list(reader.header["signal_channels"][reader.header["signal_channels"]["stream_id"] == "nidq"]["name"])
    if isinstance(nidq_sync, str):
        if nidq_sync not in names or nidq_sync.startswith("XD"):
            raise ValueError(f"{nidq_sync!r} is not an analog NIDQ channel in {folder}: {names}")
        raw = reader.get_analogsignal_chunk(0, 0, None, None, stream_index=ni_index,
                                            channel_indexes=[names.index(nidq_sync)])[:, 0].astype(np.int32)
        high = raw > (raw.min() + raw.max()) / 2
    else:
        words = [n for n in names if n.startswith("XD")]
        if not 0 <= nidq_sync < 16 * len(words):
            raise ValueError(f"digital line {nidq_sync} is not saved in the NIDQ file in {folder} "
                             f"({len(words)} digital words)")
        raw = reader.get_analogsignal_chunk(0, 0, None, None, stream_index=ni_index,
                                            channel_indexes=[names.index(words[nidq_sync // 16])])[:, 0]
        high = (raw.view(np.uint16) >> (nidq_sync % 16)) & 1
    ni_rate = float(ni_meta["niSampRate"])
    ni_t = (get_rising_frames_from_ttl(high, threshold=0.5) + int(ni_meta["firstSample"])) / ni_rate

    _, im_meta = _sync_stream(reader, folder, imec_stream)
    sync_id = f"{imec_stream}-SYNC"
    if sync_id not in list(reader.header["signal_streams"]["id"]):
        raise ValueError(f"{imec_stream} in {folder} has no saved SY0 channel")
    sy = reader.get_analogsignal_chunk(0, 0, None, None,
                                       stream_index=list(reader.header["signal_streams"]["id"]).index(sync_id))
    im_t = ((get_rising_frames_from_ttl((sy[:, 0].view(np.uint16) >> imec_sync_bit) & 1, threshold=0.5)
             + int(im_meta["firstSample"]))
            / float(im_meta["imSampRate"]))

    if ni_t.size < 2 or im_t.size < 2:
        raise ValueError(f"{folder}: {ni_t.size} NIDQ and {im_t.size} imec sync edges; at least 2 of each needed")
    j = np.clip(np.searchsorted(im_t, ni_t), 1, im_t.size - 1)
    nearest = np.where(np.abs(im_t[j] - ni_t) < np.abs(im_t[j - 1] - ni_t), j, j - 1)
    paired = np.abs(im_t[nearest] - ni_t) < _SYNC_MATCH
    if paired.sum() < max(2, _SYNC_MIN_PAIRED * min(ni_t.size, im_t.size)):
        raise ValueError(f"{folder}: only {paired.sum()} of {ni_t.size} NIDQ sync edges pair with one of the "
                         f"{im_t.size} {imec_stream} sync edges within {_SYNC_MATCH} s")
    ni_e, im_e = ni_t[paired], im_t[nearest[paired]]

    ap_meta = read_meta_file(_find_meta(Path(folder), f"{device}.ap"))
    if "firstSample" not in ap_meta:
        raise ValueError(f"{device}.ap .meta in {folder}: no firstSample")
    if n_samples is None:
        n_samples = reader.get_signal_size(0, 0, stream_index=ni_index)
    t = (int(ni_meta["firstSample"]) + np.arange(n_samples)) / ni_rate
    mapped = np.interp(t, ni_e, im_e)
    for end, (a, b) in ((t < ni_e[0], (0, 1)), (t > ni_e[-1], (-2, -1))):
        slope = (im_e[b] - im_e[a]) / (ni_e[b] - ni_e[a])
        mapped[end] = im_e[a] + (t[end] - ni_e[a]) * slope
    return mapped - int(ap_meta["firstSample"]) / float(ap_meta["imSampRate"])


def _sync_stream(reader: SpikeGLXRawIO, folder: str | Path, stream_id: str) -> tuple[int, dict]:
    """Stream index and .meta of the first segment of a stream; the .meta must give
    firstSample and the .bin must hold the size the .meta states."""
    streams = list(reader.header["signal_streams"]["id"])
    if stream_id not in streams:
        raise FileNotFoundError(f"no {stream_id} .bin with its .meta in {folder} (git-annex content not present?)")
    info = reader.signals_info_dict[0, stream_id]
    if "firstSample" not in info["meta"]:
        raise ValueError(f"{info['meta_file']}: no firstSample")
    bin_file = Path(info["bin_file"])
    if bin_file.stat().st_size != int(info["meta"]["fileSizeBytes"]):
        raise ValueError(f"{bin_file} holds {bin_file.stat().st_size} bytes, its .meta states "
                         f"{info['meta']['fileSizeBytes']} (git-annex pointer file?)")
    return streams.index(stream_id), info["meta"]
