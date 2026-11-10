"""MonkeyLogic BHV2 file parser.

BHV2 is a custom binary format used by NIMH MonkeyLogic (MATLAB) to store
behavioral data. Each field is a typed block:

    name_len (uint64) | name (ascii) | type_len (uint64) | type (ascii) |
    ndims (uint64) | dims (uint64[ndims]) | data (bytes)

Struct and cell types are recursive -- each sub-element is a complete block.
Data are written column-major (MATLAB order, ``mlbhv2.m``), and arrays keep
their MATLAB size: numeric arrays are reshaped in Fortran order, a char array
with more than one row is a list of its rows, and cell and struct arrays are
a ``MatlabArray`` (a list in file order with the size in ``dims``).

Format details informed by Julia Sprenger's MonkeyLogicRawIO for Neo:
  https://github.com/NeuralEnsemble/python-neo/pull/1030

Usage::

    from fairephys.bhv2 import load_bhv2
    data = load_bhv2("path/to/file.bhv2")
    # Top-level keys: IndexPosition, FileInfo, MLConfig, Trial1..TrialN,
    # TrialRecord (summary), FileIndex, plus optional Cam1_* webcam data.
"""

import math
import struct
from pathlib import Path

import numpy as np

_DTYPES = {
    "double": (np.float64, 8),
    "single": (np.float32, 4),
    "uint8": (np.uint8, 1),
    "uint16": (np.uint16, 2),
    "uint32": (np.uint32, 4),
    "uint64": (np.uint64, 8),
    "int8": (np.int8, 1),
    "int16": (np.int16, 2),
    "int32": (np.int32, 4),
    "int64": (np.int64, 8),
    "logical": (np.bool_, 1),
}


class MatlabArray(list):
    """A MATLAB cell or struct array: its elements in file (column-major) order, its size in dims."""

    def __init__(self, items, dims):
        super().__init__(items)
        self.dims = tuple(dims)


def _take(buf: bytes, pos: int, n: int, what: str) -> tuple[bytes, int]:
    """n bytes of *buf* from *pos*; running past the end of the file is an error."""
    if pos + n > len(buf):
        raise ValueError(f"{what} needs {n} bytes at byte {pos}, the file ends at byte {len(buf)}")
    return buf[pos : pos + n], pos + n


def _uint64(buf: bytes, pos: int, what: str) -> tuple[int, int]:
    data, pos = _take(buf, pos, 8, what)
    return struct.unpack("<Q", data)[0], pos


def _read_field(buf: bytes, pos: int) -> tuple[str, object, int]:
    """Read one block from *buf* at *pos*. Returns (name, value, new_pos)."""
    name_len, pos = _uint64(buf, pos, "name length")
    data, pos = _take(buf, pos, name_len, "name")
    name = data.decode("ascii", errors="replace")
    type_len, pos = _uint64(buf, pos, f"type length of {name!r}")
    data, pos = _take(buf, pos, type_len, f"type of {name!r}")
    type_name = data.decode("ascii", errors="replace")
    ndims, pos = _uint64(buf, pos, f"number of dimensions of {name!r}")
    data, pos = _take(buf, pos, 8 * ndims, f"size of {name!r}")
    dims = list(struct.unpack(f"<{ndims}Q", data))
    n_elem = math.prod(dims) if dims else 0

    if type_name == "char":
        data, pos = _take(buf, pos, n_elem, f"text of {name!r}")
        if len(dims) > 2 and math.prod(dims[2:]) != 1:
            raise ValueError(f"char array {name!r} of size {dims}: more than two dimensions are not supported")
        if len(dims) == 2 and dims[0] > 1:
            chars = np.frombuffer(data, dtype="S1").reshape(dims, order="F")
            value = [b"".join(row).decode("ascii", errors="replace") for row in chars]
        else:
            value = data.decode("ascii", errors="replace")

    elif type_name == "cell":
        items = []
        for _ in range(n_elem):
            _, v, pos = _read_field(buf, pos)
            items.append(v)
        value = MatlabArray(items, dims)

    elif type_name == "function_handle":
        # function_handle stores a single sub-field: the string from func2str()
        _, value, pos = _read_field(buf, pos)

    elif type_name == "struct":
        n_fields, pos = _uint64(buf, pos, f"number of fields of {name!r}")
        structs = []
        for _ in range(n_elem):
            s = {}
            for _ in range(n_fields):
                fn, fv, pos = _read_field(buf, pos)
                s[fn] = fv
            structs.append(s)
        value = structs[0] if n_elem == 1 else MatlabArray(structs, dims)

    elif type_name in _DTYPES:
        dt, sz = _DTYPES[type_name]
        data, pos = _take(buf, pos, n_elem * sz, f"{type_name} data of {name!r}")
        arr = np.frombuffer(data, dtype=dt)
        value = arr[0] if n_elem == 1 else (arr.reshape(dims, order="F") if len(dims) > 1 else arr)

    else:
        raise ValueError(f"Unknown BHV2 type: {type_name!r} at field {name!r}")

    return name, value, pos


def load_bhv2(path: str | Path) -> dict:
    """Load a MonkeyLogic BHV2 file into a dict. A truncated or damaged file
    is an error naming the file and the byte where its variable starts."""
    buf = Path(path).read_bytes()
    pos, result = 0, {}
    while pos < len(buf):
        try:
            name, value, end = _read_field(buf, pos)
        except ValueError as e:
            raise ValueError(f"{path}: damaged BHV2 variable at byte {pos}: {e}") from e
        result[name] = value
        pos = end
    return result
