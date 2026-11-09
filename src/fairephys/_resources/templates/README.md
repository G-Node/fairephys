# Metadata Templates

Empty YAML templates for an extracellular electrophysiology dataset, and the `curate.py` template `fairephys init` renders. Field names and nesting are fixed by the schemas in `_resources/schemas/`; a structural change is a new schema version.

## Quick Start

```bash
# 1. Scaffold a dataset (templates, curate.py, directory layout)
fairephys init YOUR-DATASET --format blackrock

# 2. Add source documents to YOUR-DATASET/metadata/source/, fill the YAML in metadata/curated/

# 3. Compile to odML (validates the templates strictly)
fairephys compile YOUR-DATASET/metadata/curated -o YOUR-DATASET/curated-data/metadata/YOUR-DATASET
# or, with Neo and openMINDS enrichment, through the dataset's own script:
uv run python YOUR-DATASET/curate.py --compile
```

## Dataset Folder Structure

```
YOUR-DATASET/
  curate.py                    # Pipeline script (from curate.py.template)
  metadata/
    source/                    # Raw documents from researchers
    curated/                   # Structured YAML (input to the compiler)
  raw-data/                    # Raw recordings
  curated-data/                # BIDS root (auto-generated)
    metadata/                  # YOUR-DATASET.odml.* and the openMINDS files
```

## Template Files

| File | Content |
|------|---------|
| `dataset.yaml` | Project identity, license, contributors, related publications |
| `subject.yaml` | Subject biology, surgical preparation |
| `hardware.yaml` | Equipment specs, probes |
| `protocol.yaml` | Behavioral/experimental conditions |
| `processing.yaml` | Data analysis pipelines |
| `recordings.yaml` | Per-session techniques + placements (acute) |
| `sessions.yaml` | Session identifiers |
| `datacite.yaml` | DOI registration metadata |
| `collection.yaml` | Shared Project/Consortium of a curation project; lives in `collection/metadata/` above the datasets, or is given with `--shared-metadata` |

The `affiliation` of the Custodian and of each Contributor in `dataset.yaml` holds `institution`, `lab`, `department`, `address`, `city` and `country`; the descriptor lists each affiliation as "lab, department, institution, city, country" (the parts that are filled), openMINDS takes the institution.

`Related Publications` in `dataset.yaml` holds one entry per related paper, data descriptor, code or other resource: `identifier` (a DOI, a URL, `arxiv:`, `pmid:`, `hdl:` or a hyphenated ISBN such as `978-3-161-48410-0`; empty for a publication without one), `relation` (a DataCite relationType such as `IsSupplementTo`, `IsDescribedBy` or `IsReferencedBy`) and `citation`; `Related Publications: []` when there is none. `docs/pipeline.md` (Related publications) says what each output makes of an entry.

## Example

`fairephys init` writes these templates with comments on every field; `BASE` in the package's `tests/conftest.py` is a small, complete set of values.
