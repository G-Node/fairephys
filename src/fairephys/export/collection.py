"""Build an openMINDS Collection from odML using fairgraph.

Declarative: entity structure defined in openminds_schemas.yaml. With a KG
client, entities the EBRAINS KG already holds (Organizations, Persons, ...)
are looked up and reused; without one (kg_client=None) nothing is looked up
and everything is built anew, offline.

Usage:
    from fairephys.export import build_collection, get_kg_client
    client = get_kg_client()
    collection = build_collection(odml_path, client, target_space="my-space")
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
import odml
from fairgraph import Collection, IRI, KGClient
import fairgraph.openminds.core as fgcore
import fairgraph.openminds.controlled_terms as fgct
import fairgraph.openminds.ephys as fgephys
import fairgraph.openminds.sands as fgsands

from ..metadata.identifiers import related_publications
from ..metadata.sessions import session_identity
from ..metadata.subjects import session_subject, subject_sections
from ..odml import nested, prop, props, section_at, unit
from ..placements import matched_probes
from ..registry import REGISTRY_DIR, load as load_registry
from .anatomy import Anatomy, atlas_path, load_atlas
from .kg_lookup import KGResolver
from .templating import fill_template, machine_label

__all__ = ["SchemaBuilder", "KGResolver", "build_collection", "save_collection", "kg_space",
           "fill_template", "SCHEMAS_PATH", "NO_SPACE_HELP"]

logger = logging.getLogger(__name__)

_DOI_RE = re.compile(r"^(https?://doi\.org/)?10\.\d{4,}/")
# A bare DOI (no resolver prefix)
_BARE_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")

SCHEMAS_NAME = "openminds_schemas.yaml"
SCHEMAS_PATH = REGISTRY_DIR / SCHEMAS_NAME
# Collection metadata (Dataset section): the KG space of the pushes
KG_SPACE_FIELD = "kg space"
NO_SPACE_HELP = f"give --kg-space or set '{KG_SPACE_FIELD}' in the collection metadata"

# Units accepted by per_session_config.relative_time_unit (fixed length in days)
_DAYS_PER_UNIT = {"day": 1, "week": 7}
# Template token derived from the Dataset section (SchemaBuilder.dataset_citation)
DATASET_CITATION = "dataset citation"
# Registry field types by how build_field resolves them
_TERM_TYPES = ("term", "multi_term")
_LINK_TYPES = ("link", "match_link", "identifier_list", "custom_anatomy")


def _iri(val: Any) -> IRI:
    """IRI of a URL value, a bare DOI as its https://doi.org/ URL; any other
    value (e.g. a short citation) is an error, as it would be lost."""
    val = str(val).strip()
    if _BARE_DOI_RE.match(val):
        val = f"https://doi.org/{val}"
    try:
        return IRI(val)
    except ValueError as err:
        raise ValueError(f"Not an IRI (an http(s) URL or a DOI): '{val}'") from err


def _load_yaml(path: Path) -> dict:
    """Load YAML file, return empty dict if not found."""
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _as_list(value) -> list:
    """A plain property value as a list (None -> [])."""
    if value in (None, ""):
        return []
    return value if isinstance(value, list) else [value]


def _resolve_fg_class(class_path: str):
    """Resolve 'core.Subject' or 'controlled_terms.Species' to fairgraph class."""
    if not class_path or "." not in class_path:
        return None
    module, name = class_path.split(".", 1)
    modules = {"controlled_terms": fgct, "core": fgcore, "ephys": fgephys, "sands": fgsands}
    return getattr(modules.get(module), name, None)


def _postprocess(fdef: dict, val: Any) -> Any:
    """Apply registry value options to a plain odML field value.

    `basename: true` keeps the last path segment of a URL or path
    (e.g. a repository label derived from its IRI).
    """
    if fdef.get("basename") and isinstance(val, str):
        return val.rstrip("/").rsplit("/", 1)[-1]
    return val


def _parse_date(value: Any) -> date | None:
    """Parse an ISO date value; None when absent or invalid."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _get_nested(data: dict, path: str) -> Any:
    """Get value using dot path (e.g., 'affiliation.institution')."""
    for key in path.split("."):
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def _odml_field_name(fdef: dict, field: str, key: str = "odml") -> str:
    """Return the odML property name for a field definition."""
    return fdef.get(key, field.replace("_", " "))


def _check_condition(condition: str, data: dict, built: dict) -> bool:
    """Check condition for building nested entity.

    Uses truthiness (not just ``is not None``) so empty strings are treated
    as absent, preventing invalid entities like DOI with bare prefix.
    """
    if not condition:
        return True
    if condition in built:
        return built[condition] is not None
    if " == " in condition:
        path, expected = condition.split(" == ")
        return str(_get_nested(data, path.strip())) == expected.strip()
    return bool(_get_nested(data, condition))


def kg_space(doc: odml.Document) -> str | None:
    """The KG space the collection metadata names for the pushes (Dataset/kg space)."""
    return prop(section_at(doc, "Dataset"), KG_SPACE_FIELD) or None


class SchemaBuilder:
    """Build fairgraph entities from odML using openminds_schemas.yaml.

    All entity construction is registry-driven. Modes:

    single:  One entity from one odML section.
    multiple:  One entity per subsection (e.g., Contributors -> Person[]).
    per_subject:  One entity per subject section.
    per_session:  One entity per session using base fields from a different
        section. Supports date-based dedup.
        (Used by SubjectState.)
    per_session_nested:  One entity per placement of each session (the
        placements of placements.matched_probes). Returns list-of-lists for
        index-matched linking. (Used by ElectrodeArrayUsage.)

    Index-matched linking:
        When a target was built as a list (per-session), and the current entity
        is also per-session, link resolution matches by index position. This
        ensures RecordingActivity[i].inputs -> SubjectState[i].

    An entity that cannot be built, or a KG lookup that fails, is an error
    (a node built in its place would be wrong or duplicated at the push).
    """

    def __init__(self, doc: odml.Document, registry: dict, kg_client: KGClient | None = None,
                 target_space: str | None = None, atlas: dict | None = None):
        self.doc = doc
        self.schemas = registry.get("schemas", {})
        self.build_order = registry.get("build_order", [])
        self.entities = {}
        self._entity_kwargs: dict[str, list[dict]] = {}  # schema -> [kwargs] for match_link
        self.collection = Collection()
        # IRIs of entities referenced as-is under a `keep_existing` schema
        # policy; exposed on the Collection so push_collection can skip them.
        self.keep_existing_ids: set[str] = set()
        self.collection.keep_existing_ids = self.keep_existing_ids
        # KG nodes referenced as they are whose values differ from the odML
        # (the odML values do not reach them); reported with the collection
        self.kg_differences: list[str] = []
        self.collection.kg_differences = self.kg_differences
        # odML values with no openMINDS node (e.g. a related publication by
        # URL); reported with the collection
        self.warnings: list[str] = []
        self.collection.warnings = self.warnings
        self.collection.kg_space = kg_space(doc)
        # (class, existence query values) -> node: one node per identity without a KG id
        self._identity_nodes: dict[tuple, Any] = {}
        self.kg = KGResolver(kg_client)
        self.target_space = target_space
        self._current_multi_index = None
        self._current_subject = None  # subject being built in per_subject mode
        self._entity_keys: dict[str, list[tuple]] = {}  # per-session entities: (subject id, session)
        self._term_cache: dict[tuple[str, str], Any] = {}  # (class, value) -> result
        self.anatomy = Anatomy(self.collection, self.kg, atlas)

    def _should_rebuild(self, existing) -> bool:
        """True if we should rebuild locally (entity is ours or space unknown)."""
        if not self.target_space:
            return True  # no space info: always rebuild (offline mode)
        entity_space = getattr(existing, "space", None)
        if not entity_space:
            return True  # no space on entity: rebuild to be safe
        return entity_space == self.target_space

    def _should_reference(self, schema: dict, existing) -> bool:
        """True if an existing KG entity should be referenced as-is.

        Schemas flagged `keep_existing: true` (shared entities such as
        Project/Consortium, owned by the KG once created) are never rebuilt;
        otherwise only cross-space entities are referenced.
        """
        return bool(schema.get("keep_existing")) or not self._should_rebuild(existing)

    def _reference_existing(self, schema_name: str, schema: dict, existing,
                            kwargs: dict, data: dict | None = None):
        """Add an existing KG entity to the collection without rebuilding it.

        Its KG values stay as they are, so the fields where the odML differs
        are reported (see _kg_differences). `kwargs` are the values built
        from the odML, `data` the odML section as a dict (for the fields
        not built before the lookup).
        """
        self.collection.add(existing)
        self._store_kwargs(schema_name, kwargs)
        if schema.get("keep_existing"):
            self.keep_existing_ids.add(existing.id)
            reason = "keep_existing"
            logger.info("  Keeping existing %s %s (keep_existing)", schema_name, existing.id)
        else:
            reason = f"space '{getattr(existing, 'space', '?')}'"
            logger.info("  Referencing %s from space '%s'", schema_name, getattr(existing, "space", "?"))
        if diffs := self._kg_differences(schema, existing, kwargs, data):
            self.kg_differences.append(f"{schema_name} {existing.id} ({reason}): {'; '.join(diffs)}")
            logger.warning("  %s %s is referenced as it is (%s) and differs from the odML: %s",
                           schema_name, existing.id, reason, "; ".join(diffs))
        return existing

    def _kg_differences(self, schema: dict, existing, kwargs: dict, data: dict | None) -> list[str]:
        """Fields where a KG node referenced as it is differs from the odML.

        Compared: plain odML fields (text and numbers), and nested entities
        (an ORCID, an affiliation) that the odML gives and the KG node lacks.
        """
        diffs = []
        nested_defs = schema.get("nested", {})
        for field, fdef in schema.get("fields", {}).items():
            if not isinstance(fdef, dict):
                continue
            ftype = fdef.get("type", "odml")
            kg_value = getattr(existing, field, None)
            if ftype == "odml":
                local = kwargs.get(field)
                if local is None and data is not None:
                    local = _postprocess(fdef, data.get(_odml_field_name(fdef, field)))
                if local in (None, "") or isinstance(local, (list, dict)):
                    continue
                if not isinstance(kg_value, (str, int, float)) or str(kg_value).strip() != str(local).strip():
                    diffs.append(f"{field} is {kg_value!r} in the KG, {local!r} in the odML")
            elif ftype in ("nested_ref", "nested_list") and data is not None:
                if kg_value is not None and not (isinstance(kg_value, list) and not kg_value):
                    continue
                if any(self._nested_given(t, nested_defs, data) for t in _as_list(fdef.get("target"))):
                    diffs.append(f"{field} is missing in the KG, given in the odML")
        return diffs

    @staticmethod
    def _nested_given(name: str, nested_defs: dict, data: dict) -> bool:
        """True if the odML holds the values a nested entity is built from
        (its condition, followed through conditions naming other nested entities)."""
        seen = set()
        while name in nested_defs and name not in seen:
            seen.add(name)
            condition = nested_defs[name].get("condition")
            if condition not in nested_defs:
                return _check_condition(condition, data, {})
            name = condition
        return False

    @staticmethod
    def _set_space(entity, space: str | None) -> None:
        """Copy KG space onto a locally-built entity (best-effort).

        fairgraph exposes ``space`` as a read-only property on some types
        (e.g. Organization); push_collection assigns the target space at
        upload time for those.
        """
        if not space:
            return
        try:
            entity.space = space
        except (AttributeError, TypeError):
            pass

    def _resolve_search_space(self, schema_or_defn: dict) -> str | None:
        """Resolve search_space config to actual space name for KG query."""
        space = schema_or_defn.get("kg_lookup", {}).get("search_space")
        if space == "target":
            return self.target_space  # None in offline mode: no filter
        return space or None  # a named space (e.g. "common"), else every readable space

    @staticmethod
    def _reuse(existing, kwargs: dict) -> str | None:
        """Give kwargs the KG id of an existing entity; returns its space."""
        if not existing:
            return None
        kwargs["id"] = existing.id
        return getattr(existing, "space", None)

    def _reuse_kg_id(self, fg_class, kg_lookup: dict | None, kwargs: dict, schema: dict) -> str | None:
        """Look up entity in KG and reuse its ID. Returns existing space."""
        if not kg_lookup:
            return None
        existing = self.kg.lookup_kg(fg_class, kg_lookup.get("fields", []), kwargs,
                                     search_space=self._resolve_search_space(schema))
        return self._reuse(existing, kwargs)

    @staticmethod
    def _merge_into(node, kwargs: dict):
        """Merge kwargs into an already built node with the same KG id.

        List-valued properties become the union (existing items first,
        deduplicated by KG id or equality); scalar properties are only
        filled in where the node has no value yet. Returns the node.
        """
        def _same(a, b):
            """Identity by KG id; embedded metadata (no id) compared property-wise.

            Plain `a == b` is avoided for nodes: openminds implements it via a
            JSON-LD export that raises for linked objects without an @id.
            """
            ida, idb = getattr(a, "id", None), getattr(b, "id", None)
            if ida or idb:
                return ida == idb
            names = [p.name for p in getattr(type(a), "properties", [])]
            if not names:
                return a == b
            return type(a) is type(b) and all(
                _same(getattr(a, p, None), getattr(b, p, None)) for p in names)

        for key, value in kwargs.items():
            if key == "id":
                continue
            current = getattr(node, key, None)
            if isinstance(value, list) or isinstance(current, list):
                merged = list(current or [])
                for item in (value if isinstance(value, list) else [value]):
                    if not any(_same(item, m) for m in merged):
                        merged.append(item)
                setattr(node, key, merged)
            elif current in (None, "", []):
                setattr(node, key, value)
        return node

    def _build_entity(self, fg_class, kwargs: dict, existing_space: str | None, add: bool,
                      label: str, existence_query: list | None = None) -> Any:
        """Construct entity, set space, add to collection.

        One KG identity is one node: if an entity with the same KG id was
        already built in this collection (e.g. a PI appearing both as
        Custodian and as Contributor), the new values are merged into that
        node instead of creating a second object. Otherwise the Collection
        would keep whichever object was added last, losing the other's
        properties (such as an additional Affiliation).

        Without a KG id (offline, or not yet in the KG), one identity is one
        node as well: an entity whose existence query values (fairgraph's or
        the registry's `existence_query`, e.g. a Person's given and family
        name, an Organization's full name, an ORCID's identifier) equal
        those of a node built before is merged into that node, as fairgraph
        would match both to one KG node at the push.

        `existence_query` (registry) replaces the fields fairgraph matches at
        push to find an existing node for an entity without a KG id, when all
        of them have a value. An entity fairgraph cannot construct is an
        error, named by `label` (the schema).
        """
        existing_node = self.collection.nodes.get(kwargs.get("id")) if kwargs.get("id") else None
        if existing_node is not None and isinstance(existing_node, fg_class):
            logger.debug("  Merging %s into existing node %s", label, existing_node.id)
            return self._merge_into(existing_node, kwargs)
        identity = self._identity_key(fg_class, kwargs, existence_query)
        if (same := self._identity_nodes.get(identity)) is not None:
            logger.debug("  Merging %s into the node of the same identity %s", label, identity)
            if add and same.id is None:
                self.collection.add(same)
            return self._merge_into(same, kwargs)
        try:
            entity = fg_class(**kwargs)
        except (TypeError, ValueError, NameError) as e:
            raise ValueError(f"{label}: cannot build {getattr(fg_class, '__name__', fg_class)}: {e}") from e
        if existing_space:
            self._set_space(entity, existing_space)
        if existence_query and not kwargs.get("id") and all(kwargs.get(f) for f in existence_query):
            entity.existence_query_properties = tuple(existence_query)
        if add:
            self.collection.add(entity)
        if identity:
            self._identity_nodes[identity] = entity
        return entity

    @staticmethod
    def _identity_key(fg_class, kwargs: dict, existence_query: list | None = None) -> tuple | None:
        """(class, values) of an entity without KG id by the fields that
        identify it at the push (the registry's existence query, else
        fairgraph's); None when one of them is missing or not a plain value."""
        fields = tuple(existence_query or getattr(fg_class, "existence_query_properties", None) or ())
        if not fields or kwargs.get("id"):
            return None
        values = [kwargs.get(f) for f in fields]
        if not all(isinstance(v, (str, int, float, date)) and v != "" for v in values):
            return None
        return (fg_class.__name__, *map(str, values))

    def render_template(self, template: str, section: odml.Section | None,
                        context: dict | None = None) -> str | None:
        """Fill "{token}" placeholders of a registry template from odML.

        Resolution order per token: a key of `context` (per-session values
        such as session_id), "dataset citation" (dataset_citation), an
        absolute odML path when the token contains
        "/" ("{Dataset/short name}"), else a property of `section`
        ("{subject id}"). A missing, empty or multi-valued token makes the
        whole result None so the field is skipped; optional "{?token}"s
        render as empty text (see fill_template).
        """
        def lookup(name):
            if context and name in context:
                return context[name]
            if name == DATASET_CITATION:
                return self.dataset_citation()
            if "/" in name:
                sec_path, name = name.rsplit("/", 1)
                return prop(self.get_section(sec_path), name)
            return prop(section, name)

        return fill_template(template, lookup)

    def dataset_citation(self) -> str | None:
        """The dataset's short citation, which qualifies the paradigm name:
        "<family name> et al., <year>" from the first of the Dataset's
        Contributors (the DataCite authors, in order), "<family name>, <year>"
        with one. The year is that of the Dataset's `release date` (a first
        publication elsewhere), else the release year of this export (the
        citation's build year); the DatasetVersion's release_date follows the
        same rule. None
        without contributors or a family name of the first."""
        ds = self.get_section("Dataset")
        authors = [s for s in ds["Contributors"].sections if s.name.startswith("Contributors_")] \
            if ds is not None and "Contributors" in ds.sections else []
        family = str(prop(authors[0], "family name") or "").strip() if authors else ""
        if not family:
            return None
        released = prop(ds, "release date")
        year = date.fromisoformat(str(released)).year if released else date.today().year
        return f"{family}{' et al.' if len(authors) > 1 else ''}, {year}"

    def get_section(self, path: str | None) -> odml.Section | None:
        """The odML section at a slash path, None without a path or section."""
        return section_at(self.doc, path) if path else None

    def resolve_term(self, term_class: str, value: str):
        """Resolve controlled term with caching.

        Caches results to avoid repeated exception handling for unknown terms
        and to deduplicate term instances across the collection.
        """
        if not value or not term_class:
            return None
        key = (term_class, value)
        if key in self._term_cache:
            return self._term_cache[key]
        cls = _resolve_fg_class(term_class)
        if not cls:
            self._term_cache[key] = None
            return None
        try:
            result = cls.by_name(value)
        except (KeyError, ValueError, TypeError):  # fairgraph: unknown name
            result = None
        if not result:
            logger.debug("  Term not found: %s '%s'", term_class, value)
        self._term_cache[key] = result
        return result

    def resolve_terms(self, term_class: str, values) -> list:
        """Resolve multiple controlled terms."""
        return [t for v in _as_list(values) if (t := self.resolve_term(term_class, v))]

    def resolve_multi_term(self, term_classes: list[str], values, fallback_class: str | None = None) -> list:
        """Resolve values against multiple vocabularies. First match wins per value.

        When *fallback_class* is set, unresolved values become instances of that
        class (e.g. ``controlled_terms.TermSuggestion``) instead of being dropped.
        """
        resolved = []
        for val in _as_list(values):
            if result := next((r for tc in term_classes if (r := self.resolve_term(tc, val))), None):
                resolved.append(result)
            elif fallback_class:
                cache_key = (fallback_class, val)
                if cache_key not in self._term_cache:
                    fg_cls = _resolve_fg_class(fallback_class)
                    if not fg_cls:
                        continue
                    self._term_cache[cache_key] = fg_cls(name=val)
                    self.collection.add(self._term_cache[cache_key])
                    logger.info("  TermSuggestion fallback: '%s'", val)
                resolved.append(self._term_cache[cache_key])
        return resolved

    def build_nested(self, name: str, defn: dict, data: dict, built: dict) -> Any:
        """Build nested entity with optional KG lookup."""
        if not _check_condition(defn.get("condition"), data, built):
            return None
        fg_class = _resolve_fg_class(defn.get("fairgraph_class"))
        if not fg_class:
            return None

        kwargs = {}
        for field, fdef in defn.get("fields", {}).items():
            if not isinstance(fdef, dict):
                continue
            ftype = fdef.get("type", "odml")
            if ftype == "nested_ref":
                if built.get(fdef.get("target")):
                    kwargs[field] = built[fdef["target"]]
            elif ftype == "literal":
                if fdef.get("value") is not None:
                    kwargs[field] = fdef["value"]
            elif ftype == "iri":
                val = fdef.get("value") or _get_nested(data, _odml_field_name(fdef, field))
                if val:
                    kwargs[field] = _iri(val)
            elif ftype == "link":
                # Top-level entity built earlier in build_order (no odML access needed)
                if (val := self.build_field(field, fdef, None)) is not None:
                    kwargs[field] = val
            else:
                val = _get_nested(data, _odml_field_name(fdef, field))
                if val is not None:
                    prefix = fdef.get("prefix", "")
                    if prefix and "doi.org" in prefix and not _DOI_RE.match(str(val)):
                        logger.debug("  Skipping invalid DOI value: '%s'", val)
                        continue
                    kwargs[field] = f"{prefix}{val}" if prefix else val
        if not kwargs:
            return None

        add = defn.get("add_to_collection", True)
        existing_space = None
        if kg_lookup := defn.get("kg_lookup"):
            existing = self.kg.lookup_kg(fg_class, kg_lookup.get("fields", []), kwargs,
                                         search_space=self._resolve_search_space(defn))
            if existing and not self._should_rebuild(existing):
                if add:
                    self.collection.add(existing)
                return existing
            existing_space = self._reuse(existing, kwargs)  # ours: rebuilt with its id
        return self._build_entity(fg_class, kwargs, existing_space, add, f"nested {name}",
                                  defn.get("existence_query"))

    def _lookup_kwargs(self, schema: dict, data: dict, section: odml.Section | None) -> dict:
        """The values of the schema's kg_lookup fields, built from the odML."""
        kwargs = {}
        for field in schema.get("kg_lookup", {}).get("fields", []):
            fdef = schema.get("fields", {}).get(field)
            if not isinstance(fdef, dict):
                continue
            if fdef.get("type") == "template":
                val = self.build_field(field, fdef, section) if section else None
            else:
                val = _postprocess(fdef, data.get(_odml_field_name(fdef, field)))
            if val is not None:
                kwargs[field] = val
        return kwargs

    def _nested_kwargs(self, schema: dict, data: dict, section: odml.Section | None) -> dict:
        """kwargs of an entity with nested entities: the nested entities (built
        first, in registry order) for nested_ref and nested_list fields, plain
        odML fields from `data`, every other field type from `section`."""
        built: dict = {}
        for name, defn in schema.get("nested", {}).items():
            built[name] = self.build_nested(name, defn, data, built)
        kwargs = {}
        for field, fdef in schema.get("fields", {}).items():
            if not isinstance(fdef, dict):
                continue
            ftype = fdef.get("type", "odml")
            if ftype == "nested_ref":
                if built.get(fdef.get("target")):
                    kwargs[field] = built[fdef["target"]]
            elif ftype == "nested_list":
                if vals := [built[t] for t in _as_list(fdef.get("target")) if built.get(t)]:
                    kwargs[field] = vals
            elif ftype != "odml":
                if section and (val := self.build_field(field, fdef, section)) is not None:
                    kwargs[field] = val
            else:
                val = _postprocess(fdef, data.get(_odml_field_name(fdef, field)))
                if val is None and fdef.get("default"):
                    val = fdef.get("default")
                if val is not None:
                    kwargs[field] = val
            if field not in kwargs and (fallback := fdef.get("fallback")) and section is not None:
                if (val := self.build_field(field, fallback, section)) is not None:
                    kwargs[field] = val
        return kwargs

    def build_with_nested(self, schema_name: str, schema: dict, data: dict,
                          section: odml.Section = None) -> Any:
        """Build entity with nested dependencies.

        KG lookup is attempted first using only the lookup fields. Space-aware
        behavior: cross-space entities are referenced as-is, while entities in
        our target space are rebuilt locally with the KG ID to pick up the
        latest metadata (ContactInfo, keywords, etc.).
        """
        fg_class = _resolve_fg_class(schema.get("fairgraph_class"))
        if not fg_class:
            return None
        existing = None
        if kg_lookup := schema.get("kg_lookup"):
            lookup_kwargs = self._lookup_kwargs(schema, data, section)
            existing = self.kg.lookup_kg(fg_class, kg_lookup.get("fields", []), lookup_kwargs,
                                         search_space=self._resolve_search_space(schema))
            if existing and self._should_reference(schema, existing):
                return self._reference_existing(schema_name, schema, existing, lookup_kwargs, data)
        kwargs = self._nested_kwargs(schema, data, section)
        if not kwargs:
            return None
        self._store_kwargs(schema_name, kwargs)
        existing_space = self._reuse(existing, kwargs)  # ours: rebuilt with its id
        return self._build_entity(fg_class, kwargs, existing_space, schema.get("add_to_collection", True),
                                  schema_name, schema.get("existence_query"))

    def build_field(self, field: str, fdef: dict, section: odml.Section | None) -> Any:
        """Value of one field of `section` by the registry type of `fdef`
        (None when the odML gives nothing)."""
        ftype = fdef.get("type", "odml")
        if ftype in _TERM_TYPES:
            return self._term_field(field, fdef, section)
        if ftype in _LINK_TYPES:
            return self._link_field(fdef, section)
        return self._value_field(field, fdef, section)

    def _value_field(self, field: str, fdef: dict, section: odml.Section | None) -> Any:
        """A field of a plain value: literal, template, iri, today, datetime,
        odml (the property, a list when `list`), quantity."""
        ftype = fdef.get("type", "odml")
        if ftype == "literal":
            return fdef.get("value")
        if ftype == "template":
            text = self.render_template(fdef.get("value", ""), section, self._session_context(section))
            return machine_label(text) if text and field == "lookup_label" else text
        if ftype == "iri":
            val = fdef.get("value") or prop(section, fdef.get("odml", ""))
            return _iri(val) if val else None
        if ftype == "today":
            stated = prop(section, fdef["odml"]) if fdef.get("odml") else None
            return date.fromisoformat(str(stated)) if stated else date.today()
        if ftype == "datetime":
            val = prop(section, _odml_field_name(fdef, field))
            try:
                return datetime.fromisoformat(str(val)) if val else None
            except ValueError:
                return None
        if ftype == "odml":
            val = _postprocess(fdef, prop(section, _odml_field_name(fdef, field), fdef.get("default")))
            return [val] if fdef.get("list") and val and not isinstance(val, list) else val
        if ftype == "quantity":
            return self._quantity(section, _odml_field_name(fdef, field, "odml_field"))
        return None

    @staticmethod
    def _quantity(section: odml.Section | None, name: str) -> fgcore.QuantitativeValue | None:
        """The property's first value with its unit, None when empty."""
        values = _as_list(prop(section, name))
        if not values or not values[0]:
            return None
        unit_name = unit(section, name)
        return fgcore.QuantitativeValue(
            value=values[0], unit=fgct.UnitOfMeasurement.by_name(unit_name) if unit_name else None)

    def _term_field(self, field: str, fdef: dict, section: odml.Section | None) -> Any:
        """A controlled-term field: term (one vocabulary, the registry value
        or the property), multi_term (several vocabularies, first match wins;
        merge_fields collects several properties, exclude_fields drops terms
        given in other properties, fallback_class keeps unresolved values)."""
        name = _odml_field_name(fdef, field, "odml_field")
        if fdef.get("type") == "term":
            val = fdef.get("value") or prop(section, name)
            tc = fdef.get("term_class")
            return (self.resolve_terms(tc, val) or None) if fdef.get("list") else self.resolve_term(tc, val)
        tcs, fallback = fdef.get("term_classes", []), fdef.get("fallback_class")
        if merge_fields := fdef.get("merge_fields"):
            values = [v for mf in merge_fields for v in _as_list(prop(section, mf))]
            return self.resolve_multi_term(tcs, values, fallback) or None
        terms = self.resolve_multi_term(tcs, prop(section, name), fallback)
        if exclude := fdef.get("exclude_fields"):
            other = [v for ef in exclude for v in _as_list(prop(section, ef))]
            taken = {getattr(t, "name", None) for t in self.resolve_multi_term(tcs, other)}
            terms = [t for t in terms if getattr(t, "name", None) not in taken]
        return terms or None

    def _link_field(self, fdef: dict, section: odml.Section | None) -> Any:
        """A field linking other entities: link (built earlier), match_link
        (by a property's value), identifier_list, custom_anatomy."""
        ftype = fdef["type"]
        if ftype == "identifier_list":
            return self._identifier_list(fdef, section)
        if ftype == "custom_anatomy":
            return self.anatomy.build(fdef, props(section)) if section is not None else None
        if ftype == "match_link":
            return self._matched_entity(fdef, prop(section, fdef.get("match_field", "")))
        target, is_list = fdef.get("target"), fdef.get("list", False)
        if target not in self.entities:
            return None
        linked = self.entities[target]
        if not isinstance(linked, list):
            return [linked] if is_list else linked
        if match := fdef.get("match"):
            return self._matched_links(target, linked, match, section, is_list)
        # Index-matched linking: a per-session target (list) while iterating
        # in multiple mode links to the entry of the same index
        idx = self._current_multi_index
        if idx is not None and idx < len(linked):
            item = linked[idx]
            if is_list:
                return item if isinstance(item, list) else [item]  # list-of-lists: usages per session
            return item
        # KGObjects define __eq__ but not __hash__ (unhashable): deduplicate by identity
        return list({id(e): e for e in linked}.values()) if is_list else (linked[0] if linked else None)

    def _matched_entity(self, fdef: dict, match_value: Any) -> Any:
        """The entity of fdef's target whose build kwargs (locally built values,
        since KG-fetched entities may have proxy attributes) or attributes
        named in match_attrs equal `match_value`; None without a match."""
        if not match_value:
            return None
        target_name = fdef.get("target", "")
        targets = self.entities.get(target_name, [])
        if not isinstance(targets, list):
            targets = [targets]
        target_kwargs = self._entity_kwargs.get(target_name, [])
        for i, entity in enumerate(targets):
            kw = target_kwargs[i] if i < len(target_kwargs) else {}
            for attr in fdef.get("match_attrs", ["name"]):
                if kw.get(attr) == match_value or getattr(entity, attr, None) == match_value:
                    return entity
        logger.warning("  No %s matching %s='%s'", target_name, fdef.get("match_field"), match_value)
        return None

    def _identifier_list(self, fdef: dict, section: odml.Section | None) -> Any:
        """The identifier nodes of the entries of fdef's odml_section (see
        fairephys.metadata.identifiers), each as the node `classes` names for
        its identifier type, with the identifier by the class's pattern;
        `relation` keeps the entries of that relation, `single` the first
        node. Entries of a type without a class are listed in the collection
        warnings by a list field (a single field picks among the entries a
        list field reports), as are entries without identifier. A node built before (e.g. the dataset DOI) is
        the same node."""
        name, classes = fdef["odml_section"], fdef.get("classes", {})
        nodes = []
        for entry in related_publications(section, name):
            if (relation := fdef.get("relation")) and entry.relation != relation:
                continue
            if entry.identifier is None:
                if not fdef.get("single"):
                    self.warnings.append(f"{name} entry '{entry.citation}': no identifier; not in the collection")
                    logger.warning("  %s", self.warnings[-1])
                continue
            kind = entry.identifier.type
            if kind not in classes:
                if not fdef.get("single"):
                    self.warnings.append(f"{name} entry '{entry.given}': openMINDS has no node for a {kind} here "
                                         f"(only {', '.join(classes)}); not in the collection")
                    logger.warning("  %s", self.warnings[-1])
                continue
            spec = classes[kind]
            node = self._build_entity(_resolve_fg_class(spec["class"]),
                                      {"identifier": spec["identifier"].format(entry.identifier.value)},
                                      None, True, kind)
            if not any(n is node for n in nodes):
                nodes.append(node)
        if fdef.get("single"):
            return nodes[0] if nodes else None
        return nodes or None

    def _store_kwargs(self, schema_name: str, kwargs: dict) -> None:
        """Store kwargs used during entity build for match_link resolution."""
        self._entity_kwargs.setdefault(schema_name, []).append(kwargs)

    def build_simple(self, schema_name: str, schema: dict, section: odml.Section = None) -> Any:
        """Build simple entity without nested dependencies."""
        fg_class = _resolve_fg_class(schema.get("fairgraph_class"))
        if not fg_class:
            return None
        kwargs = {f: v for f, fdef in schema.get("fields", {}).items()
                  if isinstance(fdef, dict) and (v := self.build_field(f, fdef, section)) is not None}
        if not kwargs:
            return None
        existing_space = None
        if kg_lookup := schema.get("kg_lookup"):
            existing = self.kg.lookup_kg(fg_class, kg_lookup.get("fields", []), kwargs,
                                         search_space=self._resolve_search_space(schema))
            if existing and self._should_reference(schema, existing):
                return self._reference_existing(schema_name, schema, existing, kwargs)
            existing_space = self._reuse(existing, kwargs)
        self._store_kwargs(schema_name, kwargs)
        return self._build_entity(fg_class, kwargs, existing_space, schema.get("add_to_collection", True),
                                  schema_name, schema.get("existence_query"))

    def lookup_instance(self, schema: dict, lookup: dict) -> Any:
        """Look up standard openMINDS instance."""
        fg_class = _resolve_fg_class(schema.get("fairgraph_class"))
        if not fg_class:
            return None
        name = lookup.get("name")
        if not name and lookup.get("from_odml"):
            name = prop(self.get_section(schema.get("odml_section")), lookup["from_odml"])
        if name and (entity := self.kg.lookup_instance(fg_class, name)):
            self.collection.add(entity)
            return entity
        return None

    def _session_context(self, section: odml.Section | None) -> dict | None:
        """Template context of a session section: session_id, subject_id and
        task (see session_identity)."""
        parent = getattr(section, "parent", None)
        if section is None or getattr(parent, "name", None) != "Sessions":
            return None
        return session_identity(self.doc, section)

    def _matched_links(self, target: str, linked: list, match: str,
                       section: odml.Section | None, is_list: bool) -> Any:
        """Per-session entities of `target` for the session (match: session)
        or the subject (match: subject) being built."""
        keys = self._entity_keys.get(target, [])
        if match == "session":
            found = [e for e, (_, ses) in zip(linked, keys) if ses is section]
        elif match == "subject":
            sid = str(prop(self._current_subject, "subject id"))
            found = [e for e, (subj, _) in zip(linked, keys) if str(subj) == sid]
        else:
            logger.warning("  Unknown link match '%s' for %s", match, target)
            return None
        found = list({id(e): e for e in found}.values())
        return found if is_list else (found[0] if found else None)

    def build_per_subject(self, schema_name: str, schema: dict) -> list | None:
        """One entity per subject: the entries of 'Subjects', else 'Subject'."""
        entities = []
        for subject in subject_sections(self.doc):
            self._current_subject = subject
            if entity := self.build_simple(schema_name, schema, subject):
                entities.append(entity)
        self._current_subject = None
        return entities or None

    def _per_session_base(self, schema: dict, base_section: odml.Section | None) -> dict:
        """Shared kwargs of one base section."""
        base_kwargs = {}
        for field, fdef in schema.get("fields", {}).items():
            if not isinstance(fdef, dict):
                continue
            if (val := self.build_field(field, fdef, base_section)) is not None:
                base_kwargs[field] = val
        return base_kwargs

    def _base_section(self, base_name: str | None, session: odml.Section) -> odml.Section | None:
        """The base section of a session: its own subject for "Subject", else the named section."""
        if base_name == "Subject":
            return session_subject(self.doc, session)
        return self.get_section(base_name)

    def _session_kwargs(self, schema: dict, base_kwargs: dict, base_section: odml.Section | None,
                        session: odml.Section, session_date: date | None,
                        first_date: date | None, relative_unit: str | None) -> dict:
        """kwargs of one session's entity: the base kwargs, the template fields
        rendered with the session context (in registry order, so a label
        template can reference an identifier rendered just before it) and the
        relative time since the subject's first dated session."""
        kwargs = dict(base_kwargs)
        ctx = {**session_identity(self.doc, session),
               "date_compact": session_date.strftime("%y%m%d") if session_date else ""}
        for field, fdef in schema.get("fields", {}).items():
            if isinstance(fdef, dict) and fdef.get("type") == "template":
                ctx["internal_identifier"] = kwargs.get("internal_identifier", "")
                val = self.render_template(fdef.get("value", ""), base_section, ctx)
                if val is not None:
                    kwargs[field] = machine_label(val) if field == "lookup_label" else val
        if relative_unit and session_date and first_date:
            value = (session_date - first_date).days / _DAYS_PER_UNIT[relative_unit]
            kwargs["relative_time_indication"] = fgcore.QuantitativeValue(
                value=int(value) if value.is_integer() else value,
                unit=fgct.UnitOfMeasurement.by_name(relative_unit))
        return kwargs

    def build_per_session(self, schema_name: str, schema: dict) -> list | None:
        """Build per-session entities from registry config.

        Pattern: read base fields from one section, iterate over Sessions to
        create one entity per session. With base_section "Subject" the base
        is the session's own subject (several subjects: matched through the
        session's "subject"). Sessions of one subject with the same dedup_by
        value share one entity. per_session_config: session_date_field;
        relative_time_unit (day or week) sets relative_time_indication = time since the subject's
        earliest dated session (undated sessions get none). Records
        (subject id, session) per entity for matched links.
        """
        fg_class = _resolve_fg_class(schema.get("fairgraph_class"))
        sessions = self.get_section("Sessions")
        if not fg_class or not sessions:
            return None
        ps_config = schema.get("per_session_config", {})
        relative_unit = ps_config.get("relative_time_unit")
        if relative_unit and relative_unit not in _DAYS_PER_UNIT:
            logger.warning("  %s: unsupported relative_time_unit '%s' (supported: %s)",
                           schema_name, relative_unit, ", ".join(_DAYS_PER_UNIT))
            relative_unit = None
        date_field = ps_config.get("session_date_field", "date")
        rows = [(ses, self._base_section(schema.get("base_section"), ses), _parse_date(prop(ses, date_field)))
                for ses in sessions.sections]
        first_date: dict[int, date] = {}
        for _, base, d in rows:
            if d and (id(base) not in first_date or d < first_date[id(base)]):
                first_date[id(base)] = d

        bases: dict[int, dict] = {}
        dedup_map: dict[str, Any] = {}
        entities, keys = [], []
        for session, base_section, session_date in rows:
            if id(base_section) not in bases:
                bases[id(base_section)] = self._per_session_base(schema, base_section)
            key = (prop(base_section, "subject id"), session)
            dedup_value = prop(session, schema["dedup_by"]) if schema.get("dedup_by") else None
            dedup_key = f"{key[0]}|{dedup_value}" if dedup_value else None
            if dedup_key and dedup_key in dedup_map:
                entities.append(dedup_map[dedup_key])
                keys.append(key)
                continue
            kwargs = self._session_kwargs(schema, bases[id(base_section)], base_section, session,
                                          session_date, first_date.get(id(base_section)), relative_unit)
            existing_space = self._reuse_kg_id(fg_class, schema.get("kg_lookup"), kwargs, schema)
            entity = self._build_entity(fg_class, kwargs, existing_space, schema.get("add_to_collection", True),
                                        schema_name, schema.get("existence_query"))
            if dedup_key:
                dedup_map[dedup_key] = entity
            entities.append(entity)
            keys.append(key)
        self._entity_keys[schema_name] = keys
        return entities or None

    def _placement_field(self, field: str, fdef: dict, device: odml.Section, values: dict) -> Any:
        """A field of a placement's entity: match_link links the device (by
        its exact name), custom_anatomy builds the region's entity, other
        fields read the placement's properties."""
        ftype = fdef.get("type", "odml")
        if ftype == "match_link":
            return self._matched_entity(fdef, prop(device, "name"))
        if ftype == "custom_anatomy":
            return self.anatomy.build(fdef, values)
        return _postprocess(fdef, values.get(_odml_field_name(fdef, field)))

    def build_per_session_nested(self, schema_name: str, schema: dict) -> list | None:
        """One entity per placement of each session: the session's Electrode
        Placements, else its subject's Implanted Probes (chronic arrays), see
        placements.matched_probes, which rejects a placement naming no
        Hardware/Probes entry or a device twice. Returns list-of-lists so
        RecordingActivity[i].devices gets session i's entities via
        index-matched linking. The lookup_label_template is filled
        (fill_template) from the session identity ({session_id},
        {subject_id}, {task}), {dataset} (short name), {probe} (the device's
        exact name) and the placement's properties; a label that repeats
        within a session is an error (two placements would merge into one
        KG node).
        """
        fg_class = _resolve_fg_class(schema.get("fairgraph_class"))
        sessions = self.get_section("Sessions")
        if not fg_class or not sessions:
            return None
        label_tpl = schema.get("lookup_label_template", "")
        dataset = prop(self.get_section("Dataset"), "short name", "")
        all_entities: list[list] = []
        for session in sessions.sections:
            identity = session_identity(self.doc, session)
            session_entities, labels = [], []
            for _, device, placement in matched_probes(self.doc, session):
                values = {k: v for k, v in placement.items() if not isinstance(v, dict)}
                values["probe"] = prop(device, "name")
                kwargs = {f: v for f, fdef in schema.get("fields", {}).items() if isinstance(fdef, dict)
                          and (v := self._placement_field(f, fdef, device, values)) is not None}
                if not kwargs:
                    continue
                if label_tpl and (label := fill_template(label_tpl, {**identity, "dataset": dataset, **values}.get)):
                    if label in labels:
                        raise ValueError(f"{schema_name}: two placements of session {identity['session_id']} "
                                         f"share the label {label}; one Hardware/Probes entry per physical device")
                    labels.append(label)
                    kwargs["lookup_label"] = machine_label(label)
                existing_space = self._reuse_kg_id(fg_class, schema.get("kg_lookup"), kwargs, schema)
                session_entities.append(self._build_entity(
                    fg_class, kwargs, existing_space, schema.get("add_to_collection", True),
                    schema_name, schema.get("existence_query")))
            all_entities.append(session_entities)
        return all_entities or None

    def build_schema(self, schema_name: str) -> Any:
        """Build entity/entities for a schema."""
        schema = self.schemas.get(schema_name)
        if not schema or schema.get("build") is False:
            return None
        if instance_lookup := schema.get("instance_lookup"):
            return self.lookup_instance(schema, instance_lookup)
        mode = schema.get("mode", "single")
        if mode == "per_session":
            return self.build_per_session(schema_name, schema)
        if mode == "per_session_nested":
            return self.build_per_session_nested(schema_name, schema)
        if mode == "per_subject":
            return self.build_per_subject(schema_name, schema)

        has_nested = "nested" in schema
        section = self.get_section(schema.get("odml_section"))
        if schema.get("odml_section") and section is None:
            logger.info("  Skipping %s: no %s section", schema_name, schema["odml_section"])
            return None
        # Top-level condition check (e.g., skip FileRepository when no URL)
        if condition := schema.get("condition"):
            if not _check_condition(condition, nested(section) if section else {}, {}):
                logger.info("  Skipping %s: condition '%s' not met", schema_name, condition)
                return None
        if mode != "multiple":
            if has_nested:
                return self.build_with_nested(schema_name, schema, nested(section) if section else {}, section)
            return self.build_simple(schema_name, schema, section)
        entities = []
        for i, sub in enumerate(section.sections if section else []):
            self._current_multi_index = i
            data = nested(sub)
            if not any(data.values()):
                continue
            entity = (self.build_with_nested(schema_name, schema, data, sub) if has_nested
                      else self.build_simple(schema_name, schema, sub))
            if entity:
                entities.append(entity)
        self._current_multi_index = None
        return entities or None

    @staticmethod
    def _accept_missing_fields():
        """fairgraph logs (its default) instead of raising a missing required
        property; pinned for the classes whose fields are missing by design,
        so the collection builds and its validation lists them:
        ElectrodeArray.device_type (openMINDS has no DeviceType term for
        electrode arrays yet), DatasetVersion.digital_identifier (the DOI is
        registered after the push), Contribution.types (a contributor
        without roles), Setup.description and has_parts (a system without
        model or probes), CustomAnnotation.coordinate_space (a laterality
        needs none)."""
        for cls in (fgephys.ElectrodeArray, fgcore.DatasetVersion, fgcore.Contribution, fgcore.Setup,
                    fgsands.CustomAnnotation):
            cls.set_error_handling("log")

    def build_all(self) -> Collection:
        """Build all entities in dependency order."""
        self._accept_missing_fields()
        for name in self.build_order:
            if result := self.build_schema(name):
                self.entities[name] = result
        return self.collection


def build_collection(odml_path: Path, kg_client: KGClient | None,
                     target_space: str | None = None, atlas: Path | None = None) -> Collection:
    """Build openMINDS Collection from odML.

    With a client, entities are looked up in `target_space` (default: the
    collection metadata's kg space; an error without either). Without a
    client nothing is looked up (offline). `atlas`: the brain atlas registry
    file, default the one the collection metadata names (Dataset/anatomy
    atlas, next to collection.yaml), none without. The collection carries
    `kg_space` (for the push) and `output_dir` (the odML's directory).
    """
    odml_path = Path(odml_path)
    doc = odml.load(str(odml_path))
    if kg_client is not None and target_space is None:
        target_space = kg_space(doc)
        if not target_space:
            raise ValueError(f"No KG space to look up entities in: {NO_SPACE_HELP}")
    atlas = load_atlas(Path(atlas) if atlas else atlas_path(doc, odml_path))
    collection = SchemaBuilder(doc, load_registry(SCHEMAS_NAME), kg_client, target_space, atlas).build_all()
    collection.output_dir = odml_path.parent
    return collection


def save_collection(collection: Collection, output_path: Path) -> Path:
    """Save Collection to JSON-LD."""
    output_path = Path(output_path)
    failures = collection.validate()
    if failures:
        logger.warning("Validation warnings: %d", len(failures))
        for iid in list(failures.keys())[:5]:
            logger.warning("  - %s", iid)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    collection.save(str(output_path))
    return output_path
