"""Sanity orchestration, cross-format geometry, raw versus NWB samples, validator wrappers."""

import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from neo.rawio.baserawio import _signal_channel_dtype, _signal_stream_dtype
from probeinterface import write_probeinterface
from pynwb import NWBHDF5IO
from pynwb.ecephys import ElectricalSeries

from conftest import empty_nwbfile, linear_probe, probe_group
from fairephys import formats
from fairephys.validation import geometry, integrity, run_validation
from fairephys.validation.bids import DENO_HINT, bids_validator, default_schema_path, validate_bids
from fairephys.validation.nwb import validate_nwb
from fairephys.validation.report import ValidationReport
from fairephys.validation.sanity import run_sanity_checks


def _report():
    return ValidationReport("t")


# --- sanity orchestration -----------------------------------------------------

def test_sanity_reads_only_its_files(tmp_path):
    """Sanity reads only its file keys, a missing given file is an error, the report is saved."""
    report = run_sanity_checks(None, {"nwb": tmp_path / "no.nwb", "channels_tsv": None, "prefix": "sub-A_ses-01"},
                               "s", tmp_path / "val")
    assert report.errors == [f"[Sanity] nwb not found: {tmp_path / 'no.nwb'}"]
    assert (tmp_path / "val" / "s.md").is_file()
    with pytest.raises(ValueError, match="recording_format is required"):
        run_sanity_checks(tmp_path, {}, "s", tmp_path)


def test_a_crashing_check_is_an_error_naming_it(monkeypatch, tmp_path):
    """A check that raises reports itself as failed; the others still run."""
    raw, nwb = tmp_path / "raw.ns6", tmp_path / "x.nwb"
    raw.touch()
    nwb.touch()

    def broken(fmt, path):
        raise RuntimeError("reader broke")
    monkeypatch.setattr(integrity, "open_rawio", broken)
    report = run_sanity_checks(raw, {"nwb": nwb}, "s", tmp_path / "val", recording_format="blackrock")
    assert {"[Sanity] check_channel_distinctness failed: RuntimeError: reader broke",
            "[Sanity] check_data_integrity failed: RuntimeError: reader broke"} <= set(report.errors)
    assert any(e.startswith("[Sanity] check_recording_metadata failed") for e in report.errors)  # not an NWB file


# --- geometry -------------------------------------------------------------------

def _electrodes_nwb(path, rows, location="l"):
    """An NWB whose electrode table holds (group_name, electrode_name, rel_x, rel_y) per row."""
    nwb = empty_nwbfile()
    device = nwb.create_device(name="d")
    groups = {g: nwb.create_electrode_group(name=g, description="d", location=location, device=device)
              for g in dict.fromkeys(r[0] for r in rows)}
    nwb.add_electrode_column(name="channel_name", description="c")
    nwb.add_electrode_column(name="electrode_name", description="e")
    for i, (group, electrode, x, y) in enumerate(rows):
        nwb.add_electrode(group=groups[group], location=location, channel_name=f"ch{i}", electrode_name=electrode,
                          rel_x=x, rel_y=y)
    with NWBHDF5IO(str(path), "w") as io:
        io.write(nwb)
    return path


@pytest.fixture
def geometry_files(tmp_path):
    """Two probes (the second offset), electrodes.tsv and an NWB table in another order plus an unplaced row."""
    group = probe_group(linear_probe("probeA", ["01", "02"], [0, 1], x_offset=0),
                        linear_probe("probeB", ["01", "02"], [2, 3], pitch=50, x_offset=800))
    write_probeinterface(tmp_path / "probe_group.json", group)
    electrodes = pd.DataFrame({"name": ["probeA_01", "probeA_02", "probeB_01", "probeB_02"], "x": [0] * 4,
                               "probe_name": ["probeA"] * 2 + ["probeB"] * 2, "y": [0.0, 100.0, 0.0, 50.0]})
    electrodes.to_csv(tmp_path / "electrodes.tsv", sep="\t", index=False)
    nwb = _electrodes_nwb(tmp_path / "x.nwb", [("probeB", "02", 0.0, 50.0), ("probeA", "01", 0.0, 0.0),
                                               ("probeA", "02", 0.0, 100.0), ("probeB", "01", 0.0, 0.0),
                                               ("unplaced", "", 0.0, 0.0)])
    return tmp_path / "probe_group.json", tmp_path / "electrodes.tsv", nwb, electrodes


def test_geometry_compared_by_electrode_name(geometry_files, tmp_path):
    """Positions match by electrode name across formats; moved or unknown electrodes are errors."""
    group, electrodes_tsv, nwb, electrodes = geometry_files
    check = lambda nwb_path=nwb: geometry.check_probe_to_nwb_geometry(group, electrodes_tsv, nwb_path, _report())
    report = check()
    assert report.errors == [] and report.info["nwb_bids_probe_groups"] == "PASSED (2 groups match)"
    assert report.info["bids_geometry"] == report.info["nwb_geometry"] == "PASSED (4 electrodes match ProbeInterface)"

    electrodes.assign(y=[0.0, 100.0, 0.0, 55.0]).to_csv(electrodes_tsv, sep="\t", index=False)
    assert check().errors == ["[Sanity] BIDS electrodes.tsv: 1 electrode(s) differ from the ProbeInterface positions "
                              "(max diff 5.0 um): ['probeB_02']"]
    electrodes[electrodes["name"] != "probeB_02"].to_csv(electrodes_tsv, sep="\t", index=False)
    report = check()
    assert report.errors == [] and report.info["bids_subset"] == "3 of 4 ProbeInterface contacts in BIDS electrodes.tsv"

    electrodes.to_csv(electrodes_tsv, sep="\t", index=False)
    stranger = _electrodes_nwb(tmp_path / "y.nwb", [("probeA", "03", 0.0, 0.0), ("probeB", "01", 0.0, 0.0)])
    assert check(stranger).errors == [
        "[Sanity] NWB electrode table: 1 electrode(s) not in ProbeInterface: ['probeA_03']"]


def test_one_nwb_per_probe_is_compared_with_its_probe(geometry_files, tmp_path):
    """An NWB holding some of the session's probes is compared with those probes only."""
    group, electrodes_tsv, _, _ = geometry_files
    only_b = _electrodes_nwb(tmp_path / "b.nwb", [("probeB", "01", 0.0, 0.0), ("probeB", "02", 0.0, 50.0)])
    report = geometry.check_probe_to_nwb_geometry(group, electrodes_tsv, only_b, _report())
    assert report.errors == [] and report.info["nwb_geometry"] == "PASSED (2 electrodes match ProbeInterface)"
    pd.DataFrame({"name": ["a0", "a1", "ch0", "ch1"], "type": ["BB"] * 4,
                  "electrode_name": ["probeA_01", "probeA_02", "probeB_01", "probeB_02"]}
                 ).to_csv(tmp_path / "channels.tsv", sep="\t", index=False)
    report = geometry.check_electrode_consistency(tmp_path / "channels.tsv", only_b, _report())
    assert report.errors == [] and "2 channels of other probes not compared" in report.info["nwb_probe_subset"]
    assert geometry.BIDS_NEURAL_TYPES <= geometry.bids_channel_types()
    pd.DataFrame({"name": ["ch0", "ch1", "x"], "type": ["HP", "HP", "SYNC"]}).to_csv(
        tmp_path / "hp.tsv", sep="\t", index=False)  # hardware high-passed AP channels are neural
    report = geometry.check_electrode_consistency(tmp_path / "hp.tsv", only_b, _report())
    assert report.errors == [] and report.info["channel_names_matched"] is True
    assert report.info["channel_count_consistency"] == "PASSED (neural=2, NWB non-neural electrodes=0)"


def test_electrode_count_by_name_only_when_every_row_is_named(tmp_path):
    """The rows of one contact in several streams are one electrode; rows without an electrode name each count,
    so more NWB rows than probe contacts is still a warning; a group at the location 'unknown' is a warning."""
    write_probeinterface(tmp_path / "pg.json", probe_group(linear_probe("P", ["01", "02"], [0, 1])))
    check = lambda nwb: geometry.check_probe_geometry(tmp_path / "pg.json", nwb, _report())
    streams = _electrodes_nwb(tmp_path / "ap_lf.nwb", [("P", "01", 0.0, 0.0), ("P", "02", 0.0, 100.0),
                                                       ("P", "01", 0.0, 0.0), ("P", "02", 0.0, 100.0)])
    report = check(streams)
    assert report.warnings == [] and report.info["nwb_electrode_table"]["columns"] == sorted(
        ["location", "group", "group_name", "channel_name", "electrode_name", "rel_x", "rel_y"])  # sorted, not as written
    assert "(sorted; the file's order is neuroconv's" in report.to_markdown()
    unnamed = _electrodes_nwb(tmp_path / "more.nwb", [("P", "", 0.0, 0.0)] * 3)
    assert check(unnamed).warnings == ["[Sanity] More NWB electrodes (3) than probe contacts (2)"]
    unknown = _electrodes_nwb(tmp_path / "unknown.nwb", [("P", "01", 0.0, 0.0), ("P", "02", 0.0, 100.0)],
                              "unknown, left hemisphere")
    assert check(unknown).warnings == ["[Sanity] NWB electrode group P: location 'unknown, left hemisphere' (the "
                                       "placement names no brain region; neuroconv's placeholder)"]


def test_unconnected_contact_is_no_geometry_error():
    """Contacts wired to -1 (not connected) are valid; below -1 or a channel twice is an error."""
    for channels, error in (([0, -1, 1], None), ([-1, -1, 1], None), ([0, -2, 1], "below -1"),
                            ([0, 0, 1], "duplicate")):
        report = _report()
        positions = geometry._probe_positions([linear_probe("P", ["1", "2", "3"], channels)], report)
        assert positions == {"P_1": (0.0, 0.0), "P_2": (0.0, 100.0), "P_3": (0.0, 200.0)}
        assert (report.errors == []) if error is None else (error in report.errors[0])


def test_bids_mapping_allows_a_recorded_subset(tmp_path):
    """channels -> electrodes -> probes link up; fewer electrodes than the probe has is info, more an error."""
    pd.DataFrame({"name": ["ch1", "ch2"], "type": ["BB", "BB"], "electrode_name": ["probeA_01", "probeA_02"]}
                 ).to_csv(tmp_path / "channels.tsv", sep="\t", index=False)
    pd.DataFrame({"name": ["probeA_01", "probeA_02"], "probe_name": ["probeA"] * 2}
                 ).to_csv(tmp_path / "electrodes.tsv", sep="\t", index=False)
    files = [tmp_path / f for f in ("channels.tsv", "electrodes.tsv", "probes.tsv")]
    for count, errors in ((4, []), (1, ["[Sanity] Probe probeA: probes.tsv says 1 electrodes, electrodes.tsv has 2"])):
        pd.DataFrame({"probe_name": ["probeA"], "electrode_count": [count]}).to_csv(files[2], sep="\t", index=False)
        report = geometry.check_bids_electrode_mapping(*files, _report())
        assert report.errors == errors
    types = geometry.bids_channel_types()
    assert {"BB", "LFP", "MISC", "EMG", "TRIG"} <= types and "FOO" not in types


# --- raw versus NWB samples -------------------------------------------------------

class FakeRawIO:
    """A Neo RawIO stand-in: stream "0" (ch 1, ...) per segment, plus `streams` {id: (segments, names)}."""

    def __init__(self, samples, gain=1.0, units="uV", t_starts=None, streams=None):
        segments = samples if isinstance(samples, list) else [samples]
        self.t_starts = t_starts or [0.0] * len(segments)
        self.data = {"0": segments}
        channels = [(f"ch {i + 1}", str(i + 1), 1000.0, segments[0].dtype.str, units, g, 0.0, "0", "")
                    for i, g in enumerate(np.broadcast_to(gain, segments[0].shape[1]))]
        for sid, (segs, names) in (streams or {}).items():
            self.data[sid] = segs
            channels += [(n, f"{sid}#{n}", 1000.0, segs[0].dtype.str, units, 1.0, 0.0, sid, "") for n in names]
        self.header = {"signal_streams": np.array([(sid, sid, "") for sid in self.data], dtype=_signal_stream_dtype),
                       "signal_channels": np.array(channels, dtype=_signal_channel_dtype)}

    def _segment(self, seg_index, stream_index):
        return self.data[self.header["signal_streams"][stream_index]["id"]][seg_index]

    def segment_count(self, block_index):
        return len(self.t_starts)

    def get_signal_t_start(self, block_index, seg_index, stream_index):
        return self.t_starts[seg_index]

    def get_signal_size(self, block_index, seg_index, stream_index):
        return self._segment(seg_index, stream_index).shape[0]

    def get_analogsignal_chunk(self, block_index, seg_index, i_start, i_stop, stream_index):
        return self._segment(seg_index, stream_index)[i_start:i_stop]


def _series_nwb(path, data, conversion=1.0, channel_conversion=None, names=None, starting_times=None):
    """An NWB with one ElectricalSeries per array of `data`."""
    arrays = data if isinstance(data, list) else [data]
    nwb = empty_nwbfile()
    group = nwb.create_electrode_group(name="g", description="d", location="l", device=nwb.create_device(name="p"))
    nwb.add_electrode_column(name="channel_name", description="channel name")
    for name in names or [f"ch {i + 1}" for i in range(arrays[0].shape[1])]:
        nwb.add_electrode(group=group, location="l", channel_name=name)
    for k, (array, t0) in enumerate(zip(arrays, starting_times or [0.0] * len(arrays))):
        nwb.add_acquisition(ElectricalSeries(
            name="ElectricalSeries" if len(arrays) == 1 else f"ElectricalSeries{k}", data=array, rate=1000.0,
            starting_time=t0, conversion=conversion, channel_conversion=channel_conversion,
            electrodes=nwb.create_electrode_table_region(list(range(arrays[0].shape[1])), "all")))
    with NWBHDF5IO(str(path), "w") as io:
        io.write(nwb)
    return path


@pytest.fixture
def integrity_check(monkeypatch, tmp_path):
    def check(raw_io, nwb_path, **kwargs):
        monkeypatch.setattr(integrity, "open_rawio", lambda fmt, path: raw_io)
        return integrity.check_data_integrity(tmp_path, nwb_path, _report(), recording_format="fake", **kwargs)
    return check


def _int16(seed, shape):
    return np.random.default_rng(seed).integers(-2000, 2000, shape).astype("int16")


_VOLTS = np.random.default_rng(0).normal(0, 50e-6, (2000, 3)).astype("float32")


@pytest.mark.parametrize("raw, nwb, info, note", [
    # float samples in volts that the reader labels uV: the label is reported (noted), not used
    (FakeRawIO(_VOLTS, units="uV"), dict(data=_VOLTS),
     {"raw_scale": "1e-06 V (units uV)", "nwb_scale": "1 V", "max_sample_diff": "0.00e+00 V"}, True),
    (FakeRawIO(_int16(1, (2000, 3)), gain=0.25), dict(data=_int16(1, (2000, 3)), conversion=0.25e-6),
     {"raw_scale": "2.5e-07 V (units uV)", "nwb_scale": "2.5e-07 V"}, False),
    (FakeRawIO(_int16(2, (2000, 2)), gain=[1.0, 2.0]),
     dict(data=_int16(2, (2000, 2)), channel_conversion=np.array([1e-6, 2e-6])),
     {"nwb_scale": "1e-06..2e-06 V"}, False),
], ids=["volts labelled uV", "int16 with gain", "channel conversion"])
def test_samples_compared_on_the_nwb_scale(integrity_check, tmp_path, raw, nwb, info, note):
    """Equal samples pass whatever scale each side states; both scales are reported, a disagreement noted."""
    report = integrity_check(raw, _series_nwb(tmp_path / "t.nwb", **nwb))
    assert report.info["data_integrity_check"] == "PASSED", report.warnings
    assert {k: report.info[k] for k in info} == info
    assert ("scale_note" in report.info) is note


def test_sample_mismatches_and_wrong_scale(integrity_check, tmp_path):
    """Shifted samples or one altered channel are reported; equal samples on a wrong NWB scale are noted."""
    for stored in (np.roll(_VOLTS, 1, axis=0), _VOLTS * [1, 1.5, 1]):
        report = integrity_check(FakeRawIO(_VOLTS), _series_nwb(tmp_path / "t.nwb", stored.astype("float32")))
        assert report.info["data_integrity_check"].startswith("WARN") and any("Mismatch" in w for w in report.warnings)
    raw = _int16(3, (2000, 2))
    report = integrity_check(FakeRawIO(raw, gain=0.25), _series_nwb(tmp_path / "t.nwb", raw, conversion=0.25e-3))
    assert report.info["data_integrity_check"] == "PASSED"
    assert report.info["scale_note"].startswith("NWB scale is 1e+03 x")


def test_series_matched_to_their_segments(integrity_check, tmp_path):
    """Each NWB series is compared with its raw segment (by start time); mismatches name the series."""
    segments = [_int16(4, (3000, 2)), _int16(5, (8000, 2))]
    raw = FakeRawIO(segments, t_starts=[0.0, 7.5])
    nwb = _series_nwb(tmp_path / "t.nwb", segments, conversion=1e-6, starting_times=[0.0, 7.5])
    report = integrity_check(raw, nwb)
    assert [(r["series"], r["segment"], r["result"]) for r in report.info["integrity_series"]] == [
        ("ElectricalSeries0", 0, "PASSED"), ("ElectricalSeries1", 1, "PASSED")]
    assert "| Samples (ElectricalSeries1, segment 1) | 8000 | 8000 |" in report.to_markdown()
    meta = integrity.check_recording_metadata(nwb, _report())
    assert (meta.info["duration_seconds"], len(meta.info["recording_segments"])) == (11.0, 2)

    report = integrity_check(raw, _series_nwb(tmp_path / "t.nwb", [segments[0], segments[1] + 1], conversion=1e-6,
                                              starting_times=[0.0, 7.5]))
    assert report.warnings and all("ElectricalSeries1: Mismatch" in w for w in report.warnings)
    report = integrity_check(raw, _series_nwb(tmp_path / "t.nwb", segments[1], conversion=1e-6, starting_times=[7.5]))
    assert (report.info["data_integrity_check"], report.info["raw_samples"]) == ("PASSED", 8000)


def test_streams_matched_by_channel_names_then_sample_count(integrity_check, tmp_path):
    """Without a given stream the raw stream is found by channel names, then by sample count."""
    names = ["AP0", "AP1"]
    imec0, imec1, lf = _int16(9, (4000, 2)), _int16(10, (4006, 2)), _int16(7, (4000, 2))
    raw = FakeRawIO(_int16(8, (4000, 1)), streams={"imec0.ap": ([imec0], names), "imec1.ap": ([imec1], names),
                                                   "imec0.lf": ([lf], ["LF0", "LF1"])})
    report = integrity_check(raw, _series_nwb(tmp_path / "t.nwb", imec1, names=names))
    assert report.info["data_integrity_check"] == "PASSED"
    assert report.info["raw_stream"] == "imec1.ap (matched by channel names)"
    report = integrity_check(raw, _series_nwb(tmp_path / "stub.nwb", imec0[:300], names=names))
    assert report.info["data_integrity_check"] == (
        "SKIPPED (multi-stream, the channel names match streams imec0.ap, imec1.ap)")


def test_checks_restricted_to_the_given_stream(monkeypatch, tmp_path):
    """A stream given by id or name restricts the checks to it."""
    raw_io = FakeRawIO(np.zeros((10, 1), dtype="float32"))
    raw_io.header["signal_streams"] = np.array([("LFPx", "0", ""), ("Broa", "1", "")], dtype=_signal_stream_dtype)
    assert [integrity._resolve_stream_index(raw_io, stream_id=s) for s in ("1", "Broa", "pNeu")] == [1, 1, None]

    raw = FakeRawIO(np.zeros((100, 2), dtype="float32"),
                    streams={"xWav": ([np.random.default_rng(0).normal(size=(100, 2))], ["a", "b"])})
    monkeypatch.setattr(integrity, "open_rawio", lambda fmt, path: raw)
    (tmp_path / "raw").touch()
    distinct = lambda **kw: integrity.check_channel_distinctness(tmp_path / "raw", _report(), "tdt", **kw).errors
    assert distinct(stream_id="xWav") == []
    assert "ALL 2 channels identical" in distinct()[0]
    assert "Stream 'pNeu' not in raw" in distinct(stream_id="pNeu")[0]


def test_open_rawio_paths_and_unknown_format(monkeypatch, tmp_path):
    """A folder format opens a run folder as is and a file through its folder; an unknown format is an error."""
    import neo.rawio

    opened = []
    monkeypatch.setattr(neo.rawio, "SpikeGLXRawIO", lambda dirname: SimpleNamespace(
        parse_header=lambda: opened.append(dirname)))
    ap_bin = tmp_path / "run_g0_imec0" / "run_g0_t0.imec0.ap.bin"
    ap_bin.parent.mkdir()
    ap_bin.touch()
    formats.open_rawio("spikeglx", tmp_path)
    formats.open_rawio("spikeglx", ap_bin)
    assert opened == [str(tmp_path), str(ap_bin.parent)]
    with pytest.raises(ValueError, match="unknown raw format 'nope'"):
        formats.open_rawio("nope", tmp_path)


# --- BIDS validator and NWB Inspector --------------------------------------------------

def _validator_run(monkeypatch, stdout="", stderr="", returncode=0, raises=None):
    def run(*args, **kwargs):
        if raises:
            raise raises
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)
    monkeypatch.setattr("fairephys.validation.bids.subprocess.run", run)


def test_bids_validator_results(monkeypatch, tmp_path):
    """Validator issues become errors and warnings; no deno, no output or output that is not JSON are errors."""
    issues = {"issues": {"issues": [{"severity": "error", "location": "/a", "code": "E1"},
                                    {"severity": "warning", "location": "/b", "code": "W1"}]}}
    _validator_run(monkeypatch, stdout=json.dumps(issues))
    report = validate_bids(tmp_path, _report())
    assert (report.errors, report.warnings, report.info["bids_issues"]) == (
        ["[BIDS] /a: E1"], ["[BIDS] /b: W1"], {"errors": 1, "warnings": 1})
    for kwargs, error in [(dict(raises=FileNotFoundError("deno")), f"[BIDS] {DENO_HINT}"),
                          (dict(stderr="crashed", returncode=1), "[BIDS] Validator gave no output (exit code 1)"),
                          (dict(stdout="not json"), "[BIDS] Validator output is not JSON")]:
        _validator_run(monkeypatch, **kwargs)
        assert validate_bids(tmp_path, _report()).errors[0].startswith(error)


def test_bids_validator_setup(monkeypatch, tmp_path):
    """The validator is pinned and uses the packaged BEP032 schema or $BIDS_SCHEMA, never its own."""
    assert bids_validator().startswith("jsr:@bids/validator@")
    assert "stream_identifier" in json.dumps(json.loads(default_schema_path().read_text())["objects"]["columns"])
    _validator_run(monkeypatch, raises=AssertionError("the validator must not run without the schema"))
    assert "schema not found" in validate_bids(tmp_path, _report(), schema_path=tmp_path / "missing.json").errors[0]
    monkeypatch.setenv("BIDS_SCHEMA", str(tmp_path / "schema.json"))
    assert default_schema_path() == tmp_path / "schema.json"
    assert run_validation("s", tmp_path / "missing").errors == [f"[BIDS] Directory not found: {tmp_path / 'missing'}"]
    monkeypatch.setattr("fairephys.validation.bids._spec", lambda: {"validator": "jsr:@bids/validator"})
    with pytest.raises(ValueError, match="not pinned"):
        bids_validator()


def test_dataset_validation_scopes(monkeypatch, tmp_path):
    """--validate metadata runs the BIDS validator and shows the saved openMINDS warnings; nwb inspects each NWB."""
    _validator_run(monkeypatch, stdout=json.dumps({"issues": {"issues": []}}))
    (tmp_path / "val").mkdir()
    (tmp_path / "val" / "_openminds_warnings.json").write_text(json.dumps(["Species 'X' not resolved"]))
    nwb = tmp_path / "sub-A" / "ses-01" / "ecephys" / "sub-A_ses-01_task-rest_ecephys.nwb"
    nwb.parent.mkdir(parents=True)
    nwb.touch()
    inspected = []
    monkeypatch.setattr("fairephys.validation.validate_nwb", lambda path, report: inspected.append(path))
    report = run_validation("d", tmp_path, tmp_path / "val", scope="metadata")
    assert (report.errors, report.warnings, inspected) == ([], ["[openMINDS] Species 'X' not resolved"], [])
    assert run_validation("d", tmp_path, tmp_path / "val", scope="nwb").warnings == [] and inspected == [nwb]


def test_nwb_inspector_findings_per_file(monkeypatch, tmp_path):
    """Each NWB file keeps its findings (through the inspector's API); critical ones are errors naming the file."""
    import nwbinspector
    from nwbinspector import Importance, InspectorMessage

    def inspect(nwbfile_path):
        return [InspectorMessage(message="no age", importance=importance, check_function_name="c",
                                 object_type="Subject", object_name="subject", location="/general/subject",
                                 file_path=nwbfile_path)
                for importance in (Importance.CRITICAL, Importance.BEST_PRACTICE_VIOLATION)]
    monkeypatch.setattr(nwbinspector, "inspect_nwbfile", inspect)
    report = _report()
    for acq in ("V1", "V4"):
        (tmp_path / f"sub-A_ses-01_acq-{acq}_ecephys.nwb").touch()
        validate_nwb(tmp_path / f"sub-A_ses-01_acq-{acq}_ecephys.nwb", report)
    assert report.errors == ["[NWB] sub-A_ses-01_acq-V1_ecephys.nwb: 1 critical issues",
                             "[NWB] sub-A_ses-01_acq-V4_ecephys.nwb: 1 critical issues"]
    assert len(report.warnings) == 2 and "no age" in report.info["nwb_inspector_sub-A_ses-01_acq-V4_ecephys"]
