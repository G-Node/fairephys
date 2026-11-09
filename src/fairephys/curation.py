"""Shared curation pipeline infrastructure.

Dataset-agnostic helpers and pipeline commands used by all curate.py scripts.
Dataset-specific logic (session discovery, channel mapping, converter setup,
raw file paths) stays in each dataset's curate.py.

The CLI layer (build_parser, run_pipeline, run_export_steps and the *_cmd
functions) prints its user-facing lines: run_pipeline one line per session
before its steps. The library functions the steps call (such as
probes.write_session_probe_group and the bids writers) log (configure_logging).
"""

import argparse
import json
import logging
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

import odml
import yaml

from . import bids, enrichment, metadata, probes
from .export import (
    build_collection, save_collection, get_kg_client, push_collection, read_token,
    write_datacite, write_readme,
)
from .odml import prop, section_at
from .validation import run_validation
from .validation.sanity import run_sanity_checks  # noqa: F401  (the dataset scripts import it from here)

logger = logging.getLogger(__name__)

OPENMINDS_WARNINGS_FILENAME = "_openminds_warnings.json"
OPENMINDS_COLLECTION_VALIDATION_FILENAME = "_openminds_collection_validation.txt"
# --all without a KG token leaves the openMINDS collection out and says so
NO_TOKEN_LINE = "openMINDS collection not built: no KG token (run --openminds-collection with EBRAINS_KG_TOKEN)"
LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"

# ---------------------------------------------------------------------------
# odML helpers
# ---------------------------------------------------------------------------


def load_doc(odml_path: Path) -> odml.Document:
    return odml.load(str(odml_path))


def get_task_label(doc: odml.Document) -> str:
    """Return the BIDS task entity label from Behavioral Protocol.bids task label.

    Raises ValueError if missing: silent fallbacks mask curation gaps.
    """
    try:
        proto = doc["Behavioral Protocol"]
        prop = next((p for p in proto.properties if p.name == "bids task label"), None)
        if prop and prop.values and prop.values[0]:
            return prop.values[0]
    except KeyError:
        pass
    raise ValueError(
        "Behavioral Protocol.bids task label is not set. "
        "Fill it in protocol.yaml before running the pipeline."
    )


def get_subject_id(doc: odml.Document, session: odml.Section = None) -> str:
    """Subject id of a session (of the single Subject when no session is given)."""
    subject = metadata.session_subject(doc, session) if session is not None else doc["Subject"]
    return str(subject.properties["subject id"].values[0]).replace(" ", "")


# Session entry by session id, subject and task (metadata.sessions)
find_session = metadata.find_session


def odml_yaml_path(odml_path: Path) -> Path:
    return odml_path.with_suffix(".yaml") if odml_path.suffix == ".xml" else odml_path


def session_paths(doc: odml.Document, session: odml.Section, curated_dir: Path, *,
                  acq: str = None, task: str = None, run: int = None) -> dict[str, Path | str | int | None]:
    """The standard output paths of a session entry, or of one run or probe
    (acq) of it, under curated_dir: session-level files sub-<sub>_ses-<ses>
    (channels.tsv with the acq when given), run-level files (ecephys.json,
    NWB) sub-<sub>_ses-<ses>_task-<task>[_acq-<acq>][_run-<nn>]. The subject
    is the session's; the task the one given (runs of one entry with
    different tasks), else the entry's own when it names one, else the
    Behavioral Protocol's bids task label. Every key is always present, with
    the task, acq and run that named the files."""
    sub = get_subject_id(doc, session)
    ses = str(prop(session, "session id") or "")
    if not ses:
        raise ValueError(f"Session {session.name}: no session id")
    task = task or prop(session, "task") or get_task_label(doc)
    ses_dir = bids.session_path(curated_dir, sub, ses, "ecephys")
    prefix = bids.bids_filename(sub, "", "", ses=ses).rstrip("_.")
    run_prefix = bids.bids_filename(sub, "", "", ses=ses, task=task, acq=acq, run=run).rstrip("_.")
    channels_prefix = bids.bids_filename(sub, "", "", ses=ses, acq=acq).rstrip("_.")
    return {
        "ses_dir": ses_dir,
        "prefix": prefix,
        "run_prefix": run_prefix,
        "probes_dir": f"probes/{prefix}",
        "probe_group": Path(curated_dir) / "probes" / prefix / "probe_group.json",
        "probes_tsv": ses_dir / f"{prefix}_probes.tsv",
        "probes_json": ses_dir / f"{prefix}_probes.json",
        "electrodes_tsv": ses_dir / f"{prefix}_electrodes.tsv",
        "electrodes_json": ses_dir / f"{prefix}_electrodes.json",
        "channels_json": ses_dir / f"{prefix}_channels.json",
        "channels_tsv": ses_dir / f"{channels_prefix}_channels.tsv",
        "ecephys_json": ses_dir / f"{run_prefix}_ecephys.json",
        "nwb": ses_dir / f"{run_prefix}_ecephys.nwb",
        "task": task,
        "acq": acq,
        "run": run,
    }


# ---------------------------------------------------------------------------
# Compile helpers
# ---------------------------------------------------------------------------


def compile_metadata(templates_dir: Path, shared_dir: Path | None = None,
                     strict: bool = True) -> tuple[dict, odml.Document]:
    """Load YAML templates and compile to odML document.

    A schema error stops the pipeline (strict); with strict False it is
    logged. shared_dir is the collection-level metadata merged underneath
    the templates (default: the nearest collection/metadata above them,
    see metadata.find_shared_dir).

    Returns (meta_dict, odml_document). The caller performs dataset-specific
    Neo enrichment, then calls finalize_compilation().
    """
    meta = metadata.load_metadata(templates_dir, validate_mode="strict" if strict else "warn",
                                  shared_dir=shared_dir)
    doc = metadata.compile_to_odml(meta, omit_empty=False)
    return meta, doc


def _write_instances(instances: dict, path: Path):
    """Write resolved openMINDS instances to YAML."""
    data = {
        "_generated": datetime.now().isoformat(),
        "_openminds_version": version("openminds"),
    }
    data.update(instances)
    path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True))


def finalize_compilation(doc: odml.Document, output_dir: Path,
                         dataset_name: str,
                         enrich_openminds: bool = True,
                         validation_dir: Path = None,
                         probes_dir: Path = None) -> Path:
    """Probe geometry, openMINDS enrichment and save of the odML document.

    The contact positions and geometry of every Hardware/Probes entry come
    from its ProbeInterface probe (probes.enrich_odml_positions; nothing to
    do without entries, a geometry that does not fit is an error). The saved
    odML replaces the previous one whole: when session probe groups exist
    under probes_dir, one line says that --build-probes (and the steps after
    it) rebuild them from this odML, as whatever a dataset's --build-probes
    writes into the odML is not in it until then.

    Args:
        doc: Enriched odML document (after Neo enrichment).
        output_dir: Directory for odML output (the curated metadata dir).
        dataset_name: Base filename without extension (e.g. "my-dataset").
        enrich_openminds: Whether to run openMINDS enrichment.
        validation_dir: Directory for QA artifacts (warnings JSON).
            Falls back to output_dir if not given.
        probes_dir: The folder of the session probe groups
            (<session>/probe_group.json); the curated data's "probes" beside
            output_dir when not given.

    Returns:
        Path to saved XML file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    warn_dir = validation_dir or output_dir
    warn_dir.mkdir(parents=True, exist_ok=True)
    warnings_path = warn_dir / OPENMINDS_WARNINGS_FILENAME

    if (entries := section_at(doc, "Hardware/Probes")) is not None and entries.sections:
        probes.enrich_odml_positions(doc, probes.build_probe_group(list(entries.sections)))
    if enrich_openminds:
        warnings, instances = enrichment.openminds_enrich_document(doc)
        for w in warnings:
            logger.warning("openMINDS: %s", w)
        warnings_path.write_text(json.dumps(warnings, indent=2) + "\n")
        _write_instances(instances, output_dir / f"{dataset_name}.instances.yaml")
        print("  openMINDS enriched")
    elif warnings_path.exists():  # stale warnings of a previous enrichment
        warnings_path.unlink()
    _, xml_path = metadata.save_odml(doc, output_dir / dataset_name)
    print(f"Saved: {xml_path}")
    probes_dir = probes_dir or output_dir.parent / "probes"
    if groups := sorted(probes_dir.glob("*/probe_group.json")):
        print(f"{len(groups)} session probe groups exist under {probes_dir}: --build-probes (and the steps after it) "
              "rebuild them from this odML, which holds nothing --build-probes adds until then")
    return xml_path


# ---------------------------------------------------------------------------
# Shared CLI parser and logging
# ---------------------------------------------------------------------------


def configure_logging(verbose: bool = False) -> None:
    """Log to stderr (once per process): the fairephys logger at INFO, or at
    DEBUG with verbose; other libraries at WARNING."""
    logging.basicConfig(level=logging.WARNING, format=LOG_FORMAT)
    logging.getLogger("fairephys").setLevel(logging.DEBUG if verbose else logging.INFO)


class _VerboseAction(argparse.Action):
    """--verbose: DEBUG logging from the moment the flag is parsed."""

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, True)
        configure_logging(verbose=True)


def build_parser(description: str, *,
                 has_convert: bool = True,
                 has_sanity: bool = True,
                 has_probes: bool = True) -> argparse.ArgumentParser:
    """Build a standard argparse parser for curation pipelines, configuring
    logging as the CLI entry (see configure_logging).

    Each curate.py calls this then adds dataset-specific flags
    (e.g. --subject, --video, --task, --run).
    """
    configure_logging()
    p = argparse.ArgumentParser(description=description)

    # Pipeline mode
    p.add_argument("--all", action="store_true",
                   help="Run the full pipeline: every step and export but kg-push; the openMINDS collection "
                        "only with a KG token (EBRAINS_KG_TOKEN or ~/.config/fairephys/kg_token), else one "
                        "line says it was not built and the run goes on")
    p.add_argument("--list-sessions", action="store_true", help="List available sessions")

    # Core steps
    p.add_argument("--compile", action="store_true", help="Compile YAML to odML")
    if has_probes:
        p.add_argument("--build-probes", action="store_true", help="Build ProbeInterface from odML")
        p.add_argument("--bids-layout", action="store_true", help="Create BIDS sidecars")
    if has_convert:
        p.add_argument("--convert", action="store_true", help="Convert to NWB")
    if has_sanity:
        p.add_argument("--sanity", action="store_true", help="Run per-session sanity checks")

    # Export steps
    p.add_argument("--openminds-collection", action="store_true", help="Build openMINDS Collection")
    p.add_argument("--kg-push", action="store_true", help="Push collection to EBRAINS Knowledge Graph")
    p.add_argument("--kg-space", default=None,
                   help="KG space name (default: the one named in the collection metadata)")
    p.add_argument("--datacite", action="store_true", help="Generate DataCite metadata")
    p.add_argument("--readme", action="store_true", help="Generate README")
    p.add_argument("--validate", nargs="?", const="all", default=None,
                   choices=["all", "metadata", "nwb"],
                   help="Run dataset-wide validation. Optional scope: "
                        "'metadata' (BIDS sidecars + openMINDS, fast), "
                        "'nwb' (NWB inspection only, slow), "
                        "default 'all'")

    # Common options
    p.add_argument("--session", default=None,
                   help="Single session ID to process (default: every session)")
    p.add_argument("--odml", type=Path, help="Override odML path")
    p.add_argument("--report", type=Path, help="Save validation report to file")
    p.add_argument("--shared-metadata", type=Path, default=None,
                   help="Collection-level metadata directory merged underneath the templates "
                        "(default: the nearest collection/metadata above them)")
    p.add_argument("--no-openminds", action="store_true",
                   help="Skip openMINDS enrichment during compile")
    p.add_argument("--openminds-enrich", action="store_true",
                   help="(Re)compile with openMINDS enrichment")
    # Short stub NWB files by default, so that metadata and layout can be
    # regenerated quickly; the complete recordings only with --full (final run)
    p.add_argument("--stub-test", dest="stub_test", action="store_true", default=True,
                   help="Short NWB test files (default)")
    p.add_argument("--full", dest="stub_test", action="store_false",
                   help="Convert the complete recordings (final run)")
    p.add_argument("--verbose", action=_VerboseAction, nargs=0, default=False,
                   help="Debug logging")
    return p


def run_export_steps(args, odml_path: Path, curated_data_dir: Path, validation_dir: Path):
    """Run the export steps the CLI flags ask for: openminds-collection,
    kg-push, datacite, readme, validate (a failed validation exits 1).
    Under --all the openMINDS collection is built only with a KG token;
    without one NO_TOKEN_LINE is printed and the other steps run (a stated
    exclusion; --openminds-collection asked for itself still raises).

    Called from the individual-steps block of each curate.py main().
    """
    kg_space = getattr(args, "kg_space", None)
    if getattr(args, "openminds_collection", False):
        if getattr(args, "all", False) and read_token() is None:
            print(NO_TOKEN_LINE)
        else:
            openminds_collection_cmd(odml_path, target_space=kg_space, validation_dir=validation_dir)
    if getattr(args, "kg_push", False):
        kg_push_cmd(odml_path, kg_space)
    if getattr(args, "datacite", False):
        datacite_cmd(odml_path, curated_data_dir)
    if getattr(args, "readme", False):
        readme_cmd(odml_path, curated_data_dir)
    if scope := getattr(args, "validate", None):
        if validate_cmd(curated_data_dir, validation_dir, getattr(args, "report", None), scope=scope) != 0:
            raise SystemExit(1)


def run_pipeline(args, *, sessions, compile, steps: dict, odml_path: Path, curated_dir: Path,
                 validation_dir: Path) -> None:
    """The main of a curate.py after parse_args().

    --list-sessions prints the sessions(args) and returns. --all sets
    --compile, every flag of `steps`, the openMINDS collection, DataCite,
    README and validation (never --kg-push). One compile(enrich_openminds,
    shared_dir) when --compile or --openminds-enrich is given. When a flag of
    `steps` is set (keys are argparse destinations): the odML is loaded once,
    and for each session of sessions(args) one line is printed and each set
    step runs as step(doc, session, args); sessions is not called otherwise,
    so an export-only run discovers nothing. Then run_export_steps.
    """
    if args.list_sessions:
        for session in sessions(args):
            print(f"  {session}")
        return
    if args.all:  # every step and export but kg-push; the openMINDS collection only with a KG token
                  # (run_export_steps prints one line and goes on without one)
        args.compile = args.openminds_collection = args.datacite = args.readme = True
        for flag in steps:
            setattr(args, flag, True)
        args.validate = args.validate or "all"
    odml_path = args.odml or odml_path
    if args.compile or args.openminds_enrich:
        odml_path = compile(enrich_openminds=args.openminds_enrich or not args.no_openminds,
                            shared_dir=args.shared_metadata)
    if any(getattr(args, flag, False) for flag in steps):
        doc = load_doc(odml_path)
        for session in sessions(args):
            print(f"--- Session: {session} ---")
            for flag, step in steps.items():
                if getattr(args, flag, False):
                    step(doc, session, args)
    run_export_steps(args, odml_path, curated_dir, validation_dir)


# ---------------------------------------------------------------------------
# Pipeline commands
# ---------------------------------------------------------------------------


def openminds_collection_cmd(odml_path: Path, target_space: str = None,
                             validation_dir: Path = None):
    """Build openMINDS Collection from odML."""
    kg_client = get_kg_client()
    collection = build_collection(odml_path, kg_client, target_space=target_space)

    failures = collection.validate()
    lines = []
    if failures:
        lines.append(f"Validation warnings: {len(failures)} instances with issues")
        for instance_id, issues in failures.items():
            lines.append(f"\n  {instance_id}:")
            for key in ("type", "required"):
                for msg in issues.get(key, []):
                    lines.append(f"    - {'Type' if key == 'type' else 'Missing'}: {msg}")
    else:
        lines.append("Collection validation: no issues")
    # KG nodes referenced as they are keep their own values: edit them in the
    # KG editor or align the metadata
    if differences := getattr(collection, "kg_differences", []):
        lines.append(f"\nKG nodes referenced as they are that differ from the odML: {len(differences)}")
        lines += [f"  - {d}" for d in differences]
    if warnings := getattr(collection, "warnings", []):
        lines.append(f"\nodML values not in the collection: {len(warnings)}")
        lines += [f"  - {w}" for w in warnings]
    output = "\n".join(lines)
    print(output)

    if validation_dir is not None:
        validation_dir.mkdir(parents=True, exist_ok=True)
        output_path = validation_dir / OPENMINDS_COLLECTION_VALIDATION_FILENAME
        output_path.write_text(output + "\n")
        print(f"Validation output saved: {output_path}")

    output_path = (odml_path.parent
                   / f"{odml_path.stem.replace('.odml', '')}.openminds.jsonld")
    save_collection(collection, output_path)
    print(f"Saved: {output_path}")


def kg_push_cmd(odml_path: Path, space: str = None):
    """Push openMINDS collection to EBRAINS Knowledge Graph (the space given,
    else the one the collection metadata names)."""
    kg_client = get_kg_client()
    collection = build_collection(odml_path, kg_client, target_space=space)
    result = push_collection(collection, kg_client, space)
    print(f"Pushed to KG{f' space {space!r}' if space else ''}: {result}")


def datacite_cmd(odml_path: Path, curated_data_dir: Path):
    """Generate DataCite metadata for GIN DOI."""
    output = write_datacite(odml_yaml_path(odml_path), curated_data_dir)
    print(f"Saved: {output}")


def readme_cmd(odml_path: Path, curated_data_dir: Path):
    """Generate README from odML metadata."""
    doc = load_doc(odml_path)
    output = write_readme(doc, curated_data_dir)
    print(f"Saved: {output}")


def validate_cmd(curated_data_dir: Path, validation_dir: Path,
                 report_path: Path = None, scope: str = "all") -> int:
    """Run dataset-wide validation.

    scope: "all" (BIDS + NWB + openMINDS), "metadata" (BIDS sidecars +
    persisted openMINDS warnings, no NWB inspection), or "nwb" (NWB
    inspection only). Scoped runs save to their own report file so a
    fast metadata run never overwrites the full dataset report.
    """
    report = run_validation(session_id=f"dataset ({scope})", bids_dir=curated_data_dir,
                            validation_dir=validation_dir, scope=scope)
    print(report.to_markdown())

    validation_dir.mkdir(exist_ok=True)
    default_name = "dataset.md" if scope == "all" else f"dataset-{scope}.md"
    out_path = report_path or validation_dir / default_name
    out_path.write_text(report.to_markdown())
    print(f"\nReport saved: {out_path}")

    return 0 if report.passed else 1
