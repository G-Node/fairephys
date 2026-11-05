"""Sanity checks across formats: the ProbeInterface probes, the BIDS
electrodes, channels and probes files, and the NWB electrode table."""

import json
from functools import lru_cache
from pathlib import Path

import pandas as pd
from probeinterface import read_probeinterface
from pynwb import NWBHDF5IO

from .bids import default_schema_path
from .report import ValidationReport, files_exist, guarded
from ..converters.metadata import location_placeholder
from ..placements import UNPLACED_GROUP

# The channels.tsv types of extracellular signals (channels expected as NWB
# electrodes): the BEP032 schema's `type__channels` enums described as an
# extracellular voltage signal or as spike events (BB unfiltered, HP high-pass
# filtered, LFP low-pass filtered, MUA rectified or thresholded, SPIKES
# discrete spike events) and the electrode channel types of the ECoG and sEEG
# arrays curated here (ECOG, SEEG). Every entry is a schema enum
# (bids_channel_types); no SUA in the schema.
BIDS_NEURAL_TYPES = {"BB", "HP", "LFP", "MUA", "SPIKES", "ECOG", "SEEG"}

# Positions closer than this are the same (micrometer)
POSITION_TOLERANCE = 0.1


def bids_channel_types(schema_path: Path | None = None) -> frozenset[str]:
    """The channels.tsv 'type' values of the BIDS schema (the packaged
    BEP032 schema unless another is given)."""
    return _channel_types(Path(schema_path) if schema_path else default_schema_path())


@lru_cache(maxsize=4)
def _channel_types(schema_path: Path) -> frozenset[str]:
    schema = json.loads(schema_path.read_text())
    return frozenset(schema["objects"]["columns"]["type__channels"]["enum"])


def _nwb_electrodes(nwb_path: Path) -> tuple[pd.DataFrame, set[str]]:
    """The NWB electrode table (empty without one) and the electrode group names."""
    with NWBHDF5IO(str(nwb_path), "r") as io:
        nwb = io.read()
        df = nwb.electrodes.to_dataframe() if nwb.electrodes is not None else pd.DataFrame()
        return df, set(nwb.electrode_groups or {})


@guarded
def check_electrode_consistency(bids_tsv: Path, nwb_path: Path, report: ValidationReport) -> ValidationReport:
    """Compare BIDS channels.tsv with NWB electrodes."""
    if not files_exist(report, bids_tsv, nwb_path):
        return report
    bids_df = _read_tsv(bids_tsv)
    nwb_df, groups = _nwb_electrodes(nwb_path)
    if nwb_df.empty:
        report.add_error("Sanity", "NWB has no electrodes")
        return report
    report.add_info("total_channels", len(nwb_df))
    bids_df = _rows_of_nwb_probes(bids_df, groups, report)

    nwb_names = set(nwb_df.get("channel_name", nwb_df.get("name", nwb_df.index)).astype(str))
    if "type" not in bids_df.columns:
        if len(bids_df) != len(nwb_df):
            report.add_error("Sanity", f"Channel count: BIDS={len(bids_df)}, NWB={len(nwb_df)}")
        return report

    # Neural channels (BB, LFP, MUA, ...) must match the NWB neural electrodes;
    # the NWB may hold auxiliary electrodes that are no neural channel
    neural_mask = bids_df["type"].isin(BIDS_NEURAL_TYPES)
    bids_neural_names = set(bids_df.loc[neural_mask, "name"].astype(str))
    n_neural = int(neural_mask.sum())
    report.add_info("bids_neural_channels", n_neural)
    report.add_info("bids_auxiliary_channels", len(bids_df) - n_neural)
    nwb_aux_only = nwb_names - bids_neural_names
    n_nwb_neural = len(nwb_df) - len(nwb_aux_only)
    if n_neural != n_nwb_neural:
        report.add_error(
            "Sanity",
            f"Neural channel count mismatch: BIDS neural={n_neural}, NWB neural={n_nwb_neural} "
            f"(NWB total={len(nwb_df)}, NWB auxiliary={len(nwb_aux_only)})",
        )
    else:
        report.add_info("channel_count_consistency",
                        f"PASSED (neural={n_neural}, NWB auxiliary={len(nwb_aux_only)})")

    if invalid_types := set(bids_df["type"].unique()) - bids_channel_types():
        report.add_error("Sanity", f"Invalid BIDS channel types: {sorted(invalid_types)} "
                                   f"(must be from the BIDS schema channel types)")

    if bids_neural_names:
        if diff := bids_neural_names - nwb_names:
            report.add_warning("Sanity", f"BIDS neural only: {sorted(diff)[:5]}")
        if diff := nwb_names - bids_neural_names:
            report.add_warning("Sanity", f"NWB only: {sorted(diff)[:5]}")
        if bids_neural_names == nwb_names:
            report.add_info("channel_names_matched", True)
    return report


@guarded
def check_probe_geometry(probe_path: Path, nwb_path: Path, report: ValidationReport) -> ValidationReport:
    """Compare ProbeInterface with NWB electrodes."""
    if not files_exist(report, probe_path, nwb_path):
        return report
    probes = read_probeinterface(str(probe_path))
    nwb_df, _ = _nwb_electrodes(nwb_path)

    total = 0
    details = []
    for i, p in enumerate(probes.probes):
        n = len(p.contact_positions)
        total += n
        details.append({
            "name": p.annotations.get("name", f"Probe {i + 1}"), "contacts": n,
            "device_channel_indices": _fmt(p.device_channel_indices),
            "contact_ids": _fmt(p.contact_ids),
        })
    report.add_info("probes", [f"{d['name']}: {d['contacts']} contacts" for d in details])
    report.add_info("probe_details", details)
    report.add_info("total_probe_contacts", total)

    # NWB electrode summary; channels on no placed probe (group 'unplaced') are
    # not probe contacts, and the rows of one contact in several streams (AP and
    # LF) are one electrode when every row names its contact (rows without a
    # name each count: a recording with more channels than the probe has)
    placed = len(nwb_df)
    if not nwb_df.empty:
        # the columns sorted: neuroconv adds the property columns in set order, which varies per run
        summary = {"total": len(nwb_df), "columns": sorted(nwb_df.columns)}
        if "group_name" in nwb_df.columns:
            summary["by_group"] = nwb_df["group_name"].value_counts().to_dict()
            if "location" in nwb_df.columns and (placeholder := location_placeholder()):
                # a region the placement does not give: neuroconv's placeholder, with the hemisphere when given
                locations = nwb_df["location"].astype(str)
                unknown = nwb_df[(locations == placeholder) | locations.str.startswith(f"{placeholder}, ")]
                for group, location in sorted(set(zip(unknown["group_name"], unknown["location"]))):
                    report.add_warning("Sanity", f"NWB electrode group {group}: location {location!r} (the "
                                                 "placement names no brain region; neuroconv's placeholder)")
            rows = nwb_df[nwb_df["group_name"] != UNPLACED_GROUP]
            named = "electrode_name" in rows and bool(rows["electrode_name"].astype(str).str.strip().ne("").all())
            placed = len(rows.drop_duplicates(["group_name", "electrode_name"]) if named else rows)
        elif "group" in nwb_df.columns:
            summary["by_group"] = nwb_df["group"].apply(lambda x: getattr(x, "name", str(x))).value_counts().to_dict()
        report.add_info("nwb_electrode_table", summary)
        cols = [c for c in ["channel_name", "group_name", "location", "x", "y", "z"] if c in nwb_df.columns] \
            or list(nwb_df.columns)[:5]
        report.add_info("nwb_electrodes_preview", nwb_df[cols].head(10).to_dict("records"))

    if total < placed:
        report.add_warning("Sanity", f"More NWB electrodes ({placed}) than probe contacts ({total})")
    elif total > placed:
        report.add_info("probe_electrode_subset",
                        f"Subset selection: {total} probe contacts, {placed} active in NWB")
    return report


@guarded
def check_bids_electrode_mapping(
    channels_tsv: Path,
    electrodes_tsv: Path,
    probes_tsv: Path,
    report: ValidationReport,
) -> ValidationReport:
    """Validate internal consistency of BIDS electrode/channel/probe files.

    Checks:
    - Every neural channel's electrode_name exists in electrodes.tsv
    - Every electrode's probe_name exists in probes.tsv
    - Per probe, electrodes.tsv has at most probes.tsv electrode_count rows
      (a session may record a subset of a probe's contacts, reported as info)
    """
    if not files_exist(report, channels_tsv, electrodes_tsv, probes_tsv):
        return report
    errors_before = len(report.errors)
    ch_df, el_df, pr_df = (_read_tsv(path) for path in (channels_tsv, electrodes_tsv, probes_tsv))
    electrode_names = set(el_df["name"].astype(str))
    probe_names = set(pr_df["probe_name"].astype(str))

    if "electrode_name" in ch_df.columns:
        linked = set(ch_df.loc[ch_df["electrode_name"].astype(str) != "n/a", "electrode_name"].astype(str))
        if orphans := linked - electrode_names:
            report.add_error("Sanity", f"channels.tsv references {len(orphans)} electrode(s) not in "
                                       f"electrodes.tsv: {sorted(orphans)[:5]}")
        else:
            report.add_info("channels_to_electrodes", f"PASSED ({len(linked)} linked)")

    if "probe_name" in el_df.columns:
        el_probes = set(el_df["probe_name"].astype(str))
        if orphans := el_probes - probe_names:
            report.add_error("Sanity", f"electrodes.tsv references {len(orphans)} probe(s) not in "
                                       f"probes.tsv: {sorted(orphans)}")
        else:
            report.add_info("electrodes_to_probes", f"PASSED ({len(el_probes)} probes)")

    if "electrode_count" in pr_df.columns and "probe_name" in el_df.columns:
        for _, probe_row in pr_df.iterrows():
            pid = str(probe_row["probe_name"])
            expected = int(probe_row["electrode_count"])
            actual = int((el_df["probe_name"].astype(str) == pid).sum())
            if actual > expected:
                report.add_error("Sanity", f"Probe {pid}: probes.tsv says {expected} electrodes, "
                                           f"electrodes.tsv has {actual}")
            elif actual < expected:
                report.add_info(f"{pid}_recorded", f"{actual} of {expected} electrodes recorded")

    if len(report.errors) == errors_before:
        report.add_info("bids_electrode_mapping", "PASSED")
    return report


@guarded
def check_probe_to_nwb_geometry(
    probe_path: Path,
    electrodes_tsv: Path,
    nwb_path: Path,
    report: ValidationReport,
) -> ValidationReport:
    """Three-way geometry verification: ProbeInterface -> BIDS -> NWB.

    The contact positions of each probe (local to the probe: its x_offset in
    the group subtracted) are compared by electrode name ({probe_id}_{contact
    id}) with BIDS electrodes.tsv (x, y) and with the NWB electrode table
    (group_name, electrode_name, rel_x, rel_y). The NWB electrode groups must
    be the BIDS probes.
    """
    if not files_exist(report, probe_path, electrodes_tsv, nwb_path):
        return report
    reference = _probe_positions(read_probeinterface(str(probe_path)).probes, report)
    el_df = _read_tsv(electrodes_tsv)
    bids = {n: (_number(x), _number(y)) for n, x, y in zip(el_df["name"], el_df["x"], el_df["y"])}
    _compare_positions(report, "bids", "BIDS electrodes.tsv", reference, bids)

    nwb_df, groups = _nwb_electrodes(nwb_path)
    groups -= {UNPLACED_GROUP}
    report.add_info("nwb_electrode_groups", sorted(groups))
    bids_probes = set(el_df["probe_name"].astype(str))
    if extra := groups - bids_probes:
        report.add_error("Sanity", f"NWB electrode groups not among the BIDS probes: {sorted(extra)}")
    elif groups < bids_probes:  # one NWB per probe: compare the probes it holds
        report.add_info("nwb_bids_probe_groups", f"NWB holds probes {sorted(groups)} of {sorted(bids_probes)}")
        reference = {n: xy for n, xy in reference.items() if any(n.startswith(f"{g}_") for g in groups)}
    else:
        report.add_info("nwb_bids_probe_groups", f"PASSED ({len(groups)} groups match)")

    if nwb_df.empty:
        report.add_error("Sanity", "NWB has no electrode table")
        return report
    if missing := [c for c in ("group_name", "electrode_name", "rel_x", "rel_y") if c not in nwb_df.columns]:
        report.add_error("Sanity", f"NWB electrode table lacks the columns {missing}: "
                                   "ProbeInterface geometry not transferred to NWB")
        return report
    placed = nwb_df[nwb_df["group_name"].astype(str) != UNPLACED_GROUP]
    nwb = {f"{g}_{e}": (float(x), float(y)) for g, e, x, y in zip(
        placed["group_name"], placed["electrode_name"], placed["rel_x"], placed["rel_y"])}
    _compare_positions(report, "nwb", "NWB electrode table", reference, nwb)
    return report


def _read_tsv(path: Path) -> pd.DataFrame:
    """A BIDS TSV with every value as text ("n/a" kept as written)."""
    return pd.read_csv(path, sep="\t", dtype=str, keep_default_na=False, na_values=[])


def _number(value: str) -> float:
    """A TSV value as a number, NaN for "n/a"."""
    return float("nan") if value == "n/a" else float(value)


def _rows_of_nwb_probes(channels: pd.DataFrame, groups: set[str], report: ValidationReport) -> pd.DataFrame:
    """The channels.tsv rows of the probes the NWB holds, when a session
    writes one NWB per probe (the rows of other probes are left out, info);
    all rows otherwise."""
    if "electrode_name" not in channels.columns:
        return channels
    placed = groups - {UNPLACED_GROUP}
    names = channels["electrode_name"].astype(str)
    own = names.map(lambda e: any(e.startswith(f"{g}_") for g in placed))
    unplaced = names.isin(["n/a", ""])
    other = ~own & ~unplaced
    if not other.any():
        return channels
    report.add_info("nwb_probe_subset", f"NWB holds probes {sorted(placed)}: "
                                        f"{int(other.sum())} channels of other probes not compared")
    return channels[own | (unplaced if UNPLACED_GROUP in groups else False)]


def _probe_positions(probes, report: ValidationReport) -> dict[str, tuple[float, float]]:
    """(x, y) of every contact by electrode name, local to its probe; invalid
    device_channel_indices are errors."""
    positions = {}
    for i, probe in enumerate(probes):
        probe_id = probe.annotations.get("probe_id")
        if not probe_id:
            report.add_error("Sanity", f"Probe {i + 1} ({probe.name}) has no probe_id annotation")
            continue
        local = probe.contact_positions.astype(float)
        local[:, 0] -= probe.annotations.get("x_offset", local[:, 0].min())
        for cid, (x, y) in zip(probe.contact_ids, local):
            positions[f"{probe_id}_{cid}"] = (float(x), float(y))
        dci = probe.device_channel_indices
        connected = [v for v in dci if v != -1] if dci is not None else []
        if len(set(connected)) != len(connected):
            report.add_error("Sanity", f"{probe_id}: duplicate device_channel_indices")
        if dci is not None and any(v < -1 for v in dci):  # -1: not connected (ProbeInterface)
            report.add_error("Sanity", f"{probe_id}: device_channel_indices below -1")
    return positions


def _compare_positions(report: ValidationReport, key: str, label: str,
                       reference: dict, other: dict) -> None:
    """Electrodes of `other` against the ProbeInterface `reference`, by name:
    unknown names and differing positions are errors, fewer electrodes a
    recorded subset (info)."""
    if unknown := sorted(set(other) - set(reference)):
        report.add_error("Sanity", f"{label}: {len(unknown)} electrode(s) not in ProbeInterface: {unknown[:5]}")
    shared = sorted(set(other) & set(reference))
    diffs = {n: max(abs(reference[n][0] - other[n][0]), abs(reference[n][1] - other[n][1])) for n in shared}
    if off := [n for n in shared if diffs[n] > POSITION_TOLERANCE]:
        report.add_error("Sanity", f"{label}: {len(off)} electrode(s) differ from the ProbeInterface "
                                   f"positions (max diff {max(diffs.values()):.1f} um): {off[:5]}")
    elif shared:
        report.add_info(f"{key}_geometry", f"PASSED ({len(shared)} electrodes match ProbeInterface)")
    if len(shared) < len(reference) and not unknown:
        report.add_info(f"{key}_subset", f"{len(shared)} of {len(reference)} ProbeInterface contacts in {label}")


def _fmt(arr) -> str:
    """Format array for display."""
    if arr is None:
        return "not set"
    lst = list(arr)
    return ", ".join(str(x) for x in lst[:10]) + (f"... ({len(lst)} total)" if len(lst) > 10 else "")
