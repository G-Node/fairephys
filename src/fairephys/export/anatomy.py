"""Anatomical entities of the electrode placements (sands.CustomAnatomicalEntity).

A placement's brain region becomes one entity per (area, UBERON term,
hemisphere), named after the area of an optional brain atlas, else after the
region label. The atlas is a registry file named by the collection metadata
(Dataset/anatomy atlas, a file next to collection.yaml): region label ->
{area: <atlas area name or null>, uberon term: <UBERONParcellation attribute
or null>}. Without an atlas the UBERON term is the region label itself when
the vocabulary has it. A placement can give its own area and UBERON term
(the fields named by the registry); they take precedence over the atlas.
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
    """Builds the CustomAnatomicalEntity of placements into a collection, one
    node per (area, UBERON term, hemisphere)."""

    def __init__(self, collection, resolver: KGResolver, atlas: dict | None = None):
        self.collection = collection
        self.resolver = resolver
        self.atlas = atlas or {}
        self._cache: dict[tuple, Any] = {}

    def uberon_term(self, label: str, attr: str | None):
        """The UBERONParcellation instance: of the attribute name (a placement
        or atlas value, e.g. primary_motor_cortex), else of the label."""
        if attr:
            term = getattr(fgct.UBERONParcellation, attr, None)
            if term is None:
                logger.warning("  UBERON term '%s' is not a UBERONParcellation attribute", attr)
            return term
        return self.resolver.lookup_instance(fgct.UBERONParcellation, label)

    def build(self, fdef: dict, values: dict) -> list | None:
        """[entity] for the region of a placement (`values`: its properties),
        None without a region. fdef names the placement fields: odml_field
        (region), area_field, uberon_field and hemisphere_field."""
        label = values.get(fdef.get("odml_field", "brain region"))
        if not label:
            return None
        entry = self.atlas.get(label) or {}
        area = values.get(fdef.get("area_field", "")) or entry.get("area")
        uberon = values.get(fdef.get("uberon_field", "")) or entry.get("uberon term")
        hemisphere = values.get(fdef.get("hemisphere_field", "hemisphere"))
        key = (area or label, uberon, hemisphere)
        if key in self._cache:
            return [self._cache[key]]

        kwargs: dict[str, Any] = {"name": area or label}
        if term := self.uberon_term(label, uberon):
            kwargs["related_uberon_term"] = term
        if hemisphere and (lat := self.resolver.lookup_instance(fgct.Laterality, hemisphere)):
            # CustomAnnotation is embedded metadata, not a node of the collection
            kwargs["has_annotations"] = [fgsands.CustomAnnotation(lateralities=[lat])]
        entity = fgsands.CustomAnatomicalEntity(**kwargs)
        self.collection.add(entity)
        self._cache[key] = entity
        logger.info("  CustomAnatomicalEntity: %s (uberon=%s, hemisphere=%s)",
                    kwargs["name"], getattr(term, "name", None), hemisphere)
        return [entity]
