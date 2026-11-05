# Curation Pipeline

Convert extracellular electrophysiology datasets to NWB in a BIDS (BEP032) layout with openMINDS metadata.

## Overview

```
YAML templates (human-curated)          Raw recordings
         |                                    |
   load_metadata()  (strict schema check)     |
         |                                    |
   compile_to_odml()                          |
         |                                    v
   neo_enrich_document(doc, ...) <----- neo_extract(file, format)
         |                               (channel names, sampling rate,
   openminds_enrich_document(doc)         duration, per-run durations)
         |
   save_odml()  -->  <dataset>.odml.xml/.yaml/.html, <dataset>.instances.yaml
         |
   Enriched odML (single source of truth)
         |
         +---> build_probe_group() / build_session_probe_group() ---> ProbeInterface JSON + PNG
         |
         +---> BIDS builder (registry-driven): probes, electrodes, channels TSV + sidecars
         |
         +---> converters: raw -> NWB in the BIDS layout (geometry from ProbeInterface)
         |
         +---> validation: sanity checks across raw, ProbeInterface, BIDS and NWB;
         |                 BIDS validator (BEP032 schema), nwbinspector
         |
         +---> export: openMINDS collection (KG), DataCite, README
```

### Key Principles

- **YAML** = human-curated source (manual entries only), validated strictly at compile
- **odML** = compiled from YAML, receives programmatic enrichment (Neo), is the single source of truth. Every output reads the odML, never the YAML and never another output: a wrong value is fixed once in the YAML, and recompiling and rerunning the steps updates every format.
- **Neo metadata** goes into odML as the session-specific `Neo Metadata` section
- **Provenance** = every odML property carries a `value_origin`: the curator's `_valueOrigin` of the template file it came from, or a programmatic origin (neo, probeinterface, generated)
- **Dataset scripts** handle session selection, file discovery, dataset-specific quirks
- **Reusable code** in `fairephys` is parameterized by metadata and registries

## Metadata Templates

A dataset is described in YAML files under `metadata/curated/`, in the vocabulary of the experiment rather than of any output format. Each file names its schema (`_schema: dataset.v1`), and the compiler checks it strictly: a missing required field or a value of the wrong type stops the compilation.

| File | Describes |
|------|-----------|
| `dataset.yaml` | Name, description, licence, custodian, contributors, funding, related projects |
| `subject.yaml` | Species, sex, age, surgical preparation, and the implanted probes of a chronic recording |
| `hardware.yaml` | Recording system, hardware filters, probes, auxiliary devices (eye tracker, EMG, motion) |
| `protocol.yaml` | The behavioural protocol and stimulus presentation |
| `recordings.yaml` | Per-session recording configuration of an acute recording: techniques and electrode placements |
| `sessions.yaml` | Session ids, dates, start times, tasks, subjects |
| `processing.yaml` | Processing steps actually performed (optional) |
| `datacite.yaml` | DOI registration metadata (optional) |
| `collection.yaml` | Shared project and consortium of several datasets, in `collection/metadata/` above them (optional) |

What stays the same across sessions is written once; what varies is written per session. `Session Defaults` in `sessions.yaml` and `Recording Defaults` in `recordings.yaml` hold the shared values, and each entry of `Sessions` or `Recordings` overrides only what differs.

### Where a value comes from

Every template file has a `_valueOrigin`: the sources its values were taken from (a document, a file header, an email by date). The compiler writes it into the odML as the value origin of each property from that file; a file without one gets its file name. A comment next to a single value names that value's own source. Values the pipeline adds itself carry `neo`, `probeinterface` or `generated` instead.

A value that is not known stays empty, with a comment saying why. The pipeline never fills in a placeholder: missing scientific metadata raises an error or a warning, depending on what the output needs.

## Registries

The mappings from the odML to each output format are YAML files in `_resources/registry/`, not code: `bids_mappings.yaml`, `nwb_mappings.yaml` and `openminds_schemas.yaml` (see `_resources/registry/README.md`). A BIDS sidecar field, for example, names the odML paths it is read from, the first one filled wins, and the kind of conflict between the standards it resolves:

```yaml
SamplingFrequency:
  path: [Sessions/*/Neo Metadata/stream.sampling_rate, Sessions/*/Neo Metadata.sampling_rate_hz,
         Sessions/*.sampling rate, Hardware/Recording System.sampling rate]
  conflict_types: []  # same concept and type across standards
```

A new BIDS column or openMINDS field is therefore a new registry entry; the extractors do not change.

## Recording Formats

`fairephys.formats.RAW_FORMATS` is the one table of formats: the Neo RawIO that reads them (enrichment, sanity checks) and the converter that writes NWB.

| Format | Converter | Reader |
|--------|-----------|--------|
| `blackrock` | BlackrockConverter (neuroconv) | BlackrockRawIO |
| `spikeglx` | SpikeGLXConverter (neuroconv) | SpikeGLXRawIO |
| `tdt` | TdtConverter (neuroconv) | TdtRawIO |
| `tdt-sev` | TdtSevConverter (SEV files read directly, by their headers) | TdtRawIO |
| `plexon2` | PlexonConverter (neuroconv, Wine DLL; extra `plexon`) | Plexon2RawIO |
| `intan` | none yet | IntanRawIO |

Datasets with their own file formats (Neuroshare .mcd, MATLAB .mat, SiNAPS) write the NWB with their own converters in `curate.py`, with the shared helpers of `fairephys.converters` (`build_nwb_metadata`, `add_devices`, `channel_positions`, `write_nwbfile`).

## Module Structure

```
src/fairephys/
  formats.py                # RAW_FORMATS, open_rawio()
  events.py                 # Neo and TDT events, MonkeyLogic BHV2 trials, aligning two code sequences
  bhv2.py                   # MonkeyLogic BHV2 file reader
  odml.py                   # reading a compiled document (prop, props, unit, section_at)
  placements.py             # devices and placements: probe ids, matched_probes, channels
  registry.py               # the packaged registries
  curation.py               # shared CLI (build_parser, run_export_steps) and pipeline commands
  init.py, __main__.py      # fairephys init / compile / validate
  metadata/
    compiler.py             # YAML to odML, HTML viewer
    sessions.py, subjects.py, provenance.py
  enrichment/
    neo.py                  # extract Neo metadata, enrich odML
    openminds.py            # resolve controlled terms to openMINDS instances
  probes/
    builder.py, library.py  # ProbeInterface from odML, library lookup
  bids/
    naming.py, structure.py # filenames, directory layout
    resolver.py, builder.py # registry-driven extraction from odML
    extractors.py           # extraction functions for the BIDS fields a path alone cannot give
    ecephys.py, dataset.py  # probes, electrodes, channels TSV/JSON; dataset files
    aux.py                  # auxiliary devices: eye tracking, motion, EMG, other physio
    physio.py, emg.py, motion.py, events.py, hed.py   # auxiliary sidecars and data files
  converters/
    base.py, metadata.py, devices.py, writer.py
    extras.py               # trials, time intervals, sorted units, sampled series in the NWB
    blackrock.py, spikeglx.py, tdt.py, tdt_sev.py, plexon.py
  export/
    collection.py, kg.py, kg_lookup.py   # openMINDS collection, KG push, reuse of existing KG nodes
    anatomy.py              # anatomical entities of the placements (area, UBERON term, hemisphere)
    datacite.py, readme.py, templating.py   # DataCite record, dataset README and their text templates
  validation/
    sanity.py               # per-session orchestration and report
    integrity.py            # raw versus NWB samples, recording summary
    geometry.py             # ProbeInterface, BIDS electrodes/channels, NWB electrode table
    bids.py, nwb.py         # BIDS validator (deno), nwbinspector
    schema.py, report.py    # template schemas, markdown report
  _resources/
    schemas/                # JSON Schema per template
    registry/               # openMINDS, BIDS and NWB field mappings
    templates/              # YAML templates and curate.py.template
    bids/                   # the BEP032 schema the validator runs with
    viewer/                 # XSL/CSS/JS inlined into the odML HTML view

<dataset>/                  # scaffolded by `fairephys init <dataset>`
  metadata/curated/         # YAML templates (human-curated)
  curated-data/             # the BIDS root: metadata/, probes/, sub-*/
  curate.py                 # entry point with dataset-specific logic
```

## Dataset curate.py Pattern

`fairephys init <name> --format <format>` renders `curate.py` from `_resources/templates/curate.py.template`. The script implements session discovery and raw file lookup and wires the shared steps:

```python
def compile_cmd(sessions, enrich_openminds=True, shared_dir=None):
    meta, doc = compile_metadata(TEMPLATES_DIR, shared_dir=shared_dir)   # strict
    for session_id in sessions:
        neo_data = enrichment.neo_extract(get_raw_file(session_id), format=RECORDING_FORMAT)
        enrichment.neo_enrich_document(doc, neo_data, session_id)
    return finalize_compilation(doc, METADATA_DIR, DATASET_NAME, enrich_openminds,
                                validation_dir=VALIDATION_DIR)
```

```bash
uv run python my-dataset/curate.py --compile --build-probes
uv run python my-dataset/curate.py --bids-layout --convert --sanity --session <session id>
uv run python my-dataset/curate.py --validate --datacite --readme --openminds-collection
```

Every script takes `--verbose` (debug logging), `--shared-metadata <dir>` (the collection-level metadata, by default the nearest `collection/metadata` above the templates), `--kg-space` (default: the space the collection metadata names) and `--full` (complete NWB files instead of stubs).

## Adding Content to the NWB

Besides the recording, a dataset can write what it already holds (trials, time intervals, sorted units, sampled behavioural series) into the same NWB file. `run_conversion` takes a function that receives the NWB file after the recording is added and before it is written; nothing reopens a written file:

```python
from fairephys.converters import add_trials, add_units

def add_behaviour(nwbfile):
    add_trials(nwbfile, trials, TRIAL_COLUMNS, stub_test=stub_test)   # DataFrame: start_time, stop_time, ...
    add_units(nwbfile, units, UNIT_COLUMNS, description="Units sorted by the lab",
              resolution=1 / sampling_rate, stub_test=stub_test)

converter.run_conversion(paths["nwb"], meta, stub_test=stub_test, add_to_nwbfile=add_behaviour)
```

Each helper takes plain data and the dataset's own column descriptions. With `stub_test`, a helper cuts its content to the span of the stub recording (a table keeps the rows that overlap it, a unit its spikes inside it), so a stub stays a consistent short file.

`fairephys.events` reads the events these tables are built from: a Neo event channel, the epocs of a TDT tank, the trials of a MonkeyLogic BHV2 file, and `match_codes` to align two code sequences (for example the codes in the recording with those of the behaviour file). What the codes mean, and how a lab synchronizes its clocks, stays in the dataset's `curate.py`. The BIDS events files are written with `fairephys.bids.generate_events_tsv`.

## Auxiliary Signals

Eye tracking, EMG and motion channels recorded with the neural data are declared under `Auxiliary Devices` in `hardware.yaml`, each with a `modality` (`physio`, `emg` or `motion`) and the names of its channels. `fairephys.bids.aux` finds those channels in the recording (`extract_aux_groups`); the dataset's `curate.py` calls `generate_aux_sidecars` in its BIDS step and `write_aux_bids_data` in its conversion. Physio files sit next to the ecephys data, motion files in `motion/`, EMG as EDF in `emg/`.

## Neo Enrichment

```bash
python -m fairephys.enrichment.neo session.ns5 -f blackrock --json
```

| Field | Description | odML Location |
|-------|-------------|---------------|
| `sampling_rate_hz` | Actual sampling rate | Recording System section |
| `duration_s` | Recording duration | Session section |
| `runs` | Start time and duration of each run, when curate.py gives the run of each Neo segment | Neo Metadata section |
| `signal_channels` | Channel names, gains, units | Neo Metadata section |

## Probes and Placements

A `Hardware.Probes` entry is one physical device (a probe of a model, or one electrode of a microdrive); two probes of one model are two entries. An `Electrode Placement` is one use of a device in a session and names it exactly, so a session cannot name a device twice (the compiler rejects it). `probe_ids(doc)` derives each device's id over the dataset's device list (`S-Probe01`, `S-Probe02`), the same in every session: BIDS `probe_name`, NWB `ElectrodeGroup` and ProbeInterface `probe_id` all use it. A device moved between runs records `DV by run`; each run's NWB group takes its depth. A placement's `electrode identifiers` are the contacts recorded that session (a Neuropixels bank).

### Acute and chronic recordings

Where the placements are written depends on how the probes were used:

- **Acute recordings:** probes are inserted anew in each session, so each session lists its own `Electrode Placements` in `recordings.yaml`. `fairephys init --acute` adds this file to the scaffold.
- **Chronic recordings:** arrays are implanted once, so their placements are written once, as `Implanted Probes` under the subject's `Surgical Preparation` in `subject.yaml`. They apply to every session of that subject; a session that lists its own `Electrode Placements` uses those instead.

The `recording type` field (`acute` or `chronic`) in `hardware.yaml` records which case applies. It is not the BIDS `RecordingType`, which says whether a recording is continuous or discontinuous and is read from the recording itself.

## Channel Mapping

Channel mapping connects probe contacts to recording device channels:

1. **Placement**: `channels: "33-64"` on the Electrode Placement, the device's channels in that session (1-based positions in the curated stream)
2. **Manual**: `channel mapping` in hardware.yaml when the pinout is fixed
3. **Pattern extraction** in curate.py when channel names encode contact numbers (e.g. `Pr1_24`: probe 1, contact 24)

Channels no placed probe is wired to stay in the NWB, in the electrode group `unplaced` (no electrode in the brain for them that session) without probe geometry; BIDS channels.tsv gives them no `electrode_name`.

## ProbeInterface Integration

`probes/builder.py` creates ProbeInterface objects from odML (see `probes/README.md`): identity (name, manufacturer, model_name, serial_number), the other fields as annotations with their units, contact ids `01`, `02`, ... numbered from the tip, or from the top with `first contact: top` (library probes keep theirs), device channel indices from the placement or the channel mapping, and an `x_offset` annotation per probe for the side-by-side group layout. The PNG files show `contact_id:device_channel` per contact.

```python
from fairephys.probes import build_probe_group, build_session_probe_group, save_probe_group

group = build_probe_group(doc["Hardware"]["Probes"].sections)   # the dataset's devices
session_group = build_session_probe_group(doc, session)         # one Probe per placement, wired
save_probe_group(session_group, output_dir / f"sub-{sub}_ses-{ses}")   # JSON + PNG files
```

## Output Structure

```
my-dataset/curated-data/                # the BIDS root
  dataset_description.json
  participants.tsv, participants.json
  .bidsignore                           # metadata, datacite.yml, LICENSE
  datacite.yml, LICENSE, README.md
  metadata/
    my-dataset.odml.xml, .odml.yaml, .odml.html
    my-dataset.instances.yaml
    my-dataset.openminds.jsonld
  probes/
    sub-<sub>_ses-<ses>/                # the devices placed in the session
      probe_group.json, probe_group.png
      <probe id>.json, <probe id>.png
  sub-<sub>/
    ses-<ses>/
      ecephys/
        *_probes.tsv, *_probes.json
        *_electrodes.tsv, *_electrodes.json
        *_channels.tsv, *_channels.json
        *_task-<task>_ecephys.json
        *_task-<task>_ecephys.nwb
        *_events.tsv, *_events.json     # when the dataset writes events
        *_physio.json, *_physio.tsv.gz  # auxiliary physio such as eye tracking, when declared
      motion/, emg/                     # auxiliary motion and EMG data, when declared
my-dataset/validation/                  # sanity reports per session, dataset.md
```

## Version Control with DataLad

The recommended layout keeps each dataset as a DataLad dataset with two subdatasets, so that the raw data, the curation and the outputs each have their own history:

```
my-project/                         # DataLad dataset: the curation (templates, scripts)
  collection/metadata/              # shared project-level metadata (optional)
  datasets/my-dataset/
    curate.py
    metadata/curated/*.yaml
    raw-data/                       # subdataset: the lab's repository of the recordings
    curated-data/                   # subdataset: the outputs (the BIDS root), published on its own
    validation/                     # sanity and validation reports
```

A single dataset can be its own top-level DataLad dataset, with the same two subdatasets.

- **`raw-data`** is the lab's repository, installed as it is. The curation only reads it: no commit, no change of a file, no change of its history. Its large files are fetched on demand with `datalad get` before a step reads them.
- **`curated-data`** holds the curated outputs of `curate.py` and nothing else; the validation reports stay in the dataset folder. It is published separately (for example on GIN, with the DataCite record for its DOI) and gets its own history of releases. The recommended `.gitattributes` keeps the text formats in git (JSON, JSON-LD, TSV, YAML, XML, HTML, CSS, JS, XSL, Markdown, text, PNG, `.bidsignore`, `LICENSE`) and puts the binary formats in git-annex (NWB, EDF, `.gz`, NIfTI, video).
- **The curation dataset** records, through its subdatasets, which commit of the raw data and which commit of the outputs belong together, so each release of the outputs points back to the recordings and templates it was made from.

The writers of fairephys replace a saved file instead of writing through it: an annexed NWB, EDF or `.tsv.gz` file is a read-only link, which the writer removes before writing the new file. A saved `curated-data` can therefore be regenerated in place without `datalad unlock`. A run is saved from the inside out:

```bash
datalad get datasets/my-dataset/raw-data/<session files>      # the raw content a step reads
uv run python datasets/my-dataset/curate.py --all             # regenerate the outputs in place
datalad save -d datasets/my-dataset/curated-data -m "<what changed>"
datalad save -d . -m "<what changed>"                         # records the new curated-data commit
datalad push -d datasets/my-dataset/curated-data --to gin     # publish the outputs
```

## Validation

- **Sanity checks** (`--sanity`, per session, `validation/<session>.md`) compare the outputs with each other: raw samples against the NWB, ProbeInterface against the BIDS electrodes and the NWB electrode table, channel counts and names across raw, BIDS and NWB. No single-format validator can make these checks.
- **Dataset validation** (`--validate`, `validation/dataset.md`) runs the BIDS validator with the bundled BEP032 schema, the NWB Inspector and the openMINDS checks. `--validate metadata` skips the NWB files and is fast; `--validate nwb` inspects only them. A failed validation exits with code 1.

## openMINDS and the Knowledge Graph

`--openminds-collection` builds the openMINDS collection from the odML and needs a KG token (`EBRAINS_KG_TOKEN` or `~/.config/fairephys/kg_token`), because it reuses the nodes that already exist in the KG. `--all` leaves the collection out without a token, says so in one line and goes on. `--kg-push` uploads the collection to the space `--kg-space` names; `--all` never pushes.

## Short Test Files

By default, a conversion writes short test files (stubs): the first samples of each recording segment, at least 100 and more than the channel count. They take seconds to write, so the whole pipeline can be rerun while the metadata are still changing. Once everything is final, `--full` converts the complete recordings.

- **After a metadata change**, rerun the steps that write no recordings: `--compile --build-probes --bids-layout --validate metadata`, then the exports.
- **After a change that affects the conversion** (channel mapping, probe geometry, which streams are written), convert again: a test file first, check it, then `--full`.

## Quick Start

```bash
fairephys init my-dataset --format blackrock   # templates and curate.py
# fill in my-dataset/metadata/curated/*.yaml, then:
uv run python my-dataset/curate.py --all       # full pipeline with short test NWB files

# Or step by step:
uv run python my-dataset/curate.py --compile
uv run python my-dataset/curate.py --build-probes
uv run python my-dataset/curate.py --bids-layout --session <session id>
uv run python my-dataset/curate.py --convert --session <session id>
uv run python my-dataset/curate.py --sanity --session <session id>
uv run python my-dataset/curate.py --validate

# openMINDS + DataCite
uv run python my-dataset/curate.py --openminds-collection
uv run python my-dataset/curate.py --datacite --readme
```

## References

- [NeuroConv docs](https://neuroconv.readthedocs.io/)
- [ProbeInterface docs](https://probeinterface.readthedocs.io/)
- [BIDS BEP032](https://bids.neuroimaging.io/bep032)
- [PyNWB ecephys tutorial](https://pynwb.readthedocs.io/en/stable/tutorials/domain/ecephys.html)
- [openMINDS documentation](https://openminds-documentation.readthedocs.io/)
- [fairgraph](https://fairgraph.readthedocs.io/)
