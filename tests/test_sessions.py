"""Session identity (subject, session id, task) kept apart in every output."""

import logging

import pytest
import yaml

from conftest import sessions
from fairephys.bids.builder import extract_participants, extract_sidecar_fields
from fairephys.converters import build_nwb_metadata
from fairephys.curation import find_session
from fairephys.enrichment.neo import enrich_document
from fairephys.export.collection import SCHEMAS_PATH, SchemaBuilder, _load_yaml
from fairephys.export.readme import odml_to_readme
from fairephys.metadata import load_metadata, session_identity, session_subject, subject_sections

_ADULT = {"species": "Macaca mulatta", "biological sex": "male", "age category": "adult"}
_PLACED = {"start time": "10:00:00", "Electrode Placements": [{"probe": "Utah Array", "brain region": "M1"}]}


def _surgery(prep, anesthesia):
    return {"Surgical Preparation": {"preparation type": prep, "Surgery Details": {"anesthesia protocol": anesthesia}}}


@pytest.fixture
def doc(make_doc):
    """Subject A with two sessions, subject Z whose session 01 recorded two tasks."""
    return make_doc(Subject=None, _subjects_list=[
        {"Subject": {"subject id": "A", "weight": "9.5 kilogram", **_ADULT, **_surgery("in vivo", "ketamine")}},
        {"Subject": {"subject id": "Z", "weight": "10.6 kilogram", **_ADULT, **_surgery("ex vivo", "none")}},
    ], Sessions=[
        {"session id": "01", "subject": "A", "date": "2025-01-30", "duration": "", **_PLACED},
        {"session id": "02", "subject": "A", "date": "2025-02-01", "duration": "", **_PLACED},
        {"session id": "01", "subject": "Z", "date": "2025-02-12", "duration": "", "task": "first", **_PLACED},
        {"session id": "01", "subject": "Z", "date": "2025-02-12", "duration": "", "task": "second", **_PLACED},
    ])


def test_sessions_found_by_subject_and_task(doc):
    """find_session needs subject and task only where they tell entries apart; session_identity names them."""
    a1, a2, first, second = sessions(doc)
    assert [s.properties["subject id"].values[0] for s in subject_sections(doc)] == ["A", "Z"]
    assert session_subject(doc, second).properties["subject id"].values == ["Z"]
    assert find_session(doc, "02") is a2
    assert find_session(doc, "01", "Z", "second") is second
    assert find_session(doc, "01", "A", "rest") is a1  # no task named: the dataset's task
    with pytest.raises(ValueError, match="2 entries match"):
        find_session(doc, "01", "Z")
    with pytest.raises(KeyError, match="for B"):
        find_session(doc, "01", "B")
    with pytest.raises(KeyError, match="for Z task third"):
        find_session(doc, "01", "Z", "third")
    assert [session_identity(doc, s) for s in (a1, first)] == [
        {"subject_id": "A", "session_id": "01", "task": ""},
        {"subject_id": "Z", "session_id": "01", "task": "first"}]


def test_bids_and_nwb_take_each_sessions_subject(doc):
    """participants.tsv lists every subject; NWB Subject and ecephys.json Subject/... paths are the session's own."""
    assert [r["participant_id"] for r in extract_participants(doc)] == ["sub-A", "sub-Z"]
    subject = build_nwb_metadata(doc, sessions(doc)[2])["Subject"]
    assert (subject["subject_id"], subject["weight"]) == ("Z", "10.6 kg")
    fields = [extract_sidecar_fields(doc, s, "ecephys_json") for s in sessions(doc)]
    assert [(f["SampleEnvironment"], f["PharmaceuticalName"], f["TaskName"]) for f in fields] == [
        ("in vivo", "ketamine", "rest"), ("in vivo", "ketamine", "rest"),
        ("ex vivo", "none", "first"), ("ex vivo", "none", "second")]


def test_openminds_labels_per_subject_and_task(doc):
    """One subject state per BIDS session (shared by its tasks), one activity and device usage per recording."""
    builder = SchemaBuilder(doc, _load_yaml(SCHEMAS_PATH), kg_client=None)
    builder.build_all()
    activities = builder.entities["RecordingActivity"]
    assert [a.lookup_label for a in activities] == [
        "test-ds_sub-A_ses-01", "test-ds_sub-A_ses-02",
        "test-ds_sub-Z_ses-01_task-first", "test-ds_sub-Z_ses-01_task-second"]
    states = list({id(s): s for s in builder.entities["SubjectState"]}.values())
    assert [s.lookup_label for s in states] == ["test-ds_sub-A_ses-01", "test-ds_sub-A_ses-02", "test-ds_sub-Z_ses-01"]
    assert [s.relative_time_indication.value for s in states] == [0, 2, 0]  # days, per subject
    assert activities[2].inputs == activities[3].inputs == [states[2]]
    subjects = builder.entities["Subject"]
    assert [s.lookup_label for s in subjects] == ["test-ds_sub-A", "test-ds_sub-Z"]
    assert subjects[0].studied_states == states[:2]
    assert [u.lookup_label for a in activities[2:] for u in a.devices] == [
        "test-ds_sub-Z_ses-01_task-first_Utah-Array", "test-ds_sub-Z_ses-01_task-second_Utah-Array"]


def test_two_devices_of_one_model_are_two_usages(make_doc):
    """Two probes of one model are two devices: each placement its own usage on its own device node."""
    doc = make_doc(Hardware={"Probes": [{"name": "P 1", "model": "S-Probe", "number of contacts": 1},
                                        {"name": "P 2", "model": "S-Probe", "number of contacts": 1}]},
                   Sessions=[{"session id": "01", "date": "2025-01-30", "Electrode Placements": [
                       {"probe": "P 1", "brain region": "V1"}, {"probe": "P 2", "brain region": "V4"}]}])
    builder = SchemaBuilder(doc, _load_yaml(SCHEMAS_PATH), kg_client=None)
    builder.build_all()
    assert [(u.lookup_label, u.device.name) for a in builder.entities["RecordingActivity"] for u in a.devices] == [
        ("test-ds_sub-MonkeyA_ses-01_P-1", "P 1"), ("test-ds_sub-MonkeyA_ses-01_P-2", "P 2")]


def test_neo_enrichment_goes_to_its_session_entry(doc):
    """A recording's Neo metadata fills only the entry of its subject and task."""
    enrich_document(doc, {"format": "spikeglx", "duration_s": 1356.0}, "01", "Z", "second")
    enrich_document(doc, {"format": "spikeglx", "duration_s": 30.0}, "01", "A", "rest")
    assert [s.properties["duration"].values for s in sessions(doc)] == [["30.0 s"], [], [], ["22.6 min"]]
    assert "Neo Metadata" in sessions(doc)[3].sections and "Neo Metadata" not in sessions(doc)[2].sections


def test_readme_session_table_columns(doc, make_doc, tmp_path):
    """Subject and Task columns only when needed; a session with two tasks counts once."""
    text = odml_to_readme(doc, tmp_path)
    assert "3 recording sessions of 2 subjects from 2025-01-30 to 2025-02-12." in text
    assert "| Session | Subject | Task | Date | Probes |\n" in text
    assert "| 01 | Z | second | 2025-02-12 | Utah Array: M1 |\n" in text
    assert "| Session | Date | Probes |\n" in odml_to_readme(make_doc(), tmp_path)


def test_subjects_template_loads_as_a_list(tmp_path, caplog):
    """subject.yaml 'Subjects' becomes the subjects list; giving 'Subject' too is warned about."""
    entry = {"Subject": {"subject id": "A", "species": "Macaca mulatta", "biological sex": "male"}}
    for both in (False, True):
        data = {"_schema": "subject.v1", "Subjects": [entry], **(entry if both else {})}
        (tmp_path / "subject.yaml").write_text(yaml.safe_dump(data))
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            meta = load_metadata(tmp_path)
        assert [e["Subject"]["subject id"] for e in meta["_subjects_list"]] == ["A"]
        assert any(r.levelno == logging.WARNING for r in caplog.records) is both
