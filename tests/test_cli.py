"""The `fairephys` command, the `init` scaffold and the shared curate.py CLI layer."""

import argparse
import logging
import py_compile
import runpy
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from fairephys import curation, init
from fairephys.__main__ import main as cli


@pytest.mark.parametrize("fmt", init.FORMATS)
def test_init_scaffold(tmp_path, fmt, monkeypatch, capsys):
    """Each format with a converter scaffolds a curate.py that compiles, names its format and parses its CLI."""
    dataset_dir = init.init_dataset("org-pi", fmt, multi_subject=fmt == "tdt", acute=fmt != "blackrock",
                                    output_dir=tmp_path)
    curate = dataset_dir / "curate.py"
    py_compile.compile(str(curate), doraise=True)
    text = curate.read_text()
    assert "$" not in text and f'RECORDING_FORMAT = "{fmt}"' in text
    assert (dataset_dir / "metadata" / "curated" / "recordings.yaml").exists() is (fmt != "blackrock")
    subjects = yaml.safe_load((dataset_dir / "metadata" / "curated" / "subject.yaml").read_text())
    assert ("Subjects" in subjects) is (fmt == "tdt")
    monkeypatch.setattr(sys, "argv", [str(curate), "--help"])
    with pytest.raises(SystemExit) as done:
        runpy.run_path(str(curate), run_name="__main__")
    assert done.value.code == 0 and "--shared-metadata" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", [str(curate), "--readme"])
    with patch.object(curation, "run_export_steps") as export:
        runpy.run_path(str(curate), run_name="__main__")  # export only: the scaffold's discover_sessions would raise
    export.assert_called_once()


def test_fairephys_command(tmp_path, capsys):
    """init, validate and compile through the command; bad or existing targets are refused."""
    assert cli(["init", "x", "--format", "tdt-sev", "--output-dir", str(tmp_path)]) == 0
    assert "TdtSevConverter" in capsys.readouterr().out
    assert cli(["init", "x", "--output-dir", str(tmp_path)]) == 1
    templates = tmp_path / "x" / "metadata" / "curated"
    assert cli(["validate", str(templates)]) == 1  # the empty templates do not validate until filled
    assert cli(["--help"]) == 0 and cli([]) == 2
    with pytest.raises(ValueError, match="unknown raw format"):
        init.converter_args("nope")
    with pytest.raises(ValueError, match="no converter for the raw format 'intan'"):
        init.init_dataset("y", "intan", False, False, tmp_path)
    assert init.converter_args("spikeglx") == 'source_data={"Recording": {"folder_path": str(raw_path)}},'

    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "dataset.yaml").write_text("Dataset:\n  full name: T\n")
    assert cli(["compile", str(tmp_path / "t"), "-o", str(tmp_path / "out" / "t"), "--validate", "skip"]) == 0
    assert (tmp_path / "out" / "t.odml.xml").is_file()


def test_curate_parser():
    """The curate.py parser: flags by capability, dataset flags, defaults, --verbose debug logging."""
    build = curation.build_parser("T")
    assert {"build_probes", "bids_layout", "convert", "sanity"} <= set(vars(build.parse_args([])))
    minimal = vars(curation.build_parser("T", has_probes=False, has_convert=False, has_sanity=False).parse_args([]))
    assert not {"build_probes", "bids_layout", "convert", "sanity"} & set(minimal)
    assert {"compile", "validate"} <= set(minimal)
    build.add_argument("--subject", default="A")
    args = build.parse_args(["--subject", "Z", "--compile"])
    assert (args.subject, args.compile, args.kg_space, args.session, args.stub_test) == ("Z", True, None, None, True)
    assert build.parse_args(["--full"]).stub_test is False
    try:
        assert build.parse_args(["--verbose"]).verbose and logging.getLogger("fairephys").level == logging.DEBUG
    finally:
        logging.getLogger("fairephys").setLevel(logging.INFO)


def test_export_steps_dispatch(tmp_path, capsys):
    """run_export_steps runs the asked steps with the dataset's paths; a failed validation exits 1; under --all
    the openMINDS collection is built only with a KG token, else one line says so and the run goes on."""
    paths = tmp_path / "o.odml.xml", tmp_path / "cur", tmp_path / "val"
    args = argparse.Namespace(datacite=True, readme=True, validate=None, kg_space="s")
    with patch.object(curation, "datacite_cmd") as datacite, patch.object(curation, "readme_cmd") as readme:
        curation.run_export_steps(args, *paths)
    datacite.assert_called_once_with(paths[0], paths[1])
    readme.assert_called_once_with(paths[0], paths[1])
    every = argparse.Namespace(all=True, openminds_collection=True, datacite=True, kg_space=None)
    with patch.object(curation, "read_token", return_value=None), \
            patch.object(curation, "openminds_collection_cmd") as collection, \
            patch.object(curation, "datacite_cmd") as datacite:
        curation.run_export_steps(every, *paths)
    collection.assert_not_called()
    datacite.assert_called_once()
    assert capsys.readouterr().out.strip() == curation.NO_TOKEN_LINE
    with patch.object(curation, "read_token", return_value="t"), \
            patch.object(curation, "openminds_collection_cmd") as collection, patch.object(curation, "datacite_cmd"):
        curation.run_export_steps(every, *paths)
    collection.assert_called_once_with(paths[0], target_space=None, validation_dir=paths[2])
    alone = argparse.Namespace(openminds_collection=True)
    with patch.object(curation, "read_token", return_value=None), pytest.raises(ValueError, match="No KG token"):
        curation.run_export_steps(alone, *paths)
    with patch.object(curation, "validate_cmd", return_value=1), pytest.raises(SystemExit):
        curation.run_export_steps(argparse.Namespace(validate="all"), *paths)
    curation.run_export_steps(argparse.Namespace(), *paths)  # no flags: nothing runs


def test_openminds_collection_command_lists_kg_differences(tmp_path):
    """The collection validation file lists the KG nodes referenced as they are that differ from the odML."""
    difference = "Funding f-1 (space 'common'): award_title differs"
    collection = SimpleNamespace(validate=lambda: {}, kg_differences=[difference])
    with patch.object(curation, "get_kg_client", return_value=None), \
            patch.object(curation, "build_collection", return_value=collection), \
            patch.object(curation, "save_collection"):
        curation.openminds_collection_cmd(tmp_path / "d.odml.xml", validation_dir=tmp_path)
    text = (tmp_path / curation.OPENMINDS_COLLECTION_VALIDATION_FILENAME).read_text()
    assert text.splitlines()[2:] == ["KG nodes referenced as they are that differ from the odML: 1",
                                     f"  - {difference}"]


def test_finalize_compilation(make_doc, tmp_path, capsys):
    """The odML is saved with its probes' geometry from ProbeInterface, openMINDS instances and warnings; a second
    run changes no probe value; without enrichment stale warnings go; contacts without a geometry are an error;
    existing session probe groups are named as rebuilt by --build-probes."""
    probe = {"name": "Utah Array", "number of contacts": 96, "contact pitch": "400 micrometer",
             "contact arrangement": "matrix", "number of columns": 10, "number of rows": 10, "total span": ""}
    doc = make_doc(Hardware={"Probes": [probe]})
    xml = curation.finalize_compilation(doc, tmp_path / "metadata", "ds", validation_dir=tmp_path / "val")
    assert xml == tmp_path / "metadata" / "ds.odml.xml" and (tmp_path / "metadata" / "ds.instances.yaml").is_file()
    utah = doc["Hardware"]["Probes"].sections[0]
    enriched = [(p.name, p.values, p.value_origin) for p in utah.properties]
    assert ("total span", ["3600 um"], "probeinterface") in enriched
    assert utah.properties["contact_x"].values[0].startswith("0.0,0.0,0.0,")
    warnings = tmp_path / "val" / curation.OPENMINDS_WARNINGS_FILENAME
    assert warnings.is_file()
    assert "probe groups exist" not in capsys.readouterr().out
    (tmp_path / "probes" / "sub-A_ses-01").mkdir(parents=True)
    (tmp_path / "probes" / "sub-A_ses-01" / "probe_group.json").write_text("{}")
    curation.finalize_compilation(doc, tmp_path / "metadata", "ds", enrich_openminds=False,
                                  validation_dir=tmp_path / "val")
    assert not warnings.exists()
    assert [(p.name, p.values, p.value_origin) for p in utah.properties] == enriched
    assert f"1 session probe groups exist under {tmp_path / 'probes'}: --build-probes" in capsys.readouterr().out
    with pytest.raises(ValueError, match="Contact positions must be unique"):
        curation.finalize_compilation(make_doc(), tmp_path / "metadata", "ds", enrich_openminds=False)


def test_run_pipeline(tmp_path, capsys):
    """run_pipeline: the set steps per session on the odML loaded once, --all with a dataset step, one compile for
    --compile --openminds-enrich, --odml, --list-sessions; export-only runs never call sessions."""
    calls = []
    sessions = lambda args: calls.append("sessions") or ["s1", "s2"]
    compile = lambda enrich_openminds, shared_dir: calls.append(("compile", enrich_openminds)) or tmp_path / "c.xml"
    flags = ("build_probes", "bids_layout", "convert", "sanity", "video")
    steps = {flag: lambda doc, session, args, flag=flag: calls.append((flag, doc, session)) for flag in flags}
    parser = curation.build_parser("T")
    parser.add_argument("--video", action="store_true")

    def run(*argv, parser=parser):
        calls.clear()
        with patch.object(curation, "load_doc", side_effect=lambda path: path.name), \
                patch.object(curation, "run_export_steps") as export:
            curation.run_pipeline(parser.parse_args(argv), sessions=sessions, compile=compile, steps=steps,
                                  odml_path=tmp_path / "d.xml", curated_dir=tmp_path / "cur", validation_dir=tmp_path)
        return export.call_args.args if export.called else None

    args, odml_path, *dirs = run("--readme")
    assert calls == [] and args.readme and odml_path == tmp_path / "d.xml" and dirs == [tmp_path / "cur", tmp_path]
    assert run("--readme", parser=curation.build_parser("T", has_probes=False)) and calls == []
    assert run("--compile", "--openminds-enrich", "--no-openminds")[1] == tmp_path / "c.xml"
    assert calls == [("compile", True)]
    capsys.readouterr()
    run("--sanity", "--bids-layout", "--odml", str(tmp_path / "o.xml"))
    assert calls == ["sessions", ("bids_layout", "o.xml", "s1"), ("sanity", "o.xml", "s1"),
                     ("bids_layout", "o.xml", "s2"), ("sanity", "o.xml", "s2")]
    assert capsys.readouterr().out == "--- Session: s1 ---\n--- Session: s2 ---\n"
    args = run("--all")[0]
    assert calls == [("compile", True), "sessions", *[(f, "c.xml", s) for s in ("s1", "s2") for f in flags]]
    assert (args.openminds_collection, args.datacite, args.readme, args.validate, args.kg_push) == (
        True, True, True, "all", False)
    capsys.readouterr()
    assert run("--list-sessions") is None and calls == ["sessions"] and capsys.readouterr().out == "  s1\n  s2\n"
