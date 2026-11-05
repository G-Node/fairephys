# fairephys.metadata

Compiles YAML templates to odML.

## Usage

```python
from fairephys.metadata import load_metadata, compile_to_odml, save_odml

meta = load_metadata("metadata/curated/")
doc = compile_to_odml(meta)
save_odml(doc, "curated-data/openminds/dataset")
```

CLI:
```bash
python -m fairephys.metadata.compiler metadata/curated/ -o curated-data/openminds/dataset
```

## Template Files

- dataset.yaml, subject.yaml, hardware.yaml
- recordings.yaml, sessions.yaml
- protocol.yaml, processing.yaml
- collection.yaml: shared, project-level (see below)

## Processing

`load_metadata(templates_dir, validate_mode, shared_dir=None)`:
1. Loads shared metadata from `shared_dir`, by default the nearest `collection/metadata/` directory above `templates_dir` (`find_shared_dir()`, bounded upward search; absent → no-op)
2. Loads the dataset's YAML files and deep-merges them on top (`deep_merge()`: nested dicts merge, dataset scalars/lists win, except `Fundings`: shared entries first, then the dataset's own; a dataset entry with the same `award number` replaces the shared one)
3. Validates every file against its JSON schema (`_schema:` key or filename)
4. Applies defaults to sessions/recordings
5. Merges recordings into sessions

## Shared metadata

Metadata common to all datasets of a curation project (the umbrella `Project` and `Consortium`, and the project `Fundings`) lives once in `<project>/collection/metadata/collection.yaml` (schema `collection.v1`), shaped like `dataset.yaml` (`Dataset: {Project: ..., Consortium: ...}`) so it merges into the same odML sections (`Dataset/Project`, `Dataset/Consortium`, `Dataset/Fundings`). A dataset may override any shared value with a local block.

`Project.dataset statement` holds standard text about the project. It is maintained only in the shared file and the openMINDS export appends it to every Dataset description (empty = nothing appended). In the same way `Project.records statement` opens the Data Records section of every data descriptor (README). Its `{tokens}` are filled from the dataset, lists as prose ("a, b and c"). An optional group `{?words {token} words}` carries the words around one token and is left out with them when the field is empty: `The original recordings{?, provided as {original data format},} were converted ...` reads "The original recordings, provided as A and B, were converted ..." or "The original recordings were converted ...".

## Curation software

`compile_to_odml()` records the versions of the pipeline's tools once, in the top-level `Curation Software` section (`provenance.py`): one subsection per tool with `name`, `version` and `purpose` (validation or conversion). Python packages (fairephys, NeuroConv, PyNWB, NWB Inspector, openMINDS Python, fairgraph) give their installed version (`importlib.metadata`); the BIDS validator gives the version pinned in the registry `spec.validator` and its `schema` and `schema commit`, read from the name of the schema file it validates against (`schema-<extension>-<commit>.json`). A tool whose version cannot be resolved is left out with a warning. The data descriptor names the versions after the records statement.

The package version of fairephys stays the same between commits, so its entry also records, when fairephys runs from a git checkout, the `commit` (short hash) of its source and `uncommitted changes` (true when files under the package directory are modified, staged or untracked; edits elsewhere in the repository, such as dataset metadata, do not count). The repository is found from the package path (`git rev-parse` and `git status --porcelain`, 10 s timeout); an installed copy, an untracked one or a missing git gives the version only (logged at debug). The entry also holds the package `description` (its summary) and a `code url` when `[project.urls]` names the source repository (label Source, Repository, Source Code or GitHub). The descriptor reads "fairephys 0.1.0 (commit abc1234)", with ", with uncommitted changes" inside the brackets when that is the case, and BIDS `GeneratedBy` is built from the same entry.

## Subject identifiers

`Subject.subject id` is a pseudonym, never the animal's name: it becomes the BIDS participant label, the NWB `subject_id` and the KG identifier (`sub-<id>`), and the compiled odML is published with the dataset. Rules: alphanumeric only (`^[A-Za-z0-9]+$`, by convention an uppercase initial); `Sessions[*].subject` must equal a `subject id`; the lab's record keys and birth dates stay private like the names, so `subject.yaml` has no field for them (validation rejects `internal identifier` and `birth date`) and the age is the `age` field, an age at the date its comment and `_valueOrigin` state ("age at <date>, <source>"; the KG carries one age per subject, the same in every SubjectState); `_valueOrigin` labels and session `notes` must not embed raw file or folder names. Real names appear only in the labs' raw data and in private, gitignored files (the names list, the raw-name mapping); never in YAML, comments, code or any other file of a shared repository, `metadata/source/` included (it is tracked, not private).

## Subjects and sessions

`subject.yaml` holds one `Subject` or, for several subjects, a `Subjects` list (one `Subject` entry each, same schema); all subjects are compiled into one document (`Subjects` section, `Subject_<id>` entries) and each session names its subject (`Sessions[*].subject`). With both keys the compiler takes `Subjects` and warns. `session_subject(doc, session)` gives a session's subject (the single `Subject` otherwise); registry paths `Subject/...` resolve through it.

A session entry is identified like a BIDS recording (sub, ses, task): its `session id`, its `subject` (session ids repeat across subjects) and, when one session recorded several tasks, its `task` (one entry per task with the same session id; entries without `task` hold the dataset's task). Several runs of one task stay one entry (`runs`). `lookup_session(doc, session_id, subject, task)` finds the entry (ValueError when several match) and `session_identity(doc, session)` gives the tokens (`subject_id`, `session_id`, `task`) that openMINDS labels are built from; the task is added to a label only when the session names one, so the labels of single-task sessions do not change.

## Section Types

From `openminds_schemas.yaml` odml_types section. Maps names like "Dataset" to types like "core.DatasetVersion".

## Enrichment

Preserves `_reference` and `_definition` suffixes as odML property attributes.
