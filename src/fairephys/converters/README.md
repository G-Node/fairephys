# fairephys.converters

NWB conversion using neuroconv with ProbeInterface integration.

## Files

| File | Purpose |
|------|---------|
| `base.py` | `EphysConverter` (neuroconv data interfaces) and `RecordingConverter` (any SpikeInterface recording); the shared steps |
| `metadata.py` | Build NWB metadata dict from odML |
| `blackrock.py` | Blackrock converter (neuroconv NWBConverter) |
| `tdt.py` | TDT converter (neuroconv NWBConverter) |
| `plexon.py` | Plexon2 converter (.pl2 through PL2FileReader.dll with Wine and zugbruecke: `wine` must be on PATH) |
| `spikeglx.py` | SpikeGLX converter (the probe's AP stream, with `lf=True` its LF stream too; NIDQ readers and the sync-pulse clock mapping) |
| `tdt_sev.py` | TDT SEV-only converter (direct binary read, a `RecordingConverter`) |
| `extras.py` | The content a dataset adds inside the hook: trials, time intervals, units, sampled series |

## The hook: add_to_nwbfile

Both converters' `run_conversion(nwbfile_path, metadata, stub_test, add_to_nwbfile)` take the same hook: `add_to_nwbfile(nwbfile)` is called with the open `NWBFile` after neuroconv added the recording (its devices, electrode groups, electrode table and ElectricalSeries) and before the file is written, so a dataset's further content (trials, states, auxiliary and behavioural series, sorted units) goes into the same write. Nothing reopens a written file: a dataset never appends to an NWB. `EphysConverter.run_conversion` is neuroconv's sequence split open for the hook: `validate_metadata`, `create_nwbfile` (`make_nwbfile_from_metadata` and `add_to_nwbfile`), the hook, `write_nwbfile` (`configure_and_write_nwbfile`, neuroconv's chunking and compression). In a test file (`stub_test`) every recording stream is cut by `stub_recording` while it is written; the hook's content is cut by the helpers below.

## The helpers: extras.py

Each helper takes plain data and the caller's descriptions and writes into the `NWBFile` with pynwb; a column without a description, or a description of a column the data lack, is a `ValueError`. With `stub_test=True` (the dataset passes its own flag into the hook) the content is cut to `recording_span(nwbfile)`, the first to the last sample over the file's ElectricalSeries, which in a test file is the stub rule's span: a series keeps the samples inside it and at least its first 100 samples from the first one in it (`STUB_SAMPLES`, the recording stub's own rule, so a 100 Hz series beside a 30 kHz recording keeps 100 frames rather than the one frame the span holds, which NWB readers take as a transposed series), a table the rows whose interval overlaps it, a unit its spike times inside it (its presence intervals clipped; a unit absent from the span is left out). A table left without a row, and a units table without a unit, is not written at all (hdmf cannot write an empty column), so a test file may hold no trials; in a test file a units table is not written either when no spike of any unit falls in the span (the NWB Inspector cannot evaluate a table of empty spike lists); a series that ends before the span is not written (`add_timeseries` returns None and logs it), as an empty series is an NWB Inspector violation.

| Helper | Writes |
|--------|--------|
| `add_trials(nwbfile, trials, descriptions, stub_test)` | the trials table from a DataFrame with `start_time`, `stop_time` and the dataset's columns (seconds in the file's time base, the ElectricalSeries' clock) |
| `add_time_intervals(nwbfile, name, intervals, descriptions, description, stub_test)` | a `TimeIntervals` table (states, epochs, events) of the same shape |
| `add_units(nwbfile, units, descriptions, description, resolution, stub_test)` | the units table: per unit `spike_times`, optional `obs_intervals` and `channels` (channel names, the electrode rows by `channel_name`), and its properties as columns; `resolution` (seconds, the sorter's sample period) is required, the NWB Inspector flags a units table without it |
| `add_timeseries(nwbfile, name, data, rate, starting_time, unit, description, parent, kind, stub_test, **kwargs)` | a series at a rate: a `TimeSeries` or a subclass (`kind=SpatialSeries` with `reference_frame=...`) in the acquisition (`parent=None`), a processing module or a container in one (`Position`, `EyeTracking`); neuroconv's write compresses it like the recording |

## SpikeGLX AP and LF

`SpikeGLXConverter(source_data, probe_id, lf=False)` writes the AP stream of `source_data["Recording"]` ("imec0.ap"). With `lf=True` the probe's LF stream ("imec0.lf", neuroconv's SpikeGLX interface per stream) is written too, as a second ElectricalSeries in the same file: neuroconv's names `ElectricalSeriesAP` and `ElectricalSeriesLF` (the slot appended, `ElectricalSeriesAPImec0`, when the run enabled more than one probe), each at its own first-sample time on the probe's clock, the file anchored on the AP's first sample; a test file holds the stub rule's samples of both. The probe of the AP stream's .meta is attached under `probe_id` to both streams, as SpikeInterface's SpikeGLX extractor does (the LF stream records the same electrodes; its own .meta names its channels 384 to 767 in `snsSaveChanSubset`, which probeinterface's `read_spikeglx` cannot slice to a probe; an LF stream of another channel count fails SpikeInterface's `set_probe` when the extractor is built), and the channels keep the .meta names (`AP0`, `LF0`), so the AP and the LF channels of a contact are two rows of the electrode table sharing `group_name`, `electrode_name` and position (neuroconv joins the rows only under its own `AP0,LF0` names). A dataset whose AP band is hardware high-passed (`apfilt=1` in the IMRO table) carries its LFP only in the LF stream and chooses `lf=True`; the BIDS channels.tsv of that dataset then lists the LF channels as well.

## A placement without a brain region

NWB requires an `ElectrodeGroup.location`. A placement whose `brain region` (and `target`) is empty gets the `placeholder` the registry declares for the field (`nwb_mappings.yaml` `Ecephys.ElectrodeGroup.location`): neuroconv's own `"unknown"` (`neuroconv.tools.spikeinterface._get_ecephys_metadata_placeholders`, kept "so they are easy to identify downstream"), with the placement's hemisphere when it names one (`unknown, left hemisphere`, the format of a filled region). It is a placeholder of the target format, not a value of ours: the conversion logs it, the sanity report warns per group, and BIDS (`anatomical_location` n/a) and openMINDS keep the region empty. A dataset that converts such a placement states it under Assumptions in its curation README.

## Electrode table column order

neuroconv 0.10.2 adds the electrode table's property columns (`rel_x`, `rel_y`, `electrode_name`, ...) in the order of a Python set (`_add_electrodes_to_nwbfile`, `properties_to_add = set(data_to_add)`), which follows string hashing and so differs between runs; the columns and their values are the same. hdmf's `DynamicTable` offers no public way to reorder the columns of a built table (`colnames` cannot be set again), and fairephys does not patch neuroconv, so the order is left as written; the sanity report lists the columns sorted and says so.

## RecordingConverter

For raw files no Neo reader opens (per-channel HDF5, flat binary, a lab's own format) a dataset builds a SpikeInterface recording (`si.read_binary`, `NumpyRecording`, or a lazy `BaseRecording` whose segment's `get_traces` reads only the asked slice) and passes it to `RecordingConverter(recording, probe_group_path)`. `run_conversion(nwbfile_path, metadata, stub_test, add_to_nwbfile)` applies the same steps as `EphysConverter`: the probe group's channel groups and positions, electrode locations from the metadata, the metadata anchored on the first sample, the one test file rule (`stub_recording`), the devices with their models, and neuroconv's `add_recording_to_nwbfile`. The samples are written as stored (int16 counts stay int16); the recording's `gain_to_uV` and `offset_to_uV` give `conversion` (`channel_conversion` for unequal gains) and `offset` in volts, so a dataset sets the gain from the odML, never a literal (a float recording in volts sets `gain_to_uV=1e6, offset_to_uV=0`). A recording without them is a `ValueError` before anything is written: its samples would read as volts in every NWB reader.

The sanity checks take the same recording as the raw side: `run_sanity_checks(None, paths, label, validation_dir, raw_recording=recording)`.

### The scale from the odML

`recording_scale(doc_or_section)` gives `(gain_to_uV, offset_to_uV)` from the Recording System's `gain`, `gain unit` and `adc zero` (hardware.yaml): `gain unit` is "uV per count" or "V per count" (the gain in V per count is multiplied by 1e6), `offset_to_uV = -adc zero * gain_to_uV` (signed counts state 0, a 12-bit ADC stored around mid-scale 2048, each with its source). A missing gain, a gain that is not strictly positive, a missing unit or another unit, or a missing `adc zero` raises. A dataset passes the pair to its recording (`set_channel_gains`, `set_channel_offsets`) before `RecordingConverter`; `gain` must be the scale of the stored counts, not an amplifier factor.

## Data Flow

```
odML Document + Session
        |
        v
  build_nwb_metadata()
        |
        v
  BlackrockConverter / TDTConverter / PlexonConverter /
  SpikeGLXConverter / RecordingConverter (TdtSevConverter) (probe_group_path)
        |
        +-- Attaches ProbeInterface geometry
        +-- Sets group_name property per probe
        |
        v
  run_conversion(stub_test=True)
        |
        v
  sub-*/ses-*/ecephys/*.nwb
```

## Usage

```python
import odml
from fairephys.converters import BlackrockConverter, build_nwb_metadata

doc = odml.load("dataset.odml.xml")
session = doc["Sessions"]["t150327001"]

# Build metadata from odML (the session's start time is the odML's)
metadata = build_nwb_metadata(doc, session)

# Convert with probe attachment
converter = BlackrockConverter(
    source_data={"Recording": {"file_path": "data.ns5"}},
    probe_group_path="probes/probe_group.json",
)
converter.run_conversion("output.nwb", metadata, stub_test=True, add_to_nwbfile=None)
```

## Metadata Structure

`build_nwb_metadata()` returns neuroconv-expected dict:

```python
{
    "NWBFile": {
        "session_description": str,
        "identifier": str,
        "session_start_time": datetime,
        "experimenter": list[str],
        "lab": str,
        "institution": str,
    },
    "Subject": {
        "subject_id": str,
        "species": str,
        "sex": "M" | "F" | "U",
        "age": "P15Y",  # ISO 8601
    },
    "Ecephys": {
        "Device": [...],
        "ElectrodeGroup": [...],
        "ElectricalSeries": {...},
    },
}
```
