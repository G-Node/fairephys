# Changelog

## 0.1.3 (unreleased)

- A behavioural protocol's `described in` is a DOI and becomes a DOI node (protocol schema 1.2).
- A person's digital identifier with a type needs its value, an ORCID in its own form; both are empty otherwise (dataset schema 1.1).
- Knowledge Graph: an organization built from the metadata carries its ROR identifier and short name (`funder identifier`; new fields for institutions, funders and manufacturers; collection schema 1.1, hardware schema 1.2).
- Knowledge Graph: a dataset version links its recording protocol; an array usage links the subject state it was used in; a recording activity names who performed it (as the NWB experimenter) and its study targets (the regions of its placements); a setup is an acquisition system; a probe's contact impedance range is its intrinsic resistance.

## 0.1.2 (2026-11-19)

- openMINDS Python 0.6 and fairgraph 0.14: contribution roles such as design, supervision and funding are found.
- Dataset card in the Knowledge Graph without the usage statement.
- Template roles are openMINDS contribution types.
- Descriptor: row and column spacing of a probe where they differ.
- `probes.json`: no "n/a" for an unknown manufacturer.

## 0.1.1 (2026-11-18)

- Compile keeps a value such as `1920x1080` as text. A unit follows its number after a space (`24 kg`).
- A controlled term that openMINDS does not hold is named in the warning.
- The message after a compile and the comment on the BIDS age are corrected.
- README: where the KG token goes; the figure is linked by its full URL.
- `docs/pipeline.md`: how to run a release conversion so that each NWB file records a clean repository state.
- A generated probe can name its `unconnected contacts`; they are left out of its geometry and counts (hardware schema 1.1).
- An empty `total span` takes the larger extent of the contacts.
- `Filters: none` states that no hardware filter was applied.
- Electrode group description: probe type and number of electrodes recorded.
- NWB files carry the registry's electrode column descriptions.
- Data descriptor: terms written out and linked at first mention, a short file table and a plainer file tree.
- Statements can name the software versions and the repository address.
- BIDS sidecars describe names and locations in the words of the specification.
- Probes implanted in a subject have their probe files once, in `probes/sub-<id>/`.
- Larger probe pictures with one-line labels, a zoomed view for a dense probe; no group picture and none for a single contact.
- A dataset with several tasks gives one description per task (`task descriptions`, protocol schema 1.1).
- `keep_unplaced=False` keeps only the channels of placed probes; the raw check matches channels by name.
- The raw check can compare a file with several streams.
- `probes.json` describes `probe_name` in the words of the BIDS specification and `quality` also where it is empty.
- Data descriptor: the sessions table caption and micrometre symbols corrected.

## 0.1.0 (2026-11-16)

First release as a package of its own (pre-alpha), split from the NHP-EBRAINS curation repository, where it curated 12 electrophysiology and 2 MRI datasets.

- A lean `curate.py`: `curation.session_paths` and `curation.run_pipeline` (the shared `main`), `probes.write_session_probe_group`, and `bids.write_session_sidecars`, `bids.write_run_sidecars` and `bids.write_dataset_files` replace the scaffolding every dataset repeated; `neo_enrich_document` takes a `value_origin` for a Neo Metadata dict a dataset builds without a Neo reader.
- The `curate.py.template` of `fairephys init` is written on these functions (about 100 lines, was 250); its `--output` flag is gone.
