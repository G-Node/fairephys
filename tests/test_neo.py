"""Neo enrichment: first-sample start times, the curated streams, run entries, time zones."""

from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pytest

from conftest import sessions
from fairephys.enrichment.neo import _stream_start_times, enrich_document, extract, main
from fairephys.odml import props

# A Plexon-like file: the low-rate FP stream comes before the curated WB stream
TWO_STREAMS = {
    "file": "/data/raw/session/t150327001.pl2", "format": "plexon2",
    "block_index": 0, "nb_block": 1, "nb_segment": 1,
    "signal_streams": [{"id": "FP", "name": "FPl-Low Pass Filtered", "sampling_rate": 1000.0, "n_channels": 2},
                       {"id": "WB", "name": "WB-Wideband", "sampling_rate": 40000.0, "n_channels": 2}],
    "signal_channels": [{"name": n, "stream_id": n[:2], "sampling_rate": 1000.0 if n[:2] == "FP" else 40000.0}
                        for n in ("FP01", "FP02", "WB01", "WB02")],
    "stream_durations_s": {"FP": [12.0], "WB": [10.0]},
    "stream_start_times": {"FP": ["2020-01-02T03:04:05"], "WB": ["2020-01-02T03:04:06"]},
    "annotations": {"block": {}, "channel": {"FP": {"gain": [1, 1]}, "WB": {"gain": [2, 2]}}},
}

# A session of three runs (one TDT block each); the EMG store a little longer in the second
THREE_RUNS = {
    "format": "tdt", "block_index": 0, "nb_block": 1, "nb_segment": 3,
    "signal_streams": [{"id": "1", "name": "Broa", "sampling_rate": 24414.0625, "n_channels": 1},
                       {"id": "2", "name": "EMGx", "sampling_rate": 3051.76, "n_channels": 1}],
    "signal_channels": [{"name": "Broa 1", "stream_id": "1"}, {"name": "EMGx 1", "stream_id": "2"}],
    "stream_durations_s": {"1": [600.0, 900.0, 300.0], "2": [600.0, 900.1, 300.0]},
    "stream_start_times": {s: ["2024-01-01T10:00:00+00:00", "2024-01-01T10:20:00+00:00", "2024-01-01T10:40:00+00:00"]
                           for s in ("1", "2")},
}


@pytest.fixture
def enrich(make_doc):
    """enrich_document on a one-session document whose recording system names `curated` streams."""
    def run(neo_data, curated=None, time_zone="Europe/Berlin", **kwargs):
        doc = make_doc(Dataset={"time zone": time_zone}, Hardware={"Recording System": {"curated streams": curated}},
                       Sessions=[{"session id": "s1", "date": "2020-01-02", "start time": "", "end time": "",
                                  "sampling rate": "", "continuity": ""}])
        enrich_document(doc, neo_data, "s1", **kwargs)
        return sessions(doc)[0]
    return run


def _warnings(caplog):
    return [r for r in caplog.records if r.name == "fairephys.enrichment.neo" and r.levelname == "WARNING"]


_BLOCK = {"rec_datetime": datetime(2024, 2, 27, 10, 6, 49)}


@pytest.mark.parametrize("fmt, reader, expected", [
    # a Blackrock header is UTC; the first sample is the header time plus the stream's t_start
    ("blackrock", SimpleNamespace(raw_annotations={"blocks": [_BLOCK]},
                                  get_signal_t_start=lambda b, seg, i: [0.0034, 100.5][seg]),
     ["2024-02-27T10:06:49.003400+00:00", "2024-02-27T10:08:29.500000+00:00"]),
    # a Plexon2 header is local time (offset added later from the dataset's time zone)
    # and stamps the first sample, whatever the stream's t_start (the .pl2 clock runs since the device start)
    ("plexon2", SimpleNamespace(raw_annotations={"blocks": [{"m_CreatorDateTime": datetime(2020, 1, 2, 3, 4, 5)}]},
                                get_signal_t_start=lambda b, seg, i: [11931.877, 11991.877][seg]),
     ["2020-01-02T03:04:05", "2020-01-02T03:05:05"]),
    # a TDT tank: the TSQ Unix epoch plus the block offset
    ("tdt", SimpleNamespace(raw_annotations={"blocks": [{}]}, _global_t_start=1_700_000_000.0,
                            get_signal_t_start=lambda b, seg, i: [0.0, 60.0][seg]),
     ["2023-11-14T22:13:20+00:00", "2023-11-14T22:14:20+00:00"]),
    # SpikeGLX: the fileCreateTime of each segment's .meta
    ("spikeglx", SimpleNamespace(signals_info_dict={(seg, "s"): {"meta": {"fileCreateTime": t}} for seg, t in
                                                    enumerate(["2024-07-25T15:55:05", "2024-07-25T16:30:00"])}),
     ["2024-07-25T15:55:05", "2024-07-25T16:30:00"]),
])
def test_stream_start_times_are_first_samples(fmt, reader, expected):
    """Each segment's start: the format's time origin plus the stream's t_start."""
    assert _stream_start_times(reader, 0, 2, fmt, [{"id": "s", "name": "s"}]) == {"s": expected}


def test_curated_stream_gives_rate_channels_duration_and_start(enrich):
    """Rate, channels, duration and start are the curated stream's, with offset, without the raw path."""
    session = enrich(TWO_STREAMS, ["WB"])
    neo = session["Neo Metadata"]
    assert props(session).items() >= {"sampling rate": "40000 Hz", "continuity": "continuous",
                                      "start time": "2020-01-02T03:04:06+01:00",
                                      "end time": "2020-01-02T03:04:16+01:00"}.items()
    assert [props(neo)[k] for k in ("sampling_rate_hz", "duration_s", "format")] == [40000.0, 10.0, "plexon2"]
    assert [c.name for c in neo["signal_channels"].sections] == ["WB01", "WB02"]
    assert "file" not in props(neo)


def test_curated_streams_by_name_and_in_their_order(enrich):
    """Curated streams are found by id or name; channels follow the curated order, not Neo's."""
    tdt = {**TWO_STREAMS, "signal_streams": [{**TWO_STREAMS["signal_streams"][0], "id": "0", "name": "LFPx"},
                                             {**TWO_STREAMS["signal_streams"][1], "id": "1", "name": "Broa"}],
           "signal_channels": [{"name": "LFPx 1", "stream_id": "0"}, {"name": "Broa 1", "stream_id": "1"}],
           "stream_durations_s": {"0": [12.0], "1": [10.0]}, "stream_start_times": {}}
    neo = enrich(tdt, ["Broa"])["Neo Metadata"]
    assert (props(neo)["duration_s"], [c.name for c in neo["signal_channels"].sections]) == (10.0, ["Broa 1"])

    imec = {"signal_streams": [{"id": f"imec{i}.ap", "name": f"imec{i}.ap", "sampling_rate": 30000.0} for i in (1, 0)],
            "signal_channels": [{"name": "AP0", "stream_id": "imec1.ap"}, {"name": "AP0", "stream_id": "imec0.ap"}]}
    neo = enrich(imec, ["imec0.ap", "imec1.ap"])["Neo Metadata"]
    assert [props(c)["stream_id"] for c in neo["signal_channels"].sections] == ["imec0.ap", "imec1.ap"]


def test_without_curated_streams_the_first_stream_is_taken(enrich, caplog):
    """Several streams and none curated: the first (warned); no time zone: the header time without offset."""
    with caplog.at_level("WARNING"):
        session = enrich(TWO_STREAMS, time_zone=None)
    assert props(session["Neo Metadata"])["duration_s"] == 12.0
    assert props(session)["start time"] == "2020-01-02T03:04:05"
    assert len(_warnings(caplog)) == 2  # no curated streams, no time zone


def test_runs_keep_their_start_duration_and_other_streams(enrich, caplog):
    """Each run keeps its segment, start, duration and differing stream durations; mismatches are dropped."""
    session = enrich(THREE_RUNS, ["Broa"], runs=[5, 6, 8])
    assert (props(session)["start time"], props(session)["end time"], props(session)["continuity"]) == (
        "2024-01-01T10:00:00+00:00", "2024-01-01T10:45:00+00:00", "discontinuous")
    runs = session["Neo Metadata"]["runs"]
    assert [(r["run"], r["segment_index"], r["duration_s"], r["continuity"], r["start_time"])
            for r in map(props, runs.sections)] == [
        (5, 0, 600.0, "continuous", "2024-01-01T10:00:00+00:00"),
        (6, 1, 900.0, "continuous", "2024-01-01T10:20:00+00:00"),
        (8, 2, 300.0, "continuous", "2024-01-01T10:40:00+00:00")]
    assert props(runs["run 6"]["streams"]["2"]) == {"duration_s": 900.1}
    assert "streams" not in runs["run 5"].sections

    with caplog.at_level("WARNING"):
        session = enrich(THREE_RUNS, ["Broa"], runs=[5, 6])
    assert "runs" not in session["Neo Metadata"].sections and _warnings(caplog)


class FakeRawIO:
    """A Neo RawIO stand-in: one block of two segments, one stream of two channels."""

    header = {"nb_block": 1, "nb_segment": [2],
              "signal_streams": np.array([("Broa", "1")], dtype=[("name", "U8"), ("id", "U8")]),
              "signal_channels": np.array(
                  [(f"Broa {i}", str(i), 1000.0, "int16", "uV", 0.5, 0.0, "1") for i in (1, 2)],
                  dtype=[("name", "U8"), ("id", "U8"), ("sampling_rate", "f8"), ("dtype", "U8"), ("units", "U8"),
                         ("gain", "f8"), ("offset", "f8"), ("stream_id", "U8")]),
              "spike_channels": [], "event_channels": []}
    raw_annotations = {"blocks": [{"name": "b", "rec_datetime": datetime(2024, 1, 1, 9, 0), "nsx": np.int64(6),
                                   "segments": [
        {"signals": [{"stream_id": "1", "__array_annotations__": {"channel_names": ["a", "b"],
                                                                  "impedance": np.array([1.5, 2.5])}}]}]}]}

    def get_signal_sampling_rate(self, stream_index):
        return 1000.0

    def get_signal_size(self, block_index, seg_index, stream_index):
        return [60_000, 30_000][seg_index]

    def get_signal_t_start(self, block_index, seg_index, stream_index):
        return [0.0, 120.0][seg_index]


def test_extract_reads_streams_segments_and_annotations(monkeypatch):
    """extract(): channels, streams, per-segment durations and first samples, JSON-safe annotations."""
    monkeypatch.setattr("fairephys.enrichment.neo.open_rawio", lambda fmt, path: FakeRawIO())
    info = extract("raw.ns6", "blackrock")
    assert info["signal_streams"] == [{"name": "Broa", "id": "1", "sampling_rate": 1000.0, "n_channels": 2}]
    assert [c["name"] for c in info["signal_channels"]] == ["Broa 1", "Broa 2"]
    assert info["stream_durations_s"] == {"1": [60.0, 30.0]}
    assert info["stream_start_times"] == {"1": ["2024-01-01T09:00:00+00:00", "2024-01-01T09:02:00+00:00"]}
    assert info["annotations"] == {"block": {"rec_datetime": datetime(2024, 1, 1, 9, 0), "nsx": 6},
                                   "channel": {"1": {"impedance": [1.5, 2.5]}}}
    monkeypatch.setattr("sys.argv", ["neo", "raw.ns6", "--format", "blackrock"])
    main()  # the text summary of the same extract


def test_value_origin_of_a_dataset_built_dict(enrich):
    """A dataset's own dict (no Neo reader) with its value_origin: on the filled properties, _neo_fields and every
    property of the Neo Metadata section, and no property for a key it lacks; Neo's own dicts keep "neo.extract" on
    the fills and no origin in the section."""
    own = {"nb_segment": 1, "signal_streams": [{"id": "0", "name": "mat", "sampling_rate": 1000.0, "n_channels": 1}],
           "signal_channels": [{"name": "ch_001", "id": "1", "sampling_rate": 1000.0, "units": "uV", "stream_id": "0"}],
           "stream_durations_s": {"0": [120.0]}}
    for neo_data, curated, origin, fill in ((own, ["0"], ".mat files", ".mat files"),
                                            (TWO_STREAMS, ["WB"], None, "neo.extract")):
        session = enrich(neo_data, curated, value_origin=origin)
        neo = session["Neo Metadata"]
        assert {session.properties[k].value_origin for k in ("sampling rate", "continuity", "_neo_fields")} == {fill}
        assert len(list(neo.iterproperties())) > 10 and {p.value_origin for p in neo.iterproperties()} == {origin}
    assert [p.name for p in neo.properties][:4] == ["format", "block_index", "nb_block", "nb_segment"]
    neo = enrich(own, ["0"], value_origin=".mat files")["Neo Metadata"]
    assert [p.name for p in neo.properties] == ["nb_segment", "duration_s", "sampling_rate_hz"]
