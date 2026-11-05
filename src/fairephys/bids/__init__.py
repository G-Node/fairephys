"""BIDS structure and sidecar generation for microelectrode electrophysiology."""

from .naming import bids_filename, session_path
from .structure import METADATA_DIRNAME, create_session_layout, create_dataset_layout, write_bidsignore
from .resolver import section_to_dict
from .builder import (
    matched_probes, extract_sidecar_fields, extract_events_metadata, channel_electrode_map, channel_electrodes,
)
from .aux import extract_aux_groups, generate_aux_sidecars, write_aux_bids_data
from .ecephys import (
    generate_channels_tsv,
    generate_channels_json,
    generate_electrodes_tsv,
    generate_electrodes_json,
    generate_probes_tsv,
    generate_probes_json,
    generate_ecephys_json,
    generate_coordsystem_json,
)
from .dataset import generate_dataset_description, generate_participants_tsv, generate_participants_json
from .physio import generate_physio_json, write_physio_data
from .motion import generate_motion_json, generate_motion_channels_tsv, write_motion_data
from .emg import generate_emg_json, generate_emg_channels_tsv, write_emg_edf
from .events import generate_events_tsv, generate_events_json

__all__ = [
    # Naming
    "bids_filename",
    "session_path",
    # Structure
    "METADATA_DIRNAME",
    "create_session_layout",
    "create_dataset_layout",
    "write_bidsignore",
    # Resolver
    "section_to_dict",
    # Builder
    "matched_probes",
    "extract_sidecar_fields",
    "channel_electrode_map",
    "channel_electrodes",
    # Auxiliary signals
    "extract_aux_groups",
    "generate_aux_sidecars",
    "write_aux_bids_data",
    # Ecephys
    "generate_channels_tsv",
    "generate_channels_json",
    "generate_electrodes_tsv",
    "generate_electrodes_json",
    "generate_probes_tsv",
    "generate_probes_json",
    "generate_ecephys_json",
    "generate_coordsystem_json",
    # Dataset-level
    "generate_dataset_description",
    "generate_participants_tsv",
    "generate_participants_json",
    # Physio
    "generate_physio_json",
    "write_physio_data",
    # Motion
    "generate_motion_json",
    "generate_motion_channels_tsv",
    "write_motion_data",
    # EMG
    "generate_emg_json",
    "generate_emg_channels_tsv",
    "write_emg_edf",
    # Events
    "generate_events_tsv",
    "generate_events_json",
    "extract_events_metadata",
]
