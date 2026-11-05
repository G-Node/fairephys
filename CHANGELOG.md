# Changelog

## 0.1.0 (unreleased)

First release as a package of its own (pre-alpha), split from the NHP-EBRAINS curation repository, where it curated 12 electrophysiology and 2 MRI datasets.

- A lean `curate.py`: `curation.session_paths` and `curation.run_pipeline` (the shared `main`), `probes.write_session_probe_group`, and `bids.write_session_sidecars`, `bids.write_run_sidecars` and `bids.write_dataset_files` replace the scaffolding every dataset repeated; `neo_enrich_document` takes a `value_origin` for a Neo Metadata dict a dataset builds without a Neo reader.
- The `curate.py.template` of `fairephys init` is written on these functions (about 100 lines, was 250); its `--output` flag is gone.
