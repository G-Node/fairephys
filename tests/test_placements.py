"""Placements and probe identity: one entry per device, one id in every session and format."""

from urllib.error import HTTPError, URLError

import numpy as np
import pytest
from probeinterface import generate_linear_probe
from spikeinterface.core import NumpyRecording

from conftest import linear_probe, probe_group, sessions
from fairephys.bids.builder import extract_electrodes, extract_probes
from fairephys.converters import build_nwb_metadata
from fairephys.converters.base import attach_probes
from fairephys.odml import props
from fairephys.placements import (UNPLACED_GROUP, channel_group_names, matched_probes, parse_channels,
                                  placement_position_mm)
from fairephys.probes import (build_probe_group, build_session_probe_group, enrich_odml_positions,
                              write_session_probe_group)
from fairephys.probes.builder import _create_probe, contact_ids, parse_pitch
from fairephys.probes.library import get_from_library, library_key
from fairephys.validation.geometry import _probe_positions
from fairephys.validation.report import ValidationReport

PROBES = [
    {"name": "S-Probe A", "model": "S-Probe", "manufacturer": "Plexon", "serial number": "SN-A",
     "probe type": "silicon-probe", "number of contacts": 4, "contact pitch": "100 micrometer",
     "contact size": "12 micrometer"},
    {"name": "S-Probe B", "model": "S-Probe", "manufacturer": "Plexon", "serial number": "SN-B",
     "probe type": "silicon-probe", "number of contacts": 4, "contact pitch": "100 micrometer"},
    {"name": "V-Probe", "model": "V-Probe", "probe type": "silicon-probe", "number of contacts": 2,
     "contact pitch": "0.1 millimeter"},
]


@pytest.fixture
def placed(make_doc):
    """A dataset of PROBES with the given placements per session id."""
    def make(by_session: dict, probes=PROBES):
        return make_doc(Hardware={"Probes": probes}, Sessions=[
            {"session id": sid, "date": "2025-01-30", "start time": "10:00:00", "Electrode Placements": pls}
            for sid, pls in by_session.items()])
    return make


def _library(n=8, dtype=float):
    """get_probe stand-in: a library probe of n contacts e0.. (20 um pitch)."""
    def get_probe(manufacturer, probe_name, **kwargs):
        probe = generate_linear_probe(num_elec=n, ypitch=20)
        probe.set_contacts(positions=probe.contact_positions.astype(dtype) + [11, 0], shapes="circle",
                           shape_params={"radius": 5})
        probe.set_contact_ids([f"e{i}" for i in range(n)])
        return probe
    return get_probe


def test_device_id_is_the_same_in_every_session_and_format(placed):
    """S-Probe B is S-Probe02 alone or second, in probes.tsv, the probe group, electrodes.tsv and NWB; a probe
    without a type is an error in probes.tsv."""
    doc = placed({"01": [{"probe": "S-Probe A", "brain region": "VPL"}, {"probe": "S-Probe B", "brain region": "PUL"}],
                  "02": [{"probe": "S-Probe B", "brain region": "PUL"}],
                  "03": [{"probe": "V-Probe", "brain region": "MD"}, {"probe": "S-Probe A", "brain region": "VPL"}]})
    rows = [[(r["probe_name"], r["anatomical_location"], r["device_serial_number"]) for r in extract_probes(doc, s)]
            for s in sessions(doc)]
    assert rows == [[("S-Probe01", "VPL", "SN-A"), ("S-Probe02", "PUL", "SN-B")],
                    [("S-Probe02", "PUL", "SN-B")],
                    [("V-Probe", "MD", "n/a"), ("S-Probe01", "VPL", "SN-A")]]  # placement order
    for s in sessions(doc):
        group = build_session_probe_group(doc, s)
        ids = [r["probe_name"] for r in extract_probes(doc, s)]
        assert [p.annotations["probe_id"] for p in group.probes] == ids
        assert [g["name"] for g in build_nwb_metadata(doc, s)["Ecephys"]["ElectrodeGroup"]] == ids
    names = [e["name"] for e in extract_electrodes(doc, sessions(doc)[2], group)]
    assert names == ["V-Probe_01", "V-Probe_02"] + [f"S-Probe01_0{i}" for i in (1, 2, 3, 4)]
    untyped = placed({"01": [{"probe": "V-Probe"}]}, [{k: v for k, v in PROBES[2].items() if k != "probe type"}])
    with pytest.raises(ValueError, match="Session 01, probe V-Probe: no value for the required column type"):
        extract_probes(untyped, sessions(untyped)[0])


def test_matched_probes_fallbacks(make_doc, placed):
    """Without placements: the subject's Implanted Probes, else every device; no devices: no placements."""
    base = {"Hardware": {"Probes": PROBES}, "Sessions": [{"session id": "01", "date": "2025-01-30"}]}
    chronic = make_doc(base, Subject={"Surgical Preparation": {
        "Implanted Probes": [{"probe": "V-Probe", "brain region": "MD", "channels": "3-4"}]}})
    assert [(n, props(sec)["name"], pl["channels"]) for n, sec, pl in matched_probes(chronic, sessions(chronic)[0])] \
        == [(1, "V-Probe", "3-4")]
    bare = make_doc(base)
    assert [(props(sec)["name"], pl) for _, sec, pl in matched_probes(bare, sessions(bare)[0])] == [
        ("S-Probe A", {}), ("S-Probe B", {}), ("V-Probe", {})]
    assert [list(p.device_channel_indices) for p in build_session_probe_group(bare, sessions(bare)[0]).probes] == [
        [0, 1, 2, 3], [4, 5, 6, 7], [8, 9]]  # one block after the other
    imaging = make_doc(Hardware=None, Sessions=[{"session id": "awake"}])
    assert matched_probes(imaging, sessions(imaging)[0]) == []

    doc = placed({"01": [{"probe": "S-Probe A", "brain region": "VPL"}, {"probe": "V-Probe", "brain region": "MD"}]})
    placements = sessions(doc)[0]["Electrode Placements"].sections
    placements[1].properties["probe"].values = ["S-Probe A"]
    with pytest.raises(ValueError, match="probe S-Probe A placed more than once"):
        matched_probes(doc, sessions(doc)[0])
    placements[1].properties["probe"].values = ["S-Probe Z"]
    with pytest.raises(ValueError, match="names probe S-Probe Z, not in Hardware/Probes"):
        matched_probes(doc, sessions(doc)[0])


def test_parse_channels():
    """1-based positions and ranges in the curated stream, each channel once."""
    assert parse_channels("33-36, 40") == [32, 33, 34, 35, 39] and parse_channels("7") == [6]
    for spec, error in (("1-3,2", "listed twice"), ("0-2", "1-based"), ("5-3", "ascending")):
        with pytest.raises(ValueError, match=error):
            parse_channels(spec)


def test_placement_position_with_dv_by_run(placed):
    """[ML, AP, DV] in mm from the placement's units; DV of the run when the device was moved."""
    doc = placed({"01": [{"probe": "S-Probe A", "brain region": "VPL", "ML": "6 millimeter", "AP": "7 millimeter",
                          "DV": "14065 micrometer", "DV by run": {"8": "14.2 millimeter"}}]})
    (_, _, placement), = matched_probes(doc, sessions(doc)[0])
    assert placement_position_mm(placement) == pytest.approx([6.0, 7.0, 14.065])
    assert placement_position_mm(placement, run=5) == pytest.approx([6.0, 7.0, 14.065])
    assert placement_position_mm(placement, run=8) == pytest.approx([6.0, 7.0, 14.2])
    assert placement_position_mm({**placement, "AP": ""}) is None


def test_channel_group_names_unplaced_and_unconnected(placed):
    """Channels on no placed probe are 'unplaced'; a contact wired to -1 claims no channel."""
    doc = placed({"01": [{"probe": "V-Probe", "brain region": "MD", "channels": "3-4"}]})
    group = build_session_probe_group(doc, sessions(doc)[0])
    assert channel_group_names(group, 6) == [UNPLACED_GROUP] * 2 + ["V-Probe"] * 2 + [UNPLACED_GROUP] * 2
    unconnected = probe_group(linear_probe("P", ["01", "02", "03"], [0, -1, 1]))
    assert channel_group_names(unconnected, 3) == ["P", "P", UNPLACED_GROUP]


def test_session_probe_group_wires_each_placement(placed):
    """One probe per placement on its channels (else the block after the previous), with the device's identity."""
    doc = placed({"01": [{"probe": "S-Probe A", "brain region": "PUL", "hemisphere": "left", "channels": "5-8"},
                         {"probe": "V-Probe", "brain region": "MD"}]})
    first, second = build_session_probe_group(doc, sessions(doc)[0]).probes
    assert [list(first.device_channel_indices), list(second.device_channel_indices)] == [[4, 5, 6, 7], [8, 9]]
    assert (first.name, first.serial_number, first.model_name, first.manufacturer) == (
        "S-Probe A", "SN-A", "S-Probe", "Plexon")
    assert {k: first.annotations[k] for k in ("probe_id", "brain_region", "hemisphere", "contact_size")} == {
        "probe_id": "S-Probe01", "brain_region": "PUL", "hemisphere": "left", "contact_size": "12 micrometer"}
    assert second.contact_positions[:, 1].tolist() == [0.0, 100.0]  # 0.1 millimeter pitch

    short = placed({"01": [{"probe": "S-Probe A", "brain region": "VPL", "channels": "1-2"}]})
    with pytest.raises(ValueError, match="channels '1-2' give 2 channels for 4 contacts"):
        build_session_probe_group(short, sessions(short)[0])


def test_parametric_contact_ids_and_order():
    """Zero-padded contact ids in contact order from the tip, or from the top with 'first contact: top'."""
    assert contact_ids(3) == ["01", "02", "03"] and contact_ids(128)[::127] == ["001", "128"]
    cfg = {"name": "LMA", "number of contacts": 3, "contact pitch": 200}
    assert _create_probe(cfg).contact_positions[:, 1].tolist() == [0.0, 200.0, 400.0]
    top = _create_probe({**cfg, "first contact": "top"})
    assert list(top.contact_ids) == ["01", "02", "03"]
    assert top.contact_positions[:, 1].tolist() == [400.0, 200.0, 0.0]
    assert min(y for _, y in top.probe_planar_contour) < 0  # the tip stays at the bottom
    matrix = _create_probe({"name": "U", "number of contacts": 48, "contact pitch": 400,
                            "contact arrangement": "matrix", "number of columns": 5, "number of rows": 10})
    assert matrix.get_contact_count() == 48 and len(set(matrix.contact_positions[:, 0])) == 5


def test_generated_probe_takes_the_electrode_identifiers_as_contact_ids(placed):
    """A parametric probe's filled 'electrode identifiers' (the lab's channel numbers) are its contact ids, so
    they name the BIDS electrodes and the NWB electrode_name; a wrong count, or identifiers that differ from the
    'contact positions' ids, are errors."""
    numbered = {"name": "FMA", "probe type": "floating microelectrode array", "number of contacts": 3,
                "contact pitch": "400 micrometer", "electrode identifiers": [33, 34, 35]}
    assert list(_create_probe(numbered).contact_ids) == ["33", "34", "35"]
    doc = placed({"01": [{"probe": "FMA", "brain region": "M1", "channels": "1-3"}]}, [numbered])
    group = build_session_probe_group(doc, sessions(doc)[0])
    assert [e["name"] for e in extract_electrodes(doc, sessions(doc)[0], group)] == ["probe01_33", "probe01_34", "probe01_35"]
    recording = attach_probes(NumpyRecording(np.zeros((10, 3)), sampling_frequency=1000.0), group)
    assert list(recording.get_property("electrode_name")) == ["33", "34", "35"]
    with pytest.raises(ValueError, match="2 electrode identifiers for 3 contacts"):
        _create_probe({**numbered, "electrode identifiers": [33, 34]})
    positioned = {**numbered, "contact positions": {"contact ids": ["a", "b", "c"], "x": [0, 1, 2], "y": [0, 0, 0]}}
    assert list(_create_probe({**positioned, "electrode identifiers": ["a", "b", "c"]}).contact_ids) == ["a", "b", "c"]
    with pytest.raises(ValueError, match="differ from the contact ids of 'contact positions'"):
        _create_probe(positioned)


def test_parametric_probe_errors():
    """Geometry the builder cannot honour is an error, never a guess."""
    for cfg, error in [
        ({"first contact": "middle"}, "neither 'tip' nor 'top'"),
        ({"first contact": "top", "contact arrangement": "matrix", "number of columns": 1}, "linear probes only"),
        ({"contact arrangement": "matrix", "number of columns": 1, "number of rows": 2}, "3 contacts do not fit"),
        ({"contact arrangement": "matrix"}, "'number of columns' is not set"),
        ({"contact pitch": "1 inch"}, "contact pitch unit 'inch'"),
    ]:
        with pytest.raises(ValueError, match=error):
            _create_probe({"name": "X", "number of contacts": 3, "contact pitch": 100, **cfg})


def test_explicit_positions_and_spacings(placed):
    """'contact positions' place each contact (dict or compiled entry, origin in the odML); a matrix takes its
    row and column spacing; mismatched counts and spacings of a linear probe are errors."""
    hexa = {"name": "Hex", "number of contacts": 3, "contact pitch": "", "contact arrangement": "", "total span": "",
            "contact positions": {"contact ids": ["a", "b", "c"], "x": [0, 30, 15], "y": [0, 0, 26]}}
    probe = _create_probe(hexa)
    assert list(probe.contact_ids) == ["a", "b", "c"]
    assert probe.contact_positions.tolist() == [[0, 0], [30, 0], [15, 26]]
    doc = placed({"01": [{"probe": "Hex", "brain region": "V1"}]}, [hexa])
    group = build_session_probe_group(doc, sessions(doc)[0])
    enrich_odml_positions(doc, group)
    section, = doc["Hardware"]["Probes"].sections
    x = section.properties["contact_x"]
    assert (x.values, x.value_origin) == (["0.0,30.0,15.0"], "the YAML's contact positions")
    geometry = {n: (section.properties[n].values, section.properties[n].value_origin)
                for n in ("contact pitch", "contact arrangement", "total span")}
    assert geometry == {"contact pitch": ([], None), "contact arrangement": ([], None),  # not stated, not derived
                        "total span": (["26 um"], "the YAML's contact positions")}
    grid = _create_probe({"name": "G", "number of contacts": 4, "contact arrangement": "matrix", "number of columns": 2,
                          "row spacing": "20 micrometer", "column spacing": "0.03 millimeter"})
    assert sorted(set(grid.contact_positions[:, 0])) == [0, 30] and sorted(set(grid.contact_positions[:, 1])) == [0, 20]
    for cfg, error in [({**hexa, "number of contacts": 4}, "3 contact ids, 3 x and 3 y for 4 contacts"),
                       ({"name": "L", "number of contacts": 2, "row spacing": "20 micrometer"}, "for matrix probes")]:
        with pytest.raises(ValueError, match=error):
            _create_probe(cfg)


def test_pitch_in_micrometer():
    """Pitch text or numbers with units, micrometer by default, 0 for n/a."""
    assert parse_pitch("200 um") == parse_pitch("0.2 millimeter") == parse_pitch(200) == parse_pitch(0.2, "mm") == 200
    assert parse_pitch("n/a") == 0.0


def test_library_lookup(monkeypatch):
    """404 is no library probe (None); any other failure raises; names with spaces are not looked up."""
    assert get_from_library("plexon", "nope") is None
    for exc in (URLError("no network"), HTTPError("https://example.invalid", 500, "Server Error", {}, None)):
        def failing(exc=exc, **kwargs):
            raise exc
        monkeypatch.setattr("fairephys.probes.library.get_probe", failing)
        with pytest.raises(type(exc)):
            get_from_library("plexon", "S-Probe")
    assert get_from_library("blackrock neurotech", "Utah") is None  # failing() was not called
    assert get_from_library("blackrock", "Utah Array") is None
    assert library_key({"manufacturer": "Cambridge NeuroTech"}) == "cambridge neurotech"
    assert library_key({"manufacturer": "X", "library manufacturer": "cambridgeneurotech"}) == "cambridgeneurotech"


def test_library_probe_contacts_selected(monkeypatch, placed):
    """Identifiers cut a library probe (fewer contacts without them is an error) or, at its full count, name its
    contacts (a count mismatch is an error); a placement's pick its channels in order."""
    monkeypatch.setattr("fairephys.probes.library.get_probe", _library(8))
    cfg = {"name": "NP", "manufacturer": "imec", "model": "NP1", "number of contacts": 3}
    probe = _create_probe({**cfg, "electrode identifiers": ["e5", "e1", "e7"]})
    assert list(probe.contact_ids) == ["e5", "e1", "e7"] and probe.annotations["from_library"]
    with pytest.raises(ValueError, match="3 of the 8 library contacts recorded and no 'electrode identifiers'"):
        _create_probe(cfg)
    with pytest.raises(ValueError, match="a library probe keeps the library's contact order"):
        _create_probe({**cfg, "number of contacts": 8, "first contact": "top"})
    whole = _create_probe({**cfg, "number of contacts": 8, "electrode identifiers": [str(i) for i in range(1, 9)]})
    assert list(whole.contact_ids) == [str(i) for i in range(1, 9)]  # at the full count the list names the contacts
    with pytest.raises(ValueError, match="2 electrode identifiers for 8 contacts"):
        _create_probe({**cfg, "number of contacts": 8, "electrode identifiers": ["1", "2"]})

    probes = [{**cfg, "number of contacts": 8}]
    doc = placed({"01": [{"probe": "NP", "brain region": "V1", "electrode identifiers": ["e6", "e2", "e4"]}]}, probes)
    probe = build_session_probe_group(doc, sessions(doc)[0]).probes[0]
    assert list(probe.contact_ids) == ["e6", "e2", "e4"] and list(probe.device_channel_indices) == [0, 1, 2]
    assert probe.contact_positions[:, 1].tolist() == [120.0, 40.0, 80.0]
    doc = placed({"01": [{"probe": "NP", "brain region": "V1", "electrode identifiers": ["e6", "x"]}]}, probes)
    with pytest.raises(ValueError, match=r"Placement 1 \(NP\): electrode identifiers not among the probe's contacts"):
        build_session_probe_group(doc, sessions(doc)[0])


def test_positions_are_local_to_each_probe(monkeypatch, placed):
    """Probes are spaced by x_offset; odML, BIDS and geometry use each probe's frame, int positions too."""
    group = build_probe_group(PROBES)
    assert [p.annotations["x_offset"] for p in group.probes] == [0.0, 800.0, 1600.0]
    assert group.probes[1].contact_positions[:, 0].min() == 800.0

    monkeypatch.setattr("fairephys.probes.library.get_probe", _library(2, dtype=np.int64))
    probes = [{"name": "NP", "manufacturer": "imec", "model": "NP1", "number of contacts": 2}, PROBES[2]]
    doc = placed({"01": [{"probe": "NP", "brain region": "V1"}]}, probes)
    group = build_session_probe_group(doc, sessions(doc)[0])
    assert group.probes[0].contact_positions.dtype == np.float64  # library integers made float
    assert build_probe_group(probes[::-1]).probes[1].annotations["x_offset"] > 0  # moved beside another
    enrich_odml_positions(doc, group)
    np_sec, v_sec = doc["Hardware"]["Probes"].sections
    assert (np_sec.properties["contact_x"].values, np_sec.properties["contact_y"].values) == (
        ["11.0,11.0"], ["0.0,20.0"])
    assert "contact_x" not in v_sec.properties  # not placed in the session
    ids = np_sec.properties["electrode identifiers"]
    assert (ids.values, ids.value_origin) == (["e0", "e1"], "probeinterface")
    assert [(e["x"], e["y"]) for e in extract_electrodes(doc, sessions(doc)[0], group)] == [(11.0, 0.0), (11.0, 20.0)]
    assert _probe_positions(group.probes, ValidationReport("t")) == {"NP1_e0": (11.0, 0.0), "NP1_e1": (11.0, 20.0)}

    stranger = build_probe_group([{"name": "Z", "number of contacts": 1, "contact pitch": 0}])
    with pytest.raises(ValueError, match=r"Probe 'Z' is no Hardware/Probes entry \(NP, V-Probe\)"):
        enrich_odml_positions(doc, stranger)


def test_write_session_probe_group_empties_its_folder(placed, tmp_path):
    """The session's probe group replaces whatever its folder held (files of earlier device ids)."""
    doc = placed({"01": [{"probe": "S-Probe B"}]})
    folder = tmp_path / "probes" / "sub-A_ses-01"
    folder.mkdir(parents=True)
    (folder / "S-Probe01.json").write_text("{}")
    path = write_session_probe_group(doc, sessions(doc)[0], folder, title="sub-A ses-01")
    assert path == folder / "probe_group.json"
    assert sorted(p.name for p in folder.iterdir()) == [
        "S-Probe02.json", "S-Probe02.png", "probe_group.json", "probe_group.png"]
