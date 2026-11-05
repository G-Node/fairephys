"""fairephys.events: Neo event channels, TDT epocs, BHV2 trials and code matching, from synthetic files."""

import struct

import numpy as np
import pytest
from neo.rawio import ExampleRawIO, TdtRawIO
from neo.rawio.tdtrawio import tsq_dtype

from fairephys.events import bhv2_trials, event_times, match_codes, read_tdt_epocs


def test_event_times_of_an_event_and_an_epoch_channel():
    io = ExampleRawIO(filename="synthetic.fake")
    io.parse_header()
    times, labels = event_times(io, "Some events")
    assert times.dtype == np.float64 and list(times[:3]) == [0.0, 1.0, 2.0]
    assert list(labels[:3]) == ["trigger_a", "trigger_b", "trigger_a"]
    times, labels = event_times(io, 1)
    assert list(times[:2]) == [0.5, 1.5] and list(labels[:2]) == ["zoneX", "zoneX"]
    with pytest.raises(ValueError):
        event_times(io, "absent")


def _tdt_block(tank, name, start, onsets, values, stream_chunks=()):
    """A TDT block with one epoc store Tnum (TSQ records as Neo's tsq_dtype, the values as float64 bits) and a
    stream store xWav of two channels with `stream_chunks` data chunks per channel."""
    (tank / name).mkdir(parents=True)
    stem = tank / name / f"{tank.name}_{name}"
    fields = {"StoreName": "Tnum", "HeadName": "x", "Enabled": 1, "CircType": 0, "NumChan": 1, "StrobeMode": 0,
              "TankEvType": 257, "NumPoints": 0, "DataFormat": 4, "SampleFreq": 0}
    stream = {**fields, "StoreName": "xWav", "NumChan": 2, "TankEvType": 33025, "NumPoints": 4, "DataFormat": 0,
              "SampleFreq": 1000}
    stem.with_suffix(".Tbk").write_text("".join("[STOREHDRITEM]" + "".join(f"NAME={k};TYPE=L;VALUE={v};\n"
                                                                           for k, v in store.items())
                                                for store in (fields, stream)))
    for ext in (".Tdx", ".tev"):
        stem.with_suffix(ext).write_bytes(bytes(64))
    chunks = [(channel + 1, i) for channel, n in enumerate(stream_chunks) for i in range(n)]
    tsq = np.zeros(len(onsets) + len(chunks) + 3, dtype=tsq_dtype)
    tsq["evname"][[1, -1]], tsq["timestamp"][[1, -1]] = [b"\x01", b"\x02"], [start, start + 100]
    epocs = slice(2, 2 + len(onsets))
    tsq["evtype"][epocs], tsq["evname"][epocs], tsq["timestamp"][epocs] = 257, b"Tnum", start + np.array(onsets)
    tsq["offset"][epocs] = np.array(values, dtype=np.float64).view(np.int64)
    for k, (channel, i) in enumerate(chunks, 2 + len(onsets)):
        tsq[k] = (16 + 4 * 4, 33025, b"xWav", channel, 0, start + i * 4 / 1000, 0, 0, 1000.0)
    tsq.tofile(stem.with_suffix(".tsq"))


@pytest.mark.filterwarnings("ignore:The tbk file contains incomplete")
def test_tdt_epocs_from_the_block_start_with_their_float_values(tmp_path):
    """The epoc records of one store from the block's TSQ, in a tank (blocks by start time) or a block file; a
    block whose stream channels hold unequal chunk counts (which TdtRawIO rejects) reads the same; a store
    that is no epoc store, a wrong segment and a wrong path are errors."""
    tank = tmp_path / "tank"
    _tdt_block(tank, "Block-1", 1000.0, [1.0, 2.5], [1, 20220608], stream_chunks=(2, 2))
    _tdt_block(tank, "Block-2", 2000.0, [0.5], [-7.5], stream_chunks=(1, 2))
    times, values = read_tdt_epocs(tank, "Tnum")
    assert list(times) == [1.0, 2.5] and list(values) == [1.0, 20220608.0]
    for source, seg in ((tank, 1), (tank / "Block-2" / "tank_Block-2.tsq", 0)):
        times, values = read_tdt_epocs(source, "Tnum", seg_index=seg)
        assert list(times) == [0.5] and list(values) == [-7.5]
    with pytest.raises(ValueError, match="The sig lengths"):  # the layout Neo rejects
        TdtRawIO(dirname=str(tank / "Block-2" / "tank_Block-2.tsq")).parse_header()
    with pytest.raises(ValueError, match="'SVal' is not an epoc store of the block \\(Tnum\\)"):
        read_tdt_epocs(tank, "SVal")
    with pytest.raises(ValueError, match="'xWav' is not an epoc store"):
        read_tdt_epocs(tank, "xWav")
    with pytest.raises(ValueError, match="segment 2: the tank holds 2 blocks"):
        read_tdt_epocs(tank, "Tnum", seg_index=2)
    with pytest.raises(ValueError, match="no TDT block"):
        read_tdt_epocs(tmp_path, "Tnum")
    (tmp_path / "notes.txt").write_text("")
    with pytest.raises(ValueError, match="neither a tank folder nor a file of a TDT block"):
        read_tdt_epocs(tmp_path / "notes.txt", "Tnum")


def test_match_codes_rule():
    assert match_codes([9, 1, 2, 3, 18], [5, 9, 1, 2, 3, 18]) == [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5)]
    # the longest run first, then before and after it; of two runs of two the first in a
    assert match_codes([1, 2, 7, 3, 4, 5, 6], [7, 3, 1, 2, 4, 5, 6]) == [(0, 2), (1, 3), (4, 4), (5, 5), (6, 6)]
    # the run 7, 8 is taken, the five scattered codes a longest common subsequence holds are not
    assert match_codes([1, 2, 3, 4, 5, 7, 8], [7, 8, 1, 0, 2, 0, 3, 0, 4, 0, 5]) == [(5, 0), (6, 1)]
    with pytest.raises(ValueError):
        match_codes([1, 2], [3])


def _bhv2_field(name, value):
    """One BHV2 block: a struct for a dict, else a double column."""
    def text(s):
        return struct.pack("<Q", len(s)) + s.encode()
    if isinstance(value, dict):
        body = struct.pack("<Q", len(value)) + b"".join(_bhv2_field(k, v) for k, v in value.items())
        return text(name) + text("struct") + struct.pack("<3Q", 2, 1, 1) + body
    data = np.array(value, dtype="<f8").reshape(-1)
    return text(name) + text("double") + struct.pack("<3Q", 2, data.size, 1) + data.tobytes()


def _trial(codes, times, condition=1, error=9, start=1000.0):
    return {"Condition": condition, "TrialError": error, "ReactionTime": np.nan, "AbsoluteTrialStartTime": start,
            "BehavioralCodes": {"CodeTimes": times, "CodeNumbers": codes}}


def test_bhv2_trials(tmp_path):
    path = tmp_path / "run.bhv2"
    path.write_bytes(_bhv2_field("Trial2", _trial([9, 18], [0.0, 900.5], condition=3, start=5000.0))
                     + _bhv2_field("Trial1", _trial([9, 21, 18], [0.0, 10.0, 800.0]))
                     + _bhv2_field("TrialRecord", {"User": 0}))
    trials = list(bhv2_trials(path))
    assert [t["number"] for t in trials] == [1, 2]
    assert trials[1]["start_ms"] == 5000.0 and trials[1]["condition"] == 3 and trials[1]["trial_error"] == 9
    assert trials[0]["codes"].tolist() == [9, 21, 18] and trials[0]["code_times_ms"].tolist() == [0.0, 10.0, 800.0]
    assert np.isnan(trials[0]["trial"]["ReactionTime"])
    for content in (_bhv2_field("Trial2", _trial([9], [0.0])), _bhv2_field("TrialRecord", {"User": 0}),
                    _bhv2_field("Trial1", _trial([9, 18], [0.0])), _bhv2_field("Trial1", _trial([9.5], [0.0])),
                    _bhv2_field("Trial1", _trial([9], [0.0], condition=[1, 2]))):
        path.write_bytes(content)
        with pytest.raises(ValueError):
            list(bhv2_trials(path))
