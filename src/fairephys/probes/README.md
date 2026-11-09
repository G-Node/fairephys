# fairephys.probes

Builds ProbeInterface objects from odML hardware metadata.

## Files

| File | Purpose |
|------|---------|
| `builder.py` | ProbeGroup of the dataset's devices or of one session's placements; positions back into the odML; JSON + PNG |
| `library.py` | ProbeInterface library lookup (manufacturer key, model) |

## Data flow

```
odML Hardware/Probes sections
   |
   +-- build_probe_group(sections)             the dataset's devices
   +-- build_session_probe_group(doc, session) one Probe per placement, wired
   |
   v
enrich_odml_positions(doc, group)   contact_x, contact_y, geometry, electrode identifiers
   |
   v
save_probe_group(group, dir)        probe_group.json/png, <probe_id>.json/png
```

`write_session_probe_group(doc, session, output_dir, title="Probe Group")` is the `--build-probes` step of a session: `build_session_probe_group` saved with `save_probe_group` under `output_dir` (`curation.session_paths(...)["probe_group"].parent`), the folder emptied first so files of earlier device ids do not stay; it returns the path of `probe_group.json`. It empties the folder it is given, so a caller passes the session's own folder, never a base folder such as `curated-data/probes`.

## Probe entries

A `Hardware.Probes` entry is one physical device (see `fairephys.placements`).

| Field | Use |
|-------|-----|
| `name` | required; placements name the device by it |
| `manufacturer`, `model` | looked up in the ProbeInterface library; `library manufacturer` is the library's key when it differs from the lab's name (`cambridgeneurotech`). A probe the library does not have is built parametrically; no network or a server error is raised |
| `number of contacts`, `contact pitch`, `contact arrangement` (linear, matrix), `number of columns`, `number of rows` | parametric geometry |
| `row spacing`, `column spacing` | a matrix whose spacings differ: y and x pitch in place of `contact pitch` (an error on a linear probe) |
| `contact positions` | `contact ids`, `x`, `y` (micrometer), one per contact: the probe is placed at them, before any library lookup or generation; `contact_x`, `contact_y`, filled `electrode identifiers`, `number of contacts` and `total span` get the value origin "the YAML's contact positions"; `contact pitch`, `contact arrangement` and `number of columns` are left as the YAML has them (the positions do not state them) |
| `electrode identifiers` | a library probe recorded on fewer contacts than it has: the contact ids recorded (an error without them); at its full count, its contact ids (one per contact, a count mismatch is an error). A generated probe (parametric): its contact ids, one per contact in contact order (the lab's channel numbers, which then name the BIDS electrodes and the NWB `electrode_name`); a count other than `number of contacts` is an error, and on a probe at its `contact positions` they must equal the positions' `contact ids`. Left empty, filled with the contact ids by `enrich_odml_positions` |
| `channel mapping` | comma-separated device channel index per contact, in contact order |
| `first contact` | `tip` (default) or `top`: the end of a linear probe where the lab's contact 1 is |
| anything else | an annotation, with its unit when the entries are odML sections ("12 micrometer"; dicts carry no units) |

## Contact ids

Library probes keep the library's ids (Neuropixels `e0`, `e1`, ...). Parametric probes take the entry's filled `electrode identifiers`, else they are numbered from the tip (from the top with `first contact: top`), zero-padded to at least two digits (`01`..`24`, `001`..`128`). BIDS electrodes are named `{probe_id}_{contact id}`.

## Session groups and placements

`build_session_probe_group(doc, session)` gives one Probe per Electrode Placement of the session (the subject's Implanted Probes for chronic arrays; every device when there are no placements), carrying the device's id (`placements.probe_ids`), brain region and hemisphere.

- `electrode identifiers` on the placement: the contact ids of the device recorded that session (a Neuropixels bank: the electrodes of the IMRO table), in the recording's channel order; the probe is cut to them.
- `channels` on the placement (`"33-64"`, 1-based positions in the curated stream), else the device's `channel mapping`, else the block after the previous placement, give `device_channel_indices`.

## Group layout

Probes are placed side by side (`x_spacing`, 800 um) for plotting and for SpikeInterface's overlap check. Each probe's `x_offset` annotation is the offset added to its x positions (0 for the first); every reader subtracts it: `enrich_odml_positions` writes local `contact_x`/`contact_y`, the NWB converter writes local `rel_x`/`rel_y`, the sanity checks compare local positions.
