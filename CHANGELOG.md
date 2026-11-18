# Changelog

## 0.1.1 (unreleased)

- Compile keeps a value such as `1920x1080` as text. A unit follows its number after a space (`24 kg`).
- A controlled term that openMINDS does not hold is named in the warning.
- The message after a compile and the comment on the BIDS age are corrected.
- README: where the KG token goes; the figure is linked by its full URL.
- `docs/pipeline.md`: how to run a release conversion so that each NWB file records a clean repository state.
- A generated probe can name its `unconnected contacts`; they are left out of its geometry and counts (hardware schema 1.1).
- An empty `total span` takes the larger extent of the contacts.
- `Filters: none` states that no hardware filter was applied; an empty list still means not documented.
- The electrode group description gives the probe type and the number of electrodes recorded.
- Every NWB file carries the registry's electrode column descriptions.

## 0.1.0 (2026-11-16)

First release as a package of its own (pre-alpha), split from the NHP-EBRAINS curation repository, where it curated 12 electrophysiology and 2 MRI datasets.

- A lean `curate.py`: `curation.session_paths` and `curation.run_pipeline` (the shared `main`), `probes.write_session_probe_group`, and `bids.write_session_sidecars`, `bids.write_run_sidecars` and `bids.write_dataset_files` replace the scaffolding every dataset repeated; `neo_enrich_document` takes a `value_origin` for a Neo Metadata dict a dataset builds without a Neo reader.
- The `curate.py.template` of `fairephys init` is written on these functions (about 100 lines, was 250); its `--output` flag is gone.
