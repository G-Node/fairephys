"""The BIDS files of a session, of a run and of the dataset, from the odML
and the paths of curation.session_paths."""

import logging
from pathlib import Path

import odml

from . import builder
from .dataset import generate_dataset_description, generate_participants_json, generate_participants_tsv
from .ecephys import (generate_channels_json, generate_channels_tsv, generate_ecephys_json,
                      generate_electrodes_json, generate_electrodes_tsv, generate_probes_json, generate_probes_tsv)
from .structure import write_bidsignore

logger = logging.getLogger(__name__)


def write_session_sidecars(doc: odml.Document, session: odml.Section, paths: dict,
                           channels: list[dict] = None) -> None:
    """The session-level files: probes.tsv and .json, electrodes.tsv (from the
    session's probe group) and .json, channels.json (when the registry
    describes a column), and channels.tsv from
    the rows the dataset extracted (builder.extract_channels); channels None
    writes no channels.tsv (a dataset that writes one per probe itself)."""
    paths["ses_dir"].mkdir(parents=True, exist_ok=True)
    probes = builder.extract_probes(doc, session)
    generate_probes_tsv(probes, paths["probes_tsv"])
    generate_probes_json(probes, paths["probes_json"], probes_dir=paths["probes_dir"])
    generate_electrodes_tsv(builder.extract_electrodes(doc, session, paths["probe_group"]), paths["electrodes_tsv"])
    generate_electrodes_json(paths["electrodes_json"])
    generate_channels_json(paths["channels_json"])
    if channels is not None:
        generate_channels_tsv(channels, paths["channels_tsv"])
    logger.info(f"BIDS session sidecars: {paths['ses_dir']}")


def write_run_sidecars(doc: odml.Document, session: odml.Section, paths: dict, stream: str = None) -> None:
    """The run-level ecephys.json of paths["run"] (the run that named the
    file; the whole session when None), of the stream when given."""
    generate_ecephys_json(builder.extract_ecephys_metadata(doc, session, run=paths["run"], stream=stream),
                          paths["ecephys_json"])
    logger.info(f"Saved: {paths['ecephys_json']}")


def write_dataset_files(doc: odml.Document, curated_dir: Path, *, description: dict = None,
                        bidsignore: tuple[str, ...] = ()) -> None:
    """The dataset-level files: the BIDS root and its .bidsignore (the extra
    `bidsignore` lines after the library's), dataset_description.json
    updated with `description` (such as HEDVersion), participants.tsv and
    participants.json."""
    curated_dir = Path(curated_dir)
    curated_dir.mkdir(parents=True, exist_ok=True)
    write_bidsignore(curated_dir, extra=bidsignore)
    dataset_description = builder.extract_dataset_description(doc)
    dataset_description.update(description or {})
    generate_dataset_description(dataset_description, curated_dir / "dataset_description.json")
    generate_participants_tsv(builder.extract_participants(doc), curated_dir / "participants.tsv")
    generate_participants_json(curated_dir / "participants.json")
    logger.info(f"BIDS dataset files: {curated_dir}")
