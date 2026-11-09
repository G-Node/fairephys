"""NWB metadata from the odML, the converters' shared steps, devices and writing."""

import logging
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import h5py
import numpy as np
import odml
import pandas as pd
import pytest
from neuroconv import NWBConverter
from neuroconv.tools.testing.mock_interfaces import MockRecordingInterface
from probeinterface import write_probeinterface
from pynwb import NWBHDF5IO, TimeSeries
from pynwb.behavior import Position, SpatialSeries
from spikeinterface.core import NumpyRecording

from conftest import empty_nwbfile, linear_probe, probe_group, sessions
from fairephys.bids import generate_channels_tsv, generate_electrodes_tsv, generate_probes_tsv
from fairephys.bids.builder import extract_channels, extract_electrodes, extract_probes
from fairephys.converters import (EphysConverter, RecordingConverter, SpikeGLXConverter, add_devices, add_time_intervals,
                                  add_timeseries, add_trials, add_units, build_nwb_metadata, channel_positions,
                                  nidq_times_on_imec_clock, read_nidq_channels, recording_scale, recording_span,
                                  write_nwbfile)
from fairephys.converters.base import (_anchor_first_sample, add_unplaced_group, attach_probes, neo_channel_names,
                                       set_electrode_locations, stub_recording)
from fairephys.converters.devices import without_device_models
from fairephys.converters.metadata import load_nwb_registry
from fairephys.converters.spikeglx import _find_meta
from fairephys.converters.tdt_sev import SEV_HEADER, TdtSevConverter
from fairephys.enrichment.neo import enrich_document
from fairephys.placements import UNPLACED_GROUP
from fairephys.probes import build_session_probe_group
from fairephys.validation import ValidationReport
from fairephys.validation.geometry import check_probe_geometry
from fairephys.validation.nwb import validate_nwb
from fairephys.validation.sanity import run_sanity_checks

_PLACED = {"Electrode Placements": [{"probe": "Utah Array", "brain region": "M1"}]}


def test_subject_and_nwbfile_metadata(make_doc):
    """Subject and NWBFile from the odML: ISO age, weight, fallbacks, experimenters, related publications (a DOI
    as doi:<DOI>, another identifier as given, the citation without identifier), unique id."""
    doc = make_doc(Dataset={"Related Publications": [
        {"identifier": "https://doi.org/10.7554/eLife.51322", "relation": "IsSupplementTo",
         "citation": "Author (2020) eLife 9:e51322."},
        {"identifier": "https://github.com/lab/code", "relation": "IsReferencedBy", "citation": "Code."},
        {"identifier": "", "relation": "IsSupplementTo", "citation": "Doe, J. A thesis (2020)."}]})
    session = sessions(doc)[0]
    meta = build_nwb_metadata(doc, session, overrides={"lab": "Other"})
    assert meta["Subject"] == {"subject_id": "MonkeyA", "species": "Macaca mulatta", "sex": "F", "age": "P7Y",
                               "weight": "9.5 kg", "description": "MonkeyA"}  # nickname, else the id
    nwb = meta["NWBFile"]
    uuid.UUID(nwb["identifier"])
    assert nwb["identifier"] != build_nwb_metadata(doc, session)["NWBFile"]["identifier"]
    assert {k: nwb[k] for k in ("session_id", "session_description", "experiment_description", "experimenter",
                                "keywords", "lab", "related_publications")} == {
        "session_id": "ses01", "session_description": "Test recording", "experiment_description": "A test experiment",
        "experimenter": ["Doe, Jane"], "keywords": ["test", "ephys"], "lab": "Other",
        "related_publications": ["doi:10.7554/eLife.51322", "https://github.com/lab/code",
                                 "Doe, J. A thesis (2020)."]}


def test_no_placeholders(make_doc):
    """Unknown or missing values leave the field out (never 'unknown'); a fractional age stays exact."""
    doc = make_doc(Subject={"biological sex": "unknown", "species": None, "age": "7.5 year", "weight": None},
                   Dataset={"Custodian": None})
    meta = build_nwb_metadata(doc, sessions(doc)[0])
    assert not {"sex", "species", "weight"} & set(meta["Subject"]) and meta["Subject"]["age"] == "P7.5Y"
    assert "lab" not in meta["NWBFile"] and "institution" not in meta["NWBFile"]
    registry = load_nwb_registry()
    assert not any("default" in spec for section in ("NWBFile", "Subject") for spec in registry[section].values())
    ecephys = [f for k in ("Device", "ProbeDevice", "ElectrodeGroup")
               for f in registry["Ecephys"][k]["fields"].values()]
    assert not any("default" in field or "default_template" in field for field in ecephys)


@pytest.mark.parametrize("changes, error", [
    ({"Dataset": {"short description": None}}, "session_description is required; fill the session 'description'"),
    ({"Subject": {"age": "7 fortnight"}}, "age unit 'fortnight'"),
    ({"Hardware": {"Recording System": {"name": None, "model": None}}},
     "Hardware/Recording System: NWB name is required"),
    ({"Hardware": {"Probes": [{"name": "Utah Array"}]}},
     "NWB description is required; fill the probe's 'number of contacts'"),
])
def test_required_fields_are_errors(make_doc, changes, error):
    """A value NWB requires that the odML lacks is an error naming what to fill."""
    doc = make_doc(changes)
    with pytest.raises(ValueError, match=error):
        build_nwb_metadata(doc, sessions(doc)[0])


def test_experimenters_are_who_recorded(make_doc, caplog):
    """The session's 'performed by' (completed from the contributors), else the data collectors; never curators."""
    people = [{"given name": "Ann", "family name": "Lee", "roles": ["data collection", "coordination"]},
              {"given name": "Cur", "family name": "Ator", "roles": ["data management"]}]
    doc = make_doc(Dataset={"Contributors": people})
    assert build_nwb_metadata(doc, sessions(doc)[0])["NWBFile"]["experimenter"] == ["Lee, Ann"]
    doc = make_doc(Dataset={"Contributors": people[1:]},
                   Sessions=[{"session id": "01", "date": "2024-03-15", "start time": "10:00:00",
                              "performed by": "Ator", **_PLACED}])
    assert build_nwb_metadata(doc, sessions(doc)[0])["NWBFile"]["experimenter"] == ["Ator, Cur"]
    sessions(doc)[0].properties["performed by"].values = ["Nobody"]
    with caplog.at_level("WARNING"):
        assert build_nwb_metadata(doc, sessions(doc)[0])["NWBFile"]["experimenter"] == ["Nobody"]
    assert caplog.records
    doc = make_doc(Dataset={"Contributors": people[1:]})
    assert "experimenter" not in build_nwb_metadata(doc, sessions(doc)[0])["NWBFile"]


@pytest.mark.parametrize("start, date, tz, run, expected", [
    ("10:30:00", "2024-03-15", "Europe/Paris", None, "2024-03-15T10:30:00+01:00"),  # time of day: local
    ("2024-08-19T10:13:30.901000+00:00", "2024-08-19", "Europe/Zurich", None, "2024-08-19T12:13:30.901000+02:00"),
    ("10:30:00", "2024-03-15", None, None, "2024-03-15T10:30:00+00:00"),  # no time zone: UTC (warned)
    ("10:30:00", "2024-03-15", "Europe/Paris", 2, "2024-03-15T12:00:00+01:00"),  # the run's own start
    ("10:30:00", "2024-03-15", "Europe/Paris", 1, "2024-03-15T10:30:00+01:00"),  # a run without entry
    ("", "2024-03-15", "Europe/Paris", None, "no start time"),
    ("10:30:00", "", "Europe/Paris", None, "no date"),
])
def test_session_start_time(make_doc, start, date, tz, run, expected):
    """session_start_time in the dataset time zone: the run's start_time, else the session's start time."""
    doc = make_doc(Dataset={"time zone": tz},
                   Sessions=[{"session id": "01", "date": date, "start time": start, **_PLACED}])
    neo = odml.Section("Neo Metadata", type="neo.recording_metadata")
    runs = odml.Section("runs", type="neo.runs")
    entry = odml.Section("run 2", type="neo.run")
    entry.extend([odml.Property("run", values=[2]), odml.Property("start_time", values=["2024-03-15T11:00:00+00:00"])])
    runs.append(entry)
    neo.append(runs)
    sessions(doc)[0].append(neo)
    if expected.startswith("no "):
        with pytest.raises(ValueError, match=expected):
            build_nwb_metadata(doc, sessions(doc)[0], run=run)
    else:
        start = build_nwb_metadata(doc, sessions(doc)[0], run=run)["NWBFile"]["session_start_time"]
        assert start.isoformat() == expected


def test_ecephys_devices_and_groups(make_doc, caplog):
    """Devices with serial number; one group per placement with its location and the run's position; a
    placement without a brain region gets neuroconv's placeholder 'unknown' with its hemisphere and a warning."""
    probe = {"name": "Utah Array", "manufacturer": "Blackrock", "model": "Utah", "probe type": "utah",
             "number of contacts": 96, "serial number": "SN-1"}
    placement = {"probe": "Utah Array", "brain region": "M1", "hemisphere": "left", "ML": "6 millimeter",
                 "AP": "7 millimeter", "DV": "14 millimeter", "DV by run": {"8": "15 millimeter"},
                 "anatomical reference": "chamber center"}
    doc = make_doc(Hardware={"Probes": [probe]}, Sessions=[
        {"session id": "01", "date": "2024-03-15", "start time": "10:00:00", "Electrode Placements": [placement]}])
    ecephys = build_nwb_metadata(doc, sessions(doc)[0], run=8)["Ecephys"]
    assert ecephys["Device"] == [
        {"name": "MySystem", "description": "Blackrock Cerebus", "manufacturer": "Blackrock", "model": "Cerebus"},
        {"name": "Utah Array", "description": "Blackrock Utah", "manufacturer": "Blackrock", "model": "Utah",
         "serial_number": "SN-1"}]
    assert ecephys["ElectrodeGroup"] == [{
        "name": "Utah", "device": "Utah Array", "location": "M1, left hemisphere", "position": [6.0, 7.0, 15.0],
        "description": "96 channel utah; position (x, y, z) = (ML, AP, DV) in mm from chamber center"}]
    assert ecephys["ElectricalSeries"]["name"] == "ElectricalSeries"
    assert {"group_name", "brain_area"} <= {c["name"] for c in ecephys["Electrodes"]}
    bare = make_doc(Hardware={"Probes": [probe]}, Sessions=[
        {"session id": "01", "date": "2024-03-15", "start time": "10:00:00",
         "Electrode Placements": [{"probe": "Utah Array", "brain region": "", "hemisphere": "left"}]}])
    with caplog.at_level(logging.WARNING, logger="fairephys"):
        group, = build_nwb_metadata(bare, sessions(bare)[0])["Ecephys"]["ElectrodeGroup"]
    assert group["location"] == "unknown, left hemisphere"  # neuroconv's placeholder, the hemisphere kept
    assert "Placement of probe 'Utah Array': no brain region" in caplog.text


def _recording(n_channels, n_samples=1000):
    return NumpyRecording(np.zeros((n_samples, n_channels), dtype="int16"), sampling_frequency=1000.0)


def test_attach_probes_groups_channels_by_probe():
    """Channels grouped by probe id, local positions and contact ids; unplaced without; a misfit raises."""
    group = probe_group(linear_probe("A", ["01", "02", "03"], [0, 1, 2]),
                        linear_probe("B", ["01", "02"], [3, 4], x_offset=800))
    placed = attach_probes(_recording(5), group)
    assert placed.has_probe() and list(placed.get_property("group_name")) == ["A"] * 3 + ["B"] * 2
    np.testing.assert_allclose(placed.get_property("location"), [[0, 0], [0, 100], [0, 200], [0, 0], [0, 100]])
    partly = attach_probes(_recording(6), group)
    assert not partly.has_probe() and list(partly.get_property("group_name"))[-1] == UNPLACED_GROUP
    assert list(partly.get_property("electrode_name")) == ["01", "02", "03", "01", "02", ""]
    assert np.isnan(partly.get_property("location")[5]).all()
    with pytest.raises(ValueError, match="no device for the 'unplaced' electrode group"):
        add_unplaced_group(partly, {"Ecephys": {}})
    with pytest.raises(ValueError, match="does not fit the recording's 4 channels"):
        attach_probes(_recording(4), group)


def test_neo_channel_names_are_the_own_streams_in_channel_order():
    """Two streams with the same channel ids: the names of the recording's stream, in the recording's order."""
    header = {"signal_channels": np.array(
        [("Broa 1", "1", "1"), ("Broa 2", "2", "1"), ("EMGx 1", "1", "2"), ("EMGx 2", "2", "2")],
        dtype=[("name", "U8"), ("id", "U2"), ("stream_id", "U2")])}
    recording = SimpleNamespace(neo_reader=SimpleNamespace(header=header), stream_id="2",
                                get_channel_ids=lambda: ["2", "1"])
    assert neo_channel_names(recording) == ["EMGx 2", "EMGx 1"]


def test_electrode_locations_need_every_group():
    """brain_area from each channel's group; no group or no location is an error."""
    recording = _recording(3)
    with pytest.raises(ValueError, match="in no electrode group"):
        set_electrode_locations(recording, {})
    recording.set_property("group_name", ["A", "A", "B"])
    metadata = {"Ecephys": {"ElectrodeGroup": [{"name": "A", "location": "M1"}, {"name": "B"}]}}
    with pytest.raises(ValueError, match=r"electrode groups \['B'\] have no ElectrodeGroup location"):
        set_electrode_locations(recording, metadata)
    metadata["Ecephys"]["ElectrodeGroup"][1]["location"] = "V1"
    set_electrode_locations(recording, metadata)
    assert list(recording.get_property("brain_area")) == ["M1", "M1", "V1"]


class MockConverter(EphysConverter):
    data_interface_classes = {"Recording": MockRecordingInterface}


@pytest.fixture
def converted(make_doc, tmp_path):
    """A compiled session of two probes on 5 of 7 channels: its probe group, BIDS files and a test NWB."""
    doc = make_doc(Hardware={"Probes": [
        {"name": "P", "manufacturer": "Plexon", "model": "S", "probe type": "silicon-probe", "number of contacts": 3,
         "contact pitch": 100},
        {"name": "Q", "probe type": "silicon-probe", "number of contacts": 2, "contact pitch": 50}]}, Sessions=[
        {"session id": "01", "date": "2024-01-01", "start time": "2024-01-01T10:00:00+00:00", "Electrode Placements": [
            {"probe": "P", "brain region": "M1", "channels": "1-3"},
            {"probe": "Q", "brain region": "V1", "channels": "5-6"}]}])
    session = sessions(doc)[0]
    enrich_document(doc, {"signal_streams": [{"id": "0", "name": "s", "sampling_rate": 30000.0}], "signal_channels": [
        {"name": str(i), "units": "uV", "sampling_rate": 30000.0, "stream_id": "0"} for i in range(7)]}, "01")
    group = build_session_probe_group(doc, session)
    files = {"probe_group": tmp_path / "probe_group.json", "nwb": tmp_path / "x.nwb",
             **{f"{name}_tsv": tmp_path / f"{name}.tsv" for name in ("electrodes", "channels", "probes")}}
    write_probeinterface(files["probe_group"], group)
    generate_electrodes_tsv(extract_electrodes(doc, session, group), files["electrodes_tsv"])
    generate_probes_tsv(extract_probes(doc, session), files["probes_tsv"])
    generate_channels_tsv(extract_channels(session, group, channel_types={"default": "BB"}), files["channels_tsv"])
    converter = MockConverter(source_data={"Recording": {"num_channels": 7, "durations": (1.0,)}},
                              probe_group_path=files["probe_group"])
    converter.recording._recording_segments[0].t_start = 0.25
    files["nwb"].write_text("released")
    files["nwb"].chmod(0o444)  # a released file: a read-only annex link is replaced, not written through
    trials = pd.DataFrame({"start_time": [0.25, 0.252, 2.0], "stop_time": [0.251, 0.3, 3.0], "condition": [1, 2, 3]})
    hook = lambda nwb: add_trials(nwb, trials, {"condition": "the condition number"}, stub_test=True)
    converter.run_conversion(files["nwb"], metadata=build_nwb_metadata(doc, session), stub_test=True,
                             add_to_nwbfile=hook)
    assert converter.recording.get_num_samples(0) == 30000  # the full recording stays on the converter
    return files


def test_conversion_end_to_end(converted):
    """The test NWB holds 100 samples from the first sample on, every device with its model, unplaced channels,
    the hook's trials inside the test file's span."""
    with NWBHDF5IO(str(converted["nwb"]), "r") as io:
        nwb = io.read()
        assert nwb.acquisition["ElectricalSeries"].data.shape == (100, 7)
        assert nwb.session_start_time == datetime(2024, 1, 1, 9, 59, 59, 750000, tzinfo=timezone.utc)
        assert nwb.trials.to_dataframe()["condition"].tolist() == [1, 2]
        assert (set(nwb.devices), set(nwb.device_models)) == ({"MySystem", "P", "Q"}, {"Cerebus", "S"})
        assert nwb.electrode_groups[UNPLACED_GROUP].device.name == "MySystem"
        electrodes = nwb.electrodes.to_dataframe()
        assert list(electrodes["group_name"]) == ["S"] * 3 + [UNPLACED_GROUP] + ["probe02"] * 2 + [UNPLACED_GROUP]
        assert list(electrodes["location"])[:4] == ["M1", "M1", "M1", "no electrode placed"]


def test_converted_session_passes_the_sanity_checks(converted, tmp_path):
    """ProbeInterface, BIDS and NWB of a converted session agree in every cross-format check and the inspector."""
    report = run_sanity_checks(None, converted, "ses01", tmp_path / "validation")
    validate_nwb(converted["nwb"], report)
    assert (report.errors, report.warnings) == ([], [])
    assert report.info["nwb_geometry"] == "PASSED (5 electrodes match ProbeInterface)"
    assert report.info["bids_electrode_mapping"] == "PASSED" and report.info["channel_names_matched"] is True
    saved = (tmp_path / "validation" / "ses01.md").read_text()
    assert all(str(report.info[k]) in saved for k in ("nwb_geometry", "bids_geometry", "duration_seconds"))
    assert report.info["nwb_inspector_x"] == "No issues"


def test_recording_converter_writes_any_recording(make_doc, tmp_path):
    """Any SpikeInterface recording: int16 counts with the gain as conversion, probe positions, the test file rule,
    the first-sample anchoring, the hook's content; the sanity checks take the recording as the raw side."""
    doc = make_doc(Hardware={"Probes": [
        {"name": "P", "probe type": "silicon-probe", "number of contacts": 3, "contact pitch": 100},
        {"name": "Q", "probe type": "silicon-probe", "number of contacts": 2, "contact pitch": 50}]}, Sessions=[
        {"session id": "01", "date": "2024-01-01", "start time": "2024-01-01T10:00:00+00:00", "Electrode Placements": [
            {"probe": "P", "brain region": "M1", "channels": "1-3"},
            {"probe": "Q", "brain region": "V1", "channels": "4-5"}]}])
    session = sessions(doc)[0]
    group = build_session_probe_group(doc, session)
    write_probeinterface(tmp_path / "pg.json", group)
    recording = NumpyRecording(np.arange(5000, dtype="int16").reshape(1000, 5), sampling_frequency=1000.0, t_starts=[0.25])
    recording.set_channel_gains(0.195)
    recording.set_channel_offsets(0.0)
    converter = RecordingConverter(recording, tmp_path / "pg.json")
    hook = lambda nwb: nwb.add_acquisition(TimeSeries(name="eye", data=np.zeros(3), unit="deg", rate=1.0))
    converter.run_conversion(tmp_path / "stub.nwb", build_nwb_metadata(doc, session), stub_test=True, add_to_nwbfile=hook)
    with NWBHDF5IO(str(tmp_path / "stub.nwb"), "r") as io:
        nwb = io.read()
        series = nwb.acquisition["ElectricalSeries"]
        assert (series.data.dtype, series.data.shape, series.starting_time) == (np.dtype("int16"), (100, 5), 0.25)
        assert (series.conversion, series.offset) == (pytest.approx(0.195e-6), 0.0)
        assert series.data[3].tolist() == [15, 16, 17, 18, 19]
        assert nwb.session_start_time == datetime(2024, 1, 1, 9, 59, 59, 750000, tzinfo=timezone.utc)
        assert nwb.acquisition["eye"].data.shape == (3,)
        electrodes = nwb.electrodes.to_dataframe()
        np.testing.assert_allclose(electrodes[["rel_x", "rel_y"]].to_numpy(), channel_positions(group, 5))
        assert list(electrodes["location"]) == ["M1"] * 3 + ["V1"] * 2

    converter.run_conversion(tmp_path / "full.nwb", build_nwb_metadata(doc, session))
    report = run_sanity_checks(None, {"nwb": tmp_path / "full.nwb"}, "s", tmp_path / "val", raw_recording=recording)
    assert (report.errors, report.warnings) == ([], [])
    assert (report.info["data_integrity_check"], report.info["nwb_samples"]) == ("PASSED", 1000)
    assert report.info["channel_distinctness"] == "PASSED (1 streams, segment 0)"
    with pytest.raises(ValueError, match="not both"):
        run_sanity_checks(tmp_path, {}, "s", tmp_path, recording_format="tdt", raw_recording=recording)


def test_extras_write_the_hooks_content_cut_to_the_test_file(make_doc, tmp_path):
    """Trials, intervals, units and series through the helpers: in a test file cut to the recording's span
    (rows overlapping, samples inside and at least the first 100, spike times inside, presence intervals clipped,
    no units table without a spike in the span), in a full file whole; a column without a description, a channel
    not in the table and an unknown parent are errors."""
    doc = make_doc(Hardware={"Probes": [{"name": "P", "probe type": "silicon-probe", "number of contacts": 2,
                                         "contact pitch": 100}]},
                   Sessions=[{"session id": "01", "date": "2024-01-01", "start time": "2024-01-01T10:00:00+00:00",
                              "Electrode Placements": [{"probe": "P", "brain region": "M1", "channels": "1-2"}]}])
    session = sessions(doc)[0]
    write_probeinterface(tmp_path / "pg.json", build_session_probe_group(doc, session))
    recording = NumpyRecording(np.zeros((1000, 2), dtype="int16"), sampling_frequency=1000.0, t_starts=[0.25])
    recording.set_channel_gains(0.25)
    recording.set_channel_offsets(0.0)
    recording.set_property("channel_name", ["c1", "c2"])
    trials = pd.DataFrame({"start_time": [0.0, 0.3, 5.0], "stop_time": [0.2, 0.4, 6.0], "condition": [1, 2, 3]})
    units = [{"spike_times": [0.26, 0.3, 2.0], "obs_intervals": [[0.0, 10.0]], "channels": ["c2"], "quality": "SUA"},
             {"spike_times": [5.0], "obs_intervals": [[4.0, 10.0]], "channels": ["c1"], "quality": "MUA"}]

    def hook(stub_test):
        def add(nwb):
            assert recording_span(nwb) == (0.25, 0.349 if stub_test else 1.249)
            add_trials(nwb, trials, {"condition": "the condition number"}, stub_test=stub_test)
            add_time_intervals(nwb, "states", trials.rename(columns={"condition": "state"}), {"state": "the state"},
                               "behavioural states", stub_test=stub_test)
            if stub_test:  # no spike of any unit in the span: no table at all
                outside = [{"spike_times": [5.0], "obs_intervals": [[0.0, 10.0]]}]
                assert add_units(nwb, outside, {}, "units", resolution=1e-4, stub_test=True) == 0 and nwb.units is None
            add_units(nwb, units, {"quality": "SUA or MUA"}, "the lab's units", resolution=1e-4, stub_test=stub_test)
            behavior = nwb.create_processing_module("behavior", "signals")
            add_timeseries(nwb, "aux", np.arange(2000.0), 1000.0, 0.0, "V", "an analog input", stub_test=stub_test)
            add_timeseries(nwb, "eye", np.zeros((50, 2)), 100.0, 0.0, "V", "eye voltages", parent=behavior,
                           stub_test=stub_test)
            position = Position(name="BodyTracking")
            behavior.add(position)
            add_timeseries(nwb, "head", np.zeros((50, 3)), 100.0, 0.0, "meters", "a keypoint", parent=position,
                           kind=SpatialSeries, reference_frame="room", stub_test=stub_test)
            early = add_timeseries(nwb, "early", np.zeros(10), 1000.0, 0.0, "V", "before the test file", stub_test=stub_test)
            assert (early is None) == stub_test  # ends before the span: not written
            late = add_timeseries(nwb, "late", np.zeros(10), 1000.0, 5.0, "V", "after the test file", stub_test=stub_test)
            assert late.data.shape == (10,)  # starts after the span: its first samples stay
        return add

    converter = RecordingConverter(recording, tmp_path / "pg.json")
    metadata = build_nwb_metadata(doc, session)
    converter.run_conversion(tmp_path / "stub.nwb", metadata, stub_test=True, add_to_nwbfile=hook(True))
    converter.run_conversion(tmp_path / "full.nwb", metadata, add_to_nwbfile=hook(False))
    with NWBHDF5IO(str(tmp_path / "stub.nwb"), "r") as io:
        nwb = io.read()
        assert nwb.trials.to_dataframe()["condition"].tolist() == [2]
        assert nwb.intervals["states"].to_dataframe()["state"].tolist() == [2]
        unit, = nwb.units.to_dataframe().to_dict("records")
        assert (unit["quality"], unit["spike_times"].tolist(), unit["obs_intervals"].tolist()) == (
            "SUA", [0.26, 0.3], [[0.25, 0.349]])
        assert unit["electrodes"].index.tolist() == [1] and nwb.units.resolution == 1e-4
        aux = nwb.acquisition["aux"]
        assert (aux.starting_time, aux.data[:].tolist()) == (0.25, list(range(250, 350)))
        assert nwb.processing["behavior"]["eye"].data.shape == (25, 2)  # the span holds 10 frames: 100 asked, 25 left
        head = nwb.processing["behavior"]["BodyTracking"]["head"]
        assert (head.starting_time, head.data.shape, head.reference_frame) == (0.25, (25, 3), "room")
    with NWBHDF5IO(str(tmp_path / "full.nwb"), "r") as io:
        nwb = io.read()
        assert len(nwb.trials) == 3 and len(nwb.units) == 2 and nwb.acquisition["aux"].data.shape == (2000,)
        assert nwb.units.to_dataframe()["spike_times"].iloc[0].tolist() == [0.26, 0.3, 2.0]

    nwb = empty_nwbfile()
    with pytest.raises(ValueError, match=r"no description for the columns \['condition'\]"):
        add_trials(nwb, trials, {})
    with pytest.raises(ValueError, match=r"descriptions of columns the data lack: \['other'\]"):
        add_time_intervals(nwb, "states", trials, {"condition": "c", "other": "o"}, "states")
    with pytest.raises(ValueError, match=r"channels \['c9'\] are not in the electrode table"):
        add_units(nwb, [{"spike_times": [1.0], "channels": ["c9"]}], {}, "units", 1e-4)
    assert add_trials(nwb, trials.iloc[3:], {"condition": "c"}) == 0 and nwb.trials is None  # no row, no table
    with pytest.raises(TypeError, match="parent must be None, a ProcessingModule"):
        add_timeseries(nwb, "x", np.zeros(3), 1.0, 0.0, "V", "x", parent="behavior")


def test_recording_converter_needs_the_scale(doc, session, tmp_path):
    recording = NumpyRecording(np.zeros((300, 2), dtype="int16"), sampling_frequency=1000.0)
    with pytest.raises(ValueError, match="gain_to_uV/offset_to_uV"):
        RecordingConverter(recording).run_conversion(tmp_path / "x.nwb", build_nwb_metadata(doc, session))


@pytest.mark.parametrize("system, expected", [
    ({"gain": 5.86, "gain unit": "uV per count", "adc zero": 2048}, (5.86, -2048 * 5.86)),  # unsigned 12-bit counts
    ({"gain": 1.94875978e-07, "gain unit": "V per count", "adc zero": 0}, (0.194875978, 0.0)),  # signed counts
    ({"gain": 0.25, "gain unit": "uV per count"}, "adc zero is not set"),
    ({"gain": 0.25}, "gain unit None is not one of"),
    ({"gain": 0.25, "gain unit": "mV"}, "gain unit 'mV' is not one of"),
    ({"gain unit": "uV per count"}, "gain is not set"),
    ({"gain": 0, "gain unit": "uV per count"}, "gain 0 is not strictly positive"),
    ({"gain": -0.25, "gain unit": "uV per count"}, "gain -0.25 is not strictly positive"),
])
def test_recording_scale_from_the_recording_system(make_doc, system, expected):
    """gain_to_uV and offset_to_uV from gain, gain unit and adc zero of the document or its section; a missing
    gain or one not strictly positive, a missing unit, a unit outside the two accepted, or a missing adc zero, is
    an error."""
    doc = make_doc(Hardware={"Recording System": system})
    if isinstance(expected, str):
        with pytest.raises(ValueError, match=expected):
            recording_scale(doc)
    else:
        assert recording_scale(doc) == pytest.approx(expected)
        assert recording_scale(doc["Hardware"]["Recording System"]) == pytest.approx(expected)


def test_sev_files_read_by_their_headers(tmp_path):
    """SEV store, rate, sample type and channels from the 40-byte headers (store code reversed); one store only."""
    def sev(name, store, channel, data):
        header = np.zeros(1, SEV_HEADER)
        header[["file_type", "file_version", "event_name", "channel_num", "sample_width_bytes", "data_format",
                "decimate", "rate"]] = (b"SEV", 4, store.encode()[::-1], channel, 2, 2, 1, 2)
        (tmp_path / name).write_bytes(header.tobytes() + data.astype("<i2").tobytes())
    for ch in (2, 1):
        sev(f"T_B-1_xWav_Ch{ch}.sev", "xWav", ch, np.arange(300) * ch)
    recording = TdtSevConverter(tmp_path).recording
    assert (recording.get_sampling_frequency(), recording.get_dtype(), recording.get_num_samples(0)) == (
        24414.0625, np.dtype("int16"), 300)
    assert list(recording.get_property("channel_name")) == ["xWav 1", "xWav 2"]
    assert recording.get_traces(start_frame=2, end_frame=3).tolist() == [[2, 4]]
    sev("T_B-1_EEGx_Ch1.sev", "EEGx", 1, np.zeros(10))
    with pytest.raises(ValueError, match="stores .*name the store"):
        TdtSevConverter(tmp_path)
    assert TdtSevConverter(tmp_path, store="xWav").recording.get_num_channels() == 2


def test_test_files_and_first_sample(monkeypatch):
    """Test files keep max(100, channels + 1) samples only while writing; start moves back by t_start."""
    assert stub_recording(_recording(384, 1000)).get_num_samples(0) == 385
    assert stub_recording(_recording(4, 1000)).get_num_samples(0) == 100

    written = []
    monkeypatch.setattr(NWBConverter, "add_to_nwbfile",
                        lambda self, **kwargs: written.append(self.recording.get_num_samples(0)))
    converter = MockConverter(source_data={"Recording": {"num_channels": 4, "durations": (1.0,)}})
    converter._stub_test = True
    converter.add_to_nwbfile(nwbfile=None)
    assert written == [100] and converter.recording.get_num_samples(0) == 30000

    start = datetime(2024, 2, 27, 10, 6, 49, 830400, tzinfo=timezone.utc)
    anchored = lambda t_start: _anchor_first_sample({"NWBFile": {"session_start_time": start}}, t_start)["NWBFile"]
    assert anchored(0.0034)["session_start_time"] == datetime(2024, 2, 27, 10, 6, 49, 827000, tzinfo=timezone.utc)
    assert anchored(0.0)["session_start_time"] == start


def test_devices_writer_and_channel_positions(tmp_path):
    """Devices once with DeviceModels; files compressed; channel positions local, NaN on no probe."""
    meta = {"Ecephys": {"Device": [
        {"name": "System", "description": "Blackrock Cerebus", "manufacturer": "Blackrock", "model": "Cerebus"},
        {"name": "Utah", "description": "d", "manufacturer": "Blackrock", "model": "Utah", "serial_number": "SN-1"},
        {"name": "Other", "description": "no maker known"}]}}
    nwb = empty_nwbfile()
    add_devices(nwb, meta)
    add_devices(nwb, meta)
    assert (set(nwb.devices), set(nwb.device_models)) == ({"System", "Utah", "Other"}, {"Cerebus", "Utah"})
    assert (nwb.devices["Utah"].model.manufacturer, nwb.devices["Utah"].serial_number) == ("Blackrock", "SN-1")
    assert nwb.devices["Other"].model is None
    assert without_device_models(meta)["Ecephys"]["Device"][0] == {"name": "System", "description": "Blackrock Cerebus"}

    nwb.add_acquisition(TimeSeries(name="ts", data=np.zeros((20000, 4), dtype="int16"), unit="V", rate=1000.0))
    with h5py.File(write_nwbfile(nwb, tmp_path / "sub" / "t.nwb"), "r") as f:
        assert f["acquisition/ts/data"].compression == "gzip"

    group = probe_group(linear_probe("A", ["1", "2", "3"], [2, 0, 1]),
                        linear_probe("B", ["1", "2"], [3, 4], x_offset=800))
    positions = channel_positions(group, 6)
    np.testing.assert_allclose(positions[:5], [[0, 100], [0, 200], [0, 0], [0, 0], [0, 100]])
    assert np.isnan(positions[5]).all()
    write_probeinterface(tmp_path / "pg.json", group)
    np.testing.assert_allclose(channel_positions(tmp_path / "pg.json", 6), positions)


def test_spikeglx_files(tmp_path):
    """Exactly one .meta per stream; NIDQ XA channels scaled by niAiRangeMax / 32768 only."""
    (tmp_path / "run_g0_t0.imec0.ap.meta").write_text("")
    assert _find_meta(tmp_path, "imec0.ap").name == "run_g0_t0.imec0.ap.meta"
    with pytest.raises(FileNotFoundError, match="0 .meta files of stream imec1.ap"):
        _find_meta(tmp_path, "imec1.ap")
    (tmp_path / "run_g0_t1.imec0.ap.meta").write_text("")
    with pytest.raises(FileNotFoundError, match="2 .meta files of stream imec0.ap"):
        _find_meta(tmp_path, "imec0.ap")

    nidq = tmp_path / "nidq"
    nidq.mkdir()
    raw = np.array([[16384, -8192, 0], [32767, 0, 1]], dtype="int16")
    (nidq / "rec_g0_t0.nidq.bin").write_bytes(raw.tobytes())
    (nidq / "rec_g0_t0.nidq.meta").write_text(
        f"fileName=rec_g0_t0.nidq.bin\nfileSizeBytes={raw.nbytes}\nfileTimeSecs=0.002\nfirstSample=0\n"
        "fileCreateTime=2024-07-12T11:17:17\nnSavedChans=3\nniAiRangeMax=5\nniMAGain=1\nniMNGain=200\n"
        "niSampRate=1000\nsnsMnMaXaDw=0,0,2,1\nsnsSaveChanSubset=all\nniXAChans1=0:1\nniXDChans1=0\n"
        "typeThis=nidq\n~snsChanMap=(0,0,2,1,1)(XA0;0:0)(XA1;1:1)(XD0;2:2)\n")
    data, rate = read_nidq_channels(nidq, ["XA0", "XA1"])
    assert rate == 1000
    np.testing.assert_allclose(data, raw[:, :2].astype(float) * 5 / 32768)  # 16384 counts = 2.5 V


def _sglx_files(folder, name, raw, meta):
    folder.mkdir(exist_ok=True)
    if raw is not None:
        (folder / f"{name}.bin").write_bytes(raw.tobytes())
        meta = {"fileSizeBytes": raw.nbytes, **meta}
    (folder / f"{name}.meta").write_text(f"fileName={name}.bin\nfileCreateTime=2024-07-12T11:17:17\n"
                                         + "".join(f"{k}={v}\n" for k, v in meta.items()))


def _imec_meta(kind, rate, first, n_samples):
    ap, lf = (1, 0) if kind == "ap" else (0, 1)
    return {"fileSizeBytes": n_samples * 4, "firstSample": first, "imSampRate": rate, "nSavedChans": 2,
            "imAiRangeMax": 0.6, "imDatPrb_pn": "NP1000", "imDatPrb_sn": "12345", "snsApLfSy": f"{ap},{lf},1",
            "typeImEnabled": 1, "typeThis": "imec", "~imroTbl": "(0,384)(0 0 0 500 250 1)",
            "~snsChanMap": f"(1,1,1)({kind.upper()}0;0:0)(SY0;1:1)"}


def test_spikeglx_converter_keeps_the_spikeglx_names(tmp_path):
    """Each channel keeps its .meta name ("AP0", not neuroconv's "AP0,LF0" beside an LF stream); the probe of
    the .meta is attached under the device's id."""
    raw = np.zeros((100, 2), "int16")
    for kind, rate in (("ap", 30000), ("lf", 2500)):
        _sglx_files(tmp_path / "run_g0_imec0", f"run_g0_t0.imec0.{kind}", raw,
                    {**_imec_meta(kind, rate, 0, 100), "snsSaveChanSubset": "all"})
    recording = SpikeGLXConverter({"Recording": {"folder_path": str(tmp_path), "stream_id": "imec0.ap"}},
                                  probe_id="probe01").recording
    assert list(recording.get_property("channel_name")) == ["AP0"]
    assert list(recording.get_property("group_name")) == ["probe01"]


def test_spikeglx_converter_writes_ap_and_lf(make_doc, tmp_path):
    """With lf=True the probe's LF stream is a second ElectricalSeries of the same electrode group, on the AP
    .meta's probe (the LF .meta's snsSaveChanSubset names channels 384 to 767, which read_spikeglx cannot slice),
    each stream's channels their own electrode rows under the .meta names with the contact in electrode_name,
    both cut to the test file, the hook's content cut to their span; an LF stream as the recording is an error."""
    folder = tmp_path / "run_g0_imec0"
    _sglx_files(folder, "run_g0_t0.imec0.ap", np.arange(3000, dtype="int16").reshape(1500, 2),
                {**_imec_meta("ap", 30000, 3000, 1500), "snsSaveChanSubset": "0:383,768"})
    _sglx_files(folder, "run_g0_t0.imec0.lf", np.arange(500, dtype="int16").reshape(250, 2),
                {**_imec_meta("lf", 2500, 250, 250), "snsSaveChanSubset": "384:768"})
    converter = SpikeGLXConverter({"Recording": {"folder_path": str(tmp_path), "stream_id": "imec0.ap"}},
                                  probe_id="probe01", lf=True)
    assert [i.stream_id for i in converter.recording_interfaces] == ["imec0.ap", "imec0.lf"]
    doc = make_doc(Sessions=[{"session id": "01", "date": "2024-01-01", "start time": "2024-01-01T10:00:00+00:00",
                              "Electrode Placements": [{"probe": "Utah Array", "brain region": "M1"}]}])
    session = sessions(doc)[0]
    odml_meta = build_nwb_metadata(doc, session)
    metadata = converter.get_metadata()  # neuroconv's SpikeGLX metadata, the odML values on top (as the datasets do)
    metadata["NWBFile"].update(odml_meta["NWBFile"])
    metadata["Subject"] = odml_meta["Subject"]
    metadata["Ecephys"]["Device"] = odml_meta["Ecephys"]["Device"]
    metadata["Ecephys"]["ElectrodeGroup"] = [{**g, "name": "probe01"} for g in odml_meta["Ecephys"]["ElectrodeGroup"]]
    spans = []
    converter.run_conversion(tmp_path / "x.nwb", metadata, stub_test=True,
                             add_to_nwbfile=lambda nwb: spans.append(recording_span(nwb)))
    assert spans == [(0.1, pytest.approx(0.1 + 99 / 2500))]  # the LF stub of 100 samples outlasts the AP's
    with NWBHDF5IO(str(tmp_path / "x.nwb"), "r") as io:
        nwb = io.read()
        ap, lf = nwb.acquisition["ElectricalSeriesAP"], nwb.acquisition["ElectricalSeriesLF"]
        assert (ap.data.shape, ap.rate, ap.starting_time, ap.data[1].tolist()) == ((100, 1), 30000.0, 0.1, [2])
        assert (lf.data.shape, lf.rate, lf.starting_time, lf.data[1].tolist()) == ((100, 1), 2500.0, 0.1, [2])
        assert (ap.electrodes.data[:].tolist(), lf.electrodes.data[:].tolist()) == ([0], [1])
        electrodes = nwb.electrodes.to_dataframe()
        assert electrodes["channel_name"].tolist() == ["AP0", "LF0"]
        assert electrodes["electrode_name"].nunique() == 1 and electrodes["location"].tolist() == ["M1", "M1"]
        assert electrodes["group_name"].tolist() == ["probe01", "probe01"]
        assert nwb.session_start_time == datetime(2024, 1, 1, 9, 59, 59, 900000, tzinfo=timezone.utc)
    report = validate_nwb(tmp_path / "x.nwb", ValidationReport("x"))
    assert (report.errors, report.warnings) == ([], [])
    write_probeinterface(tmp_path / "pg.json", converter.recording.get_probegroup())
    report = check_probe_geometry(tmp_path / "pg.json", tmp_path / "x.nwb", ValidationReport("x"))
    assert report.warnings == [] and report.info["total_probe_contacts"] == 1  # AP and LF row: one electrode
    with pytest.raises(ValueError, match="the LF stream goes with an AP stream"):
        SpikeGLXConverter({"Recording": {"folder_path": str(tmp_path), "stream_id": "imec0.lf"}}, "probe01", lf=True)


def test_nidq_times_on_imec_clock(tmp_path):
    """NIDQ samples on the imec clock through the paired 1 Hz sync edges (XA or digital line), linear beyond
    the end pairs; unpairable edges and missing files raise."""
    ratio, offset = 1.0001, 0.05  # imec seconds = offset + ratio * NIDQ seconds
    rate = 10000.0
    ni_t = (50 + np.arange(100000)) / rate
    im_t = (100 + np.arange(100000)) / rate
    pulse = lambda t, phase: (t >= phase) & ((t - phase) % 1 < 0.5)  # rising edges at phase + k s, imec clock
    xa_sync = np.where(pulse(offset + ratio * ni_t, 0.3), 16000, -200)
    xa_off = np.where(pulse(offset + ratio * ni_t, 0.8), 16000, -200)
    xd = 1 + 8 * pulse(offset + ratio * ni_t, 0.3)
    _sglx_files(tmp_path, "run_g0_t0.nidq", np.column_stack([xa_off, xa_sync, xd]).astype("int16"), {
        "firstSample": 50, "nSavedChans": 3, "niAiRangeMax": 5, "niMAGain": 1, "niMNGain": 200,
        "niSampRate": rate, "snsMnMaXaDw": "0,0,2,1", "snsSaveChanSubset": "all", "niXAChans1": "0:1",
        "niXDChans1": "0:7", "typeThis": "nidq", "~snsChanMap": "(0,0,2,1,1)(XA0;0:0)(XA1;1:1)(XD0;2:2)"})
    sy = 64 * pulse(im_t, 0.3) + np.arange(im_t.size) % 2
    imec = tmp_path / "run_g0_imec0"
    _sglx_files(imec, "run_g0_t0.imec0.lf", np.column_stack([np.zeros(im_t.size), sy]).astype("int16"),
                _imec_meta("lf", rate, 100, im_t.size))
    _sglx_files(imec, "run_g0_t0.imec0.ap", None, _imec_meta("ap", 3 * rate, 300, 3 * im_t.size))

    expected = offset + ratio * ni_t - 300 / (3 * rate)
    for sync in ("XA1", 3):
        mapped = nidq_times_on_imec_clock(tmp_path, sync, "imec0.lf")
        assert np.abs(mapped - expected).max() < 2e-4  # inside, before the first pair (0.3 s) and after the last
        assert ni_t[0] < 0.25 and ni_t[-1] > 9.4
    np.testing.assert_allclose(nidq_times_on_imec_clock(tmp_path, "XA1", "imec0.lf", n_samples=10),
                               expected[:10], atol=2e-4)
    with pytest.raises(ValueError, match="only 0 of 10 NIDQ sync edges pair"):
        nidq_times_on_imec_clock(tmp_path, "XA0", "imec0.lf")
    with pytest.raises(FileNotFoundError, match="no imec0.ap .bin with its .meta"):
        nidq_times_on_imec_clock(tmp_path, "XA1", "imec0.ap")
