# Changelog

## 0.1.1 (unreleased)

- A value with a number glued to letters, such as `1920x1080` or `4a`, stays text at compile; a unit needs whitespace after the number (`24 kg`).
- A controlled term that the openMINDS vocabulary does not hold is reported as "no <class> term named '<name>'"; the lookup error goes to the debug log.
- The line printed after a compile no longer says that the odML holds nothing `--build-probes` adds.
- The registry comment on the subject age says that BIDS gives it as a number of years.
- The README says where the KG token goes and that `--all` runs without it, and links the figure by its full URL so that it shows on PyPI.
- `docs/pipeline.md` describes the repository state each NWB file records and how a release conversion records a clean working tree when only the output subdataset has changed.

## 0.1.0 (2026-11-16)

First release as a package of its own (pre-alpha), split from the NHP-EBRAINS curation repository, where it curated 12 electrophysiology and 2 MRI datasets.

- A lean `curate.py`: `curation.session_paths` and `curation.run_pipeline` (the shared `main`), `probes.write_session_probe_group`, and `bids.write_session_sidecars`, `bids.write_run_sidecars` and `bids.write_dataset_files` replace the scaffolding every dataset repeated; `neo_enrich_document` takes a `value_origin` for a Neo Metadata dict a dataset builds without a Neo reader.
- The `curate.py.template` of `fairephys init` is written on these functions (about 100 lines, was 250); its `--output` flag is gone.
