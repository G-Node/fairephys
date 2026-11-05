"""Events of a recording and of its behaviour file, as the datasets read them:
one Neo event or epoch channel, the epocs of a TDT tank, the trials of a
MonkeyLogic BHV2 file, and the alignment of two code sequences. What the codes
mean and how a lab synchronizes its clocks stay in the datasets."""

import difflib
from collections.abc import Iterator, Sequence
from pathlib import Path

import numpy as np

from .bhv2 import load_bhv2


def event_times(rawio, channel: str | int, block_index: int = 0, seg_index: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """(times in s, labels) of one event or epoch channel of a parsed Neo
    RawIO, the channel by its name or its index in header["event_channels"],
    through Neo's get_event_timestamps and rescale_event_timestamp (times on
    Neo's clock of the block). A name that is not exactly one channel's is an
    error."""
    if isinstance(channel, str):
        names = [str(n) for n in rawio.header["event_channels"]["name"]]
        if names.count(channel) != 1:
            raise ValueError(f"event channel {channel!r}: {names.count(channel)} channels of that name in {names}")
        channel = names.index(channel)
    timestamps, _, labels = rawio.get_event_timestamps(block_index, seg_index, channel)
    times = rawio.rescale_event_timestamp(timestamps, dtype="float64", event_channel_index=channel)
    return np.asarray(times, dtype=float), np.asarray(labels)


def _tdt_block_files(tank_dir: str | Path, seg_index: int) -> Path:
    """The file stem of one TDT block as TdtRawIO addresses it: `tank_dir` a
    tank folder named as its files' prefix (its blocks are the segments, in
    start time order) or one file of a block (that block alone)."""
    from neo.rawio.tdtrawio import is_tdtblock

    path = Path(tank_dir)
    if path.is_dir():
        stems = [block / f"{path.name}_{block.name}" for block in sorted(path.iterdir()) if is_tdtblock(block)]
        if not stems:
            raise ValueError(f"{path.name}: no TDT block (a folder with .tbk, .tdx, .tev and .tsq files)")
        stems.sort(key=lambda stem: _tdt_block_start(stem.with_suffix(".tsq")))
    elif path.is_file() and is_tdtblock(path.parent):
        stems = [path.with_suffix("")]
    else:
        raise ValueError(f"{path.name} is neither a tank folder nor a file of a TDT block")
    if not 0 <= seg_index < len(stems):
        raise ValueError(f"segment {seg_index}: the tank holds {len(stems)} blocks")
    return stems[seg_index]


def _tdt_block_start(tsq: Path) -> float:
    """The block's start: the timestamp of the TSQ's second record, the
    start-of-block mark (as TdtRawIO reads it)."""
    from neo.rawio.tdtrawio import EVMARK_STARTBLOCK, tsq_dtype

    records = np.memmap(tsq, dtype=tsq_dtype, mode="r", shape=(2,))
    if records[1]["evname"] != chr(EVMARK_STARTBLOCK).encode():
        raise ValueError(f"{tsq.name}: no start-of-block record")
    return float(records[1]["timestamp"])


def read_tdt_epocs(tank_dir: str | Path, store: str, seg_index: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """(onsets in s from the start of the block, values) of one epoc store of
    a TDT block. `tank_dir` is what TdtRawIO opens: a tank folder named as
    its files' prefix, its blocks then the segments `seg_index` in start time
    order, or one file of a block (its .tsq) for that block alone. The
    records are read from the block's TSQ index in Neo's record layout
    (tsq_dtype; the store's event records, channel 0, as TdtRawIO's event
    channel returns them), not through TdtRawIO itself, which indexes every
    channel of every stream of the block for one store (a minute on a long
    block) and rejects a block whose stream channels hold unequal chunk
    counts. TDT keeps an epoc's value as a float64 in the 8-byte offset field
    of its TSQ record; the values are that field read bit for bit as
    float64. A store the block's Tbk header does not list as an epoc (event)
    store is an error."""
    from neo.rawio.tdtrawio import EVTYPE_STRON, read_tbk, tsq_dtype

    stem = _tdt_block_files(tank_dir, seg_index)
    stores = read_tbk(stem.with_suffix(".Tbk"))
    epocs = [str(name, "ascii") for name in stores["StoreName"][stores["TankEvType"] == EVTYPE_STRON]]
    if store not in epocs:
        raise ValueError(f"{stem.name}: {store!r} is not an epoc store of the block ({', '.join(epocs) or 'none'})")
    tsq = stem.with_suffix(".tsq")
    records = np.memmap(tsq, dtype=tsq_dtype, mode="r")
    mask = (records["evtype"] == EVTYPE_STRON) & (records["evname"] == store.encode()) & (records["channel"] == 0)
    chosen = records[mask]
    return chosen["timestamp"] - _tdt_block_start(tsq), chosen["offset"].view(np.float64)


def match_codes(a: Sequence, b: Sequence) -> list[tuple[int, int]]:
    """Index pairs (i, j) with a[i] == b[j] that align two code sequences (a
    recording's and a behaviour file's), increasing in both: no code is used
    twice and no pair crosses another.

    The rule is difflib.SequenceMatcher's without its junk heuristics
    (Ratcliff and Obershelp): the longest run of consecutive equal codes is
    matched first (of several, the one starting first in `a`, then in `b`),
    then the same rule is applied to the parts before it and after it in both
    sequences. A long run is kept even where another alignment would match
    more codes, so the result is not always a longest common subsequence.
    No matching code is an error."""
    blocks = difflib.SequenceMatcher(None, list(a), list(b), autojunk=False).get_matching_blocks()
    pairs = [(i + k, j + k) for i, j, n in blocks for k in range(n)]
    if not pairs:
        raise ValueError(f"no code of the {len(a)} matches one of the {len(b)}")
    return pairs


def _one(value, name: str, number: int):
    """The one value of a BHV2 field (stored as an array); several or none is an error."""
    values = np.asarray(value).ravel()
    if values.size != 1:
        raise ValueError(f"Trial{number}: {name} holds {values.size} values, expected one")
    return values[0].item()


def _whole(values, name: str, number: int) -> np.ndarray:
    """Whole numbers stored as floats, as integers; a fraction is an error."""
    values = np.asarray(values, dtype=float)
    if not np.all(values == np.round(values)):
        raise ValueError(f"Trial{number}: {name} holds values that are not whole numbers")
    return values.astype(int)


def bhv2_trials(path: str | Path) -> Iterator[dict]:
    """The trials of a MonkeyLogic BHV2 file, in order, each a dict of
    number (N of its top-level field TrialN), start_ms
    (AbsoluteTrialStartTime, ms of the MonkeyLogic run), codes and
    code_times_ms (BehavioralCodes CodeNumbers and CodeTimes, ms from the
    trial start), condition and trial_error (Condition, TrialError) and trial
    (the whole TrialN struct, for a dataset's further fields). A file without
    trials, trials not numbered 1 to n, a missing field, a scalar field
    without exactly one value, a code or condition or trial error that is not
    a whole number and codes and times of unequal length are errors."""
    data = load_bhv2(path)
    numbers = sorted(int(k[5:]) for k in data if k.startswith("Trial") and k[5:].isdigit())
    if not numbers:
        raise ValueError("BHV2 file without trials")
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError(f"BHV2 trials not numbered 1 to {len(numbers)}")
    for number in numbers:
        trial = data[f"Trial{number}"]
        behavioral = trial["BehavioralCodes"]
        codes = np.asarray(behavioral["CodeNumbers"]).ravel()
        code_times = np.asarray(behavioral["CodeTimes"], dtype=float).ravel()
        if codes.size != code_times.size:
            raise ValueError(f"Trial{number}: {codes.size} codes, {code_times.size} code times")
        yield {
            "number": number,
            "start_ms": float(_one(trial["AbsoluteTrialStartTime"], "AbsoluteTrialStartTime", number)),
            "codes": _whole(codes, "CodeNumbers", number),
            "code_times_ms": code_times,
            "condition": int(_whole(_one(trial["Condition"], "Condition", number), "Condition", number)),
            "trial_error": int(_whole(_one(trial["TrialError"], "TrialError", number), "TrialError", number)),
            "trial": trial,
        }
