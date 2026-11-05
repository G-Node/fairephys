"""The content a dataset adds to its NWB beside the recording: trials, time
intervals, sorted units and sampled series, written with pynwb inside the
converters' add_to_nwbfile hook, after the recording and before the file is
written. Each helper takes plain data and the caller's descriptions. In a
test file (stub_test) the content is cut to the span of the file's
ElectricalSeries, the stub rule of base.stub_recording: a series keeps the
samples inside it and at least its first STUB_SAMPLES samples from the first
one in it (the recording stub's own rule), a table the rows whose interval
overlaps it, a unit its spike times inside it. A series or table the cut
leaves empty is not written, nor a units table without a spike in the span.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from hdmf.container import MultiContainerInterface
from hdmf.utils import get_data_shape
from pynwb import NWBFile, ProcessingModule, TimeSeries
from pynwb.ecephys import ElectricalSeries
from pynwb.epoch import TimeIntervals
from pynwb.misc import Units

from .base import STUB_SAMPLES

logger = logging.getLogger(__name__)

_TIMES = ("start_time", "stop_time")
_UNIT_KEYS = ("spike_times", "obs_intervals", "channels")


def recording_span(nwbfile: NWBFile) -> tuple[float, float]:
    """The time (seconds) of the first and of the last sample over the
    ElectricalSeries of the file's acquisition (series at a rate)."""
    series = [s for s in nwbfile.acquisition.values() if isinstance(s, ElectricalSeries)]
    if not series:
        raise ValueError("no ElectricalSeries in the NWB file: the hook runs after the recording is added")
    if any(s.rate is None for s in series):
        raise ValueError("an ElectricalSeries with timestamps instead of a rate has no sampled span")
    ends = [s.starting_time + (get_data_shape(s.data)[0] - 1) / s.rate for s in series]
    return min(s.starting_time for s in series), max(ends)


def _span(nwbfile: NWBFile, stub_test: bool) -> tuple[float, float] | None:
    return recording_span(nwbfile) if stub_test else None


def _rows_in_span(table: pd.DataFrame, span: tuple[float, float] | None) -> pd.DataFrame:
    if span is None:
        return table
    start, stop = span
    return table[(table["start_time"] <= stop) & (table["stop_time"] >= start)]


def _described_columns(columns: list[str], descriptions: dict[str, str], what: str) -> list[str]:
    """`columns` once every one has a description and no description is of a column the data lack."""
    if missing := [c for c in columns if c not in descriptions]:
        raise ValueError(f"{what}: no description for the columns {missing}")
    if unknown := sorted(set(descriptions) - set(columns)):
        raise ValueError(f"{what}: descriptions of columns the data lack: {unknown}")
    return columns


def _interval_columns(table: pd.DataFrame, descriptions: dict[str, str], what: str) -> list[str]:
    if missing := [c for c in _TIMES if c not in table.columns]:
        raise ValueError(f"{what}: the table lacks the columns {missing}")
    return _described_columns([c for c in table.columns if c not in _TIMES], descriptions, what)


def add_trials(nwbfile: NWBFile, trials: pd.DataFrame, descriptions: dict[str, str],
               stub_test: bool = False) -> int:
    """The trials table: start_time, stop_time (seconds, the file's time base)
    and the other columns of `trials`, each with its description. Returns
    the number of rows written; without a row there is no table (a table
    without rows cannot be written)."""
    columns = _interval_columns(trials, descriptions, "trials")
    rows = _rows_in_span(trials, _span(nwbfile, stub_test))
    if rows.empty:
        return 0
    for column in columns:
        nwbfile.add_trial_column(column, descriptions[column])
    for row in rows.to_dict("records"):
        nwbfile.add_trial(**row)
    return len(rows)


def add_time_intervals(nwbfile: NWBFile, name: str, intervals: pd.DataFrame, descriptions: dict[str, str],
                       description: str, stub_test: bool = False) -> TimeIntervals | None:
    """A TimeIntervals table `name` (epochs, states, events) with the columns
    of `intervals` beside start_time and stop_time, each with its description;
    None without a row."""
    columns = _interval_columns(intervals, descriptions, name)
    rows = _rows_in_span(intervals, _span(nwbfile, stub_test))
    if rows.empty:
        return None
    table = TimeIntervals(name=name, description=description)
    for column in columns:
        table.add_column(column, descriptions[column])
    for row in rows.to_dict("records"):
        table.add_interval(**row)
    nwbfile.add_time_intervals(table)
    return table


def add_units(nwbfile: NWBFile, units: list[dict], descriptions: dict[str, str], description: str,
              resolution: float, stub_test: bool = False) -> int:
    """The sorted units. Each dict holds spike_times (seconds, the file's time
    base), optionally obs_intervals ([[start, stop], ...], the unit's presence)
    and channels (the channel names of its electrodes, rows of the electrode
    table by channel_name), and the unit's properties, each a column with its
    description. `description` and `resolution` (the spike time resolution in
    seconds, the sorter's sample period) are the table's. Returns the number
    of units written; without a unit there is no table, and in a test file
    none when no spike of any unit falls in its span (a table of empty spike
    lists cannot be inspected)."""
    properties = _described_columns(sorted({k for unit in units for k in unit} - set(_UNIT_KEYS)),
                                    descriptions, "units")
    if nwbfile.units is not None:
        raise ValueError("the NWB file already holds a units table")
    rows = ({str(n): i for i, n in enumerate(nwbfile.electrodes["channel_name"][:])}
            if nwbfile.electrodes is not None else {})
    span = _span(nwbfile, stub_test)
    kept = []
    for unit in units:
        if missing := [p for p in properties if p not in unit]:
            raise ValueError(f"units: a unit lacks the properties {missing}")
        times = np.asarray(unit["spike_times"], dtype=float)
        intervals = unit.get("obs_intervals")
        if span is not None:
            start, stop = span
            times = times[(times >= start) & (times <= stop)]
            if intervals is not None:
                intervals = [[max(a, start), min(b, stop)] for a, b in intervals if a <= stop and b >= start]
                if not intervals:
                    continue
        kwargs = {p: unit[p] for p in properties}
        if "channels" in unit:
            if unknown := [c for c in unit["channels"] if c not in rows]:
                raise ValueError(f"units: channels {unknown} are not in the electrode table")
            kwargs["electrodes"] = [rows[c] for c in unit["channels"]]
        if intervals is not None:
            kwargs["obs_intervals"] = intervals
        kept.append({"spike_times": times, **kwargs})
    if not kept or (span is not None and not any(len(unit["spike_times"]) for unit in kept)):
        return 0
    nwbfile.units = Units(name="units", description=description, resolution=resolution)
    for column in properties:
        nwbfile.add_unit_column(column, descriptions[column])
    for unit in kept:
        nwbfile.add_unit(**unit)
    return len(kept)


def add_timeseries(nwbfile: NWBFile, name: str, data, rate: float, starting_time: float, unit: str,
                   description: str, parent=None, kind=TimeSeries, stub_test: bool = False, **kwargs):
    """A series sampled at `rate` from `starting_time` (seconds, the file's
    time base), of class `kind` (TimeSeries, or a subclass such as
    SpatialSeries with its own arguments in `kwargs`), added to the
    acquisition (`parent` None), a processing module or a container in one
    (Position, EyeTracking). In a test file the series keeps the samples in
    the file's span and at least STUB_SAMPLES from its first sample in it (the
    recording stub's rule; a series at a lower rate than the recording would
    keep one sample, which NWB readers take as a transposed series). Returns
    the series; None when the series ends before the span (an empty series
    is not written)."""
    data = np.asarray(data)
    if (span := _span(nwbfile, stub_test)) is not None:
        start, stop = span
        first = min(len(data), max(0, int(np.ceil((start - starting_time) * rate - 1e-6))))
        last = min(len(data), max(first + STUB_SAMPLES, int(np.floor((stop - starting_time) * rate + 1e-6)) + 1))
        if last == first:
            logger.info("Series %s has no sample in the test file's span %.6f to %.6f s: not written",
                        name, start, stop)
            return None
        data, starting_time = data[first:last], starting_time + first / rate
    series = kind(name=name, data=data, rate=float(rate), starting_time=float(starting_time), unit=unit,
                  description=description, **kwargs)
    if parent is None:
        nwbfile.add_acquisition(series)
    elif isinstance(parent, ProcessingModule):
        parent.add(series)
    elif isinstance(parent, MultiContainerInterface) and isinstance(parent.__clsconf__, dict):
        getattr(parent, parent.__clsconf__["add"])(series)
    else:
        raise TypeError(f"parent must be None, a ProcessingModule or a container of one series type, "
                        f"not {type(parent).__name__}")
    return series
