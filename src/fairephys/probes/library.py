"""ProbeInterface library lookup."""

import logging
import re
from urllib.error import HTTPError

import numpy as np
from probeinterface import Probe, get_probe, list_probes_by_manufacturer

logger = logging.getLogger(__name__)

_LIBRARY_NAME = re.compile(r"[\w.+-]+")  # library folder and file names


def library_key(cfg: dict) -> str:
    """The library's manufacturer key of a Hardware/Probes entry: its
    'library manufacturer', else its 'manufacturer' in lower case."""
    return str(cfg.get("library manufacturer") or "") or str(cfg.get("manufacturer") or "").lower()


def list_available(manufacturer: str) -> list[str]:
    """List probes available for a manufacturer."""
    return list_probes_by_manufacturer(manufacturer.lower())


def get_from_library(manufacturer: str, model: str):
    """The library probe `model` of `manufacturer` (a library key, see
    library_key), None when the library has no such probe.

    probeinterface downloads a probe it has not cached and raises HTTPError
    404 for one the library does not have; every other failure (no network,
    a server error) is raised, as it says nothing about the probe. Library
    keys and model names have no spaces, so a name with characters the
    library never uses is not looked up.
    """
    if not (_LIBRARY_NAME.fullmatch(manufacturer) and _LIBRARY_NAME.fullmatch(model)):
        logger.info("ProbeInterface library has no probe %s/%s (not a library name)", manufacturer, model)
        return None
    try:
        probe = get_probe(manufacturer=manufacturer, probe_name=model)
    except HTTPError as exc:
        if exc.code != 404:
            raise
        logger.info("ProbeInterface library has no probe %s/%s (%s)", manufacturer, model, exc.url)
        return None
    return _with_float_geometry(probe)


def _with_float_geometry(probe: Probe) -> Probe:
    """The probe with float positions and contour: a library file may hold
    integers, and a probe moved beside others in a group gets a float x
    offset added in place."""
    d = probe.to_dict(array_as_list=False)
    for key in ("contact_positions", "probe_planar_contour", "contact_plane_axes"):
        if d.get(key) is not None:
            d[key] = np.asarray(d[key], dtype=float)
    return Probe.from_dict(d)
