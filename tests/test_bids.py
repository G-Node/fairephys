"""BIDS from the odML and the session's probe group: TSV files, sidecar fields, aux files."""

import gzip
import json
from datetime import datetime

import odml
import pytest

from conftest import linear_probe, probe_group, sessions
from fairephys.bids import create_session_layout, generate_aux_sidecars, generate_events_tsv, write_aux_bids_data
from fairephys.bids.aux import extract_aux_groups, eye_columns
from fairephys.bids.builder import (channel_electrode_map, coerce_enum, extract_channels, extract_dataset_description,
                                    extract_electrodes, extract_event_codes, extract_event_levels,
                                    extract_events_metadata, extract_participants, extract_sidecar_fields)
from fairephys.bids.extractors import bracketed
from fairephys.enrichment.neo import enrich_document
from fairephys.metadata import compile_to_odml, provenance, save_odml

PROBES = [{"name": "S-Probe A", "model": "S-Probe", "manufacturer": "Plexon", "number of contacts": 2,
           "contact pitch": "100 micrometer", "conductor material": "platinum iridium"},
          {"name": "V-Probe", "model": "V-Probe", "number of contacts": 2, "contact pitch": "100 micrometer"}]


def _neo(channels, streams=None, **extra):
    """Neo data of one stream "0" holding the named channels (24414.0625 Hz, uV)."""
    streams = streams or [{"id": "0", "name": "s", "sampling_rate": 24414.0625}]
    return {"signal_streams": streams, **extra,
            "signal_channels": [c if isinstance(c, dict) else
                                {"name": c, "units": "uV", "sampling_rate": 24414.0625, "gain": 1.0, "stream_id": "0"}
                                for c in channels]}


@pytest.fixture
def placed(make_doc):
    """A session 01 with the given placements of PROBES, enriched with Neo channels c1..c4."""
    def make(placements, channels=("c1", "c2", "c3", "c4"), **changes):
        doc = make_doc(Hardware={"Probes": PROBES}, Sessions=[
            {"session id": "01", "date": "2025-01-30", "Electrode Placements": placements}], **changes)
        enrich_document(doc, _neo(channels), "01")
        return doc, sessions(doc)[0]
    return make


def test_electrodes_from_the_probe_group(placed):
    """Electrodes {probe_id}_{contact id}, x in the probe's frame, region and material from placement and device."""
    doc, session = placed([{"probe": "V-Probe", "brain region": "MD", "hemisphere": "right"},
                           {"probe": "S-Probe A", "brain region": "M1", "hemisphere": "left",
                            "anatomical targets": {"cortical layer V": "2"}}])
    group = probe_group(linear_probe("S-Probe", ["01", "02"], [0, 1]),
                        linear_probe("V-Probe", ["a", "b"], [2, 3], x_offset=800))
    rows = [(r["name"], r["probe_name"], r["x"], r["y"], r["z"], r["hemisphere"], r["anatomical_location"],
             r["material"], r["anatomical_targets"]) for r in extract_electrodes(doc, session, group)]
    assert rows == [
        ("S-Probe_01", "S-Probe", 0.0, 0.0, "n/a", "L", "M1", "platinum iridium", "n/a"),
        ("S-Probe_02", "S-Probe", 0.0, 100.0, "n/a", "L", "M1", "platinum iridium", "cortical layer V"),
        ("V-Probe_a", "V-Probe", 0.0, 0.0, "n/a", "R", "MD", "n/a", "n/a"),
        ("V-Probe_b", "V-Probe", 0.0, 100.0, "n/a", "R", "MD", "n/a", "n/a")]
    with pytest.raises(ValueError, match="probe Other of the probe group has no Electrode Placement"):
        extract_electrodes(doc, session, probe_group(linear_probe("Other", ["01"], [0])))


def test_channels_named_by_device_channel_index(placed):
    """Channel j names the electrode wired to device channel j; -1 and unwired channels are n/a."""
    doc, session = placed([{"probe": "S-Probe A", "brain region": "M1"}])
    group = probe_group(linear_probe("S-Probe", ["01", "02"], [2, 0]), linear_probe("NP", ["e1152", "e7"], [3, -1]))
    rows = extract_channels(session, group, channel_types={"prefixes": {"c": "BB"}}, bad_channels={"c2": "saturated"})
    assert [(r["name"], r["electrode_name"], r["type"], r["status"], r["status_description"], r["sampling_frequency"])
            for r in rows] == [("c1", "S-Probe_02", "BB", "n/a", "n/a", 24414.0625),
                               ("c2", "n/a", "BB", "bad", "saturated", 24414.0625),
                               ("c3", "S-Probe_01", "BB", "n/a", "n/a", 24414.0625),
                               ("c4", "NP_e1152", "BB", "n/a", "n/a", 24414.0625)]
    assert channel_electrode_map(group, ["c1", "c2", "c3", "c4"]) == {
        "c1": "S-Probe_02", "c3": "S-Probe_01", "c4": "NP_e1152"}
    assert all(r["electrode_name"] == "n/a" and r["type"] == "MISC" for r in extract_channels(session))
    with pytest.raises(ValueError, match="device channel 2 beyond the 2 channels"):
        channel_electrode_map(group, ["c1", "c2"])


def test_repeated_channel_names_by_position(placed):
    """Streams repeating channel names (SpikeGLX AP0 per probe) are named by position, never by name."""
    _, session = placed([{"probe": "S-Probe A", "brain region": "M1"}], channels=("AP0", "AP0", "AP1"))
    group = probe_group(linear_probe("NP01", ["e0"], [0]), linear_probe("NP02", ["e0"], [1]))
    assert [r["electrode_name"] for r in extract_channels(session, group)] == ["NP01_e0", "NP02_e0", "n/a"]
    with pytest.raises(ValueError, match="repeat"):
        channel_electrode_map(group, ["AP0", "AP0", "AP1"])


def test_channels_need_neo_metadata_and_required_values(placed):
    """No Neo Metadata, or a required column without value, is an error naming the session."""
    _, session = placed([{"probe": "S-Probe A", "brain region": "M1"}],
                        channels=[{"name": "c1", "sampling_rate": 1000.0, "stream_id": "0"}])
    with pytest.raises(ValueError, match="Session 01, channel c1: no value for the required column units"):
        extract_channels(session)
    session.remove(session["Neo Metadata"])
    with pytest.raises(ValueError, match="Session 01: no Neo Metadata"):
        extract_channels(session)


def test_sidecar_fields_of_runs_and_streams(make_doc, tmp_path):
    """RecordingDuration per run, per stream and per aux stream in a run, after the odML XML round trip."""
    doc = make_doc(Hardware={"Recording System": {"curated streams": ["Broa", "EMGx"]}},
                   Sessions=[{"session id": "ses01", "continuity": ""}])
    streams = [{"id": "1", "name": "Broa", "sampling_rate": 24414.0625},
               {"id": "2", "name": "EMGx", "sampling_rate": 3051.76}]
    enrich_document(doc, _neo([], streams, nb_segment=3, stream_durations_s={
        "1": [600.0, 900.0, 300.0], "2": [600.0, 900.1, 300.0]}), "ses01", runs=[5, 6, 8])
    _, xml = save_odml(doc, tmp_path / "doc")
    doc = odml.load(str(xml))
    session = sessions(doc)[0]
    fields = lambda **kw: extract_sidecar_fields(doc, session, "ecephys_json", **kw)
    assert [fields(run=r)["RecordingDuration"] for r in (5, 6, 8)] == [600.0, 900.0, 300.0]
    assert (fields()["RecordingDuration"], fields()["RecordingType"], fields(run=6)["RecordingType"]) == (
        1800.0, "discontinuous", "continuous")
    assert (fields(stream="EMGx")["SamplingFrequency"], fields(stream="EMGx")["RecordingDuration"]) == (3051.76, 1800.1)
    assert fields(run=6, stream="EMGx")["RecordingDuration"] == 900.1
    with pytest.raises(KeyError, match="no run 7"):
        fields(run=7)

    single = make_doc()
    enrich_document(single, _neo([], streams[:1], stream_durations_s={"1": [600.0]}), "ses01")
    assert extract_sidecar_fields(single, sessions(single)[0], "ecephys_json", run=1)["RecordingDuration"] == 600.0


def test_enum_fields_keep_the_bids_spelling_or_fail():
    """Enum fields take the registry's spelling; an invalid value is an error, never a substitute."""
    assert [coerce_enum(f, v) for f, v in (("RecordingType", "Continuous"), ("MicroephysCoordinateUnits", "mm"),
                                           ("TaskName", "anything"))] == ["continuous", "mm", "anything"]
    with pytest.raises(ValueError, match="RecordingType: 'chronic' is not one of continuous, discontinuous, epoched"):
        coerce_enum("RecordingType", "chronic")


def test_sidecar_fields_from_their_registry_paths(make_doc):
    """RecordingType from Neo continuity, TaskName the task label; descriptions, coordsystem, events by path."""
    doc = make_doc({"Behavioral Protocol": {"Stimulus Presentation": {"software name": "MonkeyLogic"}}},
                   Sessions=[{"session id": "01", "recording type": "chronic", "description": "Session text",
                              "coordinate system": "Stereotaxic", "coordinate units": "millimeter"}])
    session = sessions(doc)[0]
    fields = extract_sidecar_fields(doc, session, "ecephys_json")
    assert "RecordingType" not in fields
    assert (fields["TaskName"], fields["TaskDescription"]) == ("rest", "Session text")
    doc["Behavioral Protocol"].append(odml.Property("description", values=["Protocol text"]))
    assert extract_sidecar_fields(doc, session, "emg_json")["TaskDescription"] == "Protocol text"
    session.append(odml.Property("task description", values=["This entry's own task"]))  # a task of its own
    assert extract_sidecar_fields(doc, session, "ecephys_json")["TaskDescription"] == "This entry's own task"
    assert extract_sidecar_fields(doc, session, "coordsystem_json") == {
        "MicroephysCoordinateSystem": "Stereotaxic", "MicroephysCoordinateUnits": "mm"}
    assert extract_events_metadata(doc, session) == {"StimulusPresentation": {"SoftwareName": "MonkeyLogic"}}
    bare = make_doc()
    assert extract_sidecar_fields(bare, sessions(bare)[0], "coordsystem_json") == {}
    assert extract_events_metadata(bare, sessions(bare)[0]) == {}


def test_aux_device_fields(make_doc):
    """Aux device hardware filters: its own, else the recording system's, else n/a; its enums checked."""
    own = {"Hardware Filters": {"Filters": [{"name": "Amp highpass", "type": "highpass", "cutoff": "1 hertz"}]}}
    rs_filters = {"Filters": [{"name": "Anti-aliasing", "type": "lowpass", "cutoff": "7500 hertz"}, {"type": "notch"}]}
    doc = make_doc(Hardware={"Hardware Filters": rs_filters, "Auxiliary Devices": [
        {"name": "Own", "modality": "emg", **own},
        {"name": "Through", "modality": "emg", "same as recording system": True},
        {"name": "Bare", "modality": "physio", "recording": "eye", "recorded eye": "both"}]})
    session = sessions(doc)[0]
    emg = lambda name: extract_sidecar_fields(doc, session, "emg_json", device_name=name)
    assert emg("Own")["HardwareFilters"] == {"Amp highpass": {"Type": "highpass", "Cutoff": 1}}
    assert (emg("Through")["HardwareFilters"], emg("Through")["Manufacturer"]) == (
        {"Anti-aliasing": {"Type": "lowpass", "Cutoff": 7500}}, "Blackrock")
    assert emg("Bare")["HardwareFilters"] == "n/a"
    assert "RecordedEye" not in extract_sidecar_fields(doc, session, "eyetrack_json", device_name="Own")
    with pytest.raises(ValueError, match="RecordedEye: 'both' is not one of left, right, cyclopean"):
        extract_sidecar_fields(doc, session, "eyetrack_json", device_name="Bare")


def test_participants(doc):
    """One row per subject with the registry's prefix; a subject without id is an error."""
    row, = extract_participants(doc)
    assert (row["participant_id"], row["species"], row["sex"], row["handedness"]) == (
        "sub-MonkeyA", "Macaca mulatta", "female", "n/a")
    doc["Subject"].properties["subject id"].values = []
    with pytest.raises(ValueError, match="no subject id"):
        extract_participants(doc)


def test_dataset_description(make_doc, monkeypatch):
    """Funding, ethics and acknowledgements without doubled brackets; GeneratedBy with the commit."""
    monkeypatch.setattr(provenance, "source_checkout", lambda *a: {"commit": "abc1234", "uncommitted changes": True})
    doc = make_doc(Dataset={"ethics authority": "EC 1 (Ethics Committee)", "ethics approval id": "123.4",
                            "doi": "10.1234/abc", "related publications": "Doe, J. A paper (2024).",
                            "Related Projects": [{"project name": "Raw", "project url": "https://gin.example/raw"}],
                            "Fundings": [
        {"funder": "Deutsche Forschungsgemeinschaft (DFG)", "award number": "SFB-1528"},
        {"funder": "European Commission", "award number": "956669", "acknowledgement": "Funded by the EC\n(956669)."}]})
    desc = extract_dataset_description(doc)
    assert desc["Funding"] == ["Deutsche Forschungsgemeinschaft (DFG), SFB-1528", "European Commission (956669)"]
    assert desc["EthicsApprovals"] == ["EC 1 (Ethics Committee), approval 123.4"]
    assert desc["Acknowledgements"] == "Funded by the EC (956669)."
    generated, = desc["GeneratedBy"]
    assert generated["Name"] == "fairephys" and generated["Version"].endswith("+abc1234.dirty")
    assert [bracketed(t, d) for t, d in [("EC", "1"), ("DFG (Germany)", "1"), ("a", "b (c)"), ("EC", ""), ("", "1")]] \
        == ["EC (1)", "DFG (Germany), 1", "a, b (c)", "EC", "1"]
    assert (desc["Authors"], desc["DatasetDOI"]) == (["Jane Doe"], "10.1234/abc")
    assert desc["SourceDatasets"] == [{"URL": "https://gin.example/raw", "Name": "Raw"}]
    assert desc["ReferencesAndLinks"] == ["Doe, J. A paper (2024).", "https://gin.example/raw"]

    bare = compile_to_odml({"Dataset": {"full name": "T", "ethics authority": "EC"}})
    bare.remove(bare["Curation Software"])  # an odML compiled before the section existed
    desc = extract_dataset_description(bare)
    assert desc["GeneratedBy"] == [{"Name": "fairephys"}] and desc["EthicsApprovals"] == ["EC"]
    assert not {"Authors", "Funding", "Acknowledgements"} & set(desc)


def test_event_levels_and_codes(make_doc, tmp_path):
    """events.json trial_type Levels and the raw-code map from the processing states, in order; no events
    is a header-only events.tsv."""
    doc = make_doc({"Data Processing": [
        {"name": "A", "technique": "video annotation", "annotation": {"states": [
            {"label": "rest", "code": "RS", "description": "Calm rest"}, {"label": "move", "code": "M"}]}},
        {"name": "B", "technique": "spike sorting", "states": ["burst"]}]})
    assert list(extract_event_levels(doc).items()) == [("rest", "Calm rest"), ("move", ""), ("burst", "")]
    assert extract_event_codes(doc) == {"RS": "rest", "M": "move"}
    assert extract_event_levels(make_doc()) == {}
    assert generate_events_tsv([], tmp_path / "e.tsv").read_text() == "onset\tduration\n"
    columns = ["onset", "duration", "trial_type"]
    assert generate_events_tsv([], tmp_path / "e.tsv", columns).read_text() == "onset\tduration\ttrial_type\n"
    rows = [{"trial_type": "go", "onset": 1.5, "duration": 0}, {"onset": 2, "duration": 0}]
    assert generate_events_tsv(rows, tmp_path / "e.tsv", columns).read_text() == (
        "onset\tduration\ttrial_type\n1.5\t0\tgo\n2\t0\tn/a\n")
    for bad in (["trial_type", "onset", "duration"], ["onset", "duration"]):
        with pytest.raises(ValueError):
            generate_events_tsv(rows, tmp_path / "e.tsv", bad)


# the BIDS column of each eye-tracker channel, as a dataset names them from its YAML
EYE = {"eyeX": "x_coordinate", "eyeY": "y_coordinate", "pupil": "pupil_size"}


@pytest.fixture
def aux(make_doc):
    """Aux groups of run 2 of a session whose curated streams are Broa (eye, hand) and EMGx (EMG)."""
    doc = make_doc(Hardware={"Recording System": {"curated streams": ["Broa", "EMGx"], "power line frequency": 50},
                             "Auxiliary Devices": [
        {"name": "Eye Tracker", "modality": "physio", "recording": "eye", "channels": ["eyeX", "eyeY", "pupil"]},
        {"name": "Hand", "modality": "motion", "tracksys": "hand", "channels": ["handX"]},
        {"name": "EMG", "modality": "emg", "channels": ["EMG1", "EMG2"], "same as recording system": True},
        {"name": "ECG", "modality": "physio", "recording": "cardiac", "channels": ["ECG"]},
        {"name": "Absent", "modality": "physio", "recording": "resp", "channels": ["RESP"]}]})
    broa, emg = 24414.0625, 3051.7578125
    channel = lambda name, units, rate, stream: dict(name=name, units=units, sampling_rate=rate, stream_id=stream)
    channels = ([channel(n, "V", broa, "1") for n in ("Broa 1", "eyeX", "eyeY")] + [channel("pupil", "mV", broa, "1")]
                + [channel("handX", "V", emg, "2")]
                + [channel("ECG", "mV", broa, "1"), channel("EMG1", "uV", emg, "2"), channel("EMG2", "mV", emg, "2")])
    streams = [{"id": "1", "name": "Broa", "sampling_rate": broa}, {"id": "2", "name": "EMGx", "sampling_rate": emg}]
    durations = {"1": [600.0, 300.0], "2": [600.0, 300.5]}
    starts = {"1": ["2024-03-15T09:30:00+00:00", "2024-03-15T09:45:00+00:00"],   # EMG of run 2 started 3 s earlier
              "2": ["2024-03-15T09:30:00+00:00", "2024-03-15T09:44:57+00:00"]}
    enrich_document(doc, _neo(channels, streams, nb_segment=2, stream_durations_s=durations,
                              stream_start_times=starts), "ses01", runs=[1, 2])
    return extract_aux_groups(doc, sessions(doc)[0], run=2)


def test_aux_sidecars(aux, tmp_path):
    """Aux sidecars at exact rates, with the aux stream's run duration, units, StartTime and run entity."""
    assert [(g["name"], g["sampling_rate"]) for g in aux] == [
        ("Eye Tracker", 24414.0625), ("Hand", 3051.7578125), ("EMG", 3051.7578125), ("ECG", 24414.0625)]
    ses = tmp_path / "sub-MonkeyA" / "ses-ses01"
    assert create_session_layout(tmp_path, "MonkeyA", "ses01", ["ecephys", "emg"]) == {
        "session": ses, "ecephys": ses / "ecephys", "emg": ses / "emg"}
    with pytest.raises(ValueError, match="3 channels; name the column of each"):  # no column by position
        generate_aux_sidecars(aux, "MonkeyA", "ses01", tmp_path, task="rest")
    with pytest.raises(ValueError, match=r"roles for channels it lacks: \['blink'\]"):
        generate_aux_sidecars(aux, "MonkeyA", "ses01", tmp_path, task="rest", eye_columns={"blink": "pupil_size"})
    with pytest.raises(ValueError, match=r"no column named for the channels \['pupil'\]"):
        generate_aux_sidecars(aux, "MonkeyA", "ses01", tmp_path, task="rest",
                              eye_columns={"eyeX": "x_coordinate", "eyeY": "y_coordinate"})
    generate_aux_sidecars(aux, "MonkeyA", "ses01", tmp_path, task="rest", start_time=22.058, run=2, eye_columns=EYE)
    prefix = "sub-MonkeyA_ses-ses01_task-rest_run-02"
    physio = json.loads((ses / "ecephys" / f"{prefix}_recording-eye_physio.json").read_text())
    assert (physio["SamplingFrequency"], physio["StartTime"], physio["Columns"], physio["x_coordinate"]) == (
        24414.0625, 22.058, ["timestamp", "x_coordinate", "y_coordinate", "pupil_size"], {"Units": "V"})
    assert physio["pupil_size"] == {"Units": "mV"}  # the third eye channel, named by the caller
    two = {**aux[0], "channels": ["eyeX", "eyeY"]}
    assert eye_columns(two, None) == {"eyeX": "x_coordinate", "eyeY": "y_coordinate"}  # the documented default
    ecg = json.loads((ses / "ecephys" / f"{prefix}_recording-cardiac_physio.json").read_text())
    assert (ecg["Columns"], ecg["ECG"]) == (["ECG"], {"Units": "mV"})
    motion = json.loads((ses / "motion" / f"{prefix}_tracksys-hand_motion.json").read_text())
    assert (motion["SamplingFrequency"], motion["SamplingFrequencyEffective"], motion["MotionChannelCount"]) == (
        3051.7578125, 3051.7578125, 1)  # the device's own stream
    emg = json.loads((ses / "emg" / f"{prefix}_emg.json").read_text())
    assert (emg["SamplingFrequency"], emg["PowerLineFrequency"], emg["RecordingDuration"], emg["Manufacturer"],
            emg["RecordingType"]) == (3051.7578125, 50.0, 300.5, "Blackrock", "continuous")
    assert (ses / "emg" / f"{prefix}_channels.tsv").read_text().splitlines()[1:] == [
        "EMG1\tEMG\tuV\t3051.7578125\tn/a", "EMG2\tEMG\tmV\t3051.7578125\tn/a"]


def test_aux_data_files(aux, tmp_path):
    """Eye physio in BIDS columns (x, y, pupil size) without header; EDF units from the Neo channels (missing units an error),
    its start the local start of the EMG stream in the run plus the aux offset."""
    pyedflib = pytest.importorskip("pyedflib")
    import numpy as np

    data = {name: np.sin(np.arange(200) / (i + 2.0)) for i, name in enumerate(
        ["eyeX", "eyeY", "pupil", "handX", "EMG1", "EMG2"])}
    write_aux_bids_data(aux, data, "MonkeyA", "ses01", tmp_path, task="rest", start_time=2.0, eye_columns=EYE)
    physio, = tmp_path.rglob("*_physio.tsv.gz")
    first = gzip.decompress(physio.read_bytes()).decode().splitlines()[0].split("\t")
    assert len(first) == 4 and float(first[0]) == 0.0 and float(first[3]) == data["pupil"][0]
    edf, = tmp_path.rglob("*_emg.edf")
    with pyedflib.EdfReader(str(edf)) as reader:
        assert [h["dimension"] for h in reader.getSignalHeaders()] == ["uV", "mV"]
        assert reader.getStartdatetime() == datetime(2024, 3, 15, 10, 44, 59)  # 09:44:57 UTC, Paris, + 2 s
    del aux[2]["matched"][1]["units"]
    with pytest.raises(ValueError, match="no units .* EMG2"):
        write_aux_bids_data(aux, data, "MonkeyA", "ses01", tmp_path, task="rest", eye_columns=EYE)
