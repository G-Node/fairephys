# fairephys.export

Build openMINDS Collections from odML for EBRAINS Knowledge Graph.

## Pipeline

```
compile (YAML -> odML)
  -> build_collection (odML -> fairgraph Collection)
    -> save_collection (Collection -> .openminds.jsonld)
    -> push_collection (Collection -> KG space)
```

Each `curate.py` exposes these as CLI flags:
- `--openminds-collection`: build + save JSON-LD
- `--kg-push [--kg-space <space>]`: build + push to KG; without `--kg-space` the space named in the collection metadata
- `--datacite`: generate DataCite metadata
- `--readme`: generate README from odML
- `--all`: every step and export but `--kg-push`; the openMINDS collection is built only when a KG token is present (see Authentication), otherwise `run_export_steps` prints one line, "openMINDS collection not built: no KG token (run --openminds-collection with EBRAINS_KG_TOKEN)", and goes on with DataCite, README and validation (exit 0). `--openminds-collection` asked for itself still fails without a token.

## Usage

```python
from fairephys.export import build_collection, save_collection, get_kg_client

client = get_kg_client()
collection = build_collection("curated-data/metadata/dataset.odml.xml", client,
                              target_space="collab-my-space")
save_collection(collection, "curated-data/metadata/dataset.openminds.jsonld")
```

## Modules

**collection.py**: schema-driven builder (`openminds_schemas.yaml`)
- `SchemaBuilder`: walks odML sections, builds fairgraph entities per schema
- `KGResolver`: in-memory cached KG lookups (Organizations, Persons, etc.) with exact-match filtering and space-scoped search
- Space-aware rebuilds: entities in the target space are rebuilt locally (picks up ContactInfo, TermSuggestion keywords, DOI validation); entities from other spaces (e.g. `common`) are referenced as-is. Without a target space (local JSON-LD export), all entities are rebuilt.
- KG nodes referenced as-is keep their own values. The fields where they differ from the odML (an award title, an acknowledgement, an ORCID or affiliation missing on a shared Person) are logged and collected in `collection.kg_differences`; `--openminds-collection` lists them in the openMINDS validation output. Edit such nodes in the KG editor or align the metadata.
- One digital identifier, one node: an ORCID, DOI or RRID built again (the PI's ORCID as Custodian and as Contributor, the dataset DOI on Dataset and DatasetVersion) is the node built first.
- `existence_query` (schema option): the fields fairgraph matches at push for an entity without a KG id. Funding uses the award number, since fairgraph's own check (the funder alone) would merge two grants of one funder.
- `keep_existing: true` (schema flag; Project, Consortium): shared entities owned by the KG once created. If the lookup finds the entity it is referenced as-is and recorded in `collection.keep_existing_ids`, so `push_collection()` never re-saves it. Edit such entities in the KG editor.
- One KG identity, one node: when an entity with a KG id already built in the current collection is built again (a PI appears as Custodian and as Contributor), `_build_entity` merges into the existing node (union of list properties, scalars filled only where empty) instead of creating a second object. Otherwise the openMINDS Collection keeps whichever object was added last and silently drops the other's properties.
- `template` fields build EBRAINS-style labels from odML values: `Subject.internal_identifier` = `sub-<subject id>`, `Subject.lookup_label` = `<Dataset short name>_sub-<subject id>`, and one `SubjectState` per session with `ses-<session id>` / `<short name>_sub-<id>_ses-<session id>`. The dataset prefix keeps subject labels unique per KG space even though subject ids are short pseudonyms (initials) that repeat across datasets.
- SubjectStates are placed in time (`per_session_config`): `relative_time_unit: day` sets `relative_time_indication` to the days since the subject's first dated session. Sessions without a date get none.
- Shared dataset statement: `Dataset.description` is a template, `{description}` followed by the optional token `{?Dataset/Project/dataset statement}`. The statement is written once in the shared collection metadata (`Project.dataset statement`) and appended to every dataset description; when it is empty the descriptions are unchanged. An empty optional token also drops parentheses that hold nothing else: `BehavioralProtocol.name` is `{name} ({?dataset citation})` ("Resting State Paradigm (Author et al., 2026)", the plain name without contributors), where `dataset citation` is the one derived token: the first Dataset contributor's family name, "et al." with more than one contributor, and the year of the Dataset's `release date` (a first publication elsewhere), else the release year of the export (`SchemaBuilder.dataset_citation`); `DatasetVersion.release_date` is the `release date` alone: without it the version has none and the collection validation lists it as missing (never the export date); the name keeps the export year until the date is set. An optional group `{?words {token} words}` carries the words around one token and is left out with them by the same rules (`recordings{?, provided as {format},} were`). The README reuses `fill_template()` for the shared records statement. The tokens `{bids version}` and `{bids extension}` (`templating.spec_tokens()`, from the BIDS registry `spec`: `bids_version`, `ecephys_extension`) resolve in every registry template, in the shared statements (`fill_statement()`: the dataset, version and usage statements, any other token raises) and in the records statement and the descriptor's file specifications, so a version statement can name the BIDS state the files follow.
- Related publications (`identifier_list`): each `Related Publications` entry whose identifier is a DOI, Handle or ISBN is that openMINDS node in `DatasetVersion.related_publications`; an entry of another type (a URL, an arXiv id, a PMID) or without identifier is listed in `collection.warnings`, which `--openminds-collection` writes to the openMINDS validation output. `DatasetVersion.full_documentation` is the `documentation url`, else (`fallback`) the first `IsDescribedBy` entry with a DOI.
- `iri` fields take an http(s) URL or a bare DOI (written as `https://doi.org/<doi>`); any other value (e.g. a short citation in `described in`) fails the build instead of being dropped.
- Nested entities may use `type: link` fields, resolved against top-level entities built earlier in `build_order` (this is why Consortium is built first). `nested_list` accepts a list of targets and concatenates the built ones: `Custodian.affiliations` = institutional Affiliation + Consortium Affiliation (`consortium member: true`). Consortium membership lives on the Person side (`Affiliation.member_of`); the KG shows it as a reverse link on the Consortium.

**kg.py**: EBRAINS KG integration
- `get_kg_client()`: authenticated KGClient from the token
- `push_collection()`: upload entities with retry on 500 errors, skip cross-space and `keep_existing` entities, log per-entity progress
- `_save_with_retry()`: exponential backoff for transient KG errors

**datacite.py**: DataCite metadata for GIN DOI registration
- `references`: one per `Related Publications` entry, in order: `id` with GIN's prefix of the identifier type (`doi:`, `arxiv:`, `pmid:`, `url:`; a Handle as `url:https://hdl.handle.net/<handle>`), `reftype` the entry's DataCite relation, `citation` as given. An entry without identifier, or an ISBN (no GIN prefix), is left out with a warning naming its citation; the file is still written. An identifier of no recognised form fails the compile (`fairephys.metadata.identifiers`).
- `write_datacite()` writes `datacite.yml` and, next to it, `LICENSE`: the licence name (first line, as GIN's DOI service matches it against the datacite.yml licence) and URL, then the official plain-text legal code from `_resources/licenses/<SPDX id>.txt` (from creativecommons.org, `<licence>/legalcode.txt`). An id without a stored legal code gives name and URL only, with a warning. Both files are added to `.bidsignore`.

**readme.py**: README generation from odML metadata
- Nested subject sections: one bullet per filled property, labelled with section and property in sentence case ("Head post material: titanium"); a `description` is the section's own text ("Head post: ...").
- Session durations in minutes (metadata values converted by their unit, Neo durations otherwise); the session count is of distinct sessions per subject; a Subject column with several subjects; table columns that are empty in every session are left out.
- Counts take the singular for 1 ("1 recording session", "1 contact").
- No doubled brackets (`bids.extractors.bracketed`): "Deutsche Forschungsgemeinschaft (DFG), SFB-1528"; a funding without acknowledgement reads "Funder, award title (award number)".
- The recording system's channel count is labelled "System channels" (the capacity of the system, not the recorded channels).
- Data Records opens with the shared records statement, its tokens filled from the dataset with lists as prose ("a, b and c"; the original data formats through an optional group), then one line of curation software versions per purpose from the odML `Curation Software` section ("Validation: BIDS validator ... with the BEP032 schema (commit ...), ... Conversion: fairephys 0.1.0 (commit abc1234), ..."; "(commit abc1234, with uncommitted changes)" when the fairephys source had them). No versions line without a records statement (a dataset the pipeline did not convert).
- A header line under the title: "Short name: <short name> | Version: <version identifier> | Repository: <repository url>", each part when filled (the full repository URL, which the citation no longer holds while it names the DOI placeholder).
- Every generated table has a one-line caption above it saying what it lists: the recording sessions (from the columns shown), the states of a processing step (the code in the raw files, the label, named as the trial_type value of the events files when there are events files, and the description) and the file table.
- The file tree starts at the repository name (the last path part of `repository url`), else "repository root"; of the session folders of a folder (`ses-*` of a subject, `sub-*_ses-*` of `probes/`) only the example session's is shown (the session whose folders hold the most file types) and the others are counted ("... (11 more sessions)"); every file is followed by the one-sentence description of its type in square brackets (the EBRAINS data descriptor template; registry `tree`). Tree and file table read the same list, `files` in the registry `readme.yaml`: pattern, one-sentence description, format, content and specification with the writing software. A TSV with a header row lists the columns all its files on disk have, then those only some have; a JSON sidecar lists its keys the same way (files with no entries counted, or that it holds none when no file has any); images list the BIDS suffixes of their file names; a channels.tsv outside ecephys/, emg/ and motion/ takes the catch-all entry; the software gets the version the odML `Curation Software` section records (no version without one).
- The first plain mention of a term of the registry's `first use` is written out ("open metadata Markup Language (odML)"), and the first plain mention of each software of `software links` (or of a `Curation Software` tool with a code url) is linked, unless the text already links it; code, links and URLs are left as they are.
- The video rows of the file table (`*_video.*`, `*_video.json`) name the camera (manufacturer and model) and its recording software from the Hardware Auxiliary Devices of device type "video camera" (registry `device type`); without one they read "Video file in the format of the camera (MTS)" and "JSON".
- Affiliations: "lab, department, institution, city, country", the parts that are filled (the EBRAINS data descriptor template's "Institute (working group) department, university, city, country").
- Author contributions: the description, then after a comma the openMINDS contribution types it does not already name (whole words, any case); without a description the types alone, capitalised.

## Authentication

Get a token from https://editor.kg.ebrains.eu/ (Profile > Copy token) and give it as the environment variable `EBRAINS_KG_TOKEN` or in the file `~/.config/fairephys/kg_token`; a token file is never part of the package (the build excludes `.env`).

## Libraries

| Step | Library | Purpose |
|------|---------|---------|
| Enrichment | openminds | instance resolution |
| Collection | fairgraph | Entity creation, KG upload |
