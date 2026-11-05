"""Auxiliary signals (eye tracking, motion, EMG, other physio) as BIDS files.

Hardware/Auxiliary Devices declares each device with a modality and its raw
channel names; the session's Neo Metadata says which of those channels were
recorded. From that, the sidecars (physio.json, motion.json, emg.json and
their channels.tsv) and the data files (physio.tsv.gz, motion.tsv, emg.edf).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import numpy as np
import odml

from .builder import extract_sidecar_fields, find_stream
from .emg import generate_emg_channels_tsv, generate_emg_json, write_emg_edf
from .motion import generate_motion_channels_tsv, generate_motion_json, write_motion_data
from .naming import bids_filename, session_path
from .physio import generate_physio_json, write_physio_data
from ..metadata.sessions import first_sample_time
from ..odml import props, section_at, subsection

# registry section of each modality's sidecar (eye tracking is its own physio)
_SIDECAR_KEYS = {"motion": "motion_json", "emg": "emg_json", ("physio", "eye"): "eyetrack_json"}
# BIDS datatype folder of each modality (physio files sit with the ecephys data)
_DATATYPES = {"physio": "ecephys", "motion": "motion", "emg": "emg"}


def extract_aux_groups(doc: odml.Document, session: odml.Section,
                       run: int = None) -> list[dict]:
    """The auxiliary channel groups of a session: one per Hardware/Auxiliary
    Devices entry with a modality whose declared channels the session's Neo
    signal channels hold (a device with none of its channels in the session
    is left out; a session without Neo Metadata is an error).

    Each group: name, modality, channels, recording, tracksys, matched (the
    channels' Neo dicts), sampling_rate (as recorded), sidecar_fields (the
    modality's registry fields, of the given run when a session has several)
    and start (the time of the first sample of the device's stream in the run,
    metadata.sessions.first_sample_time).
    """
    devices = section_at(doc, "Hardware/Auxiliary Devices")
    if devices is None:
        return []
    signal_channels = section_at(session, "Neo Metadata/signal_channels")
    if signal_channels is None:
        session_id = props(session).get("session id", session.name)
        raise ValueError(f"Session {session_id}: no Neo Metadata; the auxiliary devices' "
                         "channels cannot be matched")
    neo_by_name = {str(ch["name"]): ch for ch in (props(s) for s in signal_channels.sections)}

    groups = []
    for sec in devices.sections:
        cfg = props(sec)
        modality, names = cfg.get("modality"), cfg.get("channels")
        if not modality or not names:
            continue
        names = [names] if isinstance(names, str) else [str(n) for n in names]
        matched = [neo_by_name[n] for n in names if n in neo_by_name]
        if not matched:
            continue
        key = _SIDECAR_KEYS.get((modality, cfg.get("recording")), _SIDECAR_KEYS.get(modality))
        # the device's own stream: its duration in the run when it differs from the curated stream's
        stream = str(matched[0].get("stream_id", ""))
        stream = stream if find_stream(subsection(session, "Neo Metadata"), stream) else None
        fields = (extract_sidecar_fields(doc, session, key, device_name=cfg["name"], run=run, stream=stream)
                  if key else {})
        rate = float(matched[0]["sampling_rate"])
        groups.append({
            "name": cfg["name"], "modality": modality, "channels": names,
            "recording": cfg.get("recording"), "tracksys": cfg.get("tracksys"),
            "matched": matched, "sampling_rate": int(rate) if rate.is_integer() else rate,
            "sidecar_fields": fields, "start": first_sample_time(doc, session, run, stream),
        })
    return groups


def _prefix(group: dict, sub: str, session_id: str, task: str, run: int = None) -> str:
    """The file name of the group's files before the suffix (physio files
    carry the recording entity, motion files the tracksys entity)."""
    modality = group["modality"]
    return bids_filename(sub, "", "", ses=session_id, task=task, run=run,
                         recording=group.get("recording") if modality == "physio" else None,
                         tracksys=group.get("tracksys") if modality == "motion" else None).rstrip("_.")


def _target_dir(group: dict, bids_root: Path, sub: str, session_id: str) -> Path:
    """The datatype folder of the group's files, created when missing."""
    path = session_path(bids_root, sub, session_id, _DATATYPES[group["modality"]])
    path.mkdir(parents=True, exist_ok=True)
    return path


def _units(channel: dict) -> str:
    return str(channel.get("units") or "n/a")


# the BIDS eye-tracking physio columns of an eye tracker's first two channels
EYE_POSITION_COLUMNS = ("x_coordinate", "y_coordinate")


def eye_columns(group: dict, roles: dict[str, str] | None) -> dict[str, str]:
    """{raw channel: BIDS physio column} of an eye-tracker group: the caller's
    `roles` (the dataset names each channel's column from its YAML, e.g.
    pupil_size), else the two position columns for a device of one or two
    channels (horizontal, then vertical, as the template states); a third
    channel without a role, or a role of a channel the device lacks, is an
    error (no column is guessed from a position)."""
    channels = list(group["channels"])
    roles = dict(roles or {})
    if unknown := [c for c in roles if c not in channels]:
        raise ValueError(f"eye tracker '{group['name']}': roles for channels it lacks: {unknown}")
    if not roles:
        if len(channels) > 2:
            raise ValueError(f"eye tracker '{group['name']}': {len(channels)} channels; name the column of each "
                             "(x_coordinate, y_coordinate, pupil_size) in eye_columns, none is taken by position")
        roles = dict(zip(channels, EYE_POSITION_COLUMNS))
    if missing := [c for c in channels if c not in roles]:
        raise ValueError(f"eye tracker '{group['name']}': no column named for the channels {missing}")
    return roles


def _physio_sidecar(group: dict, directory: Path, prefix: str, task: str, start_time: float,
                    eye_roles: dict[str, str] | None = None) -> None:
    matched = group["matched"]
    is_eye = group.get("recording") == "eye"
    if is_eye:
        # BIDS eye-tracking physio mandates its column names: each raw channel's
        # column from eye_columns, in the channels' order
        roles = eye_columns(group, eye_roles)
        units = {"timestamp": "s", **{roles[ch["name"]]: _units(ch) for ch in matched if ch["name"] in roles}}
    else:
        units = {ch["name"]: _units(ch) for ch in matched}
    generate_physio_json(
        list(units), group["sampling_rate"], units=units, start_time=start_time,
        physio_type="eyetrack" if is_eye else "generic", task_name=task,
        extra_fields=group["sidecar_fields"] or None,
        output_path=directory / f"{prefix}_physio.json",
    )


def _motion_sidecar(group: dict, directory: Path, prefix: str, task: str, start_time: float, **_) -> None:
    channels = [{"name": ch["name"], "component": ch["name"][-1].lower(), "type": "POS",
                 "tracked_point": group["tracksys"], "units": _units(ch)} for ch in group["matched"]]
    generate_motion_json(
        group["sampling_rate"], task, channels, tracking_system=group["tracksys"],
        extra_fields=group["sidecar_fields"], output_path=directory / f"{prefix}_motion.json",
    )
    generate_motion_channels_tsv(channels, directory / f"{prefix}_channels.tsv")


def _emg_sidecar(group: dict, directory: Path, prefix: str, task: str, start_time: float, **_) -> None:
    fields = group["sidecar_fields"]
    if not fields.get("PowerLineFrequency"):
        raise ValueError(f"EMG device '{group['name']}': power line frequency not set in hardware.yaml")
    generate_emg_json(
        group["sampling_rate"], task, len(group["matched"]),
        power_line_freq=fields["PowerLineFrequency"], extra_fields=fields,
        output_path=directory / f"{prefix}_emg.json",
    )
    # status stays n/a: a channel is "good" or "bad" only when the lab says so
    rows = [{"name": ch["name"], "type": "EMG", "units": _units(ch),
             "sampling_frequency": ch["sampling_rate"], "status": "n/a"} for ch in group["matched"]]
    generate_emg_channels_tsv(rows, directory / f"{prefix}_channels.tsv")


_SIDECARS = {"physio": _physio_sidecar, "motion": _motion_sidecar, "emg": _emg_sidecar}


def generate_aux_sidecars(
    groups: list[dict],
    sub: str,
    session_id: str,
    bids_root: Path,
    task: str,
    start_time: float = 0.0,
    run: int = None,
    eye_columns: dict[str, str] = None,
) -> None:
    """The BIDS sidecars of a session's auxiliary groups (extract_aux_groups),
    by modality: physio.json, motion.json with its channels.tsv, emg.json with
    its channels.tsv. ``start_time`` (s) is the start of the auxiliary data
    relative to the neural recording (physio StartTime); ``run`` adds the BIDS
    run entity (several runs of one task in a session); ``eye_columns`` names
    the BIDS column of each eye-tracker channel (see eye_columns())."""
    for group in groups:
        _SIDECARS[group["modality"]](group, _target_dir(group, bids_root, sub, session_id),
                                     _prefix(group, sub, session_id, task, run), task, start_time,
                                     eye_roles=eye_columns)


def _eye_data(group: dict, data: dict[str, np.ndarray], roles: dict[str, str] | None) -> dict[str, np.ndarray]:
    """The BIDS eye-tracking physio columns (eye_columns) of the raw channels
    present, in the channels' order, with a timestamp column."""
    columns = eye_columns(group, roles)
    n = len(next(iter(data.values())))
    return {"timestamp": np.arange(n, dtype="float64") / group["sampling_rate"],
            **{columns[name]: data[name] for name in group["channels"] if name in data}}


def write_aux_bids_data(
    groups: list[dict],
    aux_data: dict,
    sub: str,
    session_id: str,
    bids_root: Path,
    sampling_rate: float = None,
    *,
    task: str,
    run: int = None,
    start_time: float = 0.0,
    eye_columns: dict[str, str] = None,
) -> None:
    """The data files of the auxiliary groups whose channels ``aux_data``
    ({channel name: 1-D array}) holds: physio.tsv.gz (eye tracking in the
    BIDS columns ``eye_columns`` names per channel, see eye_columns(), with a
    timestamp column), motion.tsv, emg.edf (at
    ``sampling_rate``, else the group's; channel units from the matched Neo
    channels; the EDF start the group's start plus ``start_time``, the
    seconds from the neural recording's first sample to the auxiliary
    data's, as in generate_aux_sidecars). ``run``: several runs of one task
    in a session, else None."""
    for group in groups:
        present = [n for n in group["channels"] if n in aux_data]
        if not present:
            continue
        data = {n: aux_data[n] for n in present}
        directory = _target_dir(group, bids_root, sub, session_id)
        prefix = _prefix(group, sub, session_id, task, run)
        modality = group["modality"]
        if modality == "physio":
            if group.get("recording") == "eye":
                data = _eye_data(group, data, eye_columns)
            write_physio_data(data, directory / f"{prefix}_physio.tsv.gz")
        elif modality == "motion":
            write_motion_data(data, directory / f"{prefix}_motion.tsv")
        elif modality == "emg":
            units = {ch["name"]: ch.get("units") for ch in group["matched"] if ch["name"] in data}
            if missing := [n for n in present if not units.get(n)]:
                raise ValueError(f"EMG device '{group['name']}': no units in the Neo metadata "
                                 f"of channel(s) {', '.join(missing)}")
            start = group.get("start") and group["start"] + timedelta(seconds=start_time)
            write_emg_edf(data, sampling_rate or group["sampling_rate"],
                          directory / f"{prefix}_emg.edf", units=units, start=start)
