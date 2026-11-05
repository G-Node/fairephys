"""Build ProbeInterface objects from odML metadata."""

import logging
import shutil
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import odml
from probeinterface import (
    Probe, ProbeGroup, generate_linear_probe, generate_multi_columns_probe,
    write_probeinterface,
)
from probeinterface.plotting import plot_probe, plot_probegroup

from .library import get_from_library, library_key
from ..metadata.compiler import parse_value
from ..odml import props, subsection
from ..placements import derive_probe_ids, matched_probes, parse_channels, probe_ids

logger = logging.getLogger(__name__)

# Fields that map to Probe attributes (not annotations)
PROBE_ATTRS = {"name", "manufacturer", "serial number"}
# Fields used for probe geometry (not annotations)
GEOMETRY_FIELDS = {
    "number of contacts", "contact pitch", "channel mapping",
    "contact arrangement", "number of columns", "number of rows",
    "row spacing", "column spacing", "contact positions",
}
# Value origin of the geometry of a probe placed at its YAML 'contact positions'
YAML_POSITIONS = "the YAML's contact positions"


# Micrometer per pitch unit
_UM_PER_UNIT = {"um": 1.0, "micrometer": 1.0, "mm": 1000.0, "millimeter": 1000.0}


def parse_pitch(value, unit: str | None = None) -> float:
    """The contact pitch in micrometer: a number in `unit` (micrometer when
    none), or text such as '200 um', '0.2 millimeter' or '200'. 0.0 for an
    empty value or 'n/a' (a single-contact probe has no pitch)."""
    if isinstance(value, str):
        value, unit = parse_value(value.strip())
        if str(value).lower() in ("", "n/a", "none", "na"):
            return 0.0
    if unit and unit not in _UM_PER_UNIT:
        raise ValueError(f"contact pitch unit {unit!r}: one of {', '.join(_UM_PER_UNIT)}")
    return float(value) * _UM_PER_UNIT[unit or "micrometer"]


def contact_ids(n: int) -> list[str]:
    """Contact ids of a parametric probe in contact order, zero-padded to at
    least two digits ("01".."24", "001".."128")."""
    width = max(2, len(str(n)))
    return [f"{i:0{width}d}" for i in range(1, n + 1)]


def select_contacts(probe, ids, label: str):
    """The contacts of `probe` with the ids `ids`, in the order listed (the
    recording's channel order); an id that is no contact is an error."""
    position = {str(c): i for i, c in enumerate(probe.contact_ids)}
    wanted = [str(i) for i in ids]
    if missing := [i for i in wanted if i not in position]:
        raise ValueError(f"{label}: electrode identifiers not among the probe's contacts: "
                         f"{', '.join(missing[:10])}")
    if len(set(wanted)) != len(wanted):
        raise ValueError(f"{label}: an electrode identifier is listed twice")
    return probe.get_slice(np.array([position[i] for i in wanted]))


def _as_list(value) -> list:
    """A property value as a list of non-blank values ([] for None or "")."""
    values = value if isinstance(value, (list, tuple)) else [value]
    return [v for v in values if v is not None and str(v).strip()]


def _library_probe(cfg: dict, label: str):
    """The library probe of an entry with a model, cut to the recorded
    contacts when 'number of contacts' is fewer than the library's (its
    'electrode identifiers' name them: an error without them); at the full
    count filled identifiers become its contact ids (one per contact, as
    for a generated probe, so the odML, BIDS and NWB carry one list; another
    count is an error). None when the library has no such probe."""
    key = library_key(cfg)
    if not (key and cfg.get("model")):
        return None
    probe = get_from_library(key, str(cfg["model"]))
    if probe is None:
        return None
    n_contacts = cfg.get("number of contacts")
    ids = [str(i) for i in _as_list(cfg.get("electrode identifiers"))]
    if n_contacts and n_contacts < probe.get_contact_count():
        if not ids:
            raise ValueError(f"{label}: {n_contacts} of the {probe.get_contact_count()} library contacts "
                             "recorded and no 'electrode identifiers' that name them")
        probe = select_contacts(probe, ids, label)
    elif ids:
        if len(ids) != probe.get_contact_count():
            raise ValueError(f"{label}: {len(ids)} electrode identifiers for {probe.get_contact_count()} contacts")
        probe.set_contact_ids(ids)
    probe.annotate(from_library=True)
    return probe


def _parametric_probe(cfg: dict, label: str, units: dict):
    """A probe generated from pitch and arrangement (linear, or a matrix of
    'number of columns' by 'number of rows', else as many rows as fit, its
    'column spacing' and 'row spacing' where they differ from the pitch).

    A linear probe's contacts are in contact order from the end its
    'first contact' names: the tip (default, ProbeInterface's order) or the
    top (contact 1 farthest from the tip, the last one next to it)."""
    n_contacts = cfg["number of contacts"]
    pitch = parse_pitch(cfg.get("contact pitch", ""), units.get("contact pitch"))
    first = str(cfg.get("first contact") or "tip")
    if cfg.get("contact arrangement", "linear") != "matrix":
        if cfg.get("row spacing") or cfg.get("column spacing"):
            raise ValueError(f"{label}: 'row spacing' and 'column spacing' are for matrix probes; "
                             "a linear probe's spacing is its 'contact pitch'")
        probe = generate_linear_probe(num_elec=n_contacts, ypitch=pitch)
        if first == "top":
            probe.set_contacts(positions=probe.contact_positions[::-1], shapes=probe.contact_shapes[::-1],
                               shape_params=list(probe.contact_shape_params[::-1]),
                               plane_axes=probe.contact_plane_axes[::-1])
        elif first != "tip":
            raise ValueError(f"{label}: first contact {first!r} is neither 'tip' nor 'top'")
        return probe
    if cfg.get("first contact"):
        raise ValueError(f"{label}: 'first contact' applies to linear probes only")
    n_cols = cfg.get("number of columns")
    if not n_cols:
        raise ValueError(f"{label}: contact arrangement is 'matrix' but 'number of columns' is not set")
    n_rows = cfg.get("number of rows") or n_contacts // n_cols
    if n_rows * n_cols < n_contacts:
        raise ValueError(f"{label}: {n_contacts} contacts do not fit {n_rows} rows x {n_cols} columns")
    xpitch = parse_pitch(cfg["column spacing"], units.get("column spacing")) if cfg.get("column spacing") else pitch
    ypitch = parse_pitch(cfg["row spacing"], units.get("row spacing")) if cfg.get("row spacing") else pitch
    probe = generate_multi_columns_probe(num_columns=n_cols, num_contact_per_column=n_rows,
                                         xpitch=xpitch, ypitch=ypitch)
    if n_contacts < n_rows * n_cols:
        probe = probe.get_slice(np.arange(n_contacts))
    return probe


def _positioned_probe(cfg: dict, label: str):
    """A probe at the entry's 'contact positions' (x and y in micrometer of
    each of its 'contact ids'), None when the entry gives none."""
    if not (positions := cfg.get("contact positions")):
        return None
    ids, xs, ys = (_as_list(positions.get(key)) for key in ("contact ids", "x", "y"))
    if not ids or not len(ids) == len(xs) == len(ys) == cfg.get("number of contacts"):
        raise ValueError(f"{label}: contact positions give {len(ids)} contact ids, {len(xs)} x and {len(ys)} y "
                         f"for {cfg.get('number of contacts')} contacts")
    if cfg.get("first contact"):
        raise ValueError(f"{label}: contact positions place every contact; 'first contact' is for generated probes")
    probe = Probe(ndim=2, si_units="um")
    probe.set_contacts(positions=np.column_stack([xs, ys]).astype(float))
    probe.set_contact_ids([str(i) for i in ids])
    probe.annotate(geometry_origin=YAML_POSITIONS)
    return probe


def _create_probe(cfg: dict, units: dict | None = None):
    """A Probe of one Hardware/Probes entry: at its 'contact positions' when
    it gives them, else the library probe of its manufacturer and model
    (contact ids and geometry as in the library), else a parametric one
    (see _parametric_probe for the end contact 1 is at). A generated probe's
    contact ids are the entry's filled 'electrode identifiers' (one per
    contact, in contact order; the lab's channel numbers reach every output
    through them), else numbered in contact order; the ids of a probe at its
    'contact positions' are the positions' and filled identifiers must equal
    them. `units` are the units of the entry's values (an odML section's),
    micrometer when absent."""
    label = f"Probe {cfg.get('name')!r}"
    ids = [str(i) for i in _as_list(cfg.get("electrode identifiers"))]
    if (probe := _positioned_probe(cfg, label)) is not None:
        if ids and ids != list(probe.contact_ids):
            raise ValueError(f"{label}: 'electrode identifiers' differ from the contact ids of 'contact positions'")
        return probe
    probe = _library_probe(cfg, label)
    if probe is not None and cfg.get("first contact"):
        raise ValueError(f"{label}: a library probe keeps the library's contact order; "
                         "'first contact' is for generated probes")
    if probe is None:
        probe = _parametric_probe(cfg, label, units or {})
        if ids and len(ids) != probe.get_contact_count():
            raise ValueError(f"{label}: {len(ids)} electrode identifiers for {probe.get_contact_count()} contacts")
        probe.set_contact_ids(ids or contact_ids(probe.get_contact_count()))
    return probe


def _config(entry) -> tuple[dict, dict]:
    """(fields, units) of a Hardware/Probes entry given as a dict (no units)
    or as an odML section (the units of its properties)."""
    if isinstance(entry, dict):
        return dict(entry), {}
    cfg = props(entry)
    if (positions := subsection(entry, "contact positions")) is not None:
        cfg["contact positions"] = props(positions)
    return cfg, {p.name: p.unit for p in entry.properties if p.unit}


def _device_probe(cfg: dict, probe_id: str, number: int = 1, units: dict | None = None):
    """The ProbeInterface Probe of one Hardware/Probes entry: its geometry
    (library or parametric), contact ids, identity (name, manufacturer,
    model_name, serial_number) and the entry's other fields as annotations,
    values with their unit ("12 micrometer")."""
    probe = _create_probe(cfg, units)
    probe.name = cfg.get("name", f"Probe_{number}")
    probe.manufacturer = cfg.get("manufacturer", "")
    if model := cfg.get("model"):
        probe.model_name = str(model)
    if serial := cfg.get("serial number"):
        probe.serial_number = str(serial)
    probe.annotate(probe_id=probe_id)  # the device's id in every format
    units = units or {}
    annotations = {
        k.replace(" ", "_"): f"{v} {units[k]}" if k in units else v
        for k, v in cfg.items()
        if k not in GEOMETRY_FIELDS and k not in PROBE_ATTRS and v
    }
    if annotations:
        probe.annotate(**annotations)
    return probe


def _add_spaced(group: ProbeGroup, probe, x_offset: float, x_spacing: int) -> float:
    """Add the probe to the group beside the previous ones (for plotting and
    SpikeInterface's overlap check), annotated with the x_offset added to its
    contact positions (0 for the first); returns the next x offset."""
    probe.move([x_offset, 0])
    probe.annotate(x_offset=float(x_offset))
    group.add_probe(probe)
    x_positions = probe.contact_positions[:, 0]
    probe_width = float(x_positions.max() - x_positions.min())
    return x_offset + max(probe_width + x_spacing, x_spacing)


def build_probe_group(probes_config: list, x_spacing: int = 800) -> ProbeGroup:
    """Build the ProbeGroup of the dataset's devices.

    Args:
        probes_config: the Hardware/Probes entries, as odML sections or as
            dicts of their properties. Required: name, number of contacts,
            contact pitch; optional: channel mapping, manufacturer, model,
            contact arrangement, number of columns, and any other field (an
            annotation). Sections keep the units of their values in the
            annotations; dicts carry none.
        x_spacing: Horizontal spacing between probes for visualization (um)
    """
    configs = [_config(entry) for entry in probes_config]
    ids = derive_probe_ids([cfg for cfg, _ in configs])
    group = ProbeGroup()
    x_offset = 0
    for i, ((cfg, units), probe_id) in enumerate(zip(configs, ids)):
        probe = _device_probe(cfg, probe_id, i + 1, units)
        if mapping := cfg.get("channel mapping"):
            probe.set_device_channel_indices([int(x) for x in str(mapping).split(",")])
        else:
            n_so_far = sum(p.get_contact_count() for p in group.probes)
            probe.set_device_channel_indices(list(range(n_so_far, n_so_far + probe.get_contact_count())))
        x_offset = _add_spaced(group, probe, x_offset, x_spacing)
    return group


def build_session_probe_group(doc, session, x_spacing: int = 800) -> ProbeGroup:
    """The ProbeGroup of one session: one Probe per Electrode Placement (the
    device named by the placement, with the device's id of the dataset), wired
    to the placement's channels.

    A placement's 'electrode identifiers' select the contacts of the device
    recorded that session (a Neuropixels bank: the electrodes of the IMRO
    table), in the order listed, which is the recording's channel order.
    device_channel_indices come from the placement's 'channels' (1-based
    positions in the curated stream), else the device's 'channel mapping',
    else the block after the previous placement. The placement's brain region
    and hemisphere are kept as annotations.
    """
    ids = probe_ids(doc)
    group = ProbeGroup()
    x_offset = 0
    next_index = 0
    for number, probe_sec, placement in matched_probes(doc, session):
        cfg, units = _config(probe_sec)
        label = f"Placement {number} ({cfg.get('name')})"
        probe = _device_probe(cfg, ids[str(cfg.get("name", ""))], number, units)
        if recorded := _as_list(placement.get("electrode identifiers")):
            probe = select_contacts(probe, recorded, label)
        n = probe.get_contact_count()
        if spec := placement.get("channels"):
            indices = parse_channels(spec)
            if len(indices) != n:
                raise ValueError(f"{label}: channels {spec!r} give {len(indices)} channels for {n} contacts")
        elif mapping := cfg.get("channel mapping"):
            indices = [int(x) for x in str(mapping).split(",")]
        else:
            indices = list(range(next_index, next_index + n))
        probe.set_device_channel_indices(indices)
        next_index = max(next_index, max(indices) + 1)
        for key in ("brain region", "hemisphere"):
            if placement.get(key):
                probe.annotate(**{key.replace(" ", "_"): placement[key]})
        x_offset = _add_spaced(group, probe, x_offset, x_spacing)
    return group


def enrich_odml_positions(doc, group: ProbeGroup):
    """Write contact positions and geometry from the ProbeGroup into the
    Hardware/Probes sections, each probe into the entry of its name.

    Positions are the probe's own (its x_offset in the group subtracted),
    comma-separated per axis (contact_x, contact_y), with the value origin
    "probeinterface", or YAML_POSITIONS for a probe placed at its YAML's
    contact positions. Empty geometry fields
    (pitch, contact count, arrangement, span) are filled from the
    ProbeInterface data, for a probe at its YAML's positions only the count
    and the span (with the positions' origin; pitch and arrangement are not
    stated by them); contact size and shape only for a library probe (a
    parametric one has no source for them). Empty 'electrode identifiers' take the
    contact ids (openMINDS ElectrodeArray.electrode_identifiers). A probe
    whose name is no entry is an error.
    """
    sections = {str(props(s).get("name", "")): s for s in doc["Hardware"]["Probes"].sections}
    for probe in group.probes:
        probe_sec = sections.get(str(probe.name))
        if probe_sec is None:
            raise ValueError(f"Probe {probe.name!r} is no Hardware/Probes entry "
                             f"({', '.join(sections) or 'none'})")
        positions = probe.contact_positions.astype(float)
        positions[:, 0] -= probe.annotations.get("x_offset", positions[:, 0].min())
        origin = probe.annotations.get("geometry_origin", "probeinterface")
        _enrich_probe_geometry(probe_sec, probe, positions, origin)
        for name, axis in (("contact_x", 0), ("contact_y", 1)):
            _set_property(probe_sec, name, [",".join(f"{v:.1f}" for v in positions[:, axis])], origin)
        ids = [str(c) for c in probe.contact_ids] if probe.contact_ids is not None else []
        if ids and not _as_list(props(probe_sec).get("electrode identifiers")):
            _set_property(probe_sec, "electrode identifiers", ids, origin)


def _set_property(section, name: str, values: list, origin: str = "probeinterface"):
    """Set the property `name` of `section` to `values` from ProbeInterface,
    adding the property when absent."""
    if name in section.properties:
        prop = section.properties[name]
        prop.values, prop.value_origin = values, origin
    else:
        section.append(odml.Property(name=name, values=values, value_origin=origin))


def _enrich_probe_geometry(probe_sec, probe, positions, origin="probeinterface"):
    """Populate empty odML probe geometry fields from a ProbeInterface probe
    (value origin `origin`). A probe at its YAML's contact positions gets only
    what the positions state (contact count, span), no pitch or arrangement.

    Only overwrites properties that are currently empty/blank.
    """
    def _set_if_empty(name, value):
        prop = next((p for p in probe_sec.properties if p.name == name), None)
        if prop is not None:
            current = str(prop.values[0]) if prop.values else ""
            if current.strip() in ("", "n/a", "None"):
                prop.values, prop.value_origin = [value], origin

    n_contacts = probe.get_contact_count()
    _set_if_empty("number of contacts", n_contacts)

    # Pitch (median y-spacing) and arrangement (unique x values): the layout of
    # a generated or library probe; positions from the YAML state neither
    if origin != YAML_POSITIONS:
        ys = np.sort(np.unique(positions[:, 1]))
        if len(ys) > 1:
            y_diffs = np.diff(ys)
            pitch = float(np.median(y_diffs))
            _set_if_empty("contact pitch", f"{pitch:.0f} um")

        n_cols = len(np.unique(positions[:, 0]))
        if n_cols == 1:
            _set_if_empty("contact arrangement", "linear")
        else:
            _set_if_empty("contact arrangement", "matrix")
            _set_if_empty("number of columns", n_cols)

    # Contact shape and size: the manufacturer's values of a library probe
    # only (a parametric probe carries ProbeInterface's generation defaults)
    if probe.annotations.get("from_library"):
        shapes = probe.contact_shapes
        if shapes is not None and len(shapes) > 0:
            _set_if_empty("contact shape", str(shapes[0]))
        shape_params = probe.contact_shape_params
        if shape_params is not None and len(shape_params) > 0:
            width = shape_params[0].get("width") or shape_params[0].get("radius")
            if width:
                _set_if_empty("contact size", f"{width} um")

    # Total span from y range
    y_span = float(positions[:, 1].max() - positions[:, 1].min())
    if y_span > 0:
        _set_if_empty("total span", f"{y_span:.0f} um")


def save_probe_group(group: ProbeGroup, output_dir: Path, title: str = "Probe Group"):
    """Save probe group and individual probes as JSON + PNG."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    for probe in group.probes:
        # Use probe_id annotation for consistent lowercase naming (probe01, probe02)
        probe_id = probe.annotations.get("probe_id", probe.name.replace(" ", "_"))
        write_probeinterface(output_dir / f"{probe_id}.json", probe)
        _plot_probe(probe, output_dir / f"{probe_id}.png")

    write_probeinterface(output_dir / "probe_group.json", group)
    _plot_group(group, output_dir / "probe_group.png", title)


def write_session_probe_group(doc, session, output_dir: Path, title: str = "Probe Group") -> Path:
    """The session's ProbeInterface group (build_session_probe_group) saved
    under output_dir (save_probe_group), the folder emptied first so that
    files of earlier device ids do not stay. Returns its probe_group.json."""
    group = build_session_probe_group(doc, session)
    output_dir = Path(output_dir)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    save_probe_group(group, output_dir, title=title)
    path = output_dir / "probe_group.json"
    logger.info(f"Saved: {path}")
    return path


def _plot_probe(probe, path: Path):
    """Plot single probe showing contact_id:device_channel on each contact."""
    fig, ax = plt.subplots(figsize=(8, 14))

    if probe.device_channel_indices is not None:
        labels = [f"{c}:{d}" for c, d in zip(probe.contact_ids, probe.device_channel_indices)]
    else:
        labels = [str(c) for c in probe.contact_ids]

    plot_probe(probe, ax=ax, text_on_contact=labels, title=False)
    ax.set_title(f"{probe.name} ({probe.manufacturer})\ncontact_id:device_ch", fontsize=11)
    ax.set_xlabel("x (um)")
    ax.set_ylabel("y (um)")
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def _plot_group(group: ProbeGroup, path: Path, title: str):
    """Plot probe group with contact IDs and device indices."""
    fig, ax = plt.subplots(figsize=(12, 12))
    plot_probegroup(group, ax=ax, with_contact_id=True, with_device_index=True, title=False)
    ax.set_title(f"{title}\n(id above, device_ch below)", fontsize=12, fontweight="bold")
    ax.set_xlabel("x (um)")
    ax.set_ylabel("y (um)")

    for probe in group.probes:
        pos = probe.contact_positions
        ax.text(pos[:, 0].mean(), pos[:, 1].max() + 400, probe.name,
                ha="center", fontsize=10, fontweight="bold")

    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
