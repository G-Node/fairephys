"""YAML templates to odML: strict schema validation, shared metadata, placements."""

import logging
import shutil
from pathlib import Path

import odml
import pytest
import yaml

import fairephys
from fairephys.curation import compile_metadata
from fairephys.metadata import (compile_to_odml, deep_merge, find_shared_dir, load_metadata, parse_value,
                                save_odml)
from fairephys.metadata.compiler import _merge_recordings_into_sessions, check_placements
from fairephys.validation.schema import validate_semantic, validate_template

TEMPLATES = Path(fairephys.__file__).parent / "_resources" / "templates"

# The required values the empty templates leave to the curator
FILLS = {
    "dataset.yaml": {"Dataset": {"species": "Macaca mulatta", "license": "CC-BY-4.0"}},
    "subject.yaml": {"Subject": {"subject id": "A", "species": "Macaca mulatta"}},
    "hardware.yaml": {"Hardware": {"recording type": "acute"}},
    "protocol.yaml": {"Behavioral Protocol": {"bids task label": "rest"}},
}


@pytest.fixture
def scaffold(tmp_path):
    """The filled shipped templates in root/datasets/x/metadata/curated below root/collection/metadata."""
    curated = tmp_path / "datasets" / "x" / "metadata" / "curated"
    curated.mkdir(parents=True)
    for path in TEMPLATES.glob("*.yaml"):
        if path.name != "collection.yaml":
            data = yaml.safe_load(path.read_text())
            for section, values in FILLS.get(path.name, {}).items():
                data[section].update(values)
            (curated / path.name).write_text(yaml.safe_dump(data, sort_keys=False))
    shared = tmp_path / "collection" / "metadata"
    shared.mkdir(parents=True)
    shutil.copy(TEMPLATES / "collection.yaml", shared / "collection.yaml")
    return curated


def test_shipped_templates_compile_strictly_and_save(scaffold, tmp_path):
    """The shipped templates pass the strict compile; save_odml writes YAML, pure XML and a self-contained viewer."""
    _, doc = compile_metadata(scaffold)
    assert {"Dataset", "Hardware", "Sessions", "Curation Software"} <= {s.name for s in doc.sections}
    yaml_path, xml_path = save_odml(doc, tmp_path / "out" / "x")
    assert yaml_path.is_file() and "xml-stylesheet" not in xml_path.read_text()
    html = xml_path.with_suffix(".html").read_text()
    assert html.startswith("<!DOCTYPE html>") and "<style>" in html and 'src="odml-notes.js"' not in html
    assert '<td class="prop-value empty">(empty)</td>' in html and "\u2014" not in html  # no dash as the empty glyph


def test_schema_error_stops_the_compile_unless_not_strict(scaffold, caplog):
    """A template that fails its schema stops compile_metadata; strict=False logs it instead."""
    data = yaml.safe_load((scaffold / "subject.yaml").read_text())
    data["Subject"]["subject id"] = "not valid"
    (scaffold / "subject.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="Schema validation failed"):
        compile_metadata(scaffold)
    with caplog.at_level(logging.WARNING):
        compile_metadata(scaffold, strict=False)
    assert any("subject id" in r.getMessage() for r in caplog.records)


def test_shared_metadata_found_above_or_given(scaffold, tmp_path):
    """collection/metadata above the templates (or a given dir) is merged underneath; the dataset wins."""
    shared = tmp_path / "collection" / "metadata"
    assert find_shared_dir(scaffold) == shared
    (shared / "collection.yaml").write_text(yaml.safe_dump({"_schema": "collection.v1", "Dataset": {
        "Project": {"name": "P", "full name": "Project"},
        "Fundings": [{"funder": "EC", "award number": "1"}, {"funder": "EC", "award number": "2"}]}}))
    dataset = yaml.safe_load((scaffold / "dataset.yaml").read_text())
    dataset["Dataset"]["Fundings"] = [{"funder": "DFG", "award number": "X"},
                                      {"funder": "EC", "award number": "2", "acknowledgement": "own"}]
    (scaffold / "dataset.yaml").write_text(yaml.safe_dump(dataset))

    meta = load_metadata(scaffold)
    assert meta["Dataset"]["Project"] == {"name": "P", "full name": "Project"}
    # the dataset's own Fundings first, then the shared ones it does not replace by award number
    assert [(f["award number"], f.get("acknowledgement")) for f in meta["Dataset"]["Fundings"]] == [
        ("X", None), ("2", "own"), ("1", None)]

    with pytest.raises(FileNotFoundError, match="Shared metadata directory not found"):
        load_metadata(scaffold, shared_dir=tmp_path / "nowhere")
    shutil.rmtree(tmp_path / "collection")
    assert find_shared_dir(scaffold) is None and "Project" not in load_metadata(scaffold)["Dataset"]


def test_deep_merge_keeps_inputs():
    """Nested dicts merge, lists and scalars of the override replace, neither input is mutated."""
    base = {"Dataset": {"Project": {"name": "P"}, "Consortium": {"name": "C", "email": "e"}}, "keep": 1}
    merged = deep_merge(base, {"Dataset": {"Consortium": {"name": "O"}}, "list": [1]})
    assert merged == {"Dataset": {"Project": {"name": "P"}, "Consortium": {"name": "O", "email": "e"}},
                      "keep": 1, "list": [1]}
    assert base["Dataset"]["Consortium"]["name"] == "C"


def test_recordings_merge_replaces_values_by_values_only():
    """A recordings.yaml entry fills and overrides its session; its empty values keep the session's."""
    merged = _merge_recordings_into_sessions({
        "Sessions": [{"session id": "01", "start time": "2024-01-01T10:00:00", "continuity": ""}],
        "Recordings": {"01": {"start time": "", "continuity": "continuous", "sampling rate": 30000, "note": None}}})
    assert merged == {"Sessions": [{"session id": "01", "start time": "2024-01-01T10:00:00",
                                    "continuity": "continuous", "sampling rate": 30000, "note": None}]}


@pytest.mark.parametrize("probes, placements, implanted, error", [
    (["P"], ["P", "P"], [], "session 01: probe P placed more than once"),
    (["P"], ["Q"], [], "session 01: probe Q not in Hardware.Probes"),
    (["P"], [], ["Q"], "subject A: probe Q not in Hardware.Probes"),
    (["P", "P"], ["P"], [], "Hardware.Probes names repeat: P"),
])
def test_check_placements(probes, placements, implanted, error):
    """A placement names one existing device, a device once per session or subject."""
    subject = {"subject id": "A"}
    if implanted:
        subject["Surgical Preparation"] = {"Implanted Probes": [{"probe": p} for p in implanted]}
    metadata = {"Hardware": {"Probes": [{"name": p} for p in probes]}, "Subject": subject,
                "Sessions": [{"session id": "01", "Electrode Placements": [{"probe": p} for p in placements]}]}
    with pytest.raises(ValueError, match=error):
        check_placements(metadata)
    with pytest.raises(ValueError, match=error):
        compile_to_odml(metadata)


def test_values_split_into_value_and_unit(tmp_path):
    """'30000 hertz' compiles to the number and its unit; text with commas survives the XML round trip."""
    assert [parse_value(v) for v in ("30000 hertz", "10.5 kilogram", "Macaca mulatta", 42)] == [
        (30000, "hertz"), (10.5, "kilogram"), ("Macaca mulatta", None), (42, None)]
    refs = ["Author, A. et al. Title one. (2025).", "Other, B. & Third, C. Title two. (2024)."]
    doc = compile_to_odml({"Dataset": {"related publications": refs},
                           "Hardware": {"Recording System": {"sampling rate": "30000 hertz"}}})
    assert "doi" not in compile_to_odml({"Dataset": {"doi": ""}}, omit_empty=True)["Dataset"].properties
    rate = doc["Hardware"]["Recording System"].properties["sampling rate"]
    assert (rate.values, rate.unit) == ([30000], "hertz")
    _, xml_path = save_odml(doc, tmp_path / "doc")
    assert odml.load(str(xml_path))["Dataset"].properties["related publications"].values == refs


def test_template_schemas():
    """Each template validates against its schema; a template without one is an error."""
    subject = {"subject id": "A", "species": "Macaca mulatta", "biological sex": "male"}
    system = {"manufacturer": "M", "sampling rate": "30000 hertz"}
    states = lambda states: {"Data Processing": [{"name": "S", "technique": "video annotation",
                                                  "annotation": {"states": states}}]}
    protocol = lambda duration, cited="": {"Behavioral Protocol": {
        "name": "P", "description": "d", "bids task label": "rest", "block duration": duration, "described in": cited}}
    table = [
        ("subject.yaml", {"Subject": subject}, None),
        ("subject.yaml", {"Subjects": [{"Subject": subject}, {"Subject": {**subject, "subject id": "B"}}]}, None),
        ("subject.yaml", {}, "(root): 'Subject' is a required property"),
        ("subject.yaml", {"Subjects": [{"Subject": {**subject, "subject id": "A-1"}}]},
         "Subjects.0.Subject.subject id: 'A-1' does not match"),
        ("subject.yaml", {"Subject": {**subject, "subject id": "Monkey A"}}, "Subject.subject id"),
        ("subject.yaml", {"Subject": {**subject, "birth date": "2010-05-04"}}, "Subject: 'birth date' should not"),
        ("subject.yaml", {"Subject": {**subject, "age": "7 year", "age reference date": "2024-03-15"}}, None),
        ("subject.yaml", {"Subject": {**subject, "age reference date": "15.03.2024"}}, "Subject.age reference date"),
        ("hardware.yaml", {"Hardware": {"Recording System": system,
                                        "Probes": [{"name": "P", "number of contacts": 2, "notes": "PF1 broken"}]}},
         None),
        ("hardware.yaml", {"Hardware": {"Probes": [{"number of contacts": 2, "probe type": "silicon-probe"}]}},
         "'silicon-probe' is not one of"),
        ("processing.yaml", states([{"label": "move", "code": "M", "description": "d"}]), None),
        ("processing.yaml", states(["move", {"label": "rest"}]), "states"),
        ("protocol.yaml", protocol("5 minute"), None),
        ("protocol.yaml", protocol("5 min"), "block duration"),
        ("protocol.yaml", protocol("5 minute", "10.1234/x"), None),
        ("protocol.yaml", protocol("5 minute", "https://example.org/p"), None),
        ("protocol.yaml", protocol("5 minute", "Author et al., 2026"), "Behavioral Protocol.described in"),
        ("protocol.yaml", {"Behavioral Protocol": {**protocol("5 minute")["Behavioral Protocol"], "reference": "R"}},
         "Behavioral Protocol: 'reference' should not"),
        ("hardware.yaml", {"Hardware": {"Probes": [{"number of contacts": 2, "electrode identifiers": ["a"]}]}},
         "Hardware.Probes.1: electrode identifiers count (1) != number of contacts (2)"),
        ("hardware.yaml", {"Hardware": {"Recording System": {**system, "gain unit": "V per count"}}}, None),
        ("hardware.yaml", {"Hardware": {"Recording System": {**system, "gain unit": "uV"}}}, "'uV' is not one of"),
        ("dataset.yaml", {"Dataset": {"release date": "15.09.2025"}}, "Dataset.release date: '15.09.2025' does not match"),
        ("nothing.yaml", {"_schema": "nothing.v9"}, "no schema 'nothing.v9'"),
    ]
    for template, data, error in table:
        errors = validate_template(data, template)
        assert errors == [] if error is None else any(error in e for e in errors), (template, data, errors)
    private = validate_template({"Subject": {**subject, "internal identifier": "K123"}}, "subject.yaml")
    assert private and not any("K123" in e for e in private)  # the private value is never printed
    probes = {"Hardware": {"Probes": [{"number of contacts": 2, "electrode identifiers": []}]}}
    assert validate_semantic(probes, "hardware.v1") == []  # empty: the ids come from ProbeInterface
