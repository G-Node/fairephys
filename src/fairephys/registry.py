"""The registries and other resources shipped with the package."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

RESOURCES = Path(__file__).parent / "_resources"
REGISTRY_DIR = RESOURCES / "registry"


@lru_cache(maxsize=None)
def load(name: str) -> dict:
    """A registry by file name ("bids_mappings.yaml"), parsed once.

    A missing registry is an error: the mappings are the library's own
    resources, never something to run without.
    """
    path = REGISTRY_DIR / name
    with open(path) as f:
        return yaml.safe_load(f) or {}
