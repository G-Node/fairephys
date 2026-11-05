"""README generator from odML metadata.

Produces a README in the section order of the EBRAINS Data Descriptor
template (version 3) from compiled odML.
All content is derived from the metadata; the few wordings no odML field
gives are the registry readme.yaml. Summary and version specifications use
the same text as the openMINDS export (dataset text + the shared Project
statement).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import odml

from ..bids.extractors import bracketed
from ..bids.structure import METADATA_DIRNAME
from ..metadata.provenance import CURATION_SOFTWARE
from ..metadata.subjects import session_subject, subject_sections
from ..odml import prop, subsection, unit
from ..placements import matched_probes
from ..registry import load as load_registry
from .templating import fill_template

# Units are stored with their openMINDS names; the README shows the symbols
UNIT_SYMBOLS = {
    "hertz": "Hz", "kilohertz": "kHz", "second": "s", "millisecond": "ms",
    "millimeter": "mm", "micrometer": "µm", "centimeter": "cm",
    "kilogram": "kg", "gram": "g", "ohm": "Ω", "kiloohm": "kΩ", "megaohm": "MΩ",
    "volt": "V", "millivolt": "mV", "microvolt": "µV",
}
# Units without a symbol are written out, in the plural for counts other than 1
# ("15 years", "1 year")
WORD_UNITS = {"minute", "hour", "day", "week", "month", "year"}
# A text value that is only a number or range and a unit name ("1-5000 hertz")
_NUMBER_UNIT_RE = re.compile(r"([-+\d.,\s]*\d)\s*(" + "|".join(UNIT_SYMBOLS) + ")")
# Session durations are shown in minutes, as the durations measured from the recordings
_MINUTES_PER_UNIT = {"millisecond": 1 / 60000, "second": 1 / 60, "minute": 1, "hour": 60}
# Behavioral Protocol flags (true/false) worded by their property name, with
# the property that details them
_POSITIONING_FLAGS = [("head fixation", "head fixation method"), ("primate chair", None)]
# What each column of the Recording Sessions table holds (its caption)
_SESSION_COLUMNS = {"Session": "session id", "Subject": "subject id", "Task": "task", "Date": "date",
                    "Duration": "duration", "Probes": "the probes placed (with brain region and hemisphere)",
                    "Description": "a description"}


def _count(n, noun: str) -> str:
    """Count and noun, plural for counts other than 1 ("1 recording session", "2 contacts")."""
    return f"{n} {noun}" if str(n) == "1" else f"{n} {noun}s"


def _sentence_case(text: str) -> str:
    """Section name as a label: "Head Post" -> "Head post"."""
    return text[:1].upper() + text[1:].lower()


def _sentence_start(text: str) -> str:
    """Text with its first letter upper case, the rest as it is."""
    return text[:1].upper() + text[1:]


def _license_label(license_id: str) -> str:
    """SPDX id as Creative Commons writes it: CC-BY-NC-SA-4.0 -> CC BY-NC-SA 4.0."""
    if not license_id.startswith("CC"):
        return license_id
    name, _, version = license_id.rpartition("-")
    return f"{name.replace('CC-', 'CC ', 1)} {version}"


# ---------------------------------------------------------------------------
# odML value helpers
# ---------------------------------------------------------------------------

def _values(sec: odml.Section | None, name: str) -> list:
    """All values of a property as a list (empty if missing)."""
    val = prop(sec, name)
    return val if isinstance(val, list) else [] if val is None else [val]


def _prop_str(sec: odml.Section | None, name: str) -> str:
    """Property value as text with its unit symbol appended (e.g. '30000 Hz')."""
    val = prop(sec, name)
    if val in (None, "", []):
        return ""
    if unit_name := unit(sec, name):
        symbol = UNIT_SYMBOLS.get(unit_name, unit_name)
        if unit_name in WORD_UNITS and val != 1:
            symbol += "s"
        return f"{val} {symbol}"
    if isinstance(val, list):
        return _list_to_str(val)
    # a unit written into the text ("1-5000 hertz") gets its symbol as well
    if isinstance(val, str) and (m := _NUMBER_UNIT_RE.fullmatch(val.strip())):
        return f"{m.group(1).strip()} {UNIT_SYMBOLS[m.group(2)]}"
    return str(val)


def _nonempty(val) -> bool:
    """True if val is truthy and not an empty list/string."""
    if val is None:
        return False
    if isinstance(val, (list, str)) and not val:
        return False
    return True


def _list_to_str(val, sep=", ") -> str:
    if isinstance(val, list):
        return sep.join(str(v) for v in val if v)
    return str(val) if val else ""


# ---------------------------------------------------------------------------
# Section builders -- each returns a markdown string (empty if no data)
# ---------------------------------------------------------------------------

def _authors_section(ds: odml.Section) -> str:
    """Format AUTHORS with numbered affiliations."""
    contributors = subsection(ds, "Contributors")
    if not contributors or not contributors.sections:
        return ""

    affiliations: list[str] = []
    aff_map: dict[str, int] = {}
    author_lines = []

    for sub in contributors.sections:
        given = prop(sub, "given name") or ""
        family = prop(sub, "family name") or ""
        if not (given or family):
            continue

        aff_sec = subsection(sub, "affiliation")
        aff_str = ""
        if aff_sec:
            parts = []
            for field in ("lab", "department", "institution", "country"):
                v = prop(aff_sec, field)
                if _nonempty(v):
                    parts.append(str(v))
            aff_str = ", ".join(parts)

        sup = ""
        if aff_str:
            if aff_str not in aff_map:
                aff_map[aff_str] = len(affiliations) + 1
                affiliations.append(aff_str)
            sup = f" ({aff_map[aff_str]})"

        author_lines.append(f"{given} {family}{sup}")

    if not author_lines:
        return ""

    lines = ["## Authors", ""]
    lines.append(", ".join(author_lines))
    lines.append("")

    if affiliations:
        lines.append("### Affiliations")
        lines.append("")
        for i, aff in enumerate(affiliations, 1):
            lines.append(f"{i}. {aff}")
        lines.append("")

    return "\n".join(lines)


def _corresponding_author(ds: odml.Section) -> str:
    """Format CORRESPONDING AUTHOR from Custodian."""
    cust = subsection(ds, "Custodian")
    if not cust:
        return ""
    given = prop(cust, "given name") or ""
    family = prop(cust, "family name") or ""
    email = prop(cust, "email") or ""
    if not (given or family):
        return ""

    lines = ["### Corresponding Author(s)", ""]
    name = f"{given} {family}".strip()
    if email:
        lines.append(f"{name}: {email}")
    else:
        lines.append(name)
    lines.append("")
    return "\n".join(lines)


def _with_statement(ds: odml.Section, field: str, statement: str) -> str:
    """Dataset text followed by the shared Project statement (as in openMINDS)."""
    project = subsection(ds, "Project")
    parts = [prop(ds, field), project and prop(project, statement)]
    return "\n\n".join(str(p).strip() for p in parts if _nonempty(p))


def _summary_section(ds: odml.Section) -> str:
    """Format SUMMARY from description + dataset statement."""
    text = _with_statement(ds, "description", "dataset statement")
    if not text:
        return ""
    return f"## Summary\n\n{text}\n"


def _version_section(ds: odml.Section) -> str:
    """Format VERSION SPECIFICATIONS from version innovation + version statement."""
    text = _with_statement(ds, "version innovation", "version statement")
    if not text:
        return ""
    return f"## Version Specifications\n\n{text}\n"


def _methods_subject(doc: odml.Document) -> str:
    """Format the subject subsection(s) for Materials and Methods."""
    subjects = subject_sections(doc)
    blocks = [_methods_one_subject(s, len(subjects) > 1) for s in subjects]
    return "\n".join(b for b in blocks if b)


def _methods_one_subject(subj: odml.Section, several: bool) -> str:
    """Subject fields, then one bullet per property of each Surgical
    Preparation subsection (an implant, a chamber, a craniotomy, whatever the
    metadata describes)."""
    parts = []
    for field, label in [
        ("species", "Species"), ("subject id", "Subject ID"),
        ("biological sex", "Biological sex"), ("age category", "Age category"),
    ]:
        v = prop(subj, field)
        if _nonempty(v):
            parts.append(f"- **{label}:** {v}")

    for field, label in [("age", "Age"), ("weight", "Weight")]:
        s = _prop_str(subj, field)
        if s:
            if field == "age" and _nonempty(prop(subj, "age reference date")):
                s += f" (at {prop(subj, 'age reference date')})"  # the date the stated age refers to
            parts.append(f"- **{label}:** {s}")

    if surg := subsection(subj, "Surgical Preparation"):
        for sub_sec in surg.sections:
            parts += _nested_bullets(sub_sec)

    if not parts:
        return ""
    heading = f"### Subject {prop(subj, 'subject id')}" if several else "### Subject"
    return heading + "\n\n" + "\n".join(parts) + "\n"


def _nested_bullets(sec: odml.Section) -> list[str]:
    """One bullet per filled property of a nested section, labelled with the
    section and the property ("- **Head post material:** titanium"); a
    description is the section's own text ("- **Head post:** ...")."""
    label = _sentence_case(sec.name)
    return [f"- **{label if p.name == 'description' else f'{label} {p.name}'}:** {_prop_str(sec, p.name)}"
            for p in sec.properties if p.values and _prop_str(sec, p.name)]


def _methods_hardware(doc: odml.Document) -> str:
    """Format recording setup subsection for Materials and Methods."""
    hw = subsection(doc, "Hardware")
    if hw is None:
        return ""

    lines = []

    rec_sys = subsection(hw, "Recording System")
    if rec_sys:
        name = prop(rec_sys, "name") or prop(rec_sys, "manufacturer") or ""
        model = prop(rec_sys, "system model") or ""
        if name:
            # the model only when the name does not already give it
            if str(model).lower() in str(name).lower():
                model = ""
            lines.append(f"- **Recording system:** {bracketed(str(name), str(model))}")
        sr = _prop_str(rec_sys, "sampling rate")
        if sr:
            lines.append(f"- **Sampling rate:** {sr}")
        # the capacity of the system, not the number of recorded channels
        nch = prop(rec_sys, "number of channels")
        if _nonempty(nch):
            lines.append(f"- **System channels:** {nch}")
        sw = prop(rec_sys, "acquisition software") or ""
        sw_ver = prop(rec_sys, "software version") or ""
        if sw:
            sw_str = str(sw)
            if sw_ver:
                sw_str += f" {sw_ver}"
            lines.append(f"- **Acquisition software:** {sw_str}")

    # Hardware filters
    filt_sec = subsection(hw, "Hardware Filters")
    if filt_sec:
        filt_container = subsection(filt_sec, "Filters")
        filt_list = (filt_container.sections if filt_container
                     else filt_sec.sections)
        if filt_list:
            filt_parts = []
            for f in filt_list:
                # name, else type; a filter with neither and no cutoff is left out
                fname = str(prop(f, "name") or prop(f, "type") or "")
                cutoff = _prop_str(f, "cutoff")
                if not (fname or cutoff):
                    continue
                desc = f"{fname} at {cutoff}" if fname and cutoff else fname or f"Cutoff {cutoff}"
                filt_parts.append(bracketed(desc, str(prop(f, "implementation") or "")))
            if filt_parts:
                lines.append(
                    f"- **Hardware filters:** {'; '.join(filt_parts)}")

    # Probes
    probes_sec = subsection(hw, "Probes")
    if probes_sec:
        for probe in probes_sec.sections:
            pname = prop(probe, "name") or "Probe"
            details = []
            ptype = prop(probe, "probe type")
            mfr = prop(probe, "manufacturer")
            model = prop(probe, "model")
            # the class with the product and its maker: "linear multi-contact probe (V-Probe, Plexon)"
            if _nonempty(model):
                product = f"{model}, {mfr}" if _nonempty(mfr) else str(model)
                details.append(bracketed(str(ptype), product) if _nonempty(ptype) else product)
            else:
                details += [str(v) for v in (ptype, mfr) if _nonempty(v)]
            ncontacts = prop(probe, "number of contacts")
            if _nonempty(ncontacts):
                details.append(_count(ncontacts, "contact"))
            pitch = _prop_str(probe, "contact pitch")
            if pitch:
                details.append(f"{pitch} pitch")
            lines.append(f"- **{pname}:** {', '.join(details)}" if details else f"- **{pname}**")
            notes = prop(probe, "notes")
            if _nonempty(notes):  # the device's status (an array excluded from the data, a known fault)
                lines.append(f"  - Notes: {notes}")

    if not lines:
        return ""
    return "### Recording Setup\n\n" + "\n".join(lines) + "\n"


def _methods_protocol(doc: odml.Document) -> str:
    """Format behavioral protocol subsection."""
    proto = subsection(doc, "Behavioral Protocol")
    if proto is None:
        return ""

    lines = []

    name = prop(proto, "name")
    if _nonempty(name):
        lines.append(f"**{name}**\n")
    desc = prop(proto, "description")
    if _nonempty(desc):
        lines.append(str(desc).strip() + "\n")

    task = prop(proto, "task")
    if _nonempty(task):
        lines.append(f"- **Task during the recordings:** {task}")
    other = prop(proto, "other tasks description")
    if prop(proto, "other tasks performed") and _nonempty(other):
        lines.append(f"- **Other tasks on the recording days:** {' '.join(str(other).split())}")
    paradigm = prop(proto, "task paradigm")
    if _nonempty(paradigm):
        lines.append(f"- **Paradigm:** {paradigm}")

    # Subject positioning: the flags that are set, worded by their property
    # name ("head fixation: <method>" when the method holds brackets itself)
    pos_parts = [bracketed(flag, str(prop(proto, method) or "") if method else "", ": ")
                 for flag, method in _POSITIONING_FLAGS if prop(proto, flag)]
    body_pos = prop(proto, "body position")
    if _nonempty(body_pos):
        pos_parts.append(str(body_pos))
    if pos_parts:
        lines.append(f"- **Subject positioning:** {', '.join(pos_parts)}")

    # Monitoring
    monitoring = []
    if prop(proto, "eye tracking"):
        method = prop(proto, "eye tracking method") or ""
        monitoring.append(bracketed("eye tracking", str(method), ": "))
    mv = prop(proto, "movement monitoring")
    if _nonempty(mv):
        monitoring.append(_list_to_str(mv))
    ph = prop(proto, "physiological monitoring")
    if _nonempty(ph):
        monitoring.append(_list_to_str(ph))
    if monitoring:
        lines.append(f"- **Monitoring:** {', '.join(monitoring)}")

    if not lines:
        return ""
    return "### Behavioral Protocol\n\n" + "\n".join(lines) + "\n"


def _methods_processing(doc: odml.Document, output_dir: Path) -> str:
    """Format data processing subsection (with a table of the annotated states;
    its caption names the events files when there are any under output_dir)."""
    from ..bids.builder import processing_states

    proc = subsection(doc, "Data Processing")
    if proc is None or not proc.sections:
        return ""

    blocks = []
    for step in proc.sections:
        name = prop(step, "name") or "Processing step"
        technique = prop(step, "technique") or ""
        desc = prop(step, "description") or ""
        header = f"**{name}**"
        # the technique only when it adds to the name ("Spike Sorting (spike sorting)")
        if technique and str(technique).lower() != str(name).lower():
            header += f" ({technique})"
        blocks.append(header)
        if desc:
            blocks.append(str(desc).strip())
        sw_sec = subsection(step, "Software")
        if sw_sec:
            sw_name = prop(sw_sec, "name") or ""
            sw_ver = prop(sw_sec, "version") or ""
            if sw_name:
                sw_str = str(sw_name)
                if sw_ver:
                    sw_str += f" {sw_ver}"
                blocks.append(f"Software: {sw_str}")
        if states := processing_states(step):
            blocks.append(_states_table(states, output_dir.is_dir() and any(output_dir.rglob("*_events.tsv"))))

    # one paragraph each, so Markdown does not run heading, text and software together
    return "### Data Processing\n\n" + "\n\n".join(blocks) + "\n" if blocks else ""


def _states_table(states: list[dict], events_files: bool = False) -> str:
    """Table of the states a processing step annotates (Code column only when one has a code),
    with a caption: the code is the lab's raw code, the label the trial_type value of the
    events files (named when the dataset has events files)."""
    columns = (["Code"] if any(_nonempty(s.get("code")) for s in states) else []) + ["Label", "Description"]
    held = {"Code": "the lab's code in its raw files",
            "Label": "the label (the trial_type value in the events files)" if events_files else "the label",
            "Description": "a description"}
    caption = f"The states this step annotates, one row each: {_prose_list(held[c] for c in columns)}."
    rows = [caption, "", "| " + " | ".join(columns) + " |", "|" + "|".join("-" * (len(c) + 2) for c in columns) + "|"]
    rows += ["| " + " | ".join(str(s.get(c.lower(), "")).strip() for c in columns) + " |" for s in states]
    return "\n".join(rows)


def _session_duration(ses: odml.Section) -> str:
    """Session duration in minutes ("15.2 min"), from the metadata or the Neo metadata.

    A metadata value with a time unit is converted to minutes (a value in
    minutes is kept as given); a value without a unit is shown as it is.
    """
    value = prop(ses, "duration")
    if _nonempty(value) and not isinstance(value, list):
        unit_name = unit(ses, "duration")
        if unit_name == "minute":
            return f"{value} min"
        if unit_name in _MINUTES_PER_UNIT:
            try:
                return f"{float(value) * _MINUTES_PER_UNIT[unit_name]:.1f} min"
            except (ValueError, TypeError):
                pass
        return _prop_str(ses, "duration")
    dur_s = prop(subsection(ses, "Neo Metadata"), "duration_s")
    if _nonempty(dur_s):
        try:
            return f"{float(dur_s) / 60:.1f} min"
        except (ValueError, TypeError):
            pass
    return ""


def _session_placements(doc: odml.Document, ses: odml.Section) -> list[tuple[str, str, str]]:
    """(device name, brain region, hemisphere) per placement of the session
    (see placements.matched_probes: the session's, else the subject's)."""
    return [(str(prop(device, "name")), str(placement.get("brain region") or ""),
             str(placement.get("hemisphere") or ""))
            for _, device, placement in matched_probes(doc, ses)]


def _session_probes(doc: odml.Document, ses: odml.Section) -> str:
    """The session's placements as "<device>: <region> (<hemisphere>)", "; "-joined."""
    return "; ".join(f"{name}: {bracketed(region, hemisphere)}" if region or hemisphere else name
                     for name, region, hemisphere in _session_placements(doc, ses))


def _session_subject_id(doc: odml.Document, ses: odml.Section) -> str:
    """Subject id of a session ('' when the session names no known subject)."""
    try:
        return str(prop(session_subject(doc, ses), "subject id") or "")
    except ValueError:
        return ""


def _methods_sessions(doc: odml.Document) -> str:
    """Format recording sessions subsection with table.

    The count is of distinct sessions per subject (one row per recording).
    A Subject column is added when there are several subjects, a Task column
    when sessions name their task (a session that recorded several tasks has
    one row per task; sessions without one show the dataset's task); the
    Probes column names each placed device with its region and hemisphere;
    columns that are empty in every session are left out.
    """
    sessions = subsection(doc, "Sessions")
    if sessions is None or not sessions.sections:
        return ""

    session_list = sessions.sections
    # a session with several recordings (one per task) is counted once
    n = len({(_session_subject_id(doc, s), str(prop(s, "session id"))) for s in session_list})
    dates = []
    for s in session_list:
        d = prop(s, "date")
        if _nonempty(d):
            dates.append(str(d))

    lines = ["### Recording Sessions", ""]
    date_range = ""
    if dates:
        unique_dates = sorted(set(dates))
        if len(unique_dates) > 1:
            date_range = f" from {unique_dates[0]} to {unique_dates[-1]}"
        else:
            date_range = f" on {unique_dates[0]}"
    n_subjects = len(subject_sections(doc))
    of_subjects = f" of {_count(n_subjects, 'subject')}" if n_subjects > 1 else ""
    lines.append(f"{_count(n, 'recording session')}{of_subjects}{date_range}.")
    lines.append("")

    named_tasks = any(_nonempty(prop(s, "task")) for s in session_list)
    dataset_task = prop(subsection(doc, "Behavioral Protocol"), "bids task label") or ""
    rows = [{
        "Session": prop(s, "session id") or "",
        **({"Subject": _session_subject_id(doc, s)} if n_subjects > 1 else {}),
        **({"Task": prop(s, "task") or dataset_task} if named_tasks else {}),
        "Date": prop(s, "date") or "",
        "Duration": _session_duration(s),
        "Probes": _session_probes(doc, s),
        "Description": prop(s, "description") or "",
    } for s in session_list]
    columns = [c for c in rows[0] if c == "Session" or any(_nonempty(r[c]) for r in rows)]
    lines += [f"One row per recording: {_prose_list(_SESSION_COLUMNS[c] for c in columns)}.", ""]
    lines.append("| " + " | ".join(columns) + " |")
    lines.append("|" + "|".join("-" * (len(c) + 2) for c in columns) + "|")
    lines += ["| " + " | ".join(str(r[c]) for c in columns) + " |" for r in rows]
    lines.append("")

    return "\n".join(lines)


def _methods_ethics(ds: odml.Section) -> str:
    """Format the ethics subsection from the statement or the single fields."""
    ethics = prop(ds, "ethics statement")
    if _nonempty(ethics):
        return f"### Ethics\n\n{str(ethics).strip()}\n"
    parts = []
    for field in ("ethics authority", "ethics approval id", "ethics directive",
                  "authorizing body", "licensed institution"):
        v = prop(ds, field)
        if _nonempty(v):
            parts.append(f"- **{field.capitalize()}:** {v}")
    if not parts:
        return ""
    return "### Ethics\n\n" + "\n".join(parts) + "\n"


def _methods_anatomy(doc: odml.Document) -> str:
    """Format the anatomical location from the dataset brain regions (worded
    by the readme registry), with the pointer to the per-session probe
    regions when a placement has a region (the Probes column of Recording
    Sessions)."""
    regions = _values(doc["Dataset"], "brain regions")
    if not regions:
        return ""
    sessions = subsection(doc, "Sessions")
    text = f"{load_registry('readme.yaml')['brain regions label']}: {', '.join(regions)}."
    if any(region for s in (sessions.sections if sessions else [])
           for _, region, _ in _session_placements(doc, s)):
        text += " The region of each probe per session is listed under Recording Sessions."
    return f"### Anatomical Location\n\n{text}\n"


def _usage_notes(ds: odml.Section) -> str:
    """Format USAGE NOTES: dataset notes, shared usage statement, licence."""
    parts = [_with_statement(ds, "usage notes", "usage statement")]
    license_id = str(prop(ds, "license") or "")
    if license_id:
        url = load_registry("licenses.yaml").get(license_id, {}).get("url")
        label = _license_label(license_id)
        name = f"[{label}]({url})" if url else label
        parts.append(f"The data are shared under the {name} licence.")
    text = "\n\n".join(p for p in parts if p)
    if not text:
        return ""
    return f"## Usage Notes\n\n{text}\n"


def _data_records(doc: odml.Document, output_dir: Path) -> str:
    """Format DATA RECORDS: shared records statement and the curation software
    versions, then the actual output directory (its tree and the file table,
    both from the readme registry's file types)."""
    if not output_dir.is_dir():
        return ""

    files = _file_types(doc, output_dir)
    tree = "```\n" + "\n".join(_build_tree(output_dir, _repository_name(doc["Dataset"]), files)) + "\n```"

    table = "\n".join([
        "One row per file type: its format, what the files hold, and the format specification "
        "with the software that wrote them.", "",
        "| File | Format | Content | Specification |", "|------|--------|---------|---------------|",
        *(f"| `{row['pattern']}` | {row['format']} | {row['content']} | {row['specification']} |"
          for row in files.values())]) if files else ""

    # How the data were converted and validated; the software versions only after
    # a records statement (a dataset the pipeline did not convert has none)
    statement = _records_statement(doc["Dataset"])
    parts = [statement, statement and _curation_software(doc), _layout_standard(output_dir), tree, table]
    return "## Data Records\n\n" + "\n\n".join(p for p in parts if p) + "\n"


def _prose_list(values) -> str:
    """Values as prose: "a", "a and b", "a, b and c" (empty values left out)."""
    items = [str(v).strip() for v in values if _nonempty(v) and str(v).strip()]
    return " and ".join(filter(None, [", ".join(items[:-1]), items[-1]])) if items else ""


def _records_statement(ds: odml.Section) -> str:
    """Shared Project records statement, its {tokens} filled from the dataset (lists as prose)."""
    project = subsection(ds, "Project")
    statement = project and prop(project, "records statement")
    if not _nonempty(statement):
        return ""
    return fill_template(str(statement).strip(), lambda name: _prose_list(_values(ds, name))) or ""


def _curation_software(doc: odml.Document) -> str:
    """Curation software versions from the odML 'Curation Software' section, one
    sentence per purpose: "Validation: BIDS validator <version> with the BEP032
    schema (commit <commit>), NWB Inspector <version> and ... Conversion:
    fairephys <version> (commit <commit>), ..."; ", with uncommitted changes"
    follows the commit of a source that had them."""
    tools = subsection(doc, CURATION_SOFTWARE)
    by_purpose: dict[str, list[str]] = {}
    for tool in tools.sections if tools else []:
        name, version = prop(tool, "name"), prop(tool, "version")
        if not (_nonempty(name) and _nonempty(version)):
            continue
        text = f"{name} {version}"
        if commit := prop(tool, "commit"):
            changes = ", with uncommitted changes" if prop(tool, "uncommitted changes") is True else ""
            text += f" (commit {commit}{changes})"
        if schema := prop(tool, "schema"):
            commit = prop(tool, "schema commit")
            text += f" with the {schema} schema" + (f" (commit {commit})" if commit else "")
        by_purpose.setdefault(str(prop(tool, "purpose") or "software"), []).append(text)
    return " ".join(f"{_sentence_case(purpose)}: {_prose_list(items)}."
                    for purpose, items in by_purpose.items())


def _layout_standard(output_dir: Path) -> str:
    """Name the layout standard (BEP032 when there are ecephys files)."""
    from ..bids.builder import load_registry as load_bids_registry

    spec = load_bids_registry().get("spec", {})
    bids_version = spec.get("bids_version", "")
    if any(output_dir.rglob("*_ecephys.*")):
        return ("The data are organised following the BIDS extension proposal "
                f"{spec.get('ecephys_extension', 'BEP032')}, which builds on BIDS "
                f"{bids_version} and is not yet part of the BIDS specification.")
    return f"The data are organised following BIDS {bids_version}."


def _repository_name(ds: odml.Section) -> str:
    """The last path part of the repository url, else "repository root"."""
    url = str(prop(ds, "repository url") or "").strip()
    name = urlparse(url).path.rstrip("/").rpartition("/")[2] if url else ""
    return f"{name}/" if name else "repository root"


def _build_tree(root: Path, root_name: str, files: dict[str, dict], max_depth: int = 4) -> list[str]:
    """Build directory tree lines, collapsing repeated ses-* dirs; the first file
    of each file type is followed by its short description in brackets."""
    described: set[str] = set()

    def describe(path: Path) -> str:
        match = _file_type(path.relative_to(root))
        if match is None or match[0] not in files or match[0] in described:
            return ""
        described.add(match[0])
        return f" ({files[match[0]]['tree']})"

    lines = [root_name]
    _walk_tree(root, lines, indent="    ", depth=0, max_depth=max_depth,
               first_session_shown=False, describe=describe)
    return lines


def _walk_tree(directory: Path, lines: list[str], indent: str,
               depth: int, max_depth: int,
               first_session_shown: bool, describe) -> bool:
    """Recursively walk directory, appending tree lines.

    Returns updated first_session_shown flag.
    """
    if depth >= max_depth:
        return first_session_shown

    entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name))
    dirs = [e for e in entries if e.is_dir() and not e.name.startswith(".")]
    files = [e for e in entries if e.is_file() and not e.name.startswith(".")]

    # Separate session dirs from other dirs
    session_dirs = [d for d in dirs if d.name.startswith("ses-")]
    other_dirs = [d for d in dirs if not d.name.startswith("ses-")]

    # Show non-session dirs normally
    for d in other_dirs:
        lines.append(f"{indent}{d.name}/")
        first_session_shown = _walk_tree(
            d, lines, indent + "    ", depth + 1, max_depth,
            first_session_shown, describe)

    # Show session dirs: expand first, collapse rest
    if session_dirs:
        if not first_session_shown:
            # Expand first session
            first = session_dirs[0]
            lines.append(f"{indent}{first.name}/")
            _walk_tree(first, lines, indent + "    ", depth + 1,
                       max_depth, True, describe)
            first_session_shown = True
            remaining = session_dirs[1:]
        else:
            remaining = session_dirs

        if remaining:
            lines.append(f"{indent}... ({_count(len(remaining), 'more session')})")

    # Show files
    for f in files:
        lines.append(f"{indent}{f.name}{describe(f)}")

    return first_session_shown


def _file_type(relative: Path) -> tuple[str, dict] | None:
    """The file type of a path relative to the dataset root: the first readme
    registry entry whose pattern matches (the metadata folder filled in) and
    its key, the pattern (with the file's suffix for an entry per suffix)."""
    path = PurePosixPath(relative.as_posix())
    for entry in load_registry("readme.yaml")["files"]:
        pattern = entry["pattern"].replace("{metadata dir}", METADATA_DIRNAME)
        if path.match(pattern):
            return (pattern.removesuffix(".*") + path.suffix if entry.get("per suffix") else pattern), entry
    return None


def _tsv_columns(path: Path) -> list[str]:
    """Column names of a TSV's header row (none for an empty file)."""
    with path.open(encoding="utf-8") as f:
        return [c for c in f.readline().rstrip("\r\n").split("\t") if c]


def _json_keys(path: Path) -> list[str]:
    """Top-level keys of a JSON sidecar (none for an empty object); a file that
    is not a JSON object raises."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: a JSON sidecar holds an object, not a {type(data).__name__}")
    return list(data)


def _shared_and_more(names: list[list[str]]) -> tuple[str, str]:
    """The names all lists hold, then those only some hold, as code-span prose lists."""
    shared = [n for n in names[0] if all(n in h for h in names[1:])] if names else []
    more = dict.fromkeys(n for h in names for n in h if n not in shared)
    return _prose_list(f"`{n}`" for n in shared), _prose_list(f"`{n}`" for n in more)


def _file_types(doc: odml.Document, output_dir: Path) -> dict[str, dict]:
    """The file types present under output_dir, in registry order, as
    {key: {pattern, tree, format, content, specification}} (see readme.yaml files).

    The content of a TSV with a header row lists the columns all its files
    have, then those only some have (header rows only; an empty file has
    none), that of a JSON sidecar the keys in the same way (files with no
    entries counted, the empty content when none has any), the BIDS suffixes
    and an entity's labels are those of the file names, a stale file (older than
    the odML record beside it, --all without a KG token) takes the stale
    content; the specification names the software with the version the odML
    Curation Software section records.
    """
    from ..bids.builder import load_registry as load_bids_registry

    bids_version = str(load_bids_registry().get("spec", {}).get("bids_version", ""))
    tools = subsection(doc, CURATION_SOFTWARE)
    versions = {str(prop(t, "name")): str(prop(t, "version"))
                for t in (tools.sections if tools else []) if _nonempty(prop(t, "version"))}
    registry = load_registry("readme.yaml")["files"]
    found: dict[str, tuple[dict, list[Path]]] = {}
    for path in sorted(output_dir.rglob("*")):
        relative = path.relative_to(output_dir)
        if path.is_file() and not any(part.startswith(".") for part in relative.parts):
            if match := _file_type(relative):
                found.setdefault(match[0], (match[1], []))[1].append(path)

    types = {}
    for key, (entry, paths) in sorted(found.items(), key=lambda item: registry.index(item[1][0])):
        values = {"suffix": paths[0].suffix.lstrip(".").upper(), "bids version": bids_version,
                  "rows": entry.get("rows", "")}
        content = entry["content"]
        if "rows" in entry:
            values["columns"], values["more columns"] = _shared_and_more([h for p in paths if (h := _tsv_columns(p))])
        if "sidecar" in entry:
            held = [k for p in paths if (k := _json_keys(p))]
            values["sidecar"] = entry["sidecar"]
            values["keys"], values["more keys"] = _shared_and_more(held)
            if not held:
                content = entry["empty content"]
            elif len(held) < len(paths):
                values["no entries"] = f"{len(paths) - len(held)} of {len(paths)} files"
        if entry.get("suffixes"):
            values["suffixes"] = _prose_list(sorted({p.name.split(".")[0].rpartition("_")[2] for p in paths}))
        if "labels" in entry:
            values["labels"] = ", ".join(sorted({m.group(1) for p in paths
                                                 if (m := re.search(rf"_{entry['labels']}-([a-zA-Z0-9]+)_", p.name))}))
        if "stale content" in entry:
            built = datetime.fromtimestamp(paths[0].stat().st_mtime)
            if any(built < datetime.fromtimestamp(o.stat().st_mtime) for o in paths[0].parent.glob("*.odml.xml")):
                content, values["built"] = entry["stale content"], f"{built:%Y-%m-%d}"
        software = _prose_list(f"{name} {versions[name]}" if name in versions else name
                               for name in entry.get("software", []))
        specification = fill_template(entry["specification"], values.get) or ""
        types[key] = {"pattern": key, "tree": entry["tree"], "format": fill_template(entry["format"], values.get),
                      "content": fill_template(content.strip(), values.get),
                      "specification": f"{specification}, written with {software}" if software else specification}
    return types


def _how_to_cite(ds: odml.Section) -> str:
    """Format HOW TO CITE from 'how to cite' (generated by the compiler unless the YAML gives one)."""
    text = _prop_str(ds, "how to cite")
    return f"## How to Cite\n\n{text.strip()}\n" if text else ""


def _acknowledgements(ds: odml.Section) -> str:
    """Format ACKNOWLEDGEMENTS from Fundings.

    A funding without an acknowledgement reads "Funder, award title (award
    number)"; after a funder that ends with a bracket ("... (DFG)") the number
    follows a comma instead of a second bracket.
    """
    fundings = subsection(ds, "Fundings")
    if not fundings or not fundings.sections:
        return ""
    lines = []
    for sub in fundings.sections:
        ack = prop(sub, "acknowledgement")
        if _nonempty(ack):
            lines.append(str(ack))
        elif funder := prop(sub, "funder"):
            text = ", ".join(str(v) for v in (funder, prop(sub, "award title")) if v)
            lines.append(bracketed(text, str(prop(sub, "award number") or "")))
    if not lines:
        return ""
    return "## Acknowledgements\n\n" + "\n\n".join(lines) + "\n"


def _author_contributions(ds: odml.Section) -> str:
    """Format AUTHOR CONTRIBUTIONS from Contributors."""
    contributors = subsection(ds, "Contributors")
    if not contributors or not contributors.sections:
        return ""
    lines = []
    for sub in contributors.sections:
        given = prop(sub, "given name") or ""
        family = prop(sub, "family name") or ""
        if not (given or family):
            continue
        name = f"{given} {family}".strip()
        # the description and, after a comma, the role terms it does not name itself
        # (whole words, any case); the terms alone, capitalised, without a description
        roles = _values(sub, "roles") or _values(sub, "role")
        desc = str(prop(sub, "contribution description") or "").strip().rstrip(".;, ")
        unnamed = [str(r) for r in roles if _nonempty(r) and not re.search(
            rf"(?<![\w-]){re.escape(str(r))}(?![\w-])", desc, re.IGNORECASE)]
        text = ", ".join([desc, *unnamed]) if desc else _sentence_start(", ".join(unnamed))
        lines.append(f"**{name}:** {text}" if text else f"**{name}**")
    if not lines:
        return ""
    return "## Author Contributions\n\n" + "\n\n".join(lines) + "\n"


def _references(ds: odml.Section) -> str:
    """Format REFERENCES from related publications."""
    pubs = _values(ds, "related publications")
    if not pubs:
        return ""
    lines = []
    for i, pub in enumerate(pubs, 1):
        pub_str = str(pub)
        if pub_str.startswith("10.") or pub_str.startswith("https://doi.org/"):
            doi = pub_str
            if not doi.startswith("https://"):
                doi = f"https://doi.org/{doi}"
            lines.append(f"{i}. {doi}")
        else:
            lines.append(f"{i}. {pub_str}")
    if not lines:
        return ""
    return "## References\n\n" + "\n".join(lines) + "\n"


# Markdown the first-mention pass leaves as it is: fenced code, code spans, links and URLs
_PROTECTED_RE = re.compile(r"```.*?```|`[^`\n]*`|\[[^\]\n]*\]\([^)\s]*\)|https?://[^\s)]+", re.DOTALL)


def _first_plain_mention(text: str, term: str) -> re.Match | None:
    """The first mention of a term as a whole word outside protected markdown."""
    protected = [m.span() for m in _PROTECTED_RE.finditer(text)]
    for m in re.finditer(rf"(?<![\w-]){re.escape(term)}(?![\w-])", text):
        if not any(start <= m.start() < end for start, end in protected):
            return m
    return None


def _first_mentions(doc: odml.Document, text: str) -> str:
    """The first plain mention of each readme registry 'first use' term
    written out (unless the written-out form comes first), and of each
    software the registry 'software links' or the Curation Software section
    (code url) links, linked (unless the text already links it)."""
    registry = load_registry("readme.yaml")
    for term, written_out in registry["first use"].items():
        m = _first_plain_mention(text, term)
        if m and not (0 <= text.find(written_out) <= m.start()):
            text = text[:m.start()] + written_out + text[m.end():]
    tools = subsection(doc, CURATION_SOFTWARE)
    links = {**registry["software links"], **{str(prop(t, "name")): str(prop(t, "code url"))
                                              for t in (tools.sections if tools else []) if prop(t, "code url")}}
    for name, url in links.items():
        if f"[{name}](" not in text and (m := _first_plain_mention(text, name)):
            text = text[:m.start()] + f"[{name}]({url})" + text[m.end():]
    return text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def odml_to_readme(doc: odml.Document, output_dir: Path) -> str:
    """Generate EBRAINS Data Descriptor README from compiled odML.

    Args:
        doc: Compiled odML document.
        output_dir: Curated data directory (scanned for file tree).
    """
    ds = subsection(doc, "Dataset")
    if ds is None:
        raise ValueError("Dataset section not found in odML document")

    title = prop(ds, "full name") or "Dataset"
    # short name and version under the title, each when filled (EBRAINS descriptor template)
    header = " | ".join(f"{label}: {value}" for label, value in (
        ("Short name", prop(ds, "short name")), ("Version", prop(ds, "version identifier"))) if _nonempty(value))

    # Assemble sections, skipping empty ones
    parts = [f"# {title}\n"] + ([f"{header}\n"] if header else [])

    for section_fn in [
        lambda: _authors_section(ds),
        lambda: _corresponding_author(ds),
        lambda: _summary_section(ds),
        lambda: _version_section(ds),
    ]:
        text = section_fn()
        if text:
            parts.append(text)

    # Materials and Methods
    methods_parts = [
        _methods_subject(doc),
        _methods_ethics(ds),
        _methods_anatomy(doc),
        _methods_hardware(doc),
        _methods_protocol(doc),
        _methods_processing(doc, output_dir),
        _methods_sessions(doc),
    ]
    methods_content = "\n".join(p for p in methods_parts if p)
    if methods_content:
        parts.append(f"## Materials and Methods\n\n{methods_content}")

    for section_fn in [
        lambda: _usage_notes(ds),
        lambda: _data_records(doc, output_dir),
        lambda: _how_to_cite(ds),
        lambda: _acknowledgements(ds),
        lambda: _author_contributions(ds),
        lambda: _references(ds),
    ]:
        text = section_fn()
        if text:
            parts.append(text)

    return _first_mentions(doc, "\n".join(parts))


def write_readme(doc: odml.Document, output_dir: Path) -> Path:
    """Generate README from odML and write to output directory."""
    readme = odml_to_readme(doc, output_dir)
    output_path = output_dir / "README.md"
    output_path.write_text(readme)
    return output_path
