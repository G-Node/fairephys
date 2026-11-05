"""Metadata enrichment from Neo and openMINDS.

Both enrichment modules operate post-compilation on the odML document.
"""

from .neo import extract as neo_extract, enrich_document as neo_enrich_document
from .openminds import enrich_document as openminds_enrich_document, load_schema_registry

__all__ = [
    "neo_extract",
    "neo_enrich_document",
    "openminds_enrich_document",
    "load_schema_registry",
]
