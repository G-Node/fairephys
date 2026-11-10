"""README and DataCite: one golden file each, and the values they compute."""

import logging
import os
import shutil
import subprocess
from datetime import date
from importlib.metadata import version

import odml
import pytest
import yaml

from conftest import sessions
from fairephys.bids import create_dataset_layout
from fairephys.bids.builder import load_registry
from fairephys.export.datacite import license_text, odml_to_datacite, validate, write_datacite
from fairephys.export.readme import odml_to_readme
from fairephys.metadata import compile_to_odml, provenance, save_odml
from fairephys.metadata.provenance import source_checkout, source_url
from fairephys.validation import bids as bids_validation

# A dataset that fills every part of the descriptor
RICH = {
    "Dataset": {
        "version identifier": "1.0.0", "repository url": "https://gin.example/Org/Test",
        "version innovation": "First release.",
        "usage notes": "Channel 7 is noisy.", "ethics statement": "Approved by the ethics committee.",
        "brain regions": ["primary motor cortex"], "original data format": ["Blackrock NSx (.ns5)", "Video (.MTS)"],
        "Related Publications": [{"identifier": "10.1234/paper", "relation": "IsSupplementTo",
                                  "citation": "Doe, J. A paper. J. Neurosci. 1, 2 (2024). https://doi.org/10.1234/paper"},
                                 {"identifier": "", "relation": "IsSupplementTo",
                                  "citation": "Doe, J. A thesis without identifier (2020)."}],
        "Project": {"name": "P", "dataset statement": "Part of project P.", "version statement": "NWB files.",
                    "usage statement": "Read with PyNWB.",
                    "records statement": "The recordings{?, provided as {original data format},} were converted."},
        "Contributors": [
            {"given name": "Jane", "family name": "Doe", "roles": ["data collection"],
             "contribution description": "Recordings", "digital identifier": {"type": "ORCID", "value": "0000-0001"},
             "affiliation": {"lab": "Test Lab", "institution": "Test Uni", "city": "Testville",
                             "country": "Germany"}},
            {"given name": "Bo", "family name": "Lee", "roles": ["data management", "coordination"],
             "affiliation": {"institution": "Other Uni"}}],
        "Fundings": [{"funder": "Test Foundation", "award number": "GRANT-001", "award title": "Test Grant"},
                     {"funder": "EC", "award number": "1", "acknowledgement": "Curation funded by the EC."}],
    },
    "Subject": {"age category": "adult", "age reference date": "2024-03-15",
                "Surgical Preparation": {"Head Post": {"material": "titanium"}}},
    "Hardware": {"Recording System": {"sampling rate": "30000 hertz", "number of channels": 128,
                                      "acquisition software": "Central", "software version": "7.0"},
                 "Hardware Filters": {"Filters": [
                     {"name": "Highpass", "cutoff": "0.3 hertz", "implementation": "Butterworth"},
                     {"name": "Bandpass", "cutoff": "1-5000 hertz"}, {"name": "", "type": ""}]},
                 "Probes": [{"name": "Utah Array", "manufacturer": "Blackrock", "model": "Utah", "probe type": "utah",
                             "number of contacts": 96, "contact pitch": "400 micrometer",
                             "notes": "Channels 1 to 4 were broken from the first session (lab, 2024-04-01)."},
                            {"name": "Tungsten", "manufacturer": "FHC", "probe type": "single electrode",
                             "number of contacts": 1}],
                 "Auxiliary Devices": [{"name": "Front camera", "device type": "video camera", "manufacturer": "TestCam",
                                        "model": "TC-1", "recording software": "TestCapture", "software version": "2.0"}]},
    "Behavioral Protocol": {"description": "Resting state in the dark.", "task": "none", "head fixation": True,
                            "head fixation method": "headpost", "primate chair": True, "body position": "seated",
                            "eye tracking": True, "eye tracking method": "video", "other tasks performed": True,
                            "other tasks description": "A reaching\ntask."},
    "Data Processing": [{"name": "State Annotation", "technique": "video annotation", "annotation": {"states": [
        {"label": "rest", "code": "RS", "description": "Calm rest"}, {"label": "move", "description": "Movement"}]}}],
    "Sessions": [{"session id": sid, "date": day, "duration": duration, "description": text, "Electrode Placements": [
        {"probe": "Utah Array", "brain region": "primary motor cortex", "hemisphere": "left"}]}
        for sid, day, duration, text in (("01", "2024-03-15", "1458.4 second", "First"),
                                         ("02", "2024-03-18", "53 minute", "Second"))],
}

# Fixed curation software (the installed versions change with every update)
SOFTWARE = [
    {"name": "BIDS validator", "version": "2.2.6", "purpose": "validation", "schema": "BEP032",
     "schema commit": "508cfc7b", "origin": "test"},
    {"name": "NWB Inspector", "version": "0.7.2", "purpose": "validation", "origin": "test"},
    {"name": "fairephys", "version": "0.1.0", "purpose": "conversion", "commit": "abc1234",
     "uncommitted changes": True, "origin": "test"},
    {"name": "NeuroConv", "version": "0.10.2", "purpose": "conversion", "origin": "test"},
]


class _Day(date):
    @classmethod
    def today(cls):
        return cls(2026, 1, 15)


@pytest.fixture
def frozen(monkeypatch):
    """Compile with a fixed build date and fixed curation software versions."""
    monkeypatch.setattr("fairephys.metadata.compiler.date", _Day)
    monkeypatch.setattr("fairephys.metadata.compiler.curation_software", lambda: [dict(t) for t in SOFTWARE])


@pytest.fixture
def curated(tmp_path):
    """A curated-data folder with the files the Data Records section describes."""
    root = tmp_path / "curated-data"
    for ses in ("ses-01", "ses-02"):
        ecephys = root / "sub-MonkeyA" / ses / "ecephys"
        ecephys.mkdir(parents=True)
        for suffix in ("ecephys.nwb", "channels.tsv", "electrodes.tsv", "probes.tsv", "recording-eye_physio.tsv.gz"):
            (ecephys / f"sub-MonkeyA_{ses}_task-rest_{suffix}").touch()
    (root / "sub-MonkeyA" / "ses-01" / "beh").mkdir()
    (root / "sub-MonkeyA" / "ses-01" / "beh" / "sub-MonkeyA_ses-01_task-rest_video.MTS").touch()
    (root / "probes" / "sub-MonkeyA_ses-01").mkdir(parents=True)
    (root / "probes" / "Utah.json").write_text("{}")
    (root / "probes" / "sub-MonkeyA_ses-01" / "Utah.json").write_text("{}")
    (root / "metadata").mkdir()
    (root / "metadata" / "test-ds.instances.yaml").write_text("{}\n")
    for name in ("dataset_description.json", "participants.tsv", "LICENSE", "datacite.yml"):
        (root / name).touch()
    return root


def test_readme_golden(frozen, make_doc, curated, golden):
    """The README of the fixture dataset, exactly."""
    golden("README.md", odml_to_readme(make_doc(RICH), curated))


def test_stale_openminds_collection_is_not_this_runs_output(frozen, make_doc, curated):
    """A JSON-LD older than the odML (--all without a KG token) is listed as not built in this run, not as derived."""
    metadata = curated / "metadata"
    collection = metadata / "test-ds.openminds.jsonld"
    collection.write_text("{}")
    os.utime(collection, (1_700_000_000, 1_700_000_000))  # 2023-11-14
    (metadata / "test-ds.odml.xml").write_text("<odML/>")
    text = odml_to_readme(make_doc(RICH), curated)
    assert "openMINDS collection: not built in this run (no KG token); the file of 2023-11-14 predates this odML" in text
    assert "derived from the odML record" not in text
    os.utime(collection, None)  # rebuilt after the odML: this run's output
    assert "derived from the odML record" in odml_to_readme(make_doc(RICH), curated)


def test_sections_without_data_are_left_out(tmp_path):
    """A dataset with a title alone gives the title and its citation, no empty sections."""
    doc = compile_to_odml({"Dataset": {"full name": "T"}})
    assert odml_to_readme(doc, tmp_path / "missing") == \
        f"# T\n\n## How to Cite\n\nT. DOI to be assigned at publication on GIN ({date.today().year}).\n"


def test_datacite_golden(frozen, make_doc, tmp_path, golden):
    """datacite.yml of the fixture dataset, exactly (written from the saved odML YAML)."""
    yaml_path, _ = save_odml(make_doc(RICH), tmp_path / "odml" / "test-ds")
    (tmp_path / "out").mkdir()
    golden("datacite.yml", write_datacite(yaml_path, tmp_path / "out").read_text())


def test_datacite_references(make_doc, tmp_path, caplog):
    """Each Related Publications entry is a reference with GIN's id prefix of its type and the entry's relation;
    an ISBN (no GIN prefix) is left out with a warning; an identifier of no recognised form raises."""
    def references(*entries):
        doc = make_doc(Dataset={"Related Publications": [
            {"identifier": i, "relation": r, "citation": f"Citation {n}."} for n, (i, r, *_) in enumerate(entries)]})
        record = odml_to_datacite(save_odml(doc, tmp_path / "t")[0])
        assert not validate(record)
        return record["references"]

    entries = [("https://doi.org/10.1234/paper", "IsSupplementTo", "doi:10.1234/paper"),
               ("10.5678/descriptor", "IsDescribedBy", "doi:10.5678/descriptor"),
               ("https://github.com/lab/code", "IsReferencedBy", "url:https://github.com/lab/code"),
               ("arxiv:2201.23456", "IsSupplementTo", "arxiv:2201.23456"),
               ("pmid:12345", "Cites", "pmid:12345"),
               ("hdl:11858/00-1735", "IsDescribedBy", "url:https://hdl.handle.net/11858/00-1735"),
               ("isbn:978-3-161-48410-0", "Cites", None)]
    with caplog.at_level(logging.WARNING):
        assert references(*entries) == [{"id": gin_id, "reftype": relation, "citation": f"Citation {n}."}
                                        for n, (_, relation, gin_id) in enumerate(entries) if gin_id]
    assert any("not in datacite.yml: Citation 6." in r.getMessage() for r in caplog.records)
    with pytest.raises(ValueError, match="no recognised form"):
        references(("Doe 2024", "IsSupplementTo"))


def _sessions_table(text: str) -> list[str]:
    return text.split("| Session |", 1)[1].split("\n\n", 1)[0].splitlines()[2:]


def test_session_durations_in_minutes(make_doc, tmp_path):
    """Metadata durations are shown in minutes (minutes as given), else the Neo duration."""
    durations = ["1458.4 second", "59.5 minute", "53 minute", "15.1 min", "2 hour", ""]
    doc = make_doc(Sessions=[{"session id": f"0{i}", "duration": d} for i, d in enumerate(durations, 1)])
    neo = odml.Section("Neo Metadata", type="neo.recording_metadata")
    neo.append(odml.Property("duration_s", values=[90.0]))
    sessions(doc)[-1].append(neo)
    rows = _sessions_table(odml_to_readme(doc, tmp_path))
    assert [r.split(" | ")[1] for r in rows] == ["24.3 min", "59.5 min", "53 min", "15.1 min", "120.0 min", "1.5 min"]


def test_how_to_cite_generated_unless_given(make_doc):
    """The citation is built from contributors, title, version, the DOI or a placeholder for it and year unless
    given."""
    for dataset, expected in [
        ({}, "Doe, J. Test dataset. DOI to be assigned at publication on GIN (YEAR)."),
        ({"Contributors": [{"given name": "Jean-Pierre", "family name": "Roux"},
                           {"given name": "Bo Ya", "family name": "Lee"}, {"given name": "A", "family name": "Zed"}],
          "version identifier": "1.0.0", "repository url": "https://gin.example/R"},
         "Roux, J.-P., Lee, B. Y. & Zed, A. Test dataset, version 1.0.0. DOI to be assigned at publication on GIN "
         "(YEAR)."),
        ({"repository url": "https://gin.example/R", "doi": "doi:10.1234/x"},
         "Doe, J. Test dataset. https://doi.org/10.1234/x (YEAR)."),
        ({"how to cite": "Cite the paper."}, "Cite the paper."),
    ]:
        doc = make_doc(Dataset=dataset)
        assert doc["Dataset"].properties["how to cite"].values == [expected.replace("YEAR", str(date.today().year))]


def test_licence_text_and_label(make_doc, tmp_path):
    """LICENSE starts with the registry name and URL; the README links the CC label; unknown licences raise."""
    name, url, blank, title, *_ = license_text("CC-BY-4.0").splitlines()
    assert (name, url, blank, title) == ("Creative Commons Attribution 4.0 International",
                                         "https://creativecommons.org/licenses/by/4.0/", "",
                                         "Attribution 4.0 International")
    for spdx, label in (("CC-BY-NC-SA-4.0", "CC BY-NC-SA 4.0"), ("CC0-1.0", "CC0 1.0")):
        assert f"shared under the [{label}](https://" in odml_to_readme(make_doc(Dataset={"license": spdx}), tmp_path)
    with pytest.raises(ValueError, match="Unknown licence 'Some-Licence-1.0'"):
        license_text("Some-Licence-1.0")


def test_author_contributions(make_doc, tmp_path):
    """Each contributor's description followed, after a comma, by the role terms it does not name
    (whole words, any case), or the terms alone, capitalised (the EBRAINS curators' edits, 2026-11-04)."""
    contributions = [
        ({"roles": ["coordination", "data management"],
          "contribution description": "Project coordination and supervision of data curation"},
         "**A B:** Project coordination and supervision of data curation, data management"),
        ({"roles": ["data management"], "contribution description": "Curation (NWB and BIDS)"},
         "**A B:** Curation (NWB and BIDS), data management"),
        ({"roles": ["data management"], "contribution description": "Data Management of the recordings."},
         "**A B:** Data Management of the recordings"),
        ({"roles": ["data collection", "coordination"]}, "**A B:** Data collection, coordination"),
        ({"contribution description": "Data curation."}, "**A B:** Data curation"),
        ({}, "**A B**"),
    ]
    doc = make_doc(Dataset={"Contributors": [{"given name": "A", "family name": "B", **c} for c, _ in contributions]})
    text = odml_to_readme(doc, tmp_path).split("## Author Contributions\n\n", 1)[1]
    assert text.strip().split("\n\n") == [expected for _, expected in contributions]


def test_curation_software_recorded_at_compile(make_doc, monkeypatch, caplog):
    """The compile records tool versions and the validator pin; missing packages drop, unpinned raises."""
    tools = {s.name: {p.name: p.values[0] for p in s.properties} for s in make_doc()["Curation Software"].sections}
    assert [n for n in tools] == ["BIDS validator", "NWB Inspector", "openMINDS Python", "fairephys", "NeuroConv",
                                  "PyNWB", "fairgraph"]
    assert all(tools[name]["version"] == version(dist) for name, dist, _ in provenance.PYTHON_PACKAGES)
    spec = load_registry()["spec"]
    assert spec["validator"].endswith("@" + tools["BIDS validator"]["version"])
    assert tools["BIDS validator"]["schema commit"] in spec["schema_file"]

    monkeypatch.setattr(provenance, "PYTHON_PACKAGES", [("Missing", "no-such-distribution", "conversion")])
    with caplog.at_level(logging.WARNING):
        assert [s.name for s in make_doc()["Curation Software"].sections] == ["BIDS validator"]
    assert any("no-such-distribution" in r.getMessage() for r in caplog.records)
    monkeypatch.setattr(bids_validation, "_spec", lambda: {"validator": "jsr:@bids/validator"})
    with pytest.raises(ValueError, match="not pinned"):
        make_doc()


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@example.org", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def test_pipeline_commit_from_a_git_checkout(tmp_path, monkeypatch):
    """Commit and uncommitted changes under the package dir only; nothing outside a checkout or without git."""
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo, pkg = tmp_path / "repo", tmp_path / "repo" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (repo / "metadata.yaml").write_text("a: 1\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    commit = _git(repo, "rev-parse", "--short", "HEAD")
    assert source_checkout(pkg) == {"commit": commit, "uncommitted changes": False}
    (repo / "metadata.yaml").write_text("a: 2\n")
    assert source_checkout(pkg)["uncommitted changes"] is False
    (pkg / "new.py").write_text("")
    assert source_checkout(pkg)["uncommitted changes"] is True
    (repo / ".venv" / "pkg").mkdir(parents=True)
    (tmp_path / "installed").mkdir()
    assert source_checkout(repo / ".venv" / "pkg") == source_checkout(tmp_path / "installed") == {}

    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")
    monkeypatch.setattr(provenance.subprocess, "run", no_git)
    assert source_checkout(pkg) == {}
    urls = [[], ["Homepage, https://e.org"], ["Homepage, https://e.org", "Repository, https://e.org/r"],
            ["Source Code, https://e.org/s"]]
    assert [source_url(u) for u in urls] == [None, None, "https://e.org/r", "https://e.org/s"]


def test_datacite_files_next_to_the_record(make_doc, tmp_path):
    """LICENSE from the licence id next to datacite.yml; .bidsignore lists both once and keeps its entries,
    the same whether the datacite record or the BIDS layout writes it last."""
    yaml_path, _ = save_odml(make_doc(), tmp_path / "odml" / "t")
    out = tmp_path / "out"
    out.mkdir()
    (out / ".bidsignore").write_text("metadata\n*_video.MTS")
    for _ in range(2):
        write_datacite(yaml_path, out)
    assert (out / "LICENSE").read_text() == license_text("CC-BY-4.0")
    assert (out / ".bidsignore").read_text() == "metadata\ndatacite.yml\nLICENSE\n*_video.MTS\n"
    create_dataset_layout(out)
    assert (out / ".bidsignore").read_text() == "metadata\ndatacite.yml\nLICENSE\n*_video.MTS\n"
    assert yaml.safe_load((out / "datacite.yml").read_text())["authors"] == [
        {"firstname": "Jane", "lastname": "Doe", "affiliation": "Test Lab, Test Uni"}]


def test_header_line_and_tree_root(make_doc, curated):
    """Short title and version under the title, each when filled; the tree root is the repository name, else
    "repository root"; probes/ shows the example session's folder and counts the others; the example session is
    the one with the most file types; the video row names no camera without one; the layout standard in Data Records
    unless the version statement names the BIDS extension."""
    (curated / "probes" / "sub-MonkeyA_ses-02").mkdir()
    text = odml_to_readme(make_doc(RICH), curated)
    assert "        ... (1 more session)\n        Utah.json [" in text
    for name in ("events.tsv", "events.json", "recording-cardiac_physio.json"):  # 8 file types, ses-01 7
        (curated / "sub-MonkeyA" / "ses-02" / "ecephys" / f"sub-MonkeyA_ses-02_task-rest_{name}").write_text("{}")
    assert "    sub-MonkeyA/\n        ses-02/\n" in odml_to_readme(make_doc(RICH), curated)
    assert text.startswith("# Test dataset\n\nShort Title: test-ds | version: 1.0.0\n\n## Authors")
    assert "```\nTest/\n    metadata/" in text
    text = odml_to_readme(make_doc(), curated)
    assert text.startswith("# Test dataset\n\nShort Title: test-ds\n\n## Authors")
    assert "```\nrepository root\n    metadata/" in text
    assert "| MTS video |" in text  # no camera in the metadata
    assert "The data are organised following the BIDS extension proposal" in text  # no version statement gives it
    named = make_doc(Dataset={"Project": {"version statement": "Organised following {bids extension}."}})
    assert odml_to_readme(named, curated).count("BEP032 Microelectrode Electrophysiology, draft") == 1


def test_file_descriptions_from_the_files_on_disk(make_doc, curated):
    """A TSV with a header row lists its columns in the file table; the tree gives the one-sentence description
    in square brackets after every file; the states caption names the events files when there are any."""
    channels = curated / "sub-MonkeyA" / "ses-01" / "ecephys" / "sub-MonkeyA_ses-01_task-rest_channels.tsv"
    row = "| `ecephys/*_channels.tsv` | TSV | One row per recorded channel"
    caption = "the label (the trial_type value in the events files)"
    text = odml_to_readme(make_doc(RICH), curated)
    assert f"{row} | BIDS 1.11.2, BEP032 draft, written with fairephys 0.1.0 |" in text and caption not in text
    channels.write_text("name\ttype\tunits\nCh1\tEXT\tuV\n")
    (channels.parent / "sub-MonkeyA_ses-01_task-rest_events.tsv").write_text("onset\tduration\ttrial_type\n")
    (curated / "probes" / "sub-MonkeyA_ses-01" / "probe_group.json").write_text("{}")
    text = odml_to_readme(make_doc(RICH), curated)
    assert f"{row}, with the columns `name`, `type` and `units` |" in text
    assert "| `*_events.tsv` | TSV | One row per event or annotated interval, with the columns `onset`, `duration` " \
           "and `trial_type` | BIDS 1.11.2, written with fairephys 0.1.0 |" in text
    assert f"The states this step annotates, one row each: the code in the raw files, {caption} and a " \
           "description." in text
    tree = text.split("```", 2)[1]
    assert "sub-MonkeyA_ses-01_task-rest_channels.tsv [the recorded channels, one row each]\n" in tree
    session_probes = "[contact geometry of the devices placed in the session, of one device or all (probe_group.json)]"
    assert f"Utah.json {session_probes}\n" in tree and f"probe_group.json {session_probes}\n" in tree


def test_columns_all_files_have_then_those_some_have(make_doc, curated):
    """The columns of a file type are those all its files have, then those only some of them have."""
    ecephys = curated / "sub-MonkeyA" / "ses-01" / "ecephys"
    (ecephys / "sub-MonkeyA_ses-01_task-rest_run-01_events.tsv").write_text("onset\tduration\ttrial_type\n")
    (ecephys / "sub-MonkeyA_ses-01_task-rest_run-02_events.tsv").write_text(
        "onset\tduration\tsuccess\ttrial_type\treward_size\n")
    assert "| `*_events.tsv` | TSV | One row per event or annotated interval, with the columns `onset`, `duration` " \
           "and `trial_type`; some files also: `success` and `reward_size` |" in odml_to_readme(make_doc(RICH), curated)


def test_channels_files_described_per_datatype(make_doc, curated):
    """The ecephys and EMG channels.tsv are file types of their own, each with its own columns."""
    ses = curated / "sub-MonkeyA" / "ses-01"
    (ses / "ecephys" / "sub-MonkeyA_ses-01_task-rest_channels.tsv").write_text("name\telectrode_name\ttype\n")
    (ses / "emg").mkdir()
    (ses / "emg" / "sub-MonkeyA_ses-01_task-rest_channels.tsv").write_text("name\ttype\tstatus\n")
    text = odml_to_readme(make_doc(RICH), curated)
    assert "| `ecephys/*_channels.tsv` | TSV | One row per recorded channel, with the columns `name`, " \
           "`electrode_name` and `type` |" in text
    assert "| `emg/*_channels.tsv` | TSV | One row per EMG channel, with the columns `name`, `type` and `status` |" in text
    assert "sub-MonkeyA_ses-01_task-rest_channels.tsv [the EMG channels, one row each]\n" in text.split("```", 2)[1]



def test_json_sidecars_list_their_keys(make_doc, curated):
    """A JSON sidecar lists the keys its files hold (files with no entries counted), one that is empty in every
    file says it holds none; images list their BIDS suffixes."""
    for ses, electrodes in (("ses-01", '{"name": {}, "x": {}}'), ("ses-02", "{}")):
        ecephys = curated / "sub-MonkeyA" / ses / "ecephys"
        (ecephys / f"sub-MonkeyA_{ses}_electrodes.json").write_text(electrodes)
        (ecephys / f"sub-MonkeyA_{ses}_channels.json").write_text("{}")
    anat = curated / "sub-MonkeyA" / "anat"
    anat.mkdir()
    for suffix in ("T1w", "T2w"):
        (anat / f"sub-MonkeyA_{suffix}.nii.gz").touch()
    text = odml_to_readme(make_doc(RICH), curated)
    assert "| `*_electrodes.json` | JSON | Sidecar of the matching electrodes.tsv, with the keys `name` and `x`; " \
           "no entries in 1 of 2 files |" in text
    assert "| `ecephys/*_channels.json` | JSON | Sidecar of the matching channels.tsv, with no entries (an empty " \
           "JSON object) |" in text
    assert "| `anat/*.nii.gz` | NIfTI (gzip) | Anatomical MR images of the subject (T1w and T2w) |" in text


def test_channels_files_outside_the_datatype_folders(make_doc, curated):
    """A channels.tsv in another datatype folder takes the catch-all entry, with its row and tree description."""
    ieeg = curated / "sub-MonkeyA" / "ses-01" / "ieeg"
    ieeg.mkdir()
    (ieeg / "sub-MonkeyA_ses-01_task-rest_channels.tsv").write_text("name\ttype\tunits\n")
    text = odml_to_readme(make_doc(RICH), curated)
    assert "| `*_channels.tsv` | TSV | One row per channel, with the columns `name`, `type` and `units` |" in text
    assert "    ieeg/\n                sub-MonkeyA_ses-01_task-rest_channels.tsv [the channels, one row each]\n" in text

def test_first_mentions_link_and_write_out_once(frozen, make_doc, tmp_path):
    """The first plain mention of a registry software name is linked once (an existing link is kept), and the
    first odML written out unless the written-out form comes first."""
    project = {"usage statement": "Use [PyNWB](https://example.org/pynwb), then PyNWB, NeuroConv and NeuroConv.",
               "records statement": "Compiled into one odML record; the odML record is the source."}
    text = odml_to_readme(make_doc(Dataset={"Project": project}), tmp_path)
    assert "Use [PyNWB](https://example.org/pynwb), then PyNWB, [NeuroConv](https://neuroconv.readthedocs.io/) " \
           "and NeuroConv." in text
    assert "into one open metadata Markup Language (odML) record; the odML record" in text
    project["records statement"] = "A single open metadata Markup Language (odML) record; odML again."
    assert project["records statement"] in odml_to_readme(make_doc(Dataset={"Project": project}), tmp_path)
