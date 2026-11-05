"""Which device was where: Hardware/Probes entries and Electrode Placements.

A Hardware/Probes entry is one physical device (a probe of a model, a single
electrode of a microdrive). A placement is one use of a device in one session
(the session's Electrode Placements, or the subject's Implanted Probes for
chronic arrays), naming the device by its exact name; a session cannot name a
device twice. Each device has one id, derived over the dataset's device list,
in every session and every format (BIDS probe_name, NWB ElectrodeGroup,
ProbeInterface probe_id).
"""

from __future__ import annotations

from collections import Counter

import odml

from .metadata.subjects import session_subject
from .odml import props, section_at

_TO_MM = {"millimeter": 1.0, "micrometer": 1e-3, "centimeter": 10.0}

# Electrode group of the channels no placed probe is wired to (recorded, but
# no electrode in the brain for them in this session)
UNPLACED_GROUP = "unplaced"


def derive_probe_ids(probe_configs: list[dict]) -> list[str]:
    """Ids of the devices, from their models: the model name (spaces to
    underscores), numbered 01, 02, ... when several devices share it, and
    probeNN for a device without a model."""
    bases = [str(cfg.get("model") or "").strip().replace(" ", "_") for cfg in probe_configs]
    counts = Counter(b for b in bases if b)
    seen: dict[str, int] = {}
    ids = []
    for i, base in enumerate(bases):
        if not base:
            ids.append(f"probe{i + 1:02d}")
        elif counts[base] > 1:
            seen[base] = seen.get(base, 0) + 1
            ids.append(f"{base}{seen[base]:02d}")
        else:
            ids.append(base)
    return ids


def probe_ids(doc: odml.Document) -> dict[str, str]:
    """Id of every Hardware/Probes entry by name, derived over the dataset's
    whole device list, so a device has the same id in every session."""
    probes = section_at(doc, "Hardware/Probes")
    if probes is None:
        return {}
    configs = [props(s) for s in probes.sections]
    return dict(zip((str(c.get("name", "")) for c in configs), derive_probe_ids(configs)))


def _placement_dict(section: odml.Section) -> dict:
    """Properties, subsections (anatomical targets, DV by run) and the units
    of the properties ('_units') of one placement."""
    data = props(section)
    data["_units"] = {p.name: p.unit for p in section.properties if p.unit}
    for sub in section.sections:
        data[sub.name] = props(sub)
        data["_units"].update({sub.name: p.unit for p in sub.properties if p.unit})
    return data


def matched_probes(doc: odml.Document, session: odml.Section) -> list[tuple[int, odml.Section, dict]]:
    """(number, device section, placement data) per Electrode Placement of the
    session (else per Implanted Probe of its subject), in placement order;
    without placements every device with an empty placement.

    Raises ValueError when a placement names no Hardware/Probes entry or a
    device twice (one entry per physical device).
    """
    probes = section_at(doc, "Hardware/Probes")
    devices = {str(props(s).get("name", "")): s for s in probes.sections} if probes is not None else {}
    if not devices:  # no electrode devices (e.g. an imaging dataset)
        return []
    placements = section_at(session, "Electrode Placements")
    if placements is None:  # chronic: placements fixed at surgery, not per session
        placements = section_at(session_subject(doc, session) or session, "Surgical Preparation/Implanted Probes")
    if placements is None or not placements.sections:
        return [(i, sec, {}) for i, sec in enumerate(devices.values(), 1)]

    session_id = props(session).get("session id", session.name)
    names = [str(props(p).get("probe", "")) for p in placements.sections]
    if missing := sorted({n for n in names if n not in devices}):
        raise ValueError(f"Session {session_id}: placement names probe {', '.join(missing)}, "
                         f"not in Hardware/Probes ({', '.join(devices) or 'none'})")
    if repeated := sorted({n for n in names if names.count(n) > 1}):
        raise ValueError(f"Session {session_id}: probe {', '.join(repeated)} placed more than once; "
                         "list each physical device as its own Hardware/Probes entry")
    return [(number, devices[name], _placement_dict(section))
            for number, (name, section) in enumerate(zip(names, placements.sections), 1)]


def session_probe_ids(doc: odml.Document, matches: list) -> list[str]:
    """The id of each matched placement's device."""
    ids = probe_ids(doc)
    return [ids[str(props(ps).get("name", ""))] for _, ps, _ in matches]


def parse_channels(spec: str) -> list[int]:
    """'33-64, 70' (1-based positions in the curated stream) -> 0-based indices."""
    indices: list[int] = []
    for part in str(spec).replace(" ", "").split(","):
        first, _, last = part.partition("-")
        first, last = int(first), int(last or first)
        if first < 1 or last < first:
            raise ValueError(f"channels {spec!r}: positions are 1-based, ranges ascending")
        indices.extend(range(first - 1, last))
    if len(set(indices)) != len(indices):
        raise ValueError(f"channels {spec!r}: a channel is listed twice")
    return indices


def placement_position_mm(placement: dict, run: int = None) -> list[float] | None:
    """[ML, AP, DV] of a placement in mm, DV of the run when the device was
    moved during the session ('DV by run'); None unless all three are given."""
    units = placement.get("_units", {})
    by_run = placement.get("DV by run") or {}
    values = [(placement.get("ML"), units.get("ML")), (placement.get("AP"), units.get("AP")),
              (by_run[str(run)], units.get("DV by run")) if str(run) in by_run
              else (placement.get("DV"), units.get("DV"))]
    if any(v in (None, "") for v, _ in values):
        return None
    return [float(v) * _TO_MM[unit or "millimeter"] for v, unit in values]


def channel_group_names(probe_group, n_channels: int) -> list[str]:
    """The probe (its probe_id) of each recording channel of a session's
    ProbeInterface group; UNPLACED_GROUP for channels on no probe."""
    names = [UNPLACED_GROUP] * n_channels
    for probe in probe_group.probes:
        name = probe.annotations.get("probe_id", probe.name or "unknown")
        for i in (probe.device_channel_indices if probe.device_channel_indices is not None else []):
            if 0 <= i < n_channels:  # -1: a contact connected to no channel
                names[i] = name
    return names
