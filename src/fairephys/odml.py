"""Reading a compiled odML document: the few helpers every module shares.

A property's value is its single value, or the list of values when there are
several; a missing or empty property is None (or the given default).
"""

from __future__ import annotations

from typing import Any

import odml


def prop(section: odml.Section | None, name: str, default: Any = None) -> Any:
    """Value of the property `name` of `section`, `default` when absent or empty."""
    if section is None or name not in section.properties:
        return default
    values = section.properties[name].values
    if not values:
        return default
    return values[0] if len(values) == 1 else values


def unit(section: odml.Section | None, name: str) -> str | None:
    """Unit of the property `name` of `section`, None when absent."""
    if section is None or name not in section.properties:
        return None
    return section.properties[name].unit or None


def props(section: odml.Section) -> dict[str, Any]:
    """The properties of `section` that have values, as {name: value}."""
    return {p.name: (p.values if len(p.values) > 1 else p.values[0])
            for p in section.properties if p.values}


def nested(section: odml.Section) -> dict[str, Any]:
    """Every property of `section` ({name: value}, [] when empty) and its
    subsections, recursively, as {name: dict}."""
    data = {p.name: (p.values[0] if len(p.values) == 1 else p.values) for p in section.properties}
    data.update({sub.name: nested(sub) for sub in section.sections})
    return data


def subsection(section: odml.Section | odml.Document | None, name: str) -> odml.Section | None:
    """The subsection `name` of `section`, None when absent."""
    if section is None or name not in section.sections:
        return None
    return section.sections[name]


def section_at(root: odml.Section | odml.Document, path: str) -> odml.Section | None:
    """The section at a slash path ("Hardware/Probes"), None when any part is absent."""
    section = root
    for part in path.split("/"):
        section = subsection(section, part)
        if section is None:
            return None
    return section
