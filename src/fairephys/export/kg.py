"""EBRAINS Knowledge Graph integration via fairgraph: the client and the push.

fairgraph is EBRAINS-specific, so the KG host is a parameter with the EBRAINS
default; the space of a push comes from the caller or the collection metadata.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from time import sleep

from fairgraph import KGClient, Collection
from fairgraph.errors import AuthenticationError, AuthorizationError, ResourceExistsError

from .collection import NO_SPACE_HELP

logger = logging.getLogger(__name__)

KG_HOST = "core.kg.ebrains.eu"
TOKEN_ENV = "EBRAINS_KG_TOKEN"
TOKEN_FILE = Path("~/.config/fairephys/kg_token")
UPLOAD_LOG = ".kg_upload_log.txt"
_TOKEN_HELP = "Get from: https://editor.kg.ebrains.eu/ -> Profile -> Copy token"


def read_token() -> str | None:
    """The KG token: the EBRAINS_KG_TOKEN environment variable, else the
    content of ~/.config/fairephys/kg_token; None without either."""
    token = os.environ.get(TOKEN_ENV)
    path = TOKEN_FILE.expanduser()
    if not token and path.is_file():
        token = path.read_text().strip()
    return token or None


def get_kg_client(token: str = None, host: str = KG_HOST) -> KGClient:
    """Create KGClient. Token from arg, else read_token()."""
    token = token or read_token()
    if not token:
        raise ValueError(f"No KG token: set {TOKEN_ENV} or write it to {TOKEN_FILE}. {_TOKEN_HELP}")
    try:
        return KGClient(host=host, token=token)
    except AuthenticationError as e:
        # fairgraph raises it without a message when the token is rejected
        raise AuthenticationError(f"KG token rejected, probably expired. {_TOKEN_HELP}") from e


# Backward compatibility
get_client = get_kg_client


def _save_with_retry(node, client: KGClient, space: str,
                     max_retries: int = 2, base_delay: float = 3) -> bool:
    """Save a node. Returns True on success, False when the KG rejected it.

    fairgraph types a rejected token (AuthenticationError, propagated: the
    push cannot go on), a forbidden write (AuthorizationError) and a
    duplicate (ResourceExistsError); every other KG error (400, 500) is a
    plain Exception, retried with exponential backoff as a transient
    server error and logged when it persists.
    """
    name = node.__class__.__name__
    for attempt in range(max_retries + 1):
        try:
            node.save(client, space=space, recursive=False, ignore_duplicates=True)
            return True
        except AuthenticationError:
            raise
        except AuthorizationError as e:
            logger.error("  403 Forbidden (permission/invalid data): %s %s", name, node.id)
            logger.debug("  Detail: %s", e)
            return False
        except ResourceExistsError as e:
            logger.error("  Already in the KG, not saved: %s %s: %s", name, node.id, e)
            return False
        except Exception as e:  # fairgraph raises its other KG errors untyped
            if attempt == max_retries:
                logger.error("  KG rejected %s %s after %d retries: %s", name, node.id, max_retries, e)
                return False
            delay = base_delay * (2 ** attempt)
            logger.warning("  Error saving %s %s, retry %d/%d in %.0fs: %s",
                           name, node.id, attempt + 1, max_retries, delay, e)
            sleep(delay)
    return False


def _skip_reason(node, space: str, keep_ids: set[str]) -> str | None:
    """Return why a node must not be saved to `space`, or None to save it.

    Nodes referenced under a `keep_existing` schema policy are never
    re-saved (the KG copy is authoritative); nodes from other KG spaces are
    referenced for linking only.
    """
    if node.id in keep_ids:
        return "kept existing"
    node_space = getattr(node, "space", None)
    if node_space and node_space != space and node.id.startswith("https://"):
        return f"space={node_space}, not {space}"
    return None


def push_collection(collection: Collection, client: KGClient, space: str | None = None,
                    log_path: Path | None = None) -> dict:
    """Push fairgraph Collection to Knowledge Graph.

    `space`: the KG space, default the collection's `kg_space` (the
    collection metadata); an error without either. The KG ids of the saved
    nodes are written to `log_path`, default .kg_upload_log.txt next to the
    collection output (`collection.output_dir`), none without one.

    Uses a custom upload loop instead of collection.upload() to handle
    per-entity errors: entities the KG rejects are logged and skipped so
    the rest can be uploaded (see _save_with_retry). Entities found via KG
    lookup from other spaces (e.g. Organizations in ``common``) and entities
    kept as-is under a `keep_existing` policy (``collection.keep_existing_ids``,
    e.g. Project/Consortium) are kept in the collection for linking but not
    re-saved.
    """
    space = space or getattr(collection, "kg_space", None)
    if not space:
        raise ValueError(f"No KG space to push to: {NO_SPACE_HELP}")
    output_dir = getattr(collection, "output_dir", None)
    log_path = Path(log_path) if log_path else (Path(output_dir) / UPLOAD_LOG if output_dir else None)
    if log_path is not None and log_path.exists():
        log_path.unlink()  # a stale log: temporary ids do not survive across sessions

    failures = collection.validate()
    if failures:
        logger.warning("Validation warnings before upload: %d", len(failures))
        for instance_id, issues in failures.items():
            logger.warning("  %s: %s", instance_id, issues)

    nodes = [n for n in collection.sort_nodes_for_upload()
             if not n.id.startswith("https://openminds.om-i.org/instances")]
    total = len(nodes)
    logger.info("Uploading %d entities to KG space '%s'...", total, space)
    keep_ids = getattr(collection, "keep_existing_ids", set())
    saved, skipped, errors = 0, 0, 0
    for i, node in enumerate(nodes, 1):
        if reason := _skip_reason(node, space, keep_ids):
            logger.info("  Skip %s %s (%s)", node.__class__.__name__, node.id, reason)
            skipped += 1
            continue
        logger.info("  [%d/%d] %s %s...", i, total, node.__class__.__name__, node.id[:60])
        if not _save_with_retry(node, client, space):
            errors += 1
            continue
        saved += 1
        if log_path is not None:
            with open(log_path, "a") as f:
                f.write(f"{node.id}\n")

    logger.info("KG push: %d saved, %d skipped (other spaces / kept existing), "
                "%d rejected", saved, skipped, errors)
    return {"saved": saved, "skipped": skipped, "errors": errors}
