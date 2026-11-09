"""Special extraction functions for complex BIDS fields."""

import odml

from ..metadata.identifiers import related_publications
from ..metadata.provenance import CURATION_SOFTWARE, PIPELINE
from ..odml import section_at
from .resolver import section_to_dict


def bracketed(text: str, detail: str, sep: str = ", ") -> str:
    """"text (detail)" without doubled or nested brackets.

    When the text already ends with a bracket ("Deutsche Forschungsgemeinschaft
    (DFG)") or the detail holds one, the detail follows `sep` instead
    ("text, detail"). Shared by the BIDS fields and the README.
    """
    if not detail:
        return text
    if not text:
        return detail
    if text.rstrip().endswith(")") or "(" in detail:
        return f"{text}{sep}{detail}"
    return f"{text} ({detail})"


def authors(dataset: odml.Section) -> list[str]:
    """Extract author names from Dataset/Contributors."""
    try:
        return [f"{section_to_dict(c).get('given name', '')} {section_to_dict(c).get('family name', '')}".strip()
                for c in dataset["Contributors"].sections]
    except KeyError:
        return []


def funding(dataset: odml.Section) -> list[str]:
    """Extract funding sources from Dataset/Fundings as 'Funder (Grant ID)'."""
    try:
        result = []
        for f in dataset["Fundings"].sections:
            d = section_to_dict(f)
            funder, grant = d.get("funder", ""), d.get("grant id", "") or d.get("award number", "")
            result.append(bracketed(str(funder), str(grant)) if funder else "")
        return [r for r in result if r]
    except KeyError:
        return []


def acknowledgements(dataset: odml.Section, dataset_data: dict) -> str | None:
    """The dataset's acknowledgement and those of its fundings, as one text."""
    texts = [dataset_data.get("acknowledgement")]
    try:
        texts += [section_to_dict(f).get("acknowledgement") for f in dataset["Fundings"].sections]
    except KeyError:
        pass
    return " ".join(" ".join(str(t).split()) for t in texts if t) or None


def ethics_approvals(dataset_data: dict) -> list[str]:
    """Compose ethics approvals as 'Authority (approval ID)'."""
    auth = dataset_data.get("ethics authority", "")
    approval = dataset_data.get("ethics approval id", "")
    if auth and approval:
        return [bracketed(str(auth), f"approval {approval}")]
    return [auth] if auth else []


def references_and_links(dataset: odml.Section) -> list[str]:
    """Every Related Publications entry (its citation, followed by its link
    unless the citation holds the identifier), then the source dataset URLs."""
    refs = [p.reference for p in related_publications(dataset)]
    refs.extend(sd["URL"] for sd in source_datasets(dataset))
    return refs


def institution_info(doc: odml.Document) -> dict:
    """Extract institution info from Dataset/Custodian/affiliation."""
    try:
        aff = section_to_dict(doc["Dataset"]["Custodian"]["affiliation"])
        return {
            "InstitutionName": aff.get("institution"),
            "InstitutionAddress": aff.get("address"),
            "InstitutionalDepartmentName": aff.get("lab"),
        }
    except KeyError:
        return {}


def hardware_filters(section: odml.Section) -> dict | str:
    """The Hardware Filters listed by `section` (Hardware, or an auxiliary
    device) as BIDS HardwareFilters: ``{"Spike highpass": {"Type": "highpass",
    "Cutoff": 300}}``, or ``"n/a"`` without filter data."""
    filters = section_at(section, "Hardware Filters/Filters")
    if filters is None:
        return "n/a"

    result = {}
    for sec in filters.sections:
        d = section_to_dict(sec)
        name = d.pop("name", None)
        if not name:
            continue
        if entry := {k.title(): v for k, v in d.items() if v}:
            result[name] = entry
    return result or "n/a"


def stimulus_presentation(doc: odml.Document) -> dict:
    """Extract BIDS StimulusPresentation from odML Behavioral Protocol.

    Returns the dict of OperatingSystem, SoftwareName and SoftwareVersion
    that have a value (empty without the section or values).
    """
    try:
        sp = doc["Behavioral Protocol"]["Stimulus Presentation"]
    except KeyError:
        return {}
    data = section_to_dict(sp)
    fields = {"OperatingSystem": "operating system", "SoftwareName": "software name",
              "SoftwareVersion": "software version"}
    return {field: data[key] for field, key in fields.items() if data.get(key)}


def generated_by(doc: odml.Document) -> list[dict]:
    """BIDS GeneratedBy: the curation pipeline as recorded in the odML 'Curation Software' section.

    Name, Version, Description and CodeURL (the latter two when recorded). The
    Version adds the commit of the pipeline source as a PEP 440 local version
    ("0.1.0+abc1234", ".dirty" appended when the source had uncommitted
    changes). Without an entry for the pipeline, its name alone.
    """
    try:
        tool = section_to_dict(doc[CURATION_SOFTWARE][PIPELINE])
    except KeyError:
        return [{"Name": PIPELINE}]
    version = str(tool.get("version") or "")
    if version and (commit := tool.get("commit")):
        dirty = ".dirty" if tool.get("uncommitted changes") is True else ""
        version += f"{'.' if '+' in version else '+'}{commit}{dirty}"
    entry = {"Name": str(tool.get("name") or PIPELINE), "Version": version,
             "Description": tool.get("description"), "CodeURL": tool.get("code url")}
    return [{key: str(value) for key, value in entry.items() if value}]


def source_datasets(dataset: odml.Section) -> list[dict]:
    """Extract source dataset URLs from Dataset/Related Projects."""
    try:
        result = []
        for proj in dataset["Related Projects"].sections:
            d = section_to_dict(proj)
            if url := d.get("project url"):
                entry = {"URL": url}
                if name := d.get("project name"):
                    entry["Name"] = name
                result.append(entry)
        return result if result else []
    except KeyError:
        return []
