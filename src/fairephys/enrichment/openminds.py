"""openMINDS enrichment for odML documents.

Post-compilation step: walks the compiled odML document, sets property.reference
to the openMINDS property IRI for every mapped property, and resolves controlled
term values to openMINDS instances (returned separately for `.instances.yaml`).

Returns (warnings, instances) where instances is a nested dict:
  {section_name: {prop_name: {value: {instance_uri, definition?}}}}
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

import yaml
import odml

logger = logging.getLogger(__name__)

# The openMINDS schema version of the property IRIs and the term instances:
# the newest the installed openminds package ships ("latest": v4 with
# openminds 0.4.0), which the EBRAINS KG serves and the fairgraph export
# (export/) resolves against. Pin a version here ("v4") should the KG lag.
OPENMINDS_VERSION = "latest"
om = importlib.import_module(f"openminds.{OPENMINDS_VERSION}")

REGISTRY_DIR = Path(__file__).parent.parent / "_resources" / "registry"
SCHEMAS_PATH = REGISTRY_DIR / "openminds_schemas.yaml"
PROPS_BASE = "https://openminds.om-i.org/props/"


def load_schema_registry(path: Path = None) -> dict:
    """Load openminds_schemas.yaml registry."""
    path = path or SCHEMAS_PATH
    if not path.exists():
        raise FileNotFoundError(f"Schema registry not found: {path}")
    with open(path) as f:
        return yaml.safe_load(f)


def _resolve_class(class_path: str) -> Any | None:
    """Resolve 'controlled_terms.Species' to openMINDS class."""
    if not class_path:
        return None
    obj = om
    for part in class_path.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


def _build_reference_map(registry: dict) -> dict[str, str]:
    """Build {odml_property_name: property_iri} from registry.

    Each openMINDS class exposes a ``properties`` list with ``.path`` (camelCase).
    Combined with the ``@vocab`` base ``https://openminds.om-i.org/props/``,
    this gives us the canonical IRI for each property.
    """
    ref_map: dict[str, str] = {}

    for schema in registry.get("schemas", {}).values():
        fg_class = _resolve_class(schema.get("fairgraph_class", ""))
        if fg_class is None or not hasattr(fg_class, "properties"):
            continue
        prop_lookup = {p.name: p.path for p in fg_class.properties}

        field_groups = [(schema.get("fields", {}), prop_lookup)]
        for nested in schema.get("nested", {}).values():
            ncls = _resolve_class(nested.get("fairgraph_class", ""))
            if ncls and hasattr(ncls, "properties"):
                field_groups.append((
                    nested.get("fields", {}),
                    {p.name: p.path for p in ncls.properties},
                ))

        for fields, lookup in field_groups:
            for fg_name, fdef in fields.items():
                if not isinstance(fdef, dict):
                    continue
                odml_name = fdef.get("odml_field") or fdef.get("odml")
                if not odml_name:
                    continue
                leaf = odml_name.rsplit(".", 1)[-1]
                path = lookup.get(fg_name)
                if path and leaf not in ref_map:
                    ref_map[leaf] = PROPS_BASE + path

    return ref_map


def _build_term_map(registry: dict) -> dict[str, list]:
    """Build {odml_property_name: [resolved_term_classes]} from registry.

    Extracts all type:term and type:multi_term fields from schemas,
    resolving term_class paths to actual openMINDS classes.
    """
    term_map: dict[str, list] = {}

    for schema in registry.get("schemas", {}).values():
        # Collect term fields from main fields and nested fields
        all_fields = list(schema.get("fields", {}).values())
        for nested in schema.get("nested", {}).values():
            all_fields.extend(nested.get("fields", {}).values())

        for field_config in all_fields:
            if not isinstance(field_config, dict):
                continue
            field_type = field_config.get("type")
            if field_type not in ("term", "multi_term"):
                continue

            # Get odML property names and term class paths
            if field_type == "multi_term":
                prop_names = field_config.get("merge_fields", [])
                if not prop_names:
                    odml_field = field_config.get("odml_field", field_config.get("odml"))
                    prop_names = [odml_field] if odml_field else []
                class_paths = field_config.get("term_classes", [])
            else:
                odml_field = field_config.get("odml_field", field_config.get("odml"))
                if not odml_field:
                    continue
                prop_names = [odml_field]
                tc = field_config.get("term_class")
                class_paths = [tc] if tc else []

            resolved = [c for p in class_paths if (c := _resolve_class(p))]
            if not resolved:
                continue

            for name in prop_names:
                existing = term_map.setdefault(name, [])
                for cls in resolved:
                    if cls not in existing:
                        existing.append(cls)

    # instance_lookup schemas (e.g. License) - resolve by name
    for schema in registry.get("schemas", {}).values():
        il = schema.get("instance_lookup")
        if not il:
            continue
        from_odml = il.get("from_odml")
        fg_class = _resolve_class(schema.get("fairgraph_class", ""))
        if not from_odml or fg_class is None:
            continue
        existing = term_map.setdefault(from_odml, [])
        if fg_class not in existing:
            existing.append(fg_class)

    return term_map


def _lookup_term(value: str, term_classes: list, where: str) -> Any | None:
    """The first instance named `value` in the term classes; a class without
    an instance library (no by_name) is logged and skipped."""
    for cls in term_classes:
        try:
            result = cls.by_name(value)
        except AttributeError as exc:
            logger.warning(f"{where}: {cls.__name__} has no instance lookup for {value!r}: {exc}")
            continue
        except TypeError:  # openminds 0.4.0 by_name fails on len(None) when no instance matches
            continue
        if result:
            return result
    return None


def _walk_sections(parent):
    """Yield all sections recursively from document or section."""
    for section in parent.sections:
        yield section
        yield from _walk_sections(section)


def enrich_document(
    doc: odml.Document, registry: dict = None,
) -> tuple[list[str], dict]:
    """Enrich odML with property references and resolve controlled terms.

    * Sets ``property.reference`` to the openMINDS **property IRI** for every
      odML property that has a schema mapping (semantic field reference).
    * Resolves controlled-term values to openMINDS instances and collects them
      in a separate *instances* dict (written to ``.instances.yaml``).

    Returns:
        (warnings, instances) - *instances* is
        ``{section_name: {prop_name: {value: {instance_uri, definition?}}}}``.
    """
    registry = registry or load_schema_registry()
    warnings: list[str] = []
    instances: dict = {}
    ref_map = _build_reference_map(registry)
    term_map = _build_term_map(registry)

    for section in _walk_sections(doc):
        for prop in section.properties:
            # Property IRI (semantic reference)
            if prop.name in ref_map:
                prop.reference = ref_map[prop.name]

            # Instance resolution → instances dict (NOT property.reference)
            if prop.name not in term_map:
                continue
            term_classes = term_map[prop.name]
            sec_inst = instances.setdefault(section.name, {})
            prop_inst = sec_inst.setdefault(prop.name, {})
            for value in prop.values:
                if not value:
                    continue
                instance = _lookup_term(str(value), term_classes, f"{section.name}.{prop.name}")
                if instance:
                    entry = {"instance_uri": instance.id}
                    defn = getattr(instance, "definition", None)
                    if defn:
                        entry["definition"] = defn
                    prop_inst[str(value)] = entry
                else:
                    class_names = ", ".join(c.__name__ for c in term_classes)
                    warnings.append(
                        f"{section.name}.{prop.name}: "
                        f"'{value}' not found in {class_names}"
                    )

    return warnings, instances
