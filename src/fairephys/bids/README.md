# fairephys.bids

BIDS sidecar generation for microelectrode electrophysiology (BEP032).

## Data flow

```
odML (single source of truth)
  |
  +-- builder.py extracts structured dicts via registry paths
  |
  +-- ecephys.py / dataset.py / physio.py / ... write TSV/JSON files
```

All metadata comes from odML. The registry (`bids_mappings.yaml`) maps odML paths to BIDS columns. Dataset-specific config (channel types, auxiliary channels) is passed as parameters from each dataset's `curate.py`.

## Modules

| Module | Purpose |
|--------|---------|
| `naming.py` | BIDS filename + path generation |
| `structure.py` | Directory layout creation |
| `resolver.py` | odML path resolution |
| `builder.py` | Registry-driven data extraction from odML; electrodes and the channel-to-electrode map from the session's ProbeInterface group |
| `aux.py` | Auxiliary signals (eye tracking, motion, EMG): groups from Hardware/Auxiliary Devices, their sidecars and data files; an eye tracker's channels map to the BIDS physio columns the dataset names per channel (`eye_columns`, from its YAML: `x_coordinate`, `y_coordinate`, `pupil_size`); one or two channels without names are the horizontal and vertical position in that order, a third channel must be named |
| `extractors.py` | Complex field extraction (authors, funding, acknowledgements, ethics approvals, hardware filters); `bracketed()` writes "text (detail)" without doubled brackets, also for the README |
| `ecephys.py` | probes/electrodes/channels TSV + JSON sidecars, ecephys.json, coordsystem.json |
| `dataset.py` | dataset_description.json, participants.tsv |
| `physio.py` | Cardiac + eye tracking sidecars |
| `emg.py` | EMG sidecars |
| `motion.py` | Hand position / motion sidecars |
| `events.py` | events.tsv + JSON; `generate_events_tsv(events, path, columns)` fixes the column order and the header of an empty file when `columns` is given |
| `hed.py` | HED version and the tags shared by every task (trial, session, reward) |

## Registry

The registry (`_resources/registry/bids_mappings.yaml`) maps odML paths to BIDS columns. Adding a new BIDS field means adding a registry entry, not code:

```yaml
probes_tsv:
  columns:
    type:
      path: Hardware/Probes/*.probe type
    hemisphere:
      path: Sessions/*/Electrode Placements/*.hemisphere
      transform: hemisphere
```

Column descriptions for JSON sidecars are also registry-driven via `sidecar_description` fields.

## Dataset-specific config

Channel types and bad channels vary per dataset and are passed as parameters from `curate.py`, never hardcoded in the package. Electrode names come from the session's ProbeInterface group (`{probe_id}_{contact id}`, as in the NWB electrode table), for electrodes.tsv and for the channel-to-electrode map:

```python
# In a dataset's curate.py
CHANNEL_TYPES = {
    "prefixes": {"Pr1_": "LFP", "Pr2_": "LFP"},
    "names": {"EMG1": "EMG", "Heart": "ECG"},
    "default": "MISC",
}

group = probe_group_dir / "probe_group.json"   # saved by --build-probes
electrodes = builder.extract_electrodes(doc, session, group)
channels = builder.extract_channels(session, group, channel_types=CHANNEL_TYPES,
                                    bad_channels={"Pr1_07": "broken contact"})
```
