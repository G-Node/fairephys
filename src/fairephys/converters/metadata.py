"""Build NWB metadata dict from odML.

Registry-driven: field mappings in nwb_mappings.yaml, transform functions here.
Two public functions:
    load_nwb_registry()  load the NWB field mappings registry
    build_nwb_metadata() build neuroconv metadata dict from odML document

The Ecephys metadata uses neuroconv's list format (Ecephys.Device and
Ecephys.ElectrodeGroup as lists of dicts), which neuroconv 0.10 deprecates
for the dict format (Devices and Ecephys.ElectrodeGroups keyed by metadata
key, DeviceModels, ElectricalSeries by metadata key) and removes on or after
August 2027 (its FutureWarning on every conversion).
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import odml
import yaml

from ..bids.resolver import section_to_dict
from ..metadata.identifiers import related_publications
from ..metadata.sessions import start_datetime
from ..metadata.subjects import session_subject
from ..bids.builder import load_registry
from ..export.readme import hardware_filters_text
from ..odml import prop, props, section_at
from ..placements import matched_probes, placement_position_mm, session_probe_ids

logger = logging.getLogger(__name__)

NWB_REGISTRY_PATH = (
    Path(__file__).parent.parent / "_resources" / "registry" / "nwb_mappings.yaml"
)


@lru_cache(maxsize=1)
def load_nwb_registry() -> dict:
    """Load NWB field mappings registry."""
    with open(NWB_REGISTRY_PATH) as f:
        return yaml.safe_load(f)


# Source resolution

def _resolve_sources(doc: odml.Document, session: odml.Section, run: int = None) -> dict[str, dict]:
    """Pre-flatten odML sections into dicts for registry lookup; 'run' is
    the run's entry in the session's Neo Metadata 'runs' ({} without one)."""
    dataset = section_at(doc, "Dataset")
    subject = session_subject(doc, session)
    runs = section_at(session, "Neo Metadata/runs")
    entries = [props(s) for s in runs.sections] if runs is not None else []
    return {
        "session": section_to_dict(session),
        "Dataset": section_to_dict(dataset) if dataset is not None else {},
        "Subject": section_to_dict(subject) if subject is not None else {},
        "run": next((e for e in entries if e.get("run") == run), {}) if run is not None else {},
    }


def _lookup(sources: dict[str, dict], spec: dict) -> Any:
    """Resolve a value from sources using a registry field spec."""
    for block in (spec, spec.get("fallback", {})):
        src = sources.get(block.get("source", ""), {})
        for key in block.get("keys", []):
            if (val := src.get(key)) is not None:
                return val
    return spec.get("default")


def _require(value: Any, field: str, spec: dict, where: str) -> Any:
    """The value, or an error naming what to fill when NWB requires the field."""
    if value is not None or not spec.get("required"):
        return value
    fill = spec.get("hint") or ", ".join(f"'{k}'" for k in spec.get("keys", []))
    raise ValueError(f"{where}: NWB {field} is required; fill {fill} in the metadata")


# Named extractors (referenced by "extract" in registry)

def _extract_session_start_time(doc: odml.Document, session, sources, _args) -> datetime:
    """Start of the NWB file in the dataset time zone: the run's own start
    (Neo Metadata runs start_time) when the file holds one run of several,
    else the session's 'start time'.

    A start time is a time of day or a datetime: local time in the Dataset
    'time zone' unless it carries an offset (e.g. UTC from a raw header).
    Without one, or without a date, the session cannot be written.
    """
    sess = sources["session"]
    sid = sess.get("session id", "?")
    tz_name = sources["Dataset"].get("time zone")
    if not tz_name:
        logger.warning("Dataset 'time zone' not set; session start times are taken as UTC")
    if (day := _parse_datetime(sess.get("date"))) is None:
        raise ValueError(f"Session {sid}: no date, cannot set the NWB session start time")
    start = sources["run"].get("start_time") or sess.get("start time")
    if start in (None, ""):
        raise ValueError(f"Session {sid}: no start time (the session 'start time', or the run's "
                         "start_time in Neo Metadata runs); NWB session_start_time is required")
    return start_datetime(start, day.date(), tz_name)


def _extract_experimenters(doc: odml.Document, _session, sources, _args) -> list[str] | None:
    """Who performed the recording, as 'Family, Given' (DANDI): the session's
    'performed by' (family names, completed from the Dataset contributors),
    else the contributors with a 'data collection' role; never the curators.
    """
    contributors = section_at(doc, "Dataset/Contributors")
    people = [props(c) for c in contributors.sections] if contributors is not None else []
    performed = sources["session"].get("performed by")
    if performed not in (None, ""):
        chosen = []
        for name in (performed if isinstance(performed, list) else [performed]):
            person = next((p for p in people if p.get("family name") == name), None)
            if person is None:
                logger.warning(f"Session {sources['session'].get('session id')}: 'performed by' "
                               f"{name!r} is no family name of Dataset/Contributors")
            chosen.append(person or {"family name": name})
    else:
        chosen = [p for p in people if "data collection" in _as_list(p.get("roles"))]
    names = [", ".join(filter(None, [p.get("family name"), p.get("given name")])) for p in chosen]
    return [n for n in names if n] or None


def _as_list(value) -> list:
    return value if isinstance(value, list) else [] if value in (None, "") else [value]


def _extract_affiliation(doc: odml.Document, _session, _sources, args) -> str | None:
    """Get lab or institution from Custodian affiliation."""
    custodian = section_at(doc, "Dataset/Custodian")
    for sec in (custodian.sections if custodian is not None else []):
        if sec.type == "core.Affiliation":
            return section_to_dict(sec).get(args.get("field", "lab"))
    return None


def _extract_age_iso8601(doc: odml.Document, session, _sources, _args) -> str | None:
    """Age with its unit as an ISO 8601 duration (P7Y, P90D, P7.5Y)."""
    if (subject := session_subject(doc, session)) is None:
        return None
    value = prop(subject, "age")
    if value in (None, ""):
        return None
    unit_map = load_nwb_registry()["transforms"]["age_unit"]
    unit = subject.properties["age"].unit or ""
    if unit not in unit_map:
        raise ValueError(f"Subject {prop(subject, 'subject id')}: age unit {unit!r} is not one of "
                         f"{list(unit_map)}; NWB age is an ISO 8601 duration")
    number = float(value)
    return f"P{int(number) if number.is_integer() else number}{unit_map[unit]}"


def _extract_uuid(_doc, _session, _sources, _args) -> str:
    """Random identifier, unique for every NWB file."""
    return str(uuid.uuid4())


def _extract_related_publications(doc: odml.Document, _session, _sources, _args) -> list[str] | None:
    """Every Related Publications entry: "doi:<DOI>" for a DOI, else the
    identifier as given, the citation for an entry without identifier."""
    pubs = [f"doi:{p.identifier.value}" if p.identifier and p.identifier.type == "DOI" else p.given or p.citation
            for p in related_publications(section_at(doc, "Dataset"))]
    return pubs or None


def _extract_weight(doc: odml.Document, session, _sources, _args) -> str | None:
    """Weight with unit, e.g. "9.5 kg"."""
    if (subject := session_subject(doc, session)) is None:
        return None
    value = prop(subject, "weight")
    if value in (None, ""):
        return None
    unit = subject.properties["weight"].unit or ""
    unit_map = load_nwb_registry().get("transforms", {}).get("weight_unit", {})
    return f"{value} {unit_map.get(unit, unit)}".strip()


_EXTRACTORS = {
    "session_start_time": _extract_session_start_time,
    "uuid": _extract_uuid,
    "related_publications": _extract_related_publications,
    "weight": _extract_weight,
    "experimenters": _extract_experimenters,
    "affiliation": _extract_affiliation,
    "age_iso8601": _extract_age_iso8601,
}


# Transforms and coercions

def _apply_transform(value: Any, transform_name: str, registry: dict) -> Any:
    """Apply a vocabulary transform from the registry (None when unmapped)."""
    if value is None:
        return None
    table = registry.get("transforms", {}).get(transform_name, {})
    return table.get(str(value).lower())


def _apply_coerce(value: Any, coerce_type: str) -> Any:
    """Apply type coercion (e.g. wrap scalar in list)."""
    if coerce_type == "array" and value is not None:
        return value if isinstance(value, list) else [value]
    return value


# Section builders

def _build_section(section_name: str, doc, session, sources, registry) -> dict:
    """Build a metadata dict for one NWB section using registry mappings."""
    result = {}
    where = f"Session {sources['session'].get('session id', session.name)}"
    for field, spec in registry.get(section_name, {}).items():
        if field.startswith("_"):
            continue

        if extract_name := spec.get("extract"):
            fn = _EXTRACTORS.get(extract_name)
            if not fn:
                continue
            value = fn(doc, session, sources, spec.get("args", {}))
        else:
            value = _lookup(sources, spec)

        if transform := spec.get("transform"):
            value = _apply_transform(value, transform, registry)
        if (post := spec.get("post")) and isinstance(value, str):
            if post == "strip_spaces":
                value = value.replace(" ", "")
        if coerce := spec.get("coerce"):
            value = _apply_coerce(value, coerce)

        if _require(value, field, spec, where) is not None:
            result[field] = value

    return result


# Ecephys extractors (referenced by "extract" in Ecephys registry specs)

def _extract_device_description(data: dict, **_) -> str | None:
    """'{manufacturer} {model}'.strip() or None."""
    return f"{data.get('manufacturer', '')} {data.get('model', '')}".strip() or None


def _extract_probe_description(data: dict, **_) -> str | None:
    """'{manufacturer} {model}'.strip(), else the name, else None."""
    return f"{data.get('manufacturer', '')} {data.get('model', '')}".strip() or data.get("name") or None


def _extract_canonical_probe_id(data: dict, ctx: dict = None, idx: int = 0, **_) -> str:
    """Derived probe ID from derive_probe_ids()."""
    return ctx["ids"][idx]


def _extract_electrode_group_description(data: dict, ctx: dict = None, placement: dict = None, **_) -> str | None:
    """'N channel type' (what the probe section gives), and the frame of the
    position when there is one; None without contacts or type."""
    n, kind = data.get("number of contacts"), data.get("probe type")
    if n and kind:
        text = f"{n} channel {kind}"
    elif n or kind:
        text = f"{n} channels" if n else str(kind)
    else:
        return None
    if placement and placement_position_mm(placement, (ctx or {}).get("run")) is not None:
        reference = placement.get("anatomical reference") or "the placement's reference"
        text += f"; position (x, y, z) = (ML, AP, DV) in mm from {reference}"
    return text


def location_placeholder() -> str | None:
    """The ElectrodeGroup location written for a placement without a brain
    region: the `placeholder` the registry declares for the field (neuroconv's
    "unknown", see _extract_placement_location), None when it declares none."""
    return load_nwb_registry()["Ecephys"]["ElectrodeGroup"]["fields"]["location"].get("placeholder")


def _extract_placement_location(data: dict, placement: dict = None, spec: dict = None, **_) -> str | None:
    """Brain region (else target) and hemisphere of the placement, "<region>,
    <hemisphere> hemisphere". A placement without a region gets the field's
    registry `placeholder` in the region's place (neuroconv's "unknown", its
    placeholder for the location the NWB schema requires, kept so that it is
    easy to identify downstream: neuroconv.tools.spikeinterface
    _get_ecephys_metadata_placeholders), with the hemisphere when given, and
    is logged as a warning; it is a placeholder of the target format, not a
    value of ours, and BIDS and openMINDS keep the region empty. None
    without a placement, or without a region and a placeholder."""
    if not placement:
        return None
    location = placement.get("brain region") or placement.get("target")
    if not location:
        if not (location := (spec or {}).get("placeholder")):
            return None
        logger.warning(f"Placement of probe {data.get('name')!r}: no brain region; the NWB ElectrodeGroup "
                       f"location takes neuroconv's placeholder {location!r} (state it in the curation README)")
    if h := placement.get("hemisphere"):
        location = f"{location}, {h} hemisphere"
    return location


def _extract_placement_position(data: dict, ctx: dict = None, placement: dict = None, **_) -> list | None:
    """[ML, AP, DV] in mm of the placement (the run's DV when the device was
    moved during the session), None unless all three are given."""
    return placement_position_mm(placement, (ctx or {}).get("run")) if placement else None


def _extract_hardware_filters(data: dict, section: odml.Section = None, **_) -> str | None:
    """The analog filters of the source section, as the data descriptor states them."""
    return hardware_filters_text(section) if section is not None else None


_ECEPHYS_EXTRACTORS = {
    "device_description": _extract_device_description,
    "probe_description": _extract_probe_description,
    "canonical_probe_id": _extract_canonical_probe_id,
    "electrode_group_description": _extract_electrode_group_description,
    "placement_location": _extract_placement_location,
    "placement_position": _extract_placement_position,
    "hardware_filters": _extract_hardware_filters,
}


def _ecephys_resolve_field(
    fspec: dict, data: dict, ctx: dict = None, idx: int = 0, placement: dict = None, section: odml.Section = None,
) -> Any:
    """Resolve a single ecephys field from its spec."""
    if extract_name := fspec.get("extract"):
        fn = _ECEPHYS_EXTRACTORS.get(extract_name)
        return fn(data, ctx=ctx, idx=idx, placement=placement, spec=fspec, section=section) if fn else None
    for key in fspec.get("keys", []):
        if (val := data.get(key)) is not None:
            return val
    return fspec.get("default")


def _build_ecephys_single(doc: odml.Document, spec: dict) -> dict:
    """Build a single ecephys item from a source section."""
    source_path = spec.get("source_section")
    sec = section_at(doc, source_path) if source_path else None
    data = section_to_dict(sec) if sec is not None else {}
    result = {}
    for field, fspec in spec.get("fields", {}).items():
        val = _require(_ecephys_resolve_field(fspec, data, section=sec), field, fspec, source_path or "Ecephys")
        if val is not None:
            result[field] = val
    return result


def _build_ecephys_matched(spec: dict, ctx: dict) -> list[dict]:
    """Build ecephys items by iterating matched probes."""
    dedup_key = spec.get("deduplicate_by")
    seen: set[str] = set()
    items = []
    for i, ((number, _probe_sec, placement), probe_data) in enumerate(
        zip(ctx["matches"], ctx["configs"])
    ):
        where = f"Session {ctx['session_id']} placement {number} ({probe_data.get('name')})"
        row = {}
        for field, fspec in spec.get("fields", {}).items():
            val = _ecephys_resolve_field(fspec, probe_data, ctx=ctx, idx=i, placement=placement)
            if _require(val, field, fspec, where) is not None:
                row[field] = val
        if dedup_key:
            dk = row.get(dedup_key)
            if dk in seen:
                continue
            seen.add(dk)
        items.append(row)
    return items


def _build_ecephys_from_registry(spec: dict) -> list[dict]:
    """Build electrode column definitions from the BIDS mappings registry."""
    key = spec.get("registry_key", "")
    return [
        {"name": col, "description": s["description"]}
        for col, s in load_registry().get(key, {}).items()
    ]


def _build_ecephys(doc: odml.Document, session: odml.Section, registry: dict,
                   run: int = None) -> dict:
    """Build Ecephys metadata from registry declarations: the devices and one
    ElectrodeGroup per Electrode Placement of the session (its position for
    this run when the device was moved between runs)."""
    ecephys_reg = registry.get("Ecephys", {})

    matches = list(matched_probes(doc, session))
    configs = [section_to_dict(ps) for _, ps, _ in matches]
    ids = session_probe_ids(doc, matches)
    ctx = {"matches": matches, "configs": configs, "ids": ids, "run": run,
           "session_id": prop(session, "session id", session.name)}

    result = {}
    for key, spec in ecephys_reg.items():
        if key.startswith("_"):
            continue
        mode = spec.get("mode", "single")

        if mode == "single":
            item = _build_ecephys_single(doc, spec)
            if spec.get("as_list"):
                result.setdefault(key, []).append(item)
            else:
                result[key] = item
        elif mode == "matched_probes":
            items = _build_ecephys_matched(spec, ctx)
            target = spec.get("target")
            if target:
                result.setdefault(target, []).extend(items)
            else:
                result[key] = items
        elif mode == "from_registry":
            result[key] = _build_ecephys_from_registry(spec)

    return result


# Public API

def build_nwb_metadata(
    doc: odml.Document, session: odml.Section, overrides: dict = None, run: int = None
) -> dict:
    """Build metadata dict for neuroconv.

    Args:
        doc: Compiled odML document (the single source of truth).
        session: Session section within the odML document.
        overrides: Optional NWBFile field overrides from dataset-specific code.
        run: The run this NWB file holds, for its own start time (Neo Metadata
            runs) and the depth of a device moved between runs ('DV by run'
            of the placement).

    Returns:
        Dict with NWBFile, Subject, and Ecephys keys for neuroconv.
    """
    registry = load_nwb_registry()
    sources = _resolve_sources(doc, session, run)

    nwbfile = _build_section("NWBFile", doc, session, sources, registry)
    if overrides:
        nwbfile.update(overrides)

    return {
        "NWBFile": nwbfile,
        "Subject": _build_section("Subject", doc, session, sources, registry),
        "Ecephys": _build_ecephys(doc, session, registry, run),
    }


GAIN_UNITS_TO_UV = {"uV per count": 1.0, "V per count": 1e6}


def recording_scale(source: odml.Document | odml.Section) -> tuple[float, float]:
    """(gain_to_uV, offset_to_uV) of the stored samples from the Recording
    System's `gain`, `gain unit` ("uV per count" or "V per count") and `adc
    zero` (the count of zero volts, 0 for signed counts):
    microvolts = gain_to_uV * count + offset_to_uV, with offset_to_uV =
    -adc zero * gain_to_uV. `source` is the odML document or its Recording
    System section. A missing gain or one not strictly positive, a missing
    unit or another unit, or a missing adc zero is an error.
    """
    section = section_at(source, "Hardware/Recording System") if isinstance(source, odml.doc.BaseDocument) else source
    values = props(section) if section is not None else {}
    gain, unit, zero = values.get("gain"), values.get("gain unit"), values.get("adc zero")
    if gain in (None, ""):
        raise ValueError("Hardware/Recording System gain is not set: the samples have no scale")
    if float(gain) <= 0:
        raise ValueError(f"Hardware/Recording System gain {gain} is not strictly positive: it is no scale")
    if unit not in GAIN_UNITS_TO_UV:
        raise ValueError(f"Hardware/Recording System gain unit {unit!r} is not one of {list(GAIN_UNITS_TO_UV)}")
    if zero in (None, ""):
        raise ValueError("Hardware/Recording System adc zero is not set: the samples have no offset "
                         "(0 for signed counts, stated with its source)")
    gain_to_uV = float(gain) * GAIN_UNITS_TO_UV[unit]
    return gain_to_uV, -int(zero) * gain_to_uV


# Utilities

def _parse_datetime(val) -> datetime | None:
    """Parse date/datetime value into timezone-aware datetime (UTC)."""
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    if isinstance(val, date) and not isinstance(val, datetime):
        return datetime(val.year, val.month, val.day, tzinfo=timezone.utc)
    if isinstance(val, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(val, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None
