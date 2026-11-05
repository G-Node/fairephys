"""BIDS directory structure creation."""

from pathlib import Path

from .naming import session_path


# Folder of the curated dataset that holds the canonical odML record and the
# metadata derived from it (openMINDS); not part of BIDS, listed in .bidsignore
METADATA_DIRNAME = "metadata"
# What .bidsignore lists first: the metadata folder and the GIN DataCite record
# with its LICENSE (export.datacite)
BIDSIGNORE = (METADATA_DIRNAME, "datacite.yml", "LICENSE")


def create_session_layout(bids_root: Path, sub: str, ses: str, datatypes: list[str] = None) -> dict[str, Path]:
    """Create BIDS session directory structure.

    Args:
        bids_root: Root of BIDS dataset
        sub: Subject label
        ses: Session label
        datatypes: List of datatypes to create (default: ['ecephys'])

    Returns:
        Dict mapping datatype names to their paths
    """
    if datatypes is None:
        datatypes = ["ecephys"]

    # Session-level path (for physio files that go at session level)
    ses_path = session_path(bids_root, sub, ses, None)
    ses_path.mkdir(parents=True, exist_ok=True)

    paths = {"session": ses_path}
    for dt in datatypes:
        path = session_path(bids_root, sub, ses, dt)
        path.mkdir(parents=True, exist_ok=True)
        paths[dt] = path

    return paths


def create_dataset_layout(bids_root: Path) -> None:
    """Create the BIDS dataset root and its .bidsignore."""
    bids_root = Path(bids_root)
    bids_root.mkdir(parents=True, exist_ok=True)
    write_bidsignore(bids_root)


def write_bidsignore(bids_root: Path, extra: tuple[str, ...] = ()) -> Path:
    """.bidsignore: BIDSIGNORE, the dataset's extra lines, then the other
    lines it lists already, each once, so the file is the same whichever
    writer runs last."""
    path = Path(bids_root) / ".bidsignore"
    lines = path.read_text().splitlines() if path.exists() else []
    path.write_text("".join(f"{line}\n" for line in dict.fromkeys([*BIDSIGNORE, *extra, *filter(None, lines)])))
    return path
