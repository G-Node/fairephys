"""Unified odML path resolution."""

from typing import Any
import odml


from ..odml import props as section_to_dict  # noqa: F401  (the properties of a section, by name)


def resolve(doc: odml.Document, path: str, sources: dict = None) -> Any:
    """Resolve odML path to value using pre-loaded source dicts.

    Args:
        doc: odML document (fallback for navigation)
        path: Path string or list of fallback paths
        sources: Pre-loaded dicts keyed by 'dataset', 'session', 'neo', 'run', 'rec_sys', 'probe', 'placement';
            'run' is the sidecar's run entry in the session's Neo Metadata 'runs';
            'subject' is the session's own subject section (see session_subject):
            "Subject/..." paths are navigated from it, so with several subjects
            each session resolves its own subject. Without it they are
            navigated from doc["Subject"] (one subject).
    """
    sources = sources or {}

    # Handle list of fallback paths
    if isinstance(path, list):
        return next((v for p in path if (v := resolve(doc, p, sources))), None)

    if not path or "." not in path:
        return None

    section_path, prop = path.rsplit(".", 1)

    # Match path prefix to source dict (more specific paths first)
    source_map = [
        ("Auxiliary Device", "aux_device"),
        ("Electrode Placements", "placement"),
        ("Neo Metadata/runs/*", "run"),
        ("Neo Metadata/stream", "stream"),
        ("Neo Metadata", "neo"),
        ("Recording System", "rec_sys"),
        ("Hardware/Probes/*", "probe"),
        ("Sessions/*", "session"),
        ("Dataset", "dataset"),
    ]

    for prefix, key in source_map:
        if prefix in section_path and key in sources:
            return sources[key].get(prop)

    # Fallback: navigate the document ("Subject/...": from the session's subject)
    parts = section_path.split("/")
    section = doc
    if parts[0] == "Subject" and sources.get("subject") is not None:
        section, parts = sources["subject"], parts[1:]
    try:
        for part in parts:
            section = section[part]
        prop_obj = next((p for p in section.properties if p.name == prop), None)
        return prop_obj.values[0] if prop_obj and prop_obj.values else None
    except (KeyError, TypeError, StopIteration):
        return None
