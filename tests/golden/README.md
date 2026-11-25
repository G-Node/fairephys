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

Brain regions (terms from the anatomy ontology [UBERON](https://obophenotype.github.io/uberon/)): primary motor cortex. The region of each probe per session is listed under Recording Sessions.

### Recording Setup

- **Recording system:** MySystem
- **Sampling rate:** 30000 Hz
- **System channels:** 128
- **Acquisition software:** Central 7.0
- **Hardware filters:** Highpass at 0.3 Hz (Butterworth); Bandpass at 1-5000 Hz
- **Utah Array:** utah (Blackrock), 96 contacts, 400 µm pitch
  - Notes: Channels 1 to 4 were broken from the first session (lab, 2024-04-01).
- **Tungsten:** single electrode, FHC, 1 contact

### Behavioral Protocol

**Test Task**

Resting state in the dark.

- **Task during the recordings:** none
- **Other tasks:** A reaching task.
- **Subject positioning:** head fixation (headpost), primate chair, seated
- **Monitoring:** eye tracking (video)

### Data Processing

**State Annotation** (video annotation)

**Table 1.** Behavioral states of this annotation: the code of the original annotation, the label and a short description.

| Code | Label | Description |
|------|-------|-------------|
| RS | rest | Calm rest |
|  | move | Movement |

### Recording Sessions

2 recording sessions from 2024-03-15 to 2024-03-18.

**Table 2.** The recording sessions: session id, date, duration of the recordings, the probes placed (with brain region and hemisphere where given) and a short description.

| Session | Date | Duration | Probes | Description |
|---------|------|----------|--------|-------------|
| 01 | 2024-03-15 | 24.3 min | Utah Array: primary motor cortex (left) | First |
| 02 | 2024-03-18 | 53 min | Utah Array: primary motor cortex (left) | Second |

## Usage Notes

Channel 7 is noisy.

Read with [PyNWB](https://pynwb.readthedocs.io/) (RRID:SCR_017452).

The data are shared under the Creative Commons Attribution 4.0 International Public License ([CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)).

## Data Records

The data are organized following the Brain Imaging Data Structure ([BIDS](https://bids.neuroimaging.io/); RRID:SCR_016124) extension proposal BEP032 Microelectrode Electrophysiology ([bids-specification pull request #2307](https://github.com/bids-standard/bids-specification/pull/2307)), a draft extension of BIDS 1.11.2. The content of the BIDS files is defined in the [BEP032 specification](https://bids-specification--2307.org.readthedocs.build/en/2307/). Beyond BIDS, the layout holds the folder `metadata/` with the open metadata Markup Language ([odML](https://g-node.github.io/python-odml/); RRID:SCR_001376) record, the canonical record of all metadata of the dataset, and the [openMINDS](https://openminds.om-i.org/) (RRID:SCR_023173) metadata; the file table below names every file type.

The recordings, provided as Blackrock NSx (.ns5) and Video (.MTS), were converted.

Validation: [BIDS validator](https://github.com/bids-standard/bids-validator) 2.2.6 (RRID:SCR_017255) with the BEP032 schema (commit 508cfc7b) and [NWB Inspector](https://nwbinspector.readthedocs.io/) 0.7.2 (RRID:SCR_025465). Conversion: fairephys 0.1.0 and [NeuroConv](https://neuroconv.readthedocs.io/) 0.10.2.

File names are made of key-value pairs and end with the file type: `sub` (subject), `ses` (session), `task` (task, or rest) and `recording` (recorded signal). The files of a session are in one folder per data type: `beh` (behavior) and `ecephys` (extracellular electrophysiology).

```
Test/
    metadata/
        test-ds.instances.yaml [for each metadata value chosen from a fixed list, such as the species or a brain region, the standard term and its web address]
    probes/
        sub-MonkeyA_ses-01/
            Utah.json [contact geometry of each probe, as data and as a drawing]
        Utah.json [contact geometry of one probe and the wiring of its contacts to the recording channels]
    sub-MonkeyA/
        ses-01/
            beh/
                sub-MonkeyA_ses-01_task-rest_video.MTS [video recording]
            ecephys/
                sub-MonkeyA_ses-01_task-rest_channels.tsv [one row per recorded channel]
                sub-MonkeyA_ses-01_task-rest_ecephys.nwb [continuous neural recording with its electrodes, devices and subject metadata]
                sub-MonkeyA_ses-01_task-rest_electrodes.tsv [one row per electrode contact, with its position]
                sub-MonkeyA_ses-01_task-rest_probes.tsv [one row per probe placed in the session]
                sub-MonkeyA_ses-01_task-rest_recording-eye_physio.json [column names, sampling frequency and start time of the matching physio.tsv.gz]
                sub-MonkeyA_ses-01_task-rest_recording-eye_physio.tsv.gz [eye-tracking signals of the recording, one column per signal]
        ... (1 more session)
    LICENSE [license of the dataset with its legal code]
    datacite.yml [citation metadata for the DOI registration on GIN]
    dataset_description.json [name, BIDS version, license, authors, funding and references of the dataset]
    participants.tsv [one row per subject]
```

**Table 3.** The file types of the dataset: format and content.

| File | Format | Content |
|------|--------|---------|
| `*_ecephys.nwb` | Neurodata Without Borders ([NWB](https://nwb.org/); RRID:SCR_015242) (HDF5) | Continuous neural recording with its electrode table, devices and subject metadata |
| `ecephys/*_channels.tsv` | tab-separated values (TSV) | One row per recorded channel: its electrode contact, signal type, unit, sampling frequency, reference, gain and status |
| `*_electrodes.tsv` | TSV | One row per electrode contact: its probe, position within the probe, hemisphere and brain region |
| `*_probes.tsv` | TSV | One row per probe placed in the session: its type, placement coordinates and angles, brain region, manufacturer and model |
| `*_physio.tsv.gz` | TSV (gzip) | Eye-tracking signals (eye), one column per signal and no header row |
| `*_physio.json` | JavaScript Object Notation (JSON) | Column names, units, sampling frequency and start time of the matching physio.tsv.gz |
| `*_video.MTS` | Video (MTS) | Video recording |
| `dataset_description.json` | JSON | Name, BIDS version, license, authors, funding, references and the software that generated the dataset |
| `participants.tsv` | TSV | One row per subject: species, sex, age and weight |
| `datacite.yml` | YAML | Citation metadata for the Digital Object Identifier (DOI) registration on GIN: title, authors, description, keywords, license, funding and references |
| `LICENSE` | Text | License of the dataset with its legal code |
| `probes/*.json` | JSON ([ProbeInterface](https://probeinterface.readthedocs.io/)) | Contact geometry of each probe: contact positions and shapes and the wiring to the recording channels |
| `probes/sub-*_ses-*/*.json` | JSON (ProbeInterface) | Contact geometry of each probe: contact positions and shapes and the wiring to the recording channels |
| `metadata/*.instances.yaml` | YAML | openMINDS terms of the controlled vocabulary values in the odML record |

## Acknowledgements

Test Foundation, Test Grant (GRANT-001)

Curation funded by the EC.

## Author Contributions

**Jane Doe:** Recordings, data collection

**Bo Lee:** Data management, coordination

## References

1. Doe, J. A paper. J. Neurosci. 1, 2 (2024). https://doi.org/10.1234/paper
2. Doe, J. A thesis without identifier (2020).
