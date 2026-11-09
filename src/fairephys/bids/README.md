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
| `structure.py` | Directory layout creation; `write_bidsignore(root, extra)`: the library's lines, a dataset's extra lines, then those the file lists already, each once |
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
| `writers.py` | The files of a curate.py step at the names of `curation.session_paths` (see below) |

## Session, run and dataset files

The three writers a dataset's `bids_cmd` calls, each with the paths of `curation.session_paths(doc, session, curated_dir, acq=None, task=None, run=None)`:

- `write_session_sidecars(doc, session, paths, channels=None)`: `probes.tsv` and `.json` (`probes_dir` names the session's probe group folder), `electrodes.tsv` (from `paths["probe_group"]`) and `.json`, `channels.json`, and `channels.tsv` from the rows the dataset extracted with `builder.extract_channels`; `channels=None` writes no `channels.tsv` (a dataset that writes one per probe, with `acq`).
- `write_run_sidecars(doc, session, paths, stream=None)`: the `ecephys.json` of the run that named the file (`paths["run"]`, the whole session when None); once per run with `session_paths(..., run=run)`, or once per probe with `acq` and its stream.
- `write_dataset_files(doc, curated_dir, description=None, bidsignore=())`: the BIDS root, `.bidsignore` with the dataset's extra lines (one `write_bidsignore`), `dataset_description.json` updated with `description` (such as `{"HEDVersion": HED_VERSION}`), `participants.tsv` and `participants.json`.

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
