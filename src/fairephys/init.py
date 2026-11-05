"""Dataset scaffolding for fairephys curation pipelines.

Generates a ready-to-fill dataset directory with curate.py, YAML templates,
and standard directory structure.

Usage:
    fairephys init my-dataset --format blackrock
    fairephys init my-dataset --format tdt --multi-subject --acute
"""

import argparse
import re
from pathlib import Path
from string import Template

from .formats import RAW_FORMATS, raw_format

# The raw formats a dataset can be scaffolded for: those with a converter
FORMATS = [name for name, spec in RAW_FORMATS.items() if spec["converter"]]

_RESOURCES_DIR = Path(__file__).parent / "_resources" / "templates"

# The commented-out Subjects block of the subject.yaml template, which
# --multi-subject enables
_SUBJECTS_COMMENTED = re.compile(r"^# Subjects:\n(?:#.*\n)+", re.MULTILINE)


def _filled(name: str, content: str, old: str | re.Pattern, new) -> str:
    """The template `name` with its one `old` (text or pattern) replaced; a
    template without it is an error, never a silent no-op."""
    pattern = old if isinstance(old, re.Pattern) else re.compile(re.escape(old))
    content, n = pattern.subn(new, content)
    if n != 1:
        raise ValueError(f"template {name}: {n} matches of {pattern.pattern!r}, expected 1")
    return content


def converter_args(fmt: str) -> str:
    """The constructor arguments of the format's converter in curate.py: the
    neuroconv source_data of the raw path, or the raw path itself for a
    converter that reads the files without neuroconv."""
    spec = raw_format(fmt)
    if spec["source"]:
        return f'source_data={{"Recording": {{"{spec["source"]}": str(raw_path)}}}},'
    return f"raw_path,  # TODO: {spec['converter']} reads the raw files itself; check its arguments"


def init_dataset(name: str, fmt: str, multi_subject: bool,
                 acute: bool, output_dir: Path) -> Path:
    """Scaffold a new dataset directory with templates and curate.py.

    Shared Project/Consortium metadata is not scaffolded per dataset; see
    _resources/templates/collection.yaml for the project-level file.
    """
    spec = raw_format(fmt)
    if not spec["converter"]:
        raise ValueError(f"no converter for the raw format {fmt!r}; one of {', '.join(FORMATS)}")
    dataset_dir = output_dir / name
    if dataset_dir.exists():
        raise FileExistsError(f"{dataset_dir} already exists")

    metadata_dir = dataset_dir / "metadata" / "curated"
    metadata_dir.mkdir(parents=True)
    (dataset_dir / "metadata" / "source").mkdir()
    (dataset_dir / "raw-data").mkdir()

    templates = [
        "dataset.yaml", "subject.yaml", "sessions.yaml",
        "hardware.yaml", "protocol.yaml", "processing.yaml",
    ]
    if acute:
        templates.append("recordings.yaml")
    for tmpl_name in templates:
        content = (_RESOURCES_DIR / tmpl_name).read_text()
        if tmpl_name == "subject.yaml" and multi_subject:
            content = _filled(tmpl_name, content, _SUBJECTS_COMMENTED,
                              lambda block: re.sub(r"^# ?", "", block[0], flags=re.MULTILINE))
        if tmpl_name == "hardware.yaml" and acute:
            content = _filled(tmpl_name, content, 'recording type: ""', "recording type: acute")
        (metadata_dir / tmpl_name).write_text(content)

    curate_template = (_RESOURCES_DIR / "curate.py.template").read_text()
    (dataset_dir / "curate.py").write_text(Template(curate_template).substitute(
        dataset_name=name,
        converter_class=spec["converter"],
        converter_args=converter_args(fmt),
        recording_format=fmt,
    ))
    (dataset_dir / "README.md").write_text(_build_readme(name))
    return dataset_dir


def _build_readme(name: str) -> str:
    return f"""\
# {name}

Dataset curation pipeline.

## Setup

1. Add source documents to `metadata/source/`
2. Fill in YAML templates in `metadata/curated/`
3. Implement `discover_sessions()` and `get_raw_file()` in `curate.py`
4. Place raw data in `raw-data/`

## Usage

```bash
# List sessions
uv run python curate.py --list-sessions

# Run full pipeline
uv run python curate.py --all

# Run individual steps
uv run python curate.py --compile
uv run python curate.py --build-probes
uv run python curate.py --bids-layout --session <session_id>
uv run python curate.py --convert --session <session_id>
uv run python curate.py --sanity --session <session_id>
uv run python curate.py --validate
```

## Directory Structure

```
{name}/
  curate.py                    # Curation pipeline script
  metadata/
    source/                    # Raw documents from researchers
    curated/                   # Structured YAML templates
  raw-data/                    # Raw recordings (not tracked)
  curated-data/                # Pipeline output (auto-generated)
  validation/                  # Validation reports
```
"""


def main(argv: list[str] | None = None) -> int:
    """CLI of `fairephys init`."""
    parser = argparse.ArgumentParser(prog="fairephys init", description="Scaffold a new dataset")
    parser.add_argument("name", help="Dataset name (e.g., org-pi)")
    parser.add_argument("--format", default="blackrock", choices=FORMATS,
                        help="Raw recording format, which selects the converter (default: blackrock)")
    parser.add_argument("--multi-subject", action="store_true",
                        help="Enable multi-subject layout in subject.yaml")
    parser.add_argument("--acute", action="store_true",
                        help="Acute recordings (include recordings.yaml for per-session placements)")
    parser.add_argument("--output-dir", default=".", type=Path,
                        help="Parent directory for the scaffolded project (default: .)")
    args = parser.parse_args(argv)

    try:
        dataset_dir = init_dataset(args.name, args.format, args.multi_subject, args.acute, args.output_dir)
    except FileExistsError as exc:
        print(f"Error: {exc}")
        return 1
    spec = raw_format(args.format)
    print(f"Created {dataset_dir}/")
    print(f"  curate.py          ({spec['converter']}, format={args.format})")
    print(f"  metadata/curated/  ({len(list((dataset_dir / 'metadata' / 'curated').iterdir()))} templates)")
    if args.multi_subject:
        print("  subject.yaml       (multi-subject enabled)")
    if args.acute:
        print("  recordings.yaml    (acute placements)")
    print()
    print("Next steps:")
    print(f"  1. Add source documents to {args.name}/metadata/source/")
    print(f"  2. Fill in YAML templates in {args.name}/metadata/curated/")
    print(f"  3. Implement discover_sessions() and get_raw_file() in {args.name}/curate.py")
    print(f"  4. Run: uv run python {args.name}/curate.py --compile")
    return 0
