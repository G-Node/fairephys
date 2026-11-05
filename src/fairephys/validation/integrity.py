"""Sanity checks of the NWB against the raw recording: the recording summary,
distinct channels in the raw streams, and the samples of every series."""

from pathlib import Path

import numpy as np
from pynwb import NWBHDF5IO

from .report import ValidationReport, files_exist, guarded
from ..formats import open_rawio

# Neo signal units -> volts (scale of the reader's units label, reported only)
NEO_UNITS_TO_VOLTS = {"uV": 1e-6, "µV": 1e-6, "mV": 1e-3, "V": 1.0}


class RecordingRawIO:
    """The part of Neo's RawIO interface the raw checks read, on a
    SpikeInterface recording: one signal stream of the recording's channels
    (gain_to_uV with units uV when the recording has gains, else 1.0 and no
    units), its segments, sizes, start times and sample chunks."""

    def __init__(self, recording):
        self.recording = recording
        ids = [str(c) for c in recording.get_channel_ids()]
        names = recording.get_property("channel_name")
        scaled = recording.has_scaleable_traces()
        gains = recording.get_channel_gains() if scaled else np.ones(len(ids))
        offsets = recording.get_channel_offsets() if scaled else np.zeros(len(ids))
        self.header = {
            "signal_streams": np.array([("recording", "0")], dtype=[("name", "U16"), ("id", "U8")]),
            "signal_channels": np.array(
                list(zip(ids if names is None else map(str, names), ids, gains, ["uV" if scaled else ""] * len(ids),
                         offsets, ["0"] * len(ids))),
                dtype=[("name", "U64"), ("id", "U64"), ("gain", "f8"), ("units", "U8"), ("offset", "f8"),
                       ("stream_id", "U8")]),
        }

    def segment_count(self, block_index: int) -> int:
        return self.recording.get_num_segments()

    def get_signal_size(self, block_index: int, seg_index: int, stream_index: int) -> int:
        return self.recording.get_num_samples(segment_index=seg_index)

    def get_signal_t_start(self, block_index: int, seg_index: int, stream_index: int) -> float:
        return self.recording.get_start_time(segment_index=seg_index)

    def get_analogsignal_chunk(self, block_index: int, seg_index: int, i_start: int, i_stop: int,
                               stream_index: int, channel_indexes=None) -> np.ndarray:
        ids = self.recording.get_channel_ids()
        channel_ids = None if channel_indexes is None else ids[np.asarray(channel_indexes)]
        return self.recording.get_traces(segment_index=seg_index, start_frame=i_start, end_frame=i_stop,
                                         channel_ids=channel_ids)


def _raw_reader(recording_format: str | None, raw_path: Path | None, raw_recording):
    """The raw side of a check: the SpikeInterface recording given, else the
    Neo reader of the raw path."""
    return RecordingRawIO(raw_recording) if raw_recording is not None else open_rawio(recording_format, raw_path)


@guarded
def check_recording_metadata(nwb_path: Path, report: ValidationReport) -> ValidationReport:
    """Extract key recording parameters from NWB."""
    if not files_exist(report, nwb_path):
        return report
    with NWBHDF5IO(str(nwb_path), "r") as io:
        nwb = io.read()
        if nwb.subject:
            report.add_info("subject_id", nwb.subject.subject_id)
            report.add_info("species", nwb.subject.species)
        report.add_info("session_description", (nwb.session_description or "n/a")[:100])
        report.add_info("session_start_time", str(nwb.session_start_time))
        if nwb.electrode_groups:
            report.add_info("electrode_groups", [f"{n} ({getattr(g, 'location', '?')})"
                                                 for n, g in nwb.electrode_groups.items()])
        if nwb.electrodes is not None:
            df = nwb.electrodes.to_dataframe()
            if "location" in df.columns:
                report.add_info("brain_regions", df["location"].dropna().unique().tolist())
        # Duration: the first series and the series continuing it (same
        # rate and channels), e.g. one series per segment of a paused
        # recording
        series = _acquisition_series(nwb)
        if series:
            first = series[0]
            parts = [ts for ts in series
                     if ts.rate == first.rate and ts.data.shape[1:] == first.data.shape[1:]]
            dur = sum(len(ts.data) / ts.rate for ts in parts)
            report.add_info("sampling_rate_hz", first.rate)
            report.add_info("duration_seconds", round(dur, 2))
            report.add_info("duration_formatted", _fmt_duration(dur))
            if len(parts) > 1:
                report.add_info("recording_segments", [
                    f"{ts.name} (start {ts.starting_time or 0:.2f}s, {_fmt_duration(len(ts.data) / ts.rate)})"
                    for ts in parts])
    return report


@guarded
def check_data_integrity(
    raw_path: Path | None,
    nwb_path: Path,
    report: ValidationReport,
    recording_format: str = None,
    block_index: int = 0,
    segment_index: int | None = None,
    stream_index: int | None = None,
    stream_id: str | None = None,
    raw_recording=None,
) -> ValidationReport:
    """Compare raw data samples with NWB.

    Every acquisition series of the NWB is compared with its raw stream and
    segment:

    - Stream: the one given (stream_id or stream_index), else the only one,
      else the stream whose channel names hold the series' channel names
      (electrodes table), told apart by the sample count if several do.
    - Segment: the one given, for a single series; else the one whose start
      time is the series' starting_time (neuroconv writes one series per
      segment, starting at the segment's start); else the series' position.

    The NWB is expected to store the samples as read (neuroconv's default),
    so both sides are compared on the NWB's own scale (conversion *
    channel_conversion, in volts). The raw reader's units label is not used
    for the comparison, as a reader may label the units wrongly (Neo labels
    every TDT store uV, also float stores that hold volts); it is reported
    next to the NWB scale.

    Args:
        recording_format: a key of fairephys.formats.RAW_FORMATS, with a raw path.
        block_index: Neo block index (default 0; TDT uses 1 block with N segments).
        segment_index: Neo segment index of a single-series NWB (default:
            the segment starting at the series' starting_time, else 0).
        stream_index: Positional index into signal_streams array.
        stream_id: Neo stream ID string (e.g. "1" for Broa). Takes priority
            over stream_index, resolved to a positional index internally.
        raw_recording: the raw side as a SpikeInterface recording instead of
            a raw path (RecordingRawIO).
    """
    if not files_exist(report, *([] if raw_path is None else [raw_path]), nwb_path):
        return report
    raw_io = _raw_reader(recording_format, raw_path, raw_recording)
    given = stream_id if stream_id is not None else stream_index
    si_given = _resolve_stream_index(raw_io, stream_index=stream_index, stream_id=stream_id)

    with NWBHDF5IO(str(nwb_path), "r") as io:
        series = _acquisition_series(io.read())
        if not series:
            report.add_error("Sanity", "No acquisition data in the NWB")
            return report
        results = []
        for position, ts in enumerate(series):
            if given is not None:
                si, skip = si_given, f"SKIPPED (stream {given} not found)"
            else:
                si, skip = _match_stream(raw_io, ts, block_index)
            seg = None if si is None else _series_segment(
                raw_io, ts, position, len(series), block_index, si, segment_index)
            if si is None:
                result = {"result": skip}
            elif seg is None:
                result = {"result": "SKIPPED (raw segment not identified)"}
            else:
                result = _compare_series(raw_io, ts, block_index, seg, si)
                if given is None and len(raw_io.header["signal_streams"]) > 1:
                    result["raw_stream"] = f"{raw_io.header['signal_streams'][si]['id']} (matched by channel names)"
            results.append({"series": ts.name, **({"segment": seg} if seg is not None else {}), **result})
    _report_integrity(report, results)
    return report


def _report_integrity(report: ValidationReport, results: list[dict]) -> None:
    """The per-series results as report info, sample mismatches as warnings."""
    several = len(results) > 1
    for r in results:
        prefix = f"{r['series']}: " if several else ""
        for msg in r.pop("mismatches", []):
            report.add_warning("Sanity", prefix + msg)
    if several:
        report.add_info("integrity_series", results)
    else:
        for key, value in results[0].items():
            if key not in ("series", "segment", "result", "max_diff"):
                report.add_info(key, value)

    compared = [r for r in results if "max_diff" in r]
    if not compared:
        report.add_info("data_integrity_check", results[0]["result"])
        return
    max_diff = max(r["max_diff"] for r in compared)
    if any(r["result"].startswith("WARN") for r in compared):
        report.add_info("data_integrity_check", f"WARN (max diff={max_diff:.2e} V)")
    elif len(compared) < len(results):
        report.add_info("data_integrity_check",
                        f"PASSED ({len(compared)} of {len(results)} series, others skipped)")
    else:
        report.add_info("data_integrity_check", "PASSED")
    report.add_info("max_sample_diff", f"{max_diff:.2e} V")


def _compare_series(raw_io, ts, block_index: int, seg: int, si: int) -> dict:
    """Compare one NWB series with raw segment *seg* of stream *si*."""
    n_raw = raw_io.get_signal_size(block_index=block_index, seg_index=seg, stream_index=si)
    # Count channels for this stream only
    ch_mask = raw_io.header["signal_channels"]["stream_id"] == raw_io.header["signal_streams"][si]["id"]
    ch_raw = int(ch_mask.sum())
    n_nwb, ch_nwb = ts.data.shape[0], ts.data.shape[1] if len(ts.data.shape) > 1 else 1
    result = {"raw_samples": n_raw, "nwb_samples": n_nwb, "raw_channels": ch_raw, "nwb_channels": ch_nwb}

    # Volts per stored unit: the NWB's own scale (compared on) and
    # the raw reader's gain and units label (reported only)
    min_ch = min(ch_raw, ch_nwb)
    nwb_scale = _nwb_volts_per_unit(ts, ch_nwb)[:min_ch]
    result.update(_scales(raw_io.header["signal_channels"][ch_mask][:min_ch], nwb_scale))

    if n_nwb < n_raw:
        result["nwb_is_stub"] = True
    max_n = min(n_raw, n_nwb)
    if max_n < 200:
        return {**result, "result": "SKIPPED (stub too small)"}

    # Compare 3 random chunks, raw and NWB samples both on the NWB
    # scale (the NWB offset is common to both and cancels out)
    np.random.seed(42)
    chunk = min(1000, max_n // 4)
    positions = np.random.randint(0, max(1, max_n - chunk), size=3)
    max_diff = 0.0
    mismatches = []
    for pos in positions:
        raw_chunk = raw_io.get_analogsignal_chunk(
            block_index=block_index, seg_index=seg,
            i_start=int(pos), i_stop=int(pos + chunk),
            stream_index=si,
        )
        nwb_chunk = ts.data[int(pos):int(pos + chunk), :]
        raw_volts = raw_chunk[:, :min_ch].astype(np.float64) * nwb_scale
        nwb_volts = np.asarray(nwb_chunk)[:, :min_ch].astype(np.float64) * nwb_scale

        diff = float(np.max(np.abs(raw_volts - nwb_volts)))
        max_diff = max(max_diff, diff)
        ptp = float(np.ptp(raw_volts))
        if (ptp > 0 and diff > ptp * 0.01) or diff > 1e-9:
            mismatches.append(f"Mismatch at {pos}: diff={diff:.2e} V")
    result["result"] = f"WARN (max diff={max_diff:.2e} V)" if mismatches else "PASSED"
    return {**result, "max_diff": max_diff, "mismatches": mismatches}


@guarded
def check_channel_distinctness(
    raw_path: Path | None,
    report: ValidationReport,
    recording_format: str = None,
    block_index: int = 0,
    segment_index: int = 0,
    stream_index: int | None = None,
    stream_id: str | None = None,
    n_samples: int = 1000,
    raw_recording=None,
) -> ValidationReport:
    """Verify channels within a stream contain distinct data.

    Reads a small chunk of the given stream (stream_id or stream_index; every
    signal stream when none is given) and checks that its channels are not
    all identical. Identical channels suggest a reader bug (e.g. wrong file
    path, case-mismatch in per-channel data files).

    Run this *before* NWB conversion as a prerequisite sanity check.

    Args:
        recording_format: a key of fairephys.formats.RAW_FORMATS, with a raw path.
        block_index: Neo block index (default 0; TDT uses 1 block with N segments).
        segment_index: Neo segment index within block.
        raw_recording: the raw side as a SpikeInterface recording instead of
            a raw path (RecordingRawIO).
    """
    if raw_path is not None and not files_exist(report, raw_path):
        return report
    raw_io = _raw_reader(recording_format, raw_path, raw_recording)
    n_streams = len(raw_io.header["signal_streams"])
    if stream_id is not None or stream_index is not None:
        given = _resolve_stream_index(raw_io, stream_index=stream_index, stream_id=stream_id)
        if given is None:
            report.add_error("Sanity", f"Stream {stream_id!r} not in {raw_path.name if raw_path else 'the recording'}")
            return report
        stream_indices = [given]
    else:
        stream_indices = range(n_streams)
    issues_found = False
    for stream_idx in stream_indices:
        stream_name = raw_io.header["signal_streams"][stream_idx]["name"]
        sid = raw_io.header["signal_streams"][stream_idx]["id"]
        ch_mask = raw_io.header["signal_channels"]["stream_id"] == sid
        n_channels = int(ch_mask.sum())
        if n_channels < 2:
            continue
        n_available = raw_io.get_signal_size(block_index=block_index, seg_index=segment_index,
                                             stream_index=stream_idx)
        n_read = min(n_samples, n_available)
        if n_read < 10:
            continue
        chunk = raw_io.get_analogsignal_chunk(
            block_index=block_index, seg_index=segment_index,
            i_start=0, i_stop=n_read, stream_index=stream_idx,
        )
        if all(np.array_equal(chunk[:, 0], chunk[:, i]) for i in range(1, chunk.shape[1])):
            issues_found = True
            report.add_error(
                "Sanity",
                f"ALL {n_channels} channels identical in stream '{stream_name}' "
                f"(segment {segment_index}): possible reader bug",
            )
            continue
        identical_pairs = [
            (i, j)
            for i in range(chunk.shape[1])
            for j in range(i + 1, chunk.shape[1])
            if np.array_equal(chunk[:, i], chunk[:, j])
        ]
        if identical_pairs:
            issues_found = True
            report.add_warning(
                "Sanity",
                f"{len(identical_pairs)} identical channel pair(s) in "
                f"stream '{stream_name}': {identical_pairs[:5]}",
            )
    if not issues_found:
        report.add_info("channel_distinctness", f"PASSED ({n_streams} streams, segment {segment_index})")
    return report


def _nwb_volts_per_unit(ts, n_channels: int) -> np.ndarray:
    """Per-channel volts per stored unit of an NWB series (conversion * channel_conversion)."""
    scale = np.full(n_channels, float(ts.conversion) if ts.conversion else 1.0)
    if getattr(ts, "channel_conversion", None) is not None:
        scale *= np.asarray(ts.channel_conversion[:], dtype=np.float64)[:n_channels]
    return scale


def _scales(raw_channels, nwb_scale: np.ndarray) -> dict:
    """The NWB scale and the raw reader's (gain * units label), noting a mismatch."""
    units = sorted({str(u).strip() for u in raw_channels["units"]})
    raw_scale = raw_channels["gain"].astype(np.float64) * np.array(
        [NEO_UNITS_TO_VOLTS.get(str(u).strip(), np.nan) for u in raw_channels["units"]])
    scales = {"raw_scale": f"{_fmt_volts(raw_scale)} (units {', '.join(units) or 'none'})",
              "nwb_scale": _fmt_volts(nwb_scale)}
    ratio = nwb_scale / raw_scale
    if ratio.size and np.all(np.isfinite(ratio)) and not np.allclose(ratio, 1.0, rtol=1e-3, atol=0):
        scales["scale_note"] = (
            f"NWB scale is {_fmt_range(ratio)} x the raw reader's (gain and units "
            f"label); the samples are compared on the NWB scale"
        )
    return scales


def _acquisition_series(nwb) -> list:
    """Sampled acquisition series of an NWB file (data at a fixed rate), in file order."""
    return [ts for ts in (nwb.acquisition or {}).values()
            if hasattr(ts, "data") and getattr(ts, "rate", None)]


def _series_channel_names(ts) -> set[str]:
    """Channel names of a series' electrodes (electrodes table column channel_name)."""
    electrodes = getattr(ts, "electrodes", None)
    if electrodes is None or "channel_name" not in electrodes.table.colnames:
        return set()
    names = electrodes.table["channel_name"].data[:]
    return {n.decode() if isinstance(n, bytes) else str(n) for n in np.asarray(names)[electrodes.data[:]]}


def _match_stream(raw_io, ts, block_index: int) -> tuple[int | None, str | None]:
    """Raw stream of a series, by its channel names, then sample count.

    Returns (stream index, None), or (None, reason the check is skipped).
    """
    streams = raw_io.header["signal_streams"]
    if len(streams) == 1:
        return 0, None
    names = _series_channel_names(ts)
    channels = raw_io.header["signal_channels"]
    candidates = [
        i for i, s in enumerate(streams) if names and names <= {
            str(v) for key in ("name", "id") for v in channels[key][channels["stream_id"] == s["id"]]}
    ]
    if len(candidates) > 1:
        # Streams with the same channel names (e.g. one per probe): the one
        # with the series' sample count, in one of its segments
        sized = [i for i in candidates if any(
            raw_io.get_signal_size(block_index=block_index, seg_index=seg, stream_index=i) == len(ts.data)
            for seg in range(raw_io.segment_count(block_index)))]
        candidates = sized or candidates
    if len(candidates) == 1:
        return candidates[0], None
    if candidates:
        ids = ", ".join(str(streams[i]["id"]) for i in candidates)
        return None, f"SKIPPED (multi-stream, the channel names match streams {ids})"
    return None, "SKIPPED (multi-stream, no stream specified or matched by channel names)"


def _series_segment(raw_io, ts, position: int, n_series: int, block_index: int,
                    si: int, segment_index: int | None) -> int | None:
    """Raw segment of a series: the one given (single series), else by start time, else by position."""
    if segment_index is not None and n_series == 1:
        return segment_index
    n_segments = raw_io.segment_count(block_index)
    if ts.starting_time is not None:
        for seg in range(n_segments):
            t_start = raw_io.get_signal_t_start(block_index=block_index, seg_index=seg, stream_index=si)
            if abs(float(t_start) - ts.starting_time) < 0.5 / ts.rate:
                return seg
    if n_series == 1:
        return 0
    return position if n_series == n_segments else None


def _fmt_duration(seconds: float) -> str:
    """Format a duration in seconds or minutes."""
    return f"{seconds:.1f}s" if seconds < 60 else f"{seconds / 60:.1f}min"


def _fmt_range(values: np.ndarray) -> str:
    """Format one value, or the min..max range of differing values."""
    lo, hi = float(np.min(values)), float(np.max(values))
    return f"{lo:.3g}" if np.isclose(lo, hi, rtol=1e-6, atol=0) else f"{lo:.3g}..{hi:.3g}"


def _fmt_volts(scale: np.ndarray) -> str:
    """Format a per-channel volts-per-unit scale."""
    return f"{_fmt_range(scale)} V" if scale.size and np.all(np.isfinite(scale)) else "n/a"


def _resolve_stream_index(
    raw_io, *, stream_index: int | None = None, stream_id: str | None = None
) -> int | None:
    """Resolve a stream to its positional index in the signal_streams array.

    If *stream_id* is given, find the stream with that id or, where ids follow
    the order of the streams in each file (e.g. TDT stores), that name.
    Otherwise fall back to *stream_index*. If neither is given: return 0 for
    single-stream files, None for multi-stream (caller should skip or handle).
    """
    streams = raw_io.header["signal_streams"]
    if stream_id is not None:
        for key in ("id", "name"):
            for i, s in enumerate(streams):
                if str(s[key]) == str(stream_id):
                    return i
        return None
    if stream_index is not None:
        return stream_index
    return 0 if len(streams) == 1 else None
