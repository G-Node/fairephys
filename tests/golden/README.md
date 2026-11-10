# Test dataset

Short Title: test-ds | version: 1.0.0

## Authors

Jane Doe (1), Bo Lee (2)

### Affiliations

1. Test Lab, Test Uni, Testville, Germany
2. Other Uni

### Corresponding Author(s)

Jane Doe: jane@example.org

## Summary

A test experiment

Part of project P.

## Version Specifications

First release.

NWB files.

## Materials and Methods

### Subject

- **Species:** Macaca mulatta
- **Subject ID:** MonkeyA
- **Biological sex:** female
- **Age category:** adult
- **Age:** 7 years (at 2024-03-15)
- **Weight:** 9.5 kg
- **Head post material:** titanium

### Ethics

Approved by the ethics committee.

### Anatomical Location

Brain regions (UBERON anatomy terms): primary motor cortex. The region of each probe per session is listed under Recording Sessions.

### Recording Setup

- **Recording system:** MySystem
- **Sampling rate:** 30000 Hz
- **System channels:** 128
- **Acquisition software:** Central 7.0
- **Hardware filters:** Highpass at 0.3 Hz (Butterworth); Bandpass at 1-5000 Hz
- **Utah Array:** utah (Utah, Blackrock), 96 contacts, 400 µm pitch
  - Notes: Channels 1 to 4 were broken from the first session (lab, 2024-04-01).
- **Tungsten:** single electrode, FHC, 1 contact

### Behavioral Protocol

**Test Task**

Resting state in the dark.

- **Task during the recordings:** none
- **Other tasks on the recording days:** A reaching task.
- **Subject positioning:** head fixation (headpost), primate chair, seated
- **Monitoring:** eye tracking (video)

### Data Processing

**State Annotation** (video annotation)

The states this step annotates, one row each: the code in the raw files, the label and a description.

| Code | Label | Description |
|------|-------|-------------|
| RS | rest | Calm rest |
|  | move | Movement |

### Recording Sessions

2 recording sessions from 2024-03-15 to 2024-03-18.

One row per recording: session id, date, duration, the probes placed (with brain region and hemisphere) and a description.

| Session | Date | Duration | Probes | Description |
|---------|------|----------|--------|-------------|
| 01 | 2024-03-15 | 24.3 min | Utah Array: primary motor cortex (left) | First |
| 02 | 2024-03-18 | 53 min | Utah Array: primary motor cortex (left) | Second |

## Usage Notes

Channel 7 is noisy.

Read with [PyNWB](https://pynwb.readthedocs.io/).

The data are shared under the [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) licence.

## Data Records

The recordings, provided as Blackrock NSx (.ns5) and Video (.MTS), were converted.

Validation: [BIDS validator](https://github.com/bids-standard/bids-validator) 2.2.6 with the BEP032 schema (commit 508cfc7b) and [NWB Inspector](https://nwbinspector.readthedocs.io/) 0.7.2. Conversion: fairephys 0.1.0 (commit abc1234, with uncommitted changes) and [NeuroConv](https://neuroconv.readthedocs.io/) 0.10.2.

The data are organised following the BIDS extension proposal BEP032 Microelectrode Electrophysiology, draft (bids-specification PR #2307 at commit 508cfc7b), which builds on BIDS 1.11.2 and is not yet part of the BIDS specification.

```
Test/
    metadata/
        test-ds.instances.yaml [openMINDS terms of the controlled vocabulary values in the odML record]
    probes/
        sub-MonkeyA_ses-01/
            Utah.json [contact geometry of the devices placed in the session, of one device or all (probe_group.json)]
        Utah.json [contact geometry of one device and the wiring of its contacts to the recording channels]
    sub-MonkeyA/
        ses-01/
            beh/
                sub-MonkeyA_ses-01_task-rest_video.MTS [video recording of the session]
            ecephys/
                sub-MonkeyA_ses-01_task-rest_channels.tsv [the recorded channels, one row each]
                sub-MonkeyA_ses-01_task-rest_ecephys.nwb [continuous neural recording of the session with its electrodes, devices and subject metadata]
                sub-MonkeyA_ses-01_task-rest_electrodes.tsv [the electrode contacts with their positions, one row each]
                sub-MonkeyA_ses-01_task-rest_probes.tsv [the probes placed in the session, one row each]
                sub-MonkeyA_ses-01_task-rest_recording-eye_physio.tsv.gz [continuous physiological signals of the session, one column per signal]
        ... (1 more session)
    LICENSE [licence of the dataset with its legal code]
    datacite.yml [citation metadata for the DOI registration on GIN]
    dataset_description.json [name, BIDS version, licence, authors, funding and references of the dataset]
    participants.tsv [the subjects, one row each]
```

One row per file type: its format, what the files hold, and the format specification with the software that wrote them.

| File | Format | Content | Specification |
|------|--------|---------|---------------|
| `*_ecephys.nwb` | NWB (HDF5) | Continuous neural recording of the session with its electrode table, devices and subject metadata, and the trials, intervals, events or sorted units the dataset holds | Neurodata Without Borders (NWB), written with NeuroConv 0.10.2 and PyNWB |
| `ecephys/*_channels.tsv` | TSV | One row per recorded channel | BIDS 1.11.2, BEP032 draft, written with fairephys 0.1.0 |
| `*_electrodes.tsv` | TSV | One row per electrode contact | BIDS 1.11.2, BEP032 draft, written with fairephys 0.1.0 |
| `*_probes.tsv` | TSV | One row per probe placed in the session | BIDS 1.11.2, BEP032 draft, written with fairephys 0.1.0 |
| `*_physio.tsv.gz` | TSV (gzip) | Continuous physiological signals (eye), one column per signal and no header row; the matching physio.json gives the column names, sampling frequency and start time | BIDS 1.11.2, written with fairephys 0.1.0 |
| `*_video.MTS` | Video (MTS) | Video recording of the session | MTS video, camera TestCam TC-1, recorded with TestCapture 2.0 |
| `dataset_description.json` | JSON | Name, BIDS version, licence, authors, funding, references and the software that generated the dataset | BIDS 1.11.2, written with fairephys 0.1.0 |
| `participants.tsv` | TSV | One row per subject | BIDS 1.11.2, written with fairephys 0.1.0 |
| `datacite.yml` | YAML | Citation metadata for the DOI registration on GIN: title, authors, description, keywords, licence, funding and references | GIN datacite.yml (DataCite), written with fairephys 0.1.0 |
| `LICENSE` | Text | Licence of the dataset with its legal code | Plain text, written with fairephys 0.1.0 |
| `probes/*.json` | JSON ([ProbeInterface](https://probeinterface.readthedocs.io/)) | Contact geometry of each device: contact positions and shapes and the wiring of the contacts to the recording channels | ProbeInterface JSON, written with ProbeInterface |
| `probes/sub-*_ses-*/*.json` | JSON (ProbeInterface) | Contact geometry of the devices placed in the session, one file per device and probe_group.json for all: contact positions and shapes and the wiring of the contacts to the recording channels | ProbeInterface JSON, written with ProbeInterface |
| `metadata/*.instances.yaml` | YAML | openMINDS terms of the controlled vocabulary values in the open metadata Markup Language (odML) record | YAML, written with fairephys 0.1.0 |

## How to Cite

Doe, J. & Lee, B. Test dataset, version 1.0.0. DOI to be assigned at publication on GIN (2026).

## Acknowledgements

Test Foundation, Test Grant (GRANT-001)

Curation funded by the EC.

## Author Contributions

**Jane Doe:** Recordings, data collection

**Bo Lee:** Data management, coordination

## References

1. Doe, J. A paper. J. Neurosci. 1, 2 (2024). https://doi.org/10.1234/paper
2. Doe, J. A thesis without identifier (2020).
