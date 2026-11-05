"""Registry-driven BIDS extraction from odML.

odML is the single source of truth: the registry (bids_mappings.yaml) names
the odML property behind every BIDS column and sidecar field, and this module
resolves them. Electrode names and positions come from the session's
ProbeInterface group, the same source as the NWB electrode table.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import odml
from probeinterface import ProbeGroup, read_probeinterface

from . import extractors
from .resolver import resolve, section_to_dict
from ..metadata.sessions import run_entry
from ..metadata.subjects import session_subject, subject_sections
from ..odml import props, section_at, subsection
from ..placements import matched_probes, probe_ids, session_probe_ids
from ..registry import load

logger = logging.getLogger(__name__)


def load_registry() -> dict:
    """The BIDS mappings registry (fairephys.registry.load)."""
    return load("bids_mappings.yaml")


def get_columns(tsv_name: str) -> list[str]:
    """Get ordered column list for a TSV file."""
    return list(load_registry().get(tsv_name, {}).get("columns", {}).keys())


def transform(value: Any, transform_name: str) -> Any:
    """The value through a named registry transform (hemisphere: left -> L);
    an empty value stays None."""
    if value in (None, ""):
        return None
    return load_registry().get("transforms", {}).get(transform_name, {}).get(value, value)


def coerce_array(field: str, value: Any) -> Any:
    """Ensure array fields are lists (from registry)."""
    array_fields = set(load_registry().get("coercions", {}).get("array_fields", []))
    if field in array_fields and isinstance(value, str):
        return [value]
    return value


def coerce_string(field: str, value: Any) -> Any:
    """Ensure string fields are strings (join arrays)."""
    string_fields = set(load_registry().get("coercions", {}).get("string_fields", []))
    if field in string_fields and isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return value


def coerce_enum(field: str, value: Any) -> Any:
    """The value of an enum field in the registry's spelling. An invalid value
    is an error naming the field and the allowed values, never a substitute."""
    allowed = load_registry().get("coercions", {}).get("enum_fields", {}).get(field)
    if allowed is None:
        return value
    match = next((a for a in allowed if str(a).lower() == str(value).lower()), None)
    if match is None:
        raise ValueError(f"{field}: {value!r} is not one of {', '.join(allowed)}")
    return match


def get_channel_type(name: str, channel_types: dict = None) -> str:
    """Determine BIDS channel type from config.

    Args:
        name: Channel name
        channel_types: Dict with 'prefixes', 'names', 'default' keys
                      If None, returns 'MISC' for all channels
    """
    if not channel_types:
        return "MISC"
    # Check prefixes first
    for prefix, ch_type in channel_types.get("prefixes", {}).items():
        if name.startswith(prefix):
            return ch_type
    # Check exact name matches
    return channel_types.get("names", {}).get(name, channel_types.get("default", "MISC"))


def _resolve_column(doc: odml.Document, spec: dict, sources: dict) -> Any:
    """The registry path of a column or field resolved, through its transform."""
    value = resolve(doc, spec.get("path"), sources)
    if t := spec.get("transform"):
        value = transform(value, t)
    return value


def _column_value(col: str, spec: dict, value: Any, required: list[str], where: str) -> Any:
    """The value a TSV column gets: the extracted value, else the registry
    default, else n/a; a required column with neither is an error."""
    if value not in (None, ""):
        return value
    if "default" in spec:
        return spec["default"]
    if col in required:
        raise ValueError(f"{where}: no value for the required column {col}")
    return "n/a"


# Per-contact anatomical target lookup
def _parse_contact_range(range_str: str) -> set[int]:
    """Parse '1-5' or '2,4,6' or '2-10' into a set of contact numbers."""
    contacts = set()
    for part in str(range_str).split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            contacts.update(range(int(lo), int(hi) + 1))
        elif part:
            contacts.add(int(part))
    return contacts


def _lookup_anatomical_targets(contact: int, targets: dict) -> str | None:
    """Collect all anatomical terms whose contact range includes this contact."""
    terms = [term for term, rng in targets.items()
             if rng and contact in _parse_contact_range(rng)]
    return ", ".join(terms) if terms else None


# Extraction functions
def extract_probes(doc: odml.Document, session: odml.Section) -> list[dict]:
    """probes.tsv rows: one per Electrode Placement of the session, named by
    the device's id; the columns come from the device entry, the placement
    and the session (registry paths)."""
    registry = load_registry()["probes_tsv"]
    columns, required = registry["columns"], registry.get("required", [])
    session_data = props(session)
    where = f"Session {session_data.get('session id', session.name)}"

    matches = list(matched_probes(doc, session))
    rows = []
    for (_, probe_sec, placement), probe_id in zip(matches, session_probe_ids(doc, matches)):
        sources = {"probe": props(probe_sec), "placement": placement, "session": session_data}
        row = {"probe_name": probe_id}
        for col, spec in columns.items():
            if col in row:
                continue
            value = _resolve_column(doc, spec, sources)
            row[col] = _column_value(col, spec, value, required, f"{where}, probe {probe_id}")
        rows.append(row)
    return rows


def _probe_group(group: ProbeGroup | Path | str) -> ProbeGroup:
    """The session's ProbeInterface group, given or read from its probe_group.json."""
    return read_probeinterface(str(group)) if isinstance(group, (str, Path)) else group


def _electrode_names(probe) -> tuple[str, list[str]]:
    """The probe's id and its electrode names {probe_id}_{contact id}: the
    names of electrodes.tsv and of the NWB electrode table."""
    probe_id = probe.annotations.get("probe_id", probe.name)
    if probe.contact_ids is None:
        raise ValueError(f"Probe {probe_id}: no contact ids in the probe group")
    return probe_id, [f"{probe_id}_{contact}" for contact in probe.contact_ids]


def extract_electrodes(doc: odml.Document, session: odml.Section,
                       group: ProbeGroup | Path | str) -> list[dict]:
    """electrodes.tsv rows: one per contact of each probe of the session's
    ProbeInterface group (a ProbeGroup or the path of its probe_group.json),
    named like the NWB electrodes and positioned in the probe plane (um, x
    from the probe's leftmost contact; z n/a for a planar probe). The other
    columns come from the device entry and the placement (registry paths);
    anatomical_targets from the placement's contact ranges (1-based positions).
    """
    registry = load_registry()["electrodes_tsv"]
    columns, required = registry["columns"], registry.get("required", [])
    where = f"Session {props(session).get('session id', session.name)}"

    matches = list(matched_probes(doc, session))
    placed = dict(zip(session_probe_ids(doc, matches), matches))
    rows = []
    for probe in _probe_group(group).probes:
        probe_id, names = _electrode_names(probe)
        if probe_id not in placed:
            raise ValueError(f"{where}: probe {probe_id} of the probe group has no Electrode Placement")
        _, probe_sec, placement = placed.pop(probe_id)
        sources = {"probe": props(probe_sec), "placement": placement}
        targets = placement.get("anatomical targets", {})
        positions = probe.contact_positions.astype(float)
        # local to the probe: its offset in the group removed (as NWB rel_x)
        xs = positions[:, 0] - float(probe.annotations.get("x_offset", positions[:, 0].min()))
        for position, (name, x, y) in enumerate(zip(names, xs, positions[:, 1]), 1):
            row = {"name": name, "probe_name": probe_id, "x": float(x), "y": float(y),
                   "z": float(positions[position - 1, 2]) if probe.ndim == 3 else "n/a"}
            for col, spec in columns.items():
                if col in row:
                    continue
                if spec.get("calculated") == "from_anatomical_targets":
                    value = _lookup_anatomical_targets(position, targets)
                else:
                    value = _resolve_column(doc, spec, sources)
                row[col] = _column_value(col, spec, value, required, f"{where}, electrode {name}")
            rows.append(row)
    for probe_id in placed:
        logger.warning("%s: placement of %s has no probe in the probe group, no electrodes", where, probe_id)
    return rows


def channel_electrodes(group: ProbeGroup | Path | str, n_channels: int) -> list[str | None]:
    """The electrode name of every channel position of the session: contact j
    of a probe, on device channel device_channel_indices[j] (a position in
    the session's signal channels), is the electrode {probe_id}_{contact id}
    of electrodes.tsv; None for a channel on no probe."""
    electrodes: list[str | None] = [None] * n_channels
    for probe in _probe_group(group).probes:
        probe_id, names = _electrode_names(probe)
        if probe.device_channel_indices is None:
            raise ValueError(f"Probe {probe_id}: no device channel indices in the probe group")
        for name, index in zip(names, probe.device_channel_indices):
            if index < 0:  # ProbeInterface: a contact not connected to any channel
                continue
            if index >= n_channels:
                raise ValueError(f"Probe {probe_id}: device channel {index} beyond the "
                                 f"{n_channels} channels of the session")
            electrodes[int(index)] = name
    return electrodes


def channel_electrode_map(group: ProbeGroup | Path | str, channel_names: list[str]) -> dict[str, str]:
    """{channel name: electrode name} of the channels wired to a probe
    (channel_electrodes keyed by name); names must be unique (streams that
    repeat channel names, e.g. SpikeGLX AP0 per probe, need the positions)."""
    if len(set(channel_names)) != len(channel_names):
        raise ValueError("channel names repeat across the session's streams; "
                         "use channel_electrodes (by position)")
    return {name: electrode for name, electrode
            in zip(channel_names, channel_electrodes(group, len(channel_names))) if electrode}


def extract_channels(session: odml.Section, group: ProbeGroup | Path | str = None, *,
                     reference_map: dict = None, channel_types: dict = None,
                     bad_channels: dict[str, str] = None) -> list[dict]:
    """channels.tsv rows: one per signal channel of the session's Neo Metadata
    (a session without it is an error).

    Args:
        session: odML session section
        group: the session's ProbeInterface group (or its probe_group.json):
            names the electrode of every channel wired to a contact, by
            channel position (channel_electrodes); without it every
            electrode_name is n/a.
        reference_map: {channel name: reference type} (build_probe_refs).
        channel_types: {'prefixes', 'names', 'default'} for the BIDS type.
        bad_channels: {channel name: description} of the channels the dataset
            declares bad; every other channel's status stays n/a.
    """
    registry = load_registry()["channels_tsv"]
    columns, required = registry["columns"], registry.get("required", [])
    session_id = props(session).get("session id", session.name)
    signal_channels = section_at(session, "Neo Metadata/signal_channels")
    if signal_channels is None:
        raise ValueError(f"Session {session_id}: no Neo Metadata; channels.tsv needs "
                         "the signal channels of the recording")
    channels = [props(s) for s in signal_channels.sections]
    names = [str(ch["name"]) for ch in channels]
    electrodes = channel_electrodes(group, len(names)) if group is not None else [None] * len(names)
    reference_map, bad_channels = reference_map or {}, bad_channels or {}

    rows = []
    for ch, name, electrode in zip(channels, names, electrodes):
        derived = {"electrode_name": electrode, "reference": reference_map.get(name),
                   "type": get_channel_type(name, channel_types),
                   "status": "bad" if name in bad_channels else None,
                   "status_description": bad_channels.get(name)}
        row = {}
        for col, spec in columns.items():
            if spec.get("derived"):
                value = derived.get(col)
            elif path := spec.get("path"):
                value = ch.get(path.split(".")[-1])
                if spec.get("type") == "float" and value not in (None, ""):
                    value = float(value)
            else:
                value = None
            row[col] = _column_value(col, spec, value, required, f"Session {session_id}, channel {name}")
        rows.append(row)
    return rows


def extract_participants(doc: odml.Document) -> list[dict]:
    """One participants.tsv row per subject, the id with the registry's prefix."""
    registry = load_registry()["participants_tsv"]
    columns, required = registry["columns"], registry.get("required", [])
    prefix = columns["participant_id"].get("prefix", "")
    rows = []
    for subject in subject_sections(doc):
        sub_id = str(props(subject).get("subject id") or "").replace(" ", "")
        if not sub_id:
            raise ValueError(f"Subject section {subject.name}: no subject id (participant_id)")
        row = {"participant_id": f"{prefix}{sub_id}"}
        for col, spec in columns.items():
            if col in row:
                continue
            value = _resolve_column(doc, spec, {"subject": subject})
            row[col] = _column_value(col, spec, value, required, f"Subject {sub_id}")
        rows.append(row)
    return rows


def extract_dataset_description(doc: odml.Document) -> dict:
    """Extract dataset_description.json fields."""
    fields = load_registry()["dataset_description_json"]["fields"]
    dataset = doc["Dataset"]
    dataset_data = section_to_dict(dataset)
    sources = {"dataset": dataset_data}

    extractor_map = {
        "author_name": lambda: extractors.authors(dataset),
        "funding_list": lambda: extractors.funding(dataset),
        "acknowledgements": lambda: extractors.acknowledgements(dataset, dataset_data),
        "ethics_approvals": lambda: extractors.ethics_approvals(dataset_data),
        "references_and_links": lambda: extractors.references_and_links(dataset, dataset_data),
        "generated_by": lambda: extractors.generated_by(doc),
        "source_datasets": lambda: extractors.source_datasets(dataset),
    }

    result = {}
    for field, spec in fields.items():
        if extract_type := spec.get("extract"):
            if val := extractor_map[extract_type]():
                result[field] = val
            continue
        if "default" in spec:
            result[field] = spec["default"]
        if val := resolve(doc, spec.get("path"), sources):
            result[field] = coerce_array(field, val)
    return result


def build_probe_refs(doc: odml.Document, session: odml.Section = None) -> dict:
    """Build probe reference type lookup: probe_id -> reference_type (None
    when the probe names none: n/a in channels.tsv).

    When *session* is provided, only probes matching the session's Electrode
    Placements are included (consistent with extract_probes/extract_electrodes).
    """

    refs = {}
    if session is not None:
        matches = list(matched_probes(doc, session))
        ids = session_probe_ids(doc, matches)
        for (_, probe_sec, _), pid in zip(matches, ids):
            probe_data = section_to_dict(probe_sec)
            refs[pid] = probe_data.get("reference type")
    else:
        ids = probe_ids(doc)
        for sec in doc["Hardware"]["Probes"].sections:
            probe_data = section_to_dict(sec)
            refs[ids[str(probe_data.get("name", ""))]] = probe_data.get("reference type")
    return refs


def _aux_device(doc: odml.Document, name: str) -> odml.Section:
    """The Hardware/Auxiliary Devices entry of that name."""
    devices = section_at(doc, "Hardware/Auxiliary Devices")
    for sec in (devices.sections if devices is not None else []):
        if props(sec).get("name") == name:
            return sec
    raise KeyError(f"Auxiliary device {name!r} not in Hardware/Auxiliary Devices")


def _aux_sources(doc: odml.Document, device: odml.Section, rec_sys: dict) -> tuple[dict, odml.Section]:
    """The device's fields as the 'aux_device' source and the section whose
    Hardware Filters apply to it. With 'same as recording system: true' the
    device identity (and its filters, unless it lists its own) are the
    Recording System's."""
    data = props(device)
    filters_root = device
    if data.get("same as recording system") in (True, "true"):
        data.update(manufacturer=rec_sys.get("manufacturer"),
                    model=rec_sys.get("system model") or rec_sys.get("processor model"),
                    **{"serial number": rec_sys.get("serial number"),
                       "software version": rec_sys.get("software version")})
        if subsection(device, "Hardware Filters") is None:
            filters_root = doc["Hardware"]
    return data, filters_root


def extract_sidecar_fields(doc: odml.Document, session: odml.Section,
                           registry_key: str, device_name: str = None,
                           run: int = None, stream: str = None) -> dict:
    """Extract BIDS sidecar fields from odML using registry mappings.

    Generic extractor that works for any modality (ecephys, motion, emg,
    coordsystem, etc.). Registry section must have a 'fields' dict mapping
    BIDS field names to path/extract/default specs. A field with no value and
    no default is left out; an enum field with an invalid value is an error.

    Args:
        doc: odML document
        session: odML session section
        registry_key: Key in bids_mappings.yaml (e.g., 'ecephys_json', 'motion_json')
        device_name: Name of auxiliary device in Hardware/Auxiliary Devices.
            When provided, loads the named device as the 'aux_device' source
            and reads HardwareFilters from the device's own Hardware Filters.
        run: BIDS run of the sidecar (several runs in a session): loads the
            run's own entry in Neo Metadata 'runs' as the 'run' source.
        stream: Neo stream id or name of the sidecar's data file, when a
            session writes one file per stream (e.g. one per Neuropixels
            probe): loads its Neo Metadata stream section as the 'stream'
            source (its own sampling rate and duration).
    """
    registry = load_registry()
    fields = registry.get(registry_key, {}).get("fields")
    if not fields:
        return {}

    neo = subsection(session, "Neo Metadata")
    sources = {
        "rec_sys": props(doc["Hardware"]["Recording System"]),
        "session": props(session),
        "dataset": props(doc["Dataset"]),
        # "Subject/..." paths resolve through the session's own subject
        "subject": session_subject(doc, session),
        "neo": props(neo) if neo is not None else {},
        "stream": _stream_entry(neo, stream) if stream is not None else {},
    }
    sources["run"] = run_entry(session, run, sources["stream"].get("id")) if run is not None else {}
    filters_root = doc["Hardware"]
    if device_name:
        sources["aux_device"], filters_root = _aux_sources(doc, _aux_device(doc, device_name), sources["rec_sys"])

    inst = extractors.institution_info(doc)
    extractor_map = {
        "institution_name": lambda: inst.get("InstitutionName"),
        "institution_address": lambda: inst.get("InstitutionAddress"),
        "institution_department": lambda: inst.get("InstitutionalDepartmentName"),
        "hardware_filters": lambda: extractors.hardware_filters(filters_root),
        "stimulus_presentation": lambda: extractors.stimulus_presentation(doc),
    }

    result = {}
    for field, spec in fields.items():
        if extract_type := spec.get("extract"):
            if val := extractor_map[extract_type]():
                result[field] = val
            continue
        if "default" in spec:
            result[field] = spec["default"]
        if val := _resolve_column(doc, spec, sources):
            result[field] = val

    for key in result:
        result[key] = coerce_enum(key, coerce_string(key, coerce_array(key, result[key])))
    for key in registry.get("coercions", {}).get("float_fields", []):
        if key in result:
            result[key] = float(result[key])
    return result


def find_stream(neo: odml.Section | None, stream: str) -> dict | None:
    """The Neo Metadata section of a signal stream (by id or name), None
    when the session's Neo Metadata has no such stream."""
    for sec in (neo.sections if neo is not None else []):
        data = props(sec)
        if sec.type == "neo.signal_stream" and stream in (str(data.get("id")), str(data.get("name"))):
            return data
    return None


def _stream_entry(neo: odml.Section | None, stream: str) -> dict:
    if (data := find_stream(neo, stream)) is None:
        raise KeyError(f"No Neo Metadata stream {stream!r}")
    return data


def extract_ecephys_metadata(doc: odml.Document, session: odml.Section,
                             run: int = None, stream: str = None) -> dict:
    """Extract ecephys.json fields (of one run when a session has several)."""
    return extract_sidecar_fields(doc, session, "ecephys_json", run=run, stream=stream)


def extract_events_metadata(doc: odml.Document, session: odml.Section) -> dict:
    """Extract events.json sidecar fields (e.g., StimulusPresentation)."""
    return extract_sidecar_fields(doc, session, "events_json")


def processing_states(step: odml.Section) -> list[dict]:
    """States listed by one Data Processing step, as dicts (label, code, description).

    A step lists them under 'states', on the step or in one of its blocks
    (e.g. 'annotation'): objects {label: the BIDS trial_type value, code: the
    lab's raw code, description}, or plain labels.
    """
    states = []
    for sec in (step, *step.sections):
        if "states" in sec.sections:
            states += [section_to_dict(s) for s in sec["states"].sections]
        elif "states" in sec.properties:
            states += [{"label": v} for v in sec.properties["states"].values]
    return [s for s in states if s.get("label") not in (None, "")]


def _event_states(doc: odml.Document) -> list[dict]:
    """States of all Data Processing steps, in order."""
    steps = subsection(doc, "Data Processing")
    if steps is None:
        return []
    return [s for step in steps.sections for s in processing_states(step)]


def extract_event_levels(doc: odml.Document) -> dict:
    """events.json trial_type Levels {label: description} from the processing states."""
    return {str(s["label"]): str(s.get("description", "")).strip() for s in _event_states(doc)}


def extract_event_codes(doc: odml.Document) -> dict:
    """The lab's raw event code -> trial_type label, for the states that have a code."""
    return {str(s["code"]): str(s["label"]) for s in _event_states(doc)
            if s.get("code") not in (None, "")}
