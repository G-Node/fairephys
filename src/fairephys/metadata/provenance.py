"""Versions of the software the curation pipeline runs with.

compile_to_odml() records them once in the odML 'Curation Software' section,
from which the data descriptor and the BIDS GeneratedBy name them. Python
packages give their installed version (importlib.metadata); the BIDS validator
gives the version pinned in the registry spec and the schema file it validates
against (see validation.bids.validator_versions). A Python package that is
not installed is left out and logged.

The pipeline itself (this package) keeps its version between commits, so its
entry also holds the git commit of its source and whether that source had
uncommitted changes, when it runs from a git checkout (see source_checkout),
its one line description and, when the package names one, its repository URL.
"""

from __future__ import annotations

import logging
import re
import subprocess
from importlib import metadata as importlib_metadata
from pathlib import Path

logger = logging.getLogger(__name__)

CURATION_SOFTWARE = "Curation Software"

# The curation pipeline: this package (also its entry name in the section) and
# the directory of its source
PIPELINE = __name__.split(".")[0]
PIPELINE_DIR = Path(__file__).resolve().parent.parent

# Seconds one git call may take before the commit is left out
GIT_TIMEOUT = 10

# Project URL labels that name the source repository: "source" and its aliases
# (PEP 753), compared lower case without punctuation or spaces
SOURCE_URL_LABELS = {"source", "repository", "sourcecode", "github"}

# Python packages of the pipeline as (name, distribution, purpose), in the
# order the descriptor names them (after the BIDS validator)
PYTHON_PACKAGES = [
    ("NWB Inspector", "nwbinspector", "validation"),
    ("openMINDS Python", "openminds", "validation"),
    (PIPELINE, PIPELINE, "conversion"),
    ("NeuroConv", "neuroconv", "conversion"),
    ("PyNWB", "pynwb", "conversion"),
    ("fairgraph", "fairgraph", "conversion"),
]


def curation_software() -> list[dict]:
    """One entry per tool with a resolvable version, the BIDS validator first.

    Each entry holds name, version and purpose ("validation" or "conversion"),
    the BIDS validator also its schema (and the schema commit), the pipeline
    also its commit and uncommitted changes (from a git checkout), description
    and code url (when known), and the origin of the values (the odML value
    origin).
    """
    from ..validation.bids import validator_versions

    bids = validator_versions()
    tools = [{"name": "BIDS validator", "version": bids.pop("version"), "purpose": "validation",
              **bids, "origin": "generated: BIDS validator pin and schema file of the registry spec"}]
    for name, dist, purpose in PYTHON_PACKAGES:
        try:
            version = importlib_metadata.version(dist)
        except importlib_metadata.PackageNotFoundError:
            logger.warning("Curation software: %s (Python package %s) not installed, left out", name, dist)
            continue
        tool = {"name": name, "version": version, "purpose": purpose,
                "origin": f"generated: installed version of the Python package {dist}"}
        if dist == PIPELINE:
            tool.update(_pipeline_details(dist))
        tools.append(tool)
    return tools


def _pipeline_details(dist: str) -> dict:
    """Commit and uncommitted changes of the pipeline source, its description
    (the package summary) and code url (a repository among its project URLs)."""
    details = source_checkout()
    origin = f"generated: installed metadata of the Python package {dist}"
    if details:
        origin += " and git state of its source checkout"
    package = importlib_metadata.metadata(dist)
    if summary := (package.get("Summary") or "").strip():
        details["description"] = summary
    if url := source_url(package.get_all("Project-URL") or []):
        details["code url"] = url
    return {**details, "origin": origin}


def source_checkout(path: Path = PIPELINE_DIR) -> dict:
    """Git commit (short hash) of the source at `path` and whether it has uncommitted changes.

    {"commit": "abc1234", "uncommitted changes": False} when the files at
    `path` are tracked in a git checkout; {} for an installed copy (also one in
    an ignored directory of a checkout, such as a virtual environment) or when
    git is missing, fails or does not answer in time (logged at debug).
    Uncommitted changes are modified, staged or untracked files under `path`
    only: metadata edited elsewhere in the repository changes no code.
    """
    def git(*args: str) -> str | None:
        try:
            # --no-optional-locks: a status never takes the index lock (a killed status would leave it)
            done = subprocess.run(["git", "--no-optional-locks", "-C", str(path), *args], capture_output=True,
                                  encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("Curation software: git %s in %s failed: %s", args[0], path, exc)
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    if git("ls-files", "--error-unmatch", "--", ".") is None:
        logger.debug("Curation software: %s is not tracked in a git checkout, version only", path)
        return {}
    if not (commit := git("rev-parse", "--short", "HEAD")):
        logger.debug("Curation software: no commit for %s, version only", path)
        return {}
    checkout = {"commit": commit}
    if (status := git("status", "--porcelain", "--", ".")) is not None:
        checkout["uncommitted changes"] = bool(status)
    return checkout


def source_url(project_urls: list[str]) -> str | None:
    """The repository URL among a package's project URLs ("Label, URL" entries
    of [project.urls]) labelled as its source; None without one."""
    for entry in project_urls:
        label, _, url = entry.partition(",")
        if re.sub(r"[^a-z0-9]", "", label.lower()) in SOURCE_URL_LABELS and url.strip():
            return url.strip()
    return None
