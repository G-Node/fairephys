"""Looking up what the EBRAINS Knowledge Graph already holds, through a fairgraph client.

Without a client (offline build) nothing is looked up and every entity is
built anew. A lookup that fails is an error: an entity created in its place
would duplicate the KG node at the next push.
"""

from __future__ import annotations

import logging
from typing import Any

from fairgraph import KGClient

logger = logging.getLogger(__name__)


class KGResolver:
    """KG and openMINDS instance lookup with in-memory caching."""

    def __init__(self, client: KGClient | None, scope: str = "in progress"):
        self.client = client
        self.scope = scope
        self._cache: dict[tuple, Any] = {}

    def _key(self, entity_type: str, **kwargs) -> tuple:
        return (entity_type, tuple(sorted(kwargs.items())))

    def lookup_instance(self, fg_class, name: str) -> Any:
        """A standard openMINDS instance (controlled term) by name, None when
        the vocabulary has none of that name; no KG access."""
        if not name:
            return None
        key = self._key(fg_class.__name__, name=name)
        if key in self._cache:
            return self._cache[key]
        try:
            entity = fg_class.by_name(name)
        except (KeyError, ValueError, TypeError) as e:  # fairgraph: unknown name
            logger.warning("  Instance: Lookup failed for %s '%s': %s", fg_class.__name__, name, e)
            entity = None
        self._cache[key] = entity
        if entity:
            logger.info("  Instance: %s '%s' -> %s", fg_class.__name__, name, getattr(entity, "id", "new"))
        return entity

    def lookup_kg(self, fg_class, fields: list, kwargs: dict,
                  search_space: str | None = None) -> Any:
        """The KG entity whose `fields` equal the values in `kwargs`, None when
        there is none or without a client.

        The KG search API does substring matching, so several results are
        fetched and the one that exactly matches the lookup values is taken.
        *search_space* restricts the search to one KG space (prevents
        cross-space substring collisions for dataset-private entities).
        """
        lookup = {f: kwargs.get(f) for f in fields if kwargs.get(f)}
        if not lookup or self.client is None:
            return None
        key = self._key(fg_class.__name__, _search_space=search_space, **lookup)
        if key in self._cache:
            return self._cache[key]
        list_kwargs = dict(client=self.client, scope=self.scope, size=10, **lookup)
        if search_space:
            list_kwargs["space"] = search_space
        entity = self._exact_match(fg_class.list(**list_kwargs), lookup)
        self._cache[key] = entity
        info = ", ".join(f"{k}={v}" for k, v in lookup.items())
        logger.info("  KG: %s %s (%s) %s", "Found" if entity else "Not found", fg_class.__name__,
                    info, f"-> {entity.id}" if entity else "- will create new")
        return entity

    @staticmethod
    def _exact_match(results: list, lookup: dict) -> Any:
        """Return first result that exactly matches all lookup fields."""
        for entity in results:
            if all(str(getattr(entity, f, None)) == str(v) for f, v in lookup.items()):
                return entity
        return None
