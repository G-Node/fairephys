"""The raw recording formats the library reads, in one table.

Keys are the names curate.py scripts and `fairephys init` use. Each entry
names the Neo RawIO class and its path argument, the converter class of
`fairephys.converters` with the neuroconv `source_data` key of the raw path
(None for a converter that reads the files itself), whether the raw
header's clock is UTC (else the header time is local time of the dataset's
time zone), and whether the header time stamps the first sample (else
Neo's time 0, see enrichment.neo).
"""

from __future__ import annotations

from pathlib import Path

RAW_FORMATS: dict[str, dict] = {
    "blackrock": {"rawio": "BlackrockRawIO", "arg": "filename", "clock": "utc",
                  "converter": "BlackrockConverter", "source": "file_path"},
    "intan": {"rawio": "IntanRawIO", "arg": "filename", "converter": None},
    "plexon2": {"rawio": "Plexon2RawIO", "arg": "filename", "header": "first sample",
                "converter": "PlexonConverter", "source": "file_path"},
    "spikeglx": {"rawio": "SpikeGLXRawIO", "arg": "dirname",
                 "converter": "SpikeGLXConverter", "source": "folder_path"},
    "tdt": {"rawio": "TdtRawIO", "arg": "dirname",
            "converter": "TdtConverter", "source": "folder_path"},
    "tdt-sev": {"rawio": "TdtRawIO", "arg": "dirname",
                "converter": "TdtSevConverter", "source": None},
}


def raw_format(fmt: str) -> dict:
    """The RAW_FORMATS entry of `fmt`; an unknown name is an error."""
    try:
        return RAW_FORMATS[fmt]
    except KeyError:
        raise ValueError(f"unknown raw format {fmt!r}; one of {', '.join(RAW_FORMATS)}") from None


def open_rawio(fmt: str, path: str | Path):
    """A parsed Neo RawIO of `path` in format `fmt` (a key of RAW_FORMATS)."""
    import neo.rawio

    spec = raw_format(fmt)
    path = Path(path)
    if spec["arg"] == "dirname" and not path.is_dir():
        path = path.parent  # a file inside the recording folder
    reader = getattr(neo.rawio, spec["rawio"])(**{spec["arg"]: str(path)})
    reader.parse_header()
    return reader
