"""Extract metadata from recordings using Neo for odML enrichment.

Two public functions:
    extract()          read a recording file, return structured metadata dict
    enrich_document()  fill empty odML properties from extracted metadata
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import odml
import yaml

from ..formats import RAW_FORMATS, open_rawio
from ..metadata.sessions import find_session
from ..odml import prop, section_at

logger = logging.getLogger(__name__)

# Standard keys present on every raw_annotations block: skipped when harvesting
_BLOCK_STANDARD_KEYS = {"name", "file_origin", "description", "segments"}
# Block annotations that hold the recording's start (Blackrock, Plexon2)
_BLOCK_DATETIME_KEYS = ("rec_datetime", "m_CreatorDateTime")


# Extraction
def extract(file_path: str | Path, format: str, block_index: int = 0) -> dict:
    """Extract metadata from a recording file via Neo RawIO.

    Args:
        file_path: Path to recording file or directory.
        format: A key of fairephys.formats.RAW_FORMATS.
        block_index: Block to extract (0-based; relevant for TDT multi-block).

    Returns:
        dict with header data, the duration of every segment of the block by
        stream id ('stream_durations_s') and the time of its first sample
        ('stream_start_times', ISO; enrich_document keeps the curated
        stream's of both), and annotations.
    """
    reader = open_rawio(format, file_path)
    streams = _extract_signal_streams(reader.header.get("signal_streams", []), reader)
    nb_seg = int(reader.header["nb_segment"][block_index])
    return {
        "file": str(file_path),
        "format": format,
        "block_index": block_index,
        "nb_block": int(reader.header["nb_block"]),
        "nb_segment": nb_seg,
        "signal_channels": _extract_signal_channels(reader.header["signal_channels"]),
        "signal_streams": streams,
        "spike_channels": _extract_spike_channels(reader.header.get("spike_channels", [])),
        "event_channels": _extract_event_channels(reader.header.get("event_channels", [])),
        "stream_durations_s": {
            s["id"]: [float(reader.get_signal_size(block_index, seg, stream_index=i) / s["sampling_rate"])
                      for seg in range(nb_seg)]
            for i, s in enumerate(streams)},
        "stream_start_times": _stream_start_times(reader, block_index, nb_seg, format, streams),
        "annotations": _extract_annotations(reader, block_index),
    }


def _extract_signal_channels(channels) -> list[dict]:
    return [
        {
            "name": str(ch["name"]),
            "id": str(ch["id"]),
            "sampling_rate": float(ch["sampling_rate"]),
            "dtype": str(ch["dtype"]),
            "units": str(ch["units"]),
            "gain": float(ch["gain"]),
            "offset": float(ch["offset"]),
            "stream_id": str(ch["stream_id"]),
        }
        for ch in channels
    ]


def _extract_signal_streams(streams, reader) -> list[dict]:
    result = []
    for i, s in enumerate(streams):
        stream_id = str(s["id"])
        n_ch = int(np.sum(reader.header["signal_channels"]["stream_id"] == stream_id))
        result.append({
            "name": str(s["name"]),
            "id": stream_id,
            "sampling_rate": float(reader.get_signal_sampling_rate(stream_index=i)),
            "n_channels": n_ch,
        })
    return result


def _extract_spike_channels(channels) -> list[dict]:
    return [
        {"name": str(ch["name"]), "id": str(ch["id"]),
         "wf_units": str(ch["wf_units"]), "wf_sampling_rate": float(ch["wf_sampling_rate"])}
        for ch in channels
    ]


def _extract_event_channels(channels) -> list[dict]:
    return [
        {"name": str(ch["name"]), "id": str(ch["id"]), "type": str(ch["type"])}
        for ch in channels
    ]


def _stream_start_times(reader, block_index: int, nb_seg: int, format: str,
                        streams: list[dict]) -> dict[str, list[str | None]]:
    """{stream id: ISO time of the first sample of every segment}: the
    recording's time origin plus the stream's t_start in the segment. The
    origin is the block's header time (UTC when RAW_FORMATS names the
    format's clock so, else the recording site's local time without offset);
    a TDT tank has no header time, its TSQ index stamps each block in Unix
    time (Neo keeps that epoch private); SpikeGLX gives each file's start in
    its .meta (fileCreateTime, local), the time of its first sample. A
    header time that stamps the first sample (RAW_FORMATS 'header', Plexon2:
    its t_start is the acquisition clock, hours into a 20 minute file) is
    the earliest stream's first sample of the first segment.
    """
    if RAW_FORMATS[format]["rawio"] == "SpikeGLXRawIO":
        return {s["id"]: [_spikeglx_file_create_time(reader, seg, s["name"]) for seg in range(nb_seg)]
                for s in streams}
    origin = _time_origin(reader, block_index, format)
    if origin is None:
        return {s["id"]: [None] * nb_seg for s in streams}
    t_starts = {s["id"]: [float(reader.get_signal_t_start(block_index, seg, i)) for seg in range(nb_seg)]
                for i, s in enumerate(streams)}
    if RAW_FORMATS[format].get("header") == "first sample" and t_starts:
        origin -= timedelta(seconds=min(t[0] for t in t_starts.values()))
    return {sid: [(origin + timedelta(seconds=t)).isoformat() for t in ts] for sid, ts in t_starts.items()}


def _time_origin(reader, block_index: int, format: str) -> datetime | None:
    """The absolute time of Neo's time 0 of the block (see _stream_start_times)."""
    block = reader.raw_annotations["blocks"][block_index]
    start = next((block[key] for key in _BLOCK_DATETIME_KEYS if block.get(key) is not None), None)
    if isinstance(start, str):
        start = datetime.fromisoformat(start)
    if start is not None and start.tzinfo is None and RAW_FORMATS[format].get("clock") == "utc":
        start = start.replace(tzinfo=timezone.utc)
    if start is None and RAW_FORMATS[format]["rawio"] == "TdtRawIO":
        epoch = float(getattr(reader, "_global_t_start", np.nan))
        start = datetime.fromtimestamp(epoch, tz=timezone.utc) if np.isfinite(epoch) else None
    return start


def _spikeglx_file_create_time(reader, seg: int, stream_name: str) -> str | None:
    """fileCreateTime of the stream's .meta file in the segment (local time
    of the recording site)."""
    info = reader.signals_info_dict.get((seg, stream_name), {})
    return info.get("meta", {}).get("fileCreateTime")


def _extract_annotations(reader, block_index: int) -> dict:
    """Harvest raw_annotations generically: the block's own keys, and the
    channel annotations (__array_annotations__) of the first segment by
    stream id."""
    block = reader.raw_annotations["blocks"][block_index]
    ann = {"block": {key: _safe_value(val) for key, val in block.items() if key not in _BLOCK_STANDARD_KEYS},
           "channel": {}}
    segments = block.get("segments") or []
    for stream_ann in (segments[0].get("signals", []) if segments else []):
        channel = {key: [_safe_value(v) for v in values]
                   for key, values in stream_ann.get("__array_annotations__", {}).items()
                   if key not in ("channel_names", "channel_ids")}
        if channel:
            ann["channel"][str(stream_ann.get("stream_id"))] = channel
    return ann


def _safe_value(obj):
    """Convert numpy/bytes values to JSON-safe Python types."""
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, (np.str_, bytes)):
        return str(obj) if isinstance(obj, np.str_) else obj.decode("utf-8", errors="replace")
    if isinstance(obj, np.ndarray):
        return [_safe_value(v) for v in obj.tolist()]
    return obj


# Enrichment (post-compilation, operates on odML document)
def enrich_document(doc: odml.Document, neo_data: dict, session_id: str = None,
                    subject: str = None, task: str = None, runs: list[int] = None,
                    value_origin: str = None):
    """Fill empty odML properties from Neo-extracted metadata.

    Finds the matching session (by session id, and by subject and task when
    given: see metadata.find_session; none is an error), fills empty scalar properties, and
    adds a Neo Metadata subsection for provenance and downstream use
    (probes, BIDS).

    Durations and the sampling rate are the curated stream's (Hardware
    'curated streams', else the first stream). Start times carry their
    offset: UTC headers keep it, local header times get the Dataset 'time
    zone'. The session's start time is the first segment's.

    runs: the run each Neo segment records, in segment order, when a session
    holds several runs (e.g. one TDT block per run). Each run's own duration
    and start time are then kept in Neo Metadata 'runs' (run-level BIDS and
    NWB fields); the session values stay those of the whole recording.

    value_origin: where neo_data comes from, for a dict a dataset builds
    without a Neo reader (enrichment/README.md lists its keys): the origin of
    the filled properties and of every property of the Neo Metadata section.
    None: the filled properties carry "neo.extract", the section's none.
    """
    if session_id is None:
        session_id = Path(neo_data.get("file", "")).stem

    session = find_session(doc, session_id, subject, task)
    neo_data = _curated_stream_data(doc, neo_data, session_id)
    tz_name = prop(section_at(doc, "Dataset"), "time zone")
    starts = [_with_offset(t, tz_name) for t in neo_data.get("segment_start_times") or []]
    neo_data = {**neo_data, "segment_start_times": starts}
    if any(s and datetime.fromisoformat(s).tzinfo is None for s in starts):
        logger.warning(f"Session {session_id}: Dataset 'time zone' not set; header start "
                       "times are recorded without offset")

    origin = value_origin or "neo.extract"
    filled = []
    if _fill_property(session, "duration", _format_duration(neo_data.get("duration_s")), origin):
        filled.append("duration")
    if streams := neo_data.get("signal_streams", []):
        if _fill_property(session, "sampling rate", f"{streams[0]['sampling_rate']:.0f} Hz", origin):
            filled.append("sampling rate")
    if (n := len(neo_data.get("signal_channels", []))) and _fill_property(session, "number of channels", n, origin):
        filled.append("number of channels")
    continuity = "discontinuous" if neo_data.get("nb_segment", 1) > 1 else "continuous"
    if _fill_property(session, "continuity", continuity, origin):
        filled.append("continuity")
    if starts and starts[0] and _fill_property(session, "start time", starts[0], origin):
        filled.append("start time")
    durations = neo_data.get("segment_durations_s") or []
    if starts and starts[-1] and durations:
        end = datetime.fromisoformat(starts[-1]) + timedelta(seconds=durations[-1])
        if _fill_property(session, "end time", end.isoformat(), origin):
            filled.append("end time")

    if filled:
        _set_property(session, "_neo_fields", ", ".join(filled), origin)
        logger.info(f"Session {session_id}: filled {filled} from Neo")

    _add_neo_metadata_section(session, neo_data, value_origin)
    if runs:
        _add_runs_section(session["Neo Metadata"], neo_data, runs, session_id, value_origin)


def _curated_stream_data(doc: odml.Document, neo_data: dict, session_id: str) -> dict:
    """neo_data reduced to the curated streams, with 'duration_s',
    'segment_durations_s' and 'segment_start_times' (first samples) of the
    curated stream (the first stream when none is curated; a dataset's own
    values are kept when it gives none by stream)."""
    streams = neo_data.get("signal_streams", [])
    if stream_ids := _curated_streams(doc):
        neo_data = _select_streams(neo_data, stream_ids)
    elif len(streams) > 1:
        logger.warning(
            f"Session {session_id}: {len(streams)} signal streams and no "
            f"Hardware/Recording System 'curated streams'; using the first "
            f"({streams[0]['id']})")
    selected = neo_data.get("signal_streams", [])
    if not selected:
        return neo_data
    curated = selected[0]
    if curated["id"] != streams[0]["id"]:
        logger.warning(f"Session {session_id}: durations from the curated stream {curated['id']} "
                       f"({curated['name']}), not the first stream {streams[0]['id']} ({streams[0]['name']})")
    if (starts := neo_data.get("stream_start_times", {}).get(curated["id"])) is not None:
        neo_data = {**neo_data, "segment_start_times": starts}
    durations = neo_data.get("stream_durations_s", {}).get(curated["id"])
    if durations is None:
        return neo_data
    return {**neo_data, "duration_s": float(sum(durations)), "segment_durations_s": durations}


def _curated_streams(doc: odml.Document) -> list[str]:
    """Stream ids or names from Hardware/Recording System 'curated streams'
    (may be empty)."""
    value = prop(section_at(doc, "Hardware/Recording System"), "curated streams")
    values = value if isinstance(value, list) else [value]
    return [str(v) for v in values if v not in (None, "")]


def _select_streams(neo_data: dict, stream_ids: list[str]) -> dict:
    """Keep only the curated streams (in the given order), their channels and
    their channel annotations.

    A curated stream is named by its Neo stream id or, where ids follow the
    order of the streams in each file (e.g. TDT stores), by its name.
    """
    streams = neo_data.get("signal_streams", [])
    by_id = {str(s["id"]): s for s in streams}
    by_name = {str(s.get("name")): s for s in streams}
    selected = [s for i in stream_ids if (s := by_id.get(i) or by_name.get(i))]
    if missing := [i for i in stream_ids if i not in by_id and i not in by_name]:
        logger.warning(f"Curated streams {missing} not in the raw file "
                       f"(streams: {[(s['id'], s.get('name')) for s in streams]})")
    order = {str(s["id"]): i for i, s in enumerate(selected)}
    ids = set(order)
    annotations = neo_data.get("annotations", {})
    channels = [c for c in neo_data.get("signal_channels", []) if str(c["stream_id"]) in ids]
    return {
        **neo_data,
        "signal_streams": selected,
        # channel positions follow the curated stream order (placements are wired by position)
        "signal_channels": sorted(channels, key=lambda c: order[str(c["stream_id"])]),
        "annotations": {**annotations, "channel": {
            sid: ann for sid, ann in annotations.get("channel", {}).items() if sid in ids}},
    }


def _with_offset(iso: str | None, tz_name: str | None) -> str | None:
    """The ISO time with its offset: a time without one is local time of the
    dataset's time zone (kept without offset when the time zone is not set)."""
    if not iso:
        return None
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None and tz_name:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt.isoformat()


def _fill_property(section: odml.Section, name: str, value, origin: str) -> bool:
    """Set a property only if it exists and is currently empty."""
    if value is None:
        return False
    prop = next((p for p in section.properties if p.name == name), None)
    if prop is None or (prop.values and prop.values[0] not in (None, "", 0)):
        return False
    prop.values = [value]
    prop.value_origin = origin
    return True


def _set_property(section: odml.Section, name: str, value, origin: str):
    """Set or create a property unconditionally."""
    prop = next((p for p in section.properties if p.name == name), None)
    if prop is not None:
        prop.values = [value]
    else:
        section.append(odml.Property(name=name, value=value, value_origin=origin))


def _format_duration(duration_s: float | None) -> str | None:
    if duration_s is None:
        return None
    return f"{duration_s:.1f} s" if duration_s < 60 else f"{duration_s / 60:.1f} min"


def _add_neo_metadata_section(session: odml.Section, neo_data: dict, value_origin: str = None):
    """Add or replace Neo Metadata subsection on a session (its properties
    with value_origin)."""
    if "Neo Metadata" in session.sections:
        session.remove(session["Neo Metadata"])

    neo_sec = odml.Section(name="Neo Metadata", type="neo.recording_metadata")
    # The raw file path is deliberately not recorded: the odML is published
    # and raw paths reveal local directories and animal names.
    for key in ("format", "block_index", "nb_block", "nb_segment", "duration_s"):
        if key in neo_data:
            neo_sec.append(odml.Property(name=key, value=neo_data[key], value_origin=value_origin))
    if streams := neo_data.get("signal_streams", []):
        neo_sec.append(odml.Property(name="sampling_rate_hz", value=streams[0]["sampling_rate"],
                                     value_origin=value_origin))

    # Streams as subsections
    by_stream = neo_data.get("stream_durations_s", {})
    for stream in neo_data.get("signal_streams", []):
        if (durations := by_stream.get(stream["id"])) is not None:
            stream = {**stream, "duration_s": float(sum(durations))}
        neo_sec.append(_dict_to_section(stream["name"], "neo.signal_stream", stream, value_origin))

    # Channel groups as subsections of a container
    for group_key, type_name in [
        ("signal_channels", "neo.signal_channel"),
        ("spike_channels", "neo.spike_channel"),
        ("event_channels", "neo.event_channel"),
    ]:
        items = neo_data.get(group_key, [])
        if not items and group_key != "signal_channels":
            continue
        container = odml.Section(name=group_key, type=f"neo.{group_key}")
        seen = {}
        for item in items:
            name = item["name"]
            seen[name] = seen.get(name, 0) + 1
            sec_name = name if seen[name] == 1 else f"{name}_{seen[name]}"
            container.append(_dict_to_section(sec_name, type_name, item, value_origin))
        neo_sec.append(container)

    session.append(neo_sec)


def _add_runs_section(neo_sec: odml.Section, neo_data: dict, runs: list[int], session_id: str,
                      value_origin: str = None):
    """Neo Metadata 'runs': per run, its Neo segment, duration and start time
    (ISO with offset), and in 'streams', by stream id, the duration and the
    start time of every stream whose values differ from the curated stream's
    (an auxiliary system at its own rate or started at another time).

    Not recorded (warning) unless the runs match the Neo segments one to one.
    """
    durations = neo_data.get("segment_durations_s") or []
    if len(durations) != len(runs):
        logger.warning(f"Session {session_id}: {len(runs)} runs but {len(durations)} "
                       f"Neo segment durations; run durations not recorded")
        return
    starts = neo_data.get("segment_start_times") or [None] * len(runs)
    by_stream = neo_data.get("stream_durations_s", {})
    stream_starts = neo_data.get("stream_start_times", {})
    container = odml.Section(name="runs", type="neo.runs")
    for index, (run, duration) in enumerate(zip(runs, durations)):
        # a run is one Neo segment: continuous (the session may not be)
        entry = {"run": int(run), "segment_index": index, "duration_s": duration, "continuity": "continuous"}
        if starts[index]:
            entry["start_time"] = starts[index]
        section = _dict_to_section(f"run {run}", "neo.run", entry, value_origin)
        others = {}
        for sid in sorted(set(by_stream) | set(stream_starts)):
            d, s = by_stream.get(sid, []), stream_starts.get(sid, [])
            own = {}
            if len(d) > index and d[index] != duration:
                own["duration_s"] = d[index]
            if len(s) > index and s[index] and s[index] != starts[index]:
                own["start_time"] = s[index]
            if own:
                others[sid] = own
        if others:
            streams = odml.Section(name="streams", type="neo.streams")
            for sid, own in others.items():
                streams.append(_dict_to_section(sid, "neo.stream", own, value_origin))
            section.append(streams)
        container.append(section)
    neo_sec.append(container)


def _dict_to_section(name: str, section_type: str, data: dict, value_origin: str = None) -> odml.Section:
    """Convert a flat dict to an odML section with properties."""
    sec = odml.Section(name=name, type=section_type)
    for k, v in data.items():
        sec.append(odml.Property(name=k, value=v, value_origin=value_origin))
    return sec


# CLI / IO


def dump(neo_info: dict, output_path: Path):
    with open(output_path, "w") as f:
        yaml.safe_dump(neo_info, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def main():
    parser = argparse.ArgumentParser(description="Extract Neo metadata for odML enrichment")
    parser.add_argument("file", help="Recording file path")
    parser.add_argument("--format", "-f", required=True, choices=list(RAW_FORMATS))
    parser.add_argument("--block-index", "-b", type=int, default=0, help="Block index (0-based)")
    parser.add_argument("--output", "-o", metavar="YAML", help="Save to YAML file")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    info = extract(args.file, args.format, block_index=args.block_index)

    if args.output:
        dump(info, Path(args.output))
        print(f"Saved to {args.output}")
    elif args.json:
        print(json.dumps(info, indent=2, default=str))
    else:
        channels = info["signal_channels"]
        print(f"Format: {info['format']} (block {info['block_index']}, {info['nb_segment']} segment(s))")
        for s in info["signal_streams"]:
            duration = sum(info["stream_durations_s"][s["id"]])
            print(f"  Stream '{s['name']}': {s['sampling_rate']:.0f} Hz, {s['n_channels']} ch, {duration:.1f} s")
        print(f"Channels ({len(channels)}): {', '.join(ch['name'] for ch in channels[:5])}...")
        print(f"Stream starts: {info['stream_start_times']}")
        ann = info.get("annotations", {})
        if ann.get("block") or ann.get("channel"):
            print(f"Annotations: block={list(ann.get('block', {}))}, channel={list(ann.get('channel', {}))}")


if __name__ == "__main__":
    main()
