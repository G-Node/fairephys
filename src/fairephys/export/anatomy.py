"""Anatomical locations of the electrode placements.

A placement's brain region is linked to what exists: its UBERON term
(UBERONParcellation) and, where an area of a brain atlas is named, the
entity of that area in the Knowledge Graph, looked up by its name and
referenced, never built. A custom entity (sands.CustomAnatomicalEntity) is
built only for a region label with neither, named after the label, one per
name. The hemisphere is not part of any of them. The atlas is a registry
file named by the collection metadata (Dataset/anatomy atlas, a file next to
collection.yaml): region label -> {area: <name of the area's entity in the
Knowledge Graph, a list of names, or null>, uberon term: <UBERONParcellation attribute or
null>}. Without an atlas the UBERON term is the region label itself when the
vocabulary has it. A placement can give its own area and UBERON term (the
fields named by the registry); they take precedence over the atlas.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import odml
import yaml
import fairgraph.openminds.controlled_terms as fgct
import fairgraph.openminds.sands as fgsands

from ..metadata.compiler import find_shared_dir
from ..odml import prop, section_at
from .kg_lookup import KGResolver

logger = logging.getLogger(__name__)

ATLAS_FIELD = "anatomy atlas"


def atlas_path(doc: odml.Document, odml_path: Path) -> Path | None:
    """The atlas registry named by the document's Dataset/anatomy atlas, a file
    next to the collection metadata found above the odML; None when none is
    named. Raises FileNotFoundError when it is named and not there."""
    name = prop(section_at(doc, "Dataset"), ATLAS_FIELD)
    if not name:
        return None
    shared = find_shared_dir(Path(odml_path).parent)
    path = (shared / str(name)) if shared else Path(str(name))
    if not path.is_file():
        raise FileNotFoundError(f"{ATLAS_FIELD} '{name}' not found next to the collection "
                                f"metadata ({shared or 'no collection/metadata above ' + str(odml_path)})")
    return path


def load_atlas(path: Path | None) -> dict:
    """The atlas registry {region label: {area, uberon term}}, {} without a path."""
    if path is None:
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


class Anatomy:
    """The anatomical locations of placements: UBERON terms, atlas areas of
    the Knowledge Graph and, built into the collection, custom entities."""

    def __init__(self, collection, resolver: KGResolver, atlas: dict | None = None):
        self.collection = collection
        self.resolver = resolver
        self.atlas = atlas or {}
        self._cache: dict[str, Any] = {}

    def uberon_term(self, label: str, attr: str | None):
        """The UBERONParcellation instance: of the attribute name (a placement
        or atlas value, e.g. primary_motor_cortex), else of the label."""
        if attr:
            term = getattr(fgct.UBERONParcellation, attr, None)
            if term is None:
                logger.warning("  UBERON term '%s' is not a UBERONParcellation attribute", attr)
            return term
        return self.resolver.lookup_instance(fgct.UBERONParcellation, label)

    def custom(self, name: str):
        """The CustomAnatomicalEntity of that name, built once."""
        if name not in self._cache:
            self._cache[name] = fgsands.CustomAnatomicalEntity(name=name)
            self.collection.add(self._cache[name])
            logger.info("  CustomAnatomicalEntity: %s", name)
        return self._cache[name]

    def area(self, name: str, space: str | None):
        """The atlas area's entity of that name in the Knowledge Graph space,
        referenced as it is; a name the space does not hold is an error.
        Without a client (offline) a custom entity of that name stands in."""
        entity = self.resolver.lookup_kg(fgsands.CustomAnatomicalEntity, ["name"], {"name": name}, search_space=space)
        if entity is None and self.resolver.client is not None:
            raise ValueError(f"Atlas area '{name}' is not in the Knowledge Graph space '{space}': "
                             f"give the exact name of its entity, or no area")
        if entity is None:
            return self.custom(name)
        self.collection.add(entity)
        return entity

    def build(self, fdef: dict, values: dict) -> list | None:
        """The locations of a placement's region (`values`: its properties):
        its UBERON term and its atlas areas (one name or a list), each where
        there is one; a custom
        entity named after the region when there is neither; None without a
        region. fdef names the placement fields: odml_field (region),
        area_field and uberon_field, and the space of the atlas areas
        (area_space)."""
        label = values.get(fdef.get("odml_field", "brain region"))
        if not label:
            return None
        entry = self.atlas.get(label) or {}
        area = values.get(fdef.get("area_field", "")) or entry.get("area")
        term = self.uberon_term(label, values.get(fdef.get("uberon_field", "")) or entry.get("uberon term"))
        located = [term] if term is not None else []
        for name in [area] if isinstance(area, str) else area or []:
            located.append(self.area(name, fdef.get("area_space")))
        return located or [self.custom(label)]
