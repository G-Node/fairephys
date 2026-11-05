"""YAML to odML compiler."""

from __future__ import annotations

import argparse
import csv
import logging
import re
import warnings
from datetime import date
from io import StringIO
from pathlib import Path
from typing import Any

import odml
import yaml
from odml.tools import XMLWriter, xmlparser

from ..registry import load as load_registry
from ..validation.schema import validate_template as validate, validate_document
from .provenance import CURATION_SOFTWARE, curation_software

logger = logging.getLogger(__name__)


def _to_csv(val) -> str:
    """odML value list -> XML string, keeping the CSV quotes of list values.

    odml 1.5.4 (and python-odml master) strips the quotes of a quoted first
    and last value, so a first value containing a comma (e.g. a reference
    "Author, A. et al. ...") is split in two when the XML is read back.
    """
    values = [str(v).strip() for v in val]
    stream = StringIO()
    csv.writer(stream, dialect="excel").writerow(values)
    text = stream.getvalue().strip()
    return f"[{text}]" if len(values) > 1 else text.strip('"')


xmlparser.to_csv = _to_csv

# Configuration
ODML_VERSION = "1.0.0"
# The document author when the Dataset names no Custodian
DEFAULT_AUTHOR = "fairephys"
# odML's own type of a section the registry gives no type for ("not specified")
UNSPECIFIED_TYPE = "n.s."
_VIEWER_DIR = Path(__file__).resolve().parent.parent / "_resources" / "viewer"
SKIP_KEYS = {"_valueOrigin", "_valueOrigins", "_source", "_schema", "_neo_fields",
             "_subjects_list"}
UNIT_PATTERN = re.compile(r'^(-?[\d.]+)\s*([a-zA-Z]\w*)$')


def load_section_types() -> dict[str, str]:
    """Section name to odML type mapping (openminds_schemas.yaml 'odml_types')."""
    return load_registry("openminds_schemas.yaml")["odml_types"]


def parse_value(value: str) -> tuple[Any, str | None]:
    """Parse '10 kg' into (10, 'kg'). Returns (value, None) if no unit."""
    if not isinstance(value, str):
        return value, None
    if match := UNIT_PATTERN.match(value.strip()):
        num, unit = match.groups()
        return (int(num) if '.' not in num else float(num)), unit
    return value, None


def _apply_defaults(metadata: dict, defaults_key: str, target_key: str, is_dict: bool = False) -> dict:
    """Apply defaults from one section to another.

    When the target container is empty and Sessions exist, applies defaults
    directly to each Session (e.g., Recording Defaults with no per-session
    Recordings entries).
    """
    if defaults_key not in metadata or target_key not in metadata:
        return metadata

    defaults = metadata.pop(defaults_key)
    targets = metadata[target_key].values() if is_dict else metadata[target_key]

    if not targets and "Sessions" in metadata:
        # No per-session entries: apply defaults to each Session directly
        for session in metadata["Sessions"]:
            for key, value in defaults.items():
                session.setdefault(key, value)
    else:
        for target in targets:
            for key, value in defaults.items():
                target.setdefault(key, value)

    return metadata


def _merge_recordings_into_sessions(metadata: dict) -> dict:
    """Merge Recordings into Sessions by session_id; a value is replaced only
    by a value (an empty one of the recording keeps the session's)."""
    if "Recordings" not in metadata or "Sessions" not in metadata:
        return metadata

    recordings = metadata.pop("Recordings")
    for session in metadata["Sessions"]:
        if (sid := session.get("session id")) and sid in recordings:
            session.update({k: v for k, v in recordings[sid].items() if not _is_empty(v) or k not in session})
    return metadata


SHARED_DIR_PARTS = ("collection", "metadata")
_SHARED_SEARCH_DEPTH = 4


def find_shared_dir(templates_dir: Path) -> Path | None:
    """Locate the nearest shared-metadata dir (``collection/metadata``).

    Searches templates_dir and up to _SHARED_SEARCH_DEPTH parents so that a
    dataset at <project>/datasets/<name>/metadata/curated finds
    <project>/collection/metadata. Returns None when there is none.
    """
    templates_dir = Path(templates_dir).resolve()
    for cand in (templates_dir, *templates_dir.parents[:_SHARED_SEARCH_DEPTH]):
        shared = cand.joinpath(*SHARED_DIR_PARTS)
        if shared.is_dir():
            return shared
    return None


# Lists of entries merged by a key field instead of replaced (see deep_merge)
MERGE_KEYS = {"Fundings": "award number"}


def _merge_by_key(base: list, override: list, key: str) -> list:
    """The override entries first (a dataset's own, in its order), then the base
    entries (shared) whose (non-empty) key is not among them."""
    own = {str(e[key]) for e in override if isinstance(e, dict) and e.get(key)}
    return list(override) + [e for e in base
                             if not (isinstance(e, dict) and e.get(key) and str(e[key]) in own)]


def deep_merge(base: dict, override: dict) -> dict:
    """Return base updated with override; nested dicts merge recursively.

    Scalars and lists from override replace base values, except the lists of
    MERGE_KEYS (e.g. shared Fundings), which list the override's entries first
    and add the base entries not overridden. Neither input is mutated.
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        elif key in MERGE_KEYS and isinstance(value, list) and isinstance(merged.get(key), list):
            merged[key] = _merge_by_key(merged[key], value, MERGE_KEYS[key])
        else:
            merged[key] = value
    return merged


def _load_yaml_dir(dir_: Path, validate_mode: str) -> tuple[dict, dict, list[str]]:
    """Load all *.yaml files in a directory into one dict.

    Files prefixed with _ are skipped. Files within one directory own
    disjoint top-level keys, so a shallow update is sufficient here.
    Returns (data, value_origins, validation_errors).
    """
    data_all, origins, errors = {}, {}, []
    for fpath in sorted(Path(dir_).glob("*.yaml")):
        if fpath.name.startswith("_"):
            continue

        with open(fpath) as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            logger.warning(f"Skipping {fpath.name}: not a YAML mapping")
            continue

        if validate_mode != "skip":
            if errs := validate(data, fpath.name):
                errors.extend(f"{fpath.name}: {e}" for e in errs)

        origin = data.pop("_valueOrigin", fpath.name)
        for key in data:
            if key not in SKIP_KEYS:
                origins[key] = origin
        data_all.update(data)
        logger.debug(f"Loaded: {fpath.name}")
    return data_all, origins, errors


def load_metadata(templates_dir: Path, validate_mode: str = "strict",
                  shared_dir: Path | None = None) -> dict:
    """Load and merge YAML template files into single metadata dict.

    Discovers templates by globbing *.yaml in templates_dir.
    Files prefixed with _ are skipped (internal/schema files).

    Shared metadata (e.g. an umbrella Project/Consortium common to all
    datasets of a curation project) is loaded first from shared_dir (given
    explicitly, it must exist; by default the nearest ``collection/metadata``
    directory above templates_dir, see find_shared_dir(), logged) and
    deep-merged underneath the dataset's own files, so dataset values always
    win.
    """
    templates_dir = Path(templates_dir)
    metadata, origins, errors = {}, {}, []

    if shared_dir is not None:
        shared_dir = Path(shared_dir)
        if not shared_dir.is_dir():
            raise FileNotFoundError(f"Shared metadata directory not found: {shared_dir}")
    elif shared_dir := find_shared_dir(templates_dir):
        logger.info("Shared metadata: %s (the nearest %s above the templates)",
                    shared_dir, "/".join(SHARED_DIR_PARTS))
    if shared_dir:
        metadata, origins, errors = _load_yaml_dir(shared_dir, validate_mode)

    dataset_meta, dataset_origins, dataset_errors = _load_yaml_dir(templates_dir, validate_mode)
    metadata = deep_merge(metadata, dataset_meta)
    origins.update(dataset_origins)
    errors.extend(dataset_errors)

    if errors:
        if validate_mode == "strict":
            raise ValueError("Schema validation failed:\n" + "\n".join(errors))
        for e in errors:
            logger.warning(e)

    metadata["_valueOrigins"] = origins

    # Several subjects: compiled into one 'Subjects' section (see compile_to_odml)
    if "Subjects" in metadata:
        if "Subject" in metadata:
            logger.warning("subject.yaml has both 'Subject' and 'Subjects'; all subjects "
                           "come from 'Subjects', remove the single 'Subject'")
        metadata["_subjects_list"] = metadata.pop("Subjects")

    metadata = _apply_defaults(metadata, "Session Defaults", "Sessions")
    metadata = _apply_defaults(metadata, "Recording Defaults", "Recordings", is_dict=True)
    metadata = _merge_recordings_into_sessions(metadata)

    # Propagate dataset-level fields to sessions for openMINDS extraction
    prep = metadata.get("Dataset", {}).get("preparation design")
    if prep and "Sessions" in metadata:
        for session in metadata["Sessions"]:
            session.setdefault("preparation design", prep)

    return metadata


def _get_section_type(name: str) -> str:
    """The odML type of a section from the registry (by its name, or by the
    name before a _<n> suffix, singular or plural); UNSPECIFIED_TYPE when the
    registry has none, logged at debug."""
    types = load_section_types()
    base = name.rsplit("_", 1)[0]
    if (section_type := types.get(name) or types.get(base) or types.get(base + "s")) is None:
        logger.debug("Section %r has no odML type in the registry (odml_types): %s", name, UNSPECIFIED_TYPE)
        return UNSPECIFIED_TYPE
    return section_type


def _is_dict_list(value: Any) -> bool:
    return isinstance(value, list) and value and all(isinstance(v, dict) for v in value)


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def dict_to_section(name: str, data: dict, value_origin: str = None,
                    omit_empty: bool = False) -> odml.Section:
    """Recursively convert dict to odML Section."""
    section = odml.Section(name=name, type=_get_section_type(name))
    neo_fields = set(data.get("_neo_fields", []))

    for key, value in data.items():
        if key in SKIP_KEYS:
            continue
        if omit_empty and _is_empty(value):
            continue

        origin = "neo" if key in neo_fields else value_origin

        if isinstance(value, dict):
            section.append(dict_to_section(key, value, value_origin, omit_empty))
        elif _is_dict_list(value):
            parent = odml.Section(name=key, type=_get_section_type(key))
            for i, item in enumerate(value, 1):
                parent.append(dict_to_section(f"{key}_{i}", item, value_origin, omit_empty))
            section.append(parent)
        else:
            val, unit = parse_value(value) if not isinstance(value, list) else (value, None)
            prop = odml.Property(name=key, value=val, unit=unit, value_origin=origin)
            section.append(prop)

    return section


def _placement_lists(metadata: dict) -> list[tuple[str, list]]:
    """(owner, placements) of every session's Electrode Placements and every
    subject's Implanted Probes."""
    found = []
    for session in metadata.get("Sessions") or []:
        if placements := session.get("Electrode Placements"):
            found.append((f"session {session.get('session id')}", placements))
    subjects = metadata.get("_subjects_list") or ([metadata["Subject"]] if "Subject" in metadata else [])
    for entry in subjects:
        subject = entry.get("Subject", entry)
        if placements := (subject.get("Surgical Preparation") or {}).get("Implanted Probes"):
            found.append((f"subject {subject.get('subject id')}", placements))
    return found


def check_placements(metadata: dict) -> None:
    """Every placement names one Hardware.Probes entry, and no entry twice per
    session or subject: a placement is one use of one physical device, so a
    repeated name hides which device went where (list each device instead)."""
    lists = _placement_lists(metadata)
    if not lists:
        return
    probes = ((metadata.get("Hardware") or {}).get("Probes") or [])
    names = [p.get("name") for p in probes if isinstance(p, dict)]
    if dup := sorted({n for n in names if names.count(n) > 1}):
        raise ValueError(f"Hardware.Probes names repeat: {', '.join(dup)}")
    errors = []
    for owner, placements in lists:
        used = [pl.get("probe") for pl in placements if isinstance(pl, dict)]
        if missing := sorted({str(n) for n in used if n not in names}):
            errors.append(f"{owner}: probe {', '.join(missing)} not in Hardware.Probes")
        if repeated := sorted({str(n) for n in used if used.count(n) > 1}):
            errors.append(f"{owner}: probe {', '.join(repeated)} placed more than once "
                          "(one Hardware.Probes entry per physical device)")
    if errors:
        raise ValueError("Electrode placements:\n  " + "\n  ".join(errors))


def compile_to_odml(metadata: dict, omit_empty: bool = False, author: str = None) -> odml.Document:
    """Convert metadata dict to odML Document.

    The document author is the one given, else the Dataset's Custodian,
    else DEFAULT_AUTHOR (logged).
    """
    check_placements(metadata)
    origins = metadata.pop("_valueOrigins", {})

    if author is None:
        custodian = metadata.get("Dataset", {}).get("Custodian", {})
        author = f"{custodian.get('given name', '')} {custodian.get('family name', '')}".strip()
        if not author:
            logger.info("odML author: the Dataset names no Custodian, using %r", DEFAULT_AUTHOR)
            author = DEFAULT_AUTHOR

    doc = odml.Document(
        author=author,
        version=ODML_VERSION,
        date=date.today(),
    )

    for name, data in metadata.items():
        if name in SKIP_KEYS:
            continue
        origin = origins.get(name)
        if isinstance(data, dict):
            doc.append(dict_to_section(name, data, origin, omit_empty))
        elif _is_dict_list(data):
            parent = odml.Section(name=name, type=_get_section_type(name))
            for i, item in enumerate(data, 1):
                parent.append(dict_to_section(f"{name}_{i}", item, origin, omit_empty))
            doc.append(parent)

    # Several subjects: one 'Subjects' section, one 'Subject_<id>' entry each
    if subjects := metadata.get("_subjects_list"):
        parent = odml.Section(name="Subjects", type=_get_section_type("Subjects"))
        for entry in subjects:
            subject = entry.get("Subject", entry)
            parent.append(dict_to_section(f"Subject_{subject.get('subject id')}", subject,
                                          origins.get("Subjects"), omit_empty))
        doc.append(parent)

    _set_dataset_citation(doc)
    _add_curation_software(doc)
    validate_document(doc)
    return doc


def _add_curation_software(doc: odml.Document) -> None:
    """Record the curation software versions in the 'Curation Software' section.

    One subsection per tool (name, version, purpose; the BIDS validator also
    its schema, the pipeline its commit and uncommitted changes), resolved from
    the environment by provenance.curation_software(). Values stay text
    ("0.10.2" is no number with a unit, a commit no number), flags booleans.
    A section of that name from the YAML is replaced.
    """
    if CURATION_SOFTWARE in doc.sections:
        doc.remove(doc[CURATION_SOFTWARE])
    section_type = _get_section_type(CURATION_SOFTWARE)
    parent = odml.Section(name=CURATION_SOFTWARE, type=section_type)
    for tool in curation_software():
        origin = tool.pop("origin")
        section = odml.Section(name=tool["name"], type=section_type)
        for key, value in tool.items():
            flag = isinstance(value, bool)
            section.append(odml.Property(name=key, values=[value if flag else str(value)],
                                         dtype="boolean" if flag else "string", value_origin=origin))
        parent.append(section)
    if parent.sections:
        doc.append(parent)


def _initials(given: str) -> str:
    """Initials of given names: "Bjørg Elisabeth" -> "B. E.", "Jean-Pierre" -> "J.-P."."""
    return " ".join("-".join(f"{p[0]}." for p in word.split("-") if p) for word in given.split())


def _set_dataset_citation(doc: odml.Document) -> None:
    """Fill the Dataset 'how to cite' with the citation of the dataset itself.

    Same style as the related publications: "Family, G., ... & Family, G. <full name>,
    version <version identifier>. <https://doi.org/<doi> or repository url> (<year>)."
    The year is the build year (the release year in the release build); the DOI
    replaces the repository URL once it is in the metadata. A 'how to cite' given in
    the YAML overrides this pattern.
    """
    if "Dataset" not in doc.sections:
        return
    ds = doc["Dataset"]

    def value(sec, name):
        prop = sec.properties[name] if name in sec.properties else None
        return str(prop.values[0]).strip() if prop is not None and prop.values and prop.values[0] not in (None, "") else ""

    if value(ds, "how to cite") or not value(ds, "full name"):
        return
    contributors = ds["Contributors"].sections if "Contributors" in ds.sections else []
    names = [", ".join(filter(None, [value(c, "family name"), _initials(value(c, "given name"))]))
             for c in contributors]
    authors = " & ".join(filter(None, [", ".join(names[:-1]), names[-1]])) if names else ""
    version = f", version {v}" if (v := value(ds, "version identifier")) else ""
    doi = re.sub(r"^(https?://doi\.org/|doi:)", "", value(ds, "doi"))
    link = f"https://doi.org/{doi}" if doi else value(ds, "repository url")
    text = (authors if authors.endswith(".") else f"{authors}.") + " " if authors else ""
    text += f"{value(ds, 'full name')}{version}" + (f". {link}" if link else "") \
        + f" ({date.today().year})."
    origin = "generated: Contributors, full name, version identifier, doi or repository url, build year"
    if "how to cite" in ds.properties:
        prop = ds.properties["how to cite"]
        prop.values, prop.value_origin = [text], origin
    else:
        ds.append(odml.Property(name="how to cite", value=text, value_origin=origin))


def _build_decorator_js(instances: dict) -> str:
    """Build <script> that adds property-doc and instance-link icons."""
    import json as _json

    return f"""<script>
(function() {{
  var INSTANCES = {_json.dumps(instances)};

  document.querySelectorAll('tr').forEach(function(tr) {{
    var nameCell = tr.querySelector('.prop-name');
    if (!nameCell) return;

    var propName = nameCell.textContent.trim();
    var refUrl = nameCell.getAttribute('data-ref');

    // Property docs link (from data-ref attribute)
    if (refUrl && refUrl.indexOf('/props/') !== -1) {{
      var pname = refUrl.split('/props/')[1].toLowerCase();
      var a = document.createElement('a');
      a.href = 'https://openminds.docs.om-i.org/en/latest/search.html?q=' + pname;
      a.target = '_blank';
      a.title = 'openMINDS property: ' + refUrl;
      a.className = 'ref-icon';
      a.innerHTML = '&#x1F4D6;';
      nameCell.appendChild(document.createTextNode(' '));
      nameCell.appendChild(a);
    }}

    // Instance links (one per resolved value)
    var section = nameCell.closest('.section, .subsection');
    var heading = section ? section.querySelector('h2, h3') : null;
    var sectionName = heading ? heading.textContent.trim() : '';

    if (INSTANCES[sectionName] && INSTANCES[sectionName][propName]) {{
      var propInst = INSTANCES[sectionName][propName];
      for (var val in propInst) {{
        if (propInst[val] && propInst[val].instance_uri) {{
          var a2 = document.createElement('a');
          a2.href = propInst[val].instance_uri;
          a2.target = '_blank';
          a2.title = val + ' \\u2192 ' + propInst[val].instance_uri;
          a2.className = 'ref-icon';
          a2.innerHTML = '&#x1F517;';
          nameCell.appendChild(document.createTextNode(' '));
          nameCell.appendChild(a2);
        }}
      }}
    }}
  }});
}})();
</script>"""


def generate_html_viewer(xml_path: Path) -> Path:
    """Generate a self-contained HTML viewer from an odML XML file.

    Applies the project's XSL stylesheet with CSS and JS inlined,
    producing an HTML file that works offline (just double-click).
    """
    from lxml import etree

    xsl_tree = etree.parse(str(_VIEWER_DIR / "odmlDocument.xsl"))
    css_text = (_VIEWER_DIR / "odml-style.css").read_text()
    js_text = (_VIEWER_DIR / "odml-notes.js").read_text()

    xsl_root = xsl_tree.getroot()

    # Inline CSS: <link rel="stylesheet" .../> -> <style>...</style>
    link = xsl_root.find(".//{http://www.w3.org/1999/XSL/Transform}template//link[@rel='stylesheet']")
    if link is None:
        link = xsl_root.find(".//link[@rel='stylesheet']")
    parent = link.getparent()
    idx = list(parent).index(link)
    style = etree.Element("style")
    style.text = css_text
    style.tail = link.tail
    parent.remove(link)
    parent.insert(idx, style)

    # Inline JS: <script src="..."/> -> <script>...</script>
    script = xsl_root.find(".//{http://www.w3.org/1999/XSL/Transform}template//script[@src]")
    if script is None:
        script = xsl_root.find(".//script[@src]")
    del script.attrib["src"]
    script.text = js_text

    # Apply XSLT and write HTML
    xml_tree = etree.parse(str(xml_path))
    result = etree.XSLT(xsl_tree)(xml_tree)
    html_str = "<!DOCTYPE html>\n" + str(result)

    # Embed resolved instances data + decorator JS for the docs and link icons
    base = xml_path.stem.replace(".odml", "")
    instances_path = xml_path.parent / f"{base}.instances.yaml"
    if instances_path.exists():

        inst_data = yaml.safe_load(instances_path.read_text())
        inst_data = {
            k: v for k, v in inst_data.items()
            if not k.startswith("_") and isinstance(v, dict)
        }
        if inst_data:
            html_str = html_str.replace(
                "</body>", _build_decorator_js(inst_data) + "\n</body>",
            )

    html_path = xml_path.with_suffix(".html")
    html_path.unlink(missing_ok=True)  # a released file may be a read-only annex link
    html_path.write_text(html_str, encoding="utf-8")
    return html_path


def save_odml(doc: odml.Document, output_path: Path) -> tuple[Path, Path]:
    """Save odML document to YAML, XML, and self-contained HTML formats.

    odml's own summary of the document's validation issues at save time is
    silenced: validate_document() has logged each issue at compile.
    """
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    yaml_path = output_path.with_suffix('.odml.yaml')
    xml_path = output_path.with_suffix('.odml.xml')
    for path in (yaml_path, xml_path):
        path.unlink(missing_ok=True)  # a released file may be a read-only annex link

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="The saved Document contains unresolved issues")
        odml.save(doc, str(yaml_path), backend='yaml')
        XMLWriter(doc).write_file(str(xml_path), local_style=False)

    # Strip stylesheet PI: the XML is pure data
    text = xml_path.read_text()
    xml_path.write_text(
        text.replace('<?xml-stylesheet  type="text/xsl" href="odmlDocument.xsl"?>\n', ''))

    # Generate self-contained HTML viewer
    generate_html_viewer(xml_path)

    return yaml_path, xml_path


def main(argv: list[str] | None = None) -> int:
    """CLI: Compile YAML templates to odML (no enrichment)."""
    parser = argparse.ArgumentParser(prog="fairephys compile", description="Compile YAML templates to odML")
    parser.add_argument("templates_dir", type=Path, help="Directory with YAML templates")
    parser.add_argument("-o", "--output", type=Path, required=True,
                        help="Output path without extension (e.g. curated-data/metadata/my-dataset)")
    parser.add_argument("--shared-metadata", type=Path, default=None,
                        help="Collection-level metadata directory merged underneath the templates "
                             "(default: the nearest collection/metadata above them)")
    parser.add_argument("--omit-empty", action="store_true")
    parser.add_argument("--validate", choices=["strict", "warn", "skip"], default="strict")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")

    metadata = load_metadata(args.templates_dir, validate_mode=args.validate,
                             shared_dir=args.shared_metadata)
    doc = compile_to_odml(metadata, omit_empty=args.omit_empty)
    yaml_path, xml_path = save_odml(doc, args.output)
    print(f"Saved: {yaml_path}")
    print(f"Saved: {xml_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
