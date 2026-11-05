"""TDT SEV file converter.

Reads TDT SEV files using memory-mapped I/O via SpikeInterface's
``BinaryRecordingExtractor``, with the metadata (store, channel, sample
type, sampling rate) from the files' own headers. This bypasses Neo's
TdtRawIO which requires parsing the full TSQ index - much faster for large
datasets where SEV files are the primary data source.

Each SEV file is one channel: 40-byte header + raw samples.  Files are
memory-mapped individually and aggregated into a multi-channel recording,
written by RecordingConverter.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
from spikeinterface.core import BinaryRecordingExtractor, aggregate_channels

from .base import RecordingConverter

#: The 40-byte header of a TDT SEV file (TDT's SEV format)
SEV_HEADER = np.dtype([("size_bytes", "<u8"), ("file_type", "S3"), ("file_version", "u1"),
                       ("event_name", "S4"), ("channel_num", "<u2"), ("total_num_channels", "<u2"),
                       ("sample_width_bytes", "<u2"), ("reserved", "<u2"), ("data_format", "u1"),
                       ("decimate", "u1"), ("rate", "<u2"), ("reserved2", "S12")])
#: Sample type by the header's data format (its low 3 bits)
SEV_DTYPES = {0: "float32", 1: "int32", 2: "int16", 3: "int8", 4: "float64", 5: "int64"}


def _sev_channel_number(path: Path) -> int:
    """Extract channel number from SEV filename (e.g. '...xWav_ch42.sev' -> 42)."""
    m = re.search(r"[cC]h(\d+)\.sev$", path.name)
    if m is None:
        raise ValueError(f"Cannot parse channel from SEV filename: {path.name}")
    return int(m.group(1))


def read_sev_header(path: Path) -> dict:
    """Store, channel, sample type and sampling rate of one SEV file, from
    its header (rate 2^(rate - 12) * 25 MHz / decimate); a file without a
    SEV header or without its rate is an error."""
    header = np.fromfile(path, dtype=SEV_HEADER, count=1)
    if not len(header) or header[0]["file_type"] != b"SEV":
        raise ValueError(f"{path.name}: no SEV header")
    h = header[0]
    if not (h["rate"] and h["decimate"]):
        raise ValueError(f"{path.name}: SEV header (version {h['file_version']}) gives no sampling rate")
    # the store name is a 4-byte code, its characters in reverse order
    return {"store": h["event_name"][::-1].decode("ascii").strip("\x00 "), "channel": int(h["channel_num"]),
            "dtype": np.dtype(SEV_DTYPES[int(h["data_format"]) & 0b111]),
            "fs": 2.0 ** (int(h["rate"]) - 12) * 25e6 / int(h["decimate"])}


def _discover_sev_metadata(sev_dir: Path, store: str = "") -> dict:
    """The store, sampling rate, sample type, channels and files (in channel
    order) of the SEV files of `store` in `sev_dir` (every SEV file when no
    store is given, which must then be of one store); files that differ in
    rate or sample type are an error."""
    files = sorted(sev_dir.glob(f"*_{store}_[cC]h*.sev" if store else "*.sev"), key=_sev_channel_number)
    if not files:
        raise ValueError(f"No SEV files{f' of store {store}' if store else ''} in {sev_dir}")
    headers = [read_sev_header(f) for f in files]
    if len(stores := {h["store"] for h in headers}) != 1:
        raise ValueError(f"{sev_dir}: SEV files of the stores {sorted(stores)}; name the store")
    first = headers[0]
    if any((h["fs"], h["dtype"]) != (first["fs"], first["dtype"]) for h in headers):
        raise ValueError(f"{sev_dir}: SEV files of store {first['store']} differ in rate or sample type")
    return {"store": first["store"], "fs": first["fs"], "dtype": first["dtype"],
            "channels": [h["channel"] for h in headers], "files": files}


class TdtSevConverter(RecordingConverter):
    """Convert TDT SEV recordings to NWB using memory-mapped file access.

    Parameters
    ----------
    sev_dir : str or Path
        Directory containing ``.sev`` files for a single recording block.
    store : str, optional
        TDT store name to read (default: the one store of the SEV files).
    probe_group_path : str or Path, optional
        Path to probe_group.json for electrode geometry.
    """

    #: SEV header size in bytes (standard TDT SEV format).
    SEV_HEADER_BYTES = SEV_HEADER.itemsize

    def __init__(
        self,
        sev_dir: str | Path,
        *,
        store: str = "",
        probe_group_path: str | Path = None,
    ):
        sev_dir = Path(sev_dir)
        if not sev_dir.exists():
            raise FileNotFoundError(f"SEV directory not found: {sev_dir}")

        meta = _discover_sev_metadata(sev_dir, store)
        self._store_name = meta["store"]
        self._fs = meta["fs"]
        self._channels = meta["channels"]
        dtype = meta["dtype"]
        sev_files = meta["files"]

        per_channel = []
        for f in sev_files:
            ch_num = _sev_channel_number(f)
            rec = BinaryRecordingExtractor(
                file_paths=[str(f)],
                sampling_frequency=self._fs,
                dtype=dtype,
                num_channels=1,
                file_offset=self.SEV_HEADER_BYTES,
                channel_ids=[str(ch_num)],
                # TDT SEV data is in µV with gain=1.0, offset=0.0
                # Reference: Neo issue #1369
                # https://github.com/NeuralEnsemble/python-neo/issues/1369
                gain_to_uV=1.0,
                offset_to_uV=0.0,
            )
            per_channel.append(rec)

        recording = aggregate_channels(per_channel)
        recording.set_property(
            "channel_name",
            [f"{self._store_name} {ch}" for ch in self._channels],
        )
        super().__init__(recording, probe_group_path)
