"""Shared fixtures. Every test runs offline and independent of the checkout it runs in.

- No ProbeInterface library download: every lookup is a probe the library lacks (HTTP 404), so
  probes are parametric; a test patches fairephys.probes.library.get_probe for a library probe.
- No git: provenance.source_checkout gives {} (version only); its own tests call the imported function.

`make_doc(**changes)` compiles BASE (a small dataset as the YAML templates give it) through
compile_to_odml with `changes` merged in (dicts recursively, None removes a key); `doc` is BASE
compiled, `session` its one session.

Golden files: `golden(name, text)` compares text exactly with golden/<name> next to this file.
With the environment variable UPDATE_GOLDEN=1 it rewrites the file instead (review the diff after):

    UPDATE_GOLDEN=1 uv run pytest -q <this test folder> -k golden
"""

import copy
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

import pytest
from probeinterface import ProbeGroup, generate_linear_probe
from pynwb import NWBFile

from fairephys.metadata import compile_to_odml

GOLDEN_DIR = Path(__file__).parent / "golden"

BASE = {
    "Dataset": {
        "short name": "test-ds", "full name": "Test dataset", "short description": "Test recording",
        "description": "A test experiment", "keywords": ["test", "ephys"], "license": "CC-BY-4.0",
        "time zone": "Europe/Paris",
        "Custodian": {"given name": "Jane", "family name": "Doe", "email": "jane@example.org",
                      "affiliation": {"lab": "Test Lab", "institution": "Test Uni"}},
        "Contributors": [{"given name": "Jane", "family name": "Doe", "roles": ["data collection"],
                          "affiliation": {"lab": "Test Lab", "institution": "Test Uni", "country": "Germany"}}],
        "Fundings": [{"funder": "Test Foundation", "award number": "GRANT-001", "award title": "Test Grant"}],
    },
    "Subject": {"subject id": "MonkeyA", "species": "Macaca mulatta", "biological sex": "female",
                "age": "7 year", "weight": "9.5 kilogram"},
    "Hardware": {
        "Recording System": {"name": "MySystem", "manufacturer": "Blackrock", "model": "Cerebus"},
        "Probes": [{"name": "Utah Array", "manufacturer": "Blackrock", "model": "Utah", "probe type": "utah",
                    "number of contacts": 96}],
    },
    "Behavioral Protocol": {"name": "Test Task", "bids task label": "rest"},
    "Sessions": [{"session id": "ses01", "date": "2024-03-15", "start time": "10:30:00",
                  "Electrode Placements": [{"probe": "Utah Array", "brain region": "M1", "hemisphere": "left"}]}],
}


def merged(base: dict, changes: dict) -> dict:
    """base with changes merged in (dicts recursively, None removes a key)."""
    out = dict(base)
    for key, value in changes.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merged(out[key], value)
        else:
            out[key] = value
    return out


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No ProbeInterface library download (404 for every probe), no git calls."""
    def not_in_library(manufacturer, probe_name, **kwargs):
        raise HTTPError(f"https://example.invalid/{manufacturer}/{probe_name}.json", 404, "Not Found", {}, None)
    monkeypatch.setattr("fairephys.probes.library.get_probe", not_in_library)
    monkeypatch.setattr("fairephys.metadata.provenance.source_checkout", lambda *args: {})


@pytest.fixture
def make_doc():
    def make(_changes: dict = None, **changes):
        return compile_to_odml(merged(copy.deepcopy(BASE), {**(_changes or {}), **changes}))
    return make


@pytest.fixture
def doc(make_doc):
    return make_doc()


@pytest.fixture
def session(doc):
    return doc["Sessions"].sections[0]


def sessions(doc) -> list:
    """The session sections of a compiled document."""
    return list(doc["Sessions"].sections)


def empty_nwbfile() -> NWBFile:
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return NWBFile(session_description="t", identifier="t", session_start_time=start)


def probe_group(*probes) -> ProbeGroup:
    group = ProbeGroup()
    for probe in probes:
        group.add_probe(probe)
    return group


def linear_probe(probe_id: str, contact_ids, channels, pitch: float = 100, x_offset: float = None):
    """A linear ProbeInterface probe with ids, wiring and (optionally) a group offset."""
    probe = generate_linear_probe(num_elec=len(contact_ids), ypitch=pitch)
    probe.set_contact_ids(list(contact_ids))
    probe.set_device_channel_indices(list(channels))
    probe.annotate(probe_id=probe_id)
    if x_offset is not None:
        probe.move([x_offset, 0])
        probe.annotate(x_offset=float(x_offset))
    return probe


@pytest.fixture
def golden():
    def check(name: str, text: str):
        path = GOLDEN_DIR / name
        if os.environ.get("UPDATE_GOLDEN") == "1":
            path.parent.mkdir(exist_ok=True)
            path.write_text(text)
            return
        assert path.is_file(), f"no golden file {path}: run with UPDATE_GOLDEN=1"
        assert text == path.read_text(), f"{name} differs from the golden file (UPDATE_GOLDEN=1 rewrites it)"
    return check
