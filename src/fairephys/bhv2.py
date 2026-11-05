"""MonkeyLogic BHV2 file parser.

BHV2 is a custom binary format used by NIMH MonkeyLogic (MATLAB) to store
behavioral data. Each field is a typed block:

    name_len (uint64) | name (ascii) | type_len (uint64) | type (ascii) |
    ndims (uint64) | dims (uint64[ndims]) | data (bytes)

Struct and cell types are recursive -- each sub-element is a complete block.

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


def _read_field(buf: bytes, pos: int) -> tuple[str, object, int]:
    """Read one block from *buf* at *pos*. Returns (name, value, new_pos)."""
    (name_len,) = struct.unpack_from("<Q", buf, pos); pos += 8
    name = buf[pos : pos + name_len].decode("ascii", errors="replace"); pos += name_len
    (type_len,) = struct.unpack_from("<Q", buf, pos); pos += 8
    type_name = buf[pos : pos + type_len].decode("ascii", errors="replace"); pos += type_len
    (ndims,) = struct.unpack_from("<Q", buf, pos); pos += 8
    dims = list(struct.unpack_from(f"<{ndims}Q", buf, pos)); pos += 8 * ndims
    n_elem = math.prod(dims) if dims else 0

    if type_name == "char":
        value = buf[pos : pos + n_elem].decode("ascii", errors="replace")
        pos += n_elem

    elif type_name == "cell":
        value = []
        for _ in range(n_elem):
            _, v, pos = _read_field(buf, pos)
            value.append(v)

    elif type_name == "function_handle":
        # function_handle stores a single sub-field: the string from func2str()
        _, value, pos = _read_field(buf, pos)

    elif type_name == "struct":
        (n_fields,) = struct.unpack_from("<Q", buf, pos); pos += 8
        structs = []
        for _ in range(n_elem):
            s = {}
            for _ in range(n_fields):
                fn, fv, pos = _read_field(buf, pos)
                s[fn] = fv
            structs.append(s)
        value = structs[0] if n_elem == 1 else structs

    elif type_name in _DTYPES:
        dt, sz = _DTYPES[type_name]
        nbytes = n_elem * sz
        arr = np.frombuffer(buf[pos : pos + nbytes], dtype=dt)
        value = arr[0] if n_elem == 1 else (arr.reshape(dims) if len(dims) > 1 else arr)
        pos += nbytes

    else:
        raise ValueError(f"Unknown BHV2 type: {type_name!r} at field {name!r}")

    return name, value, pos


def load_bhv2(path: str | Path) -> dict:
    """Load a MonkeyLogic BHV2 file into a dict."""
    buf = Path(path).read_bytes()
    pos, result = 0, {}
    while pos < len(buf):
        try:
            name, value, pos = _read_field(buf, pos)
            result[name] = value
        except (struct.error, ValueError, UnicodeDecodeError):
            break
    return result
