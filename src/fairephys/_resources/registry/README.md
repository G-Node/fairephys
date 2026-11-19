# Registry

Declarative mapping files that drive the metadata pipeline. Code reads these registries at runtime; adding a new field or entity usually means editing YAML here rather than writing Python.

## Files

| File | Purpose | Consumers |
|------|---------|-----------|
| `openminds_schemas.yaml` | openMINDS entity definitions, odML type assignments | `export/collection.py`, `enrichment/openminds.py`, `metadata/compiler.py` |
| `bids_mappings.yaml` | odML property paths to BIDS sidecar fields and TSV columns, the target BIDS spec | `bids/builder.py`, `bids/aux.py`, `bids/ecephys.py`, `bids/dataset.py`, `converters/metadata.py`, `validation/bids.py`, `export/readme.py` |
| `nwb_mappings.yaml` | odML property paths to NWBFile and Subject fields, unit transforms | `converters/metadata.py` |
| `licenses.yaml` | licences by SPDX id: name and URL (texts in `_resources/licenses`) | `export/datacite.py`, `export/readme.py` |
| `readme.yaml` | text blocks of the generated dataset README | `export/readme.py` |

The anatomy atlas (brain region names and UBERON terms) is not a registry: a project names its own table in the collection YAML (`anatomy atlas`).

All registries are read through `fairephys.registry.load(name)` (parsed once).

---

## openminds_schemas.yaml

### Overview

Single source of truth for how odML metadata maps to openMINDS Knowledge Graph entities. Three concerns in one file:

1. **odml_types** - Section type assignments (used by `metadata/compiler.py` when building odML documents)
2. **build_order** - Entity build sequence (dependency order for `export/collection.py`)
3. **schemas** - Entity definitions with field mappings

### Schema structure

Each schema entry defines one openMINDS entity type:

```yaml
Subject:
  fairgraph_class: core.Subject     # fairgraph class for KG upload
  odml_section: Subject             # where to read values from in odML
  mode: single                      # single section or multiple subsections
  kg_lookup:                        # optional: fields for KG deduplication
    fields: [lookup_label]
  fields:
    lookup_label: { odml: "subject id" }
    species:
      type: term                    # controlled vocabulary lookup
      odml_field: "species"
      term_class: controlled_terms.Species
    studied_states: { type: link, target: SubjectState, list: true }
  nested:                           # optional: entities built in same context
    SomeNested:
      fairgraph_class: core.Something
      condition: "some_field"       # only build if this field exists
      fields: { ... }
```

### Field types

| Type | Syntax | Description |
|------|--------|-------------|
| `odml` | `{ odml: "property name" }` | Read property value from odML section |
| `term` | `type: term, odml_field, term_class` | Controlled term resolved via openMINDS vocabulary |
| `link` | `{ type: link, target: EntityName }` | Reference to another entity in build_order |
| `nested_ref` | `{ type: nested_ref, target: NestedName }` | Reference to entity defined in `nested:` block |
| `nested_list` | `{ type: nested_list, target: NestedName }` or `target: [A, B]` | List of nested entity references (concatenated when several targets) |
| `template` | `{ type: template, value: "{Dataset/short name}_sub-{subject id}" }` | String built from odML values: `{Section/path/prop}` absolute path (a text value, such as a shared statement, has its `{bids version}` and `{bids extension}` filled; any other token in it raises), `{prop}` current section, per-session `{session_id}`, `{bids version}` and `{bids extension}` (the BIDS registry `spec`: `bids_version`, `ecephys_extension`; a missing value raises); values are stripped of surrounding whitespace; missing token skips the field. `{?token}` is optional: empty when missing, and the whitespace before it is dropped (`"{description}\n\n{?note}"` equals the description when there is no note). `{?words {token} words}` is an optional group: the words around one token, left out with it (`"{name}{?, see {note},} here"`) |
| `literal` | `{ type: literal, value: "..." }` | Hardcoded value |
| `date` | `{ type: date, odml: "release date" }` | The property as a date (ISO); none when empty |
| `iri` | `{ type: iri, value: "..." }` | IRI/URL value |
| `identifier_list` | `type: identifier_list, odml_section, classes: {DOI: {class: core.DOI, identifier: "https://doi.org/{}"}}` | One identifier node per entry of the subsection (each with its `identifier`, typed by `fairephys.metadata.identifiers`) whose type `classes` names; `relation:` keeps the entries of one relation, `single: true` the first node. Entries of another type (a URL, an arXiv id or a PMID among the related publications) are listed in `collection.warnings` and the openMINDS validation output |

Common modifiers: `list: true` (wrap in list), `default: "..."` (fallback), `prefix: "https://..."` (prepend to value), `fallback: {field definition}` (in schemas with `nested`: built when the field builds nothing; `DatasetVersion.full_documentation` is the documentation url, else the first related publication described as `IsDescribedBy` with a DOI).

### Shared KG nodes

- `kg_lookup.fields` find an existing KG node before a new one is built. A node found in another space (e.g. `common`) is referenced as it is; the fields where it differs from the odML are logged and listed in the openMINDS validation output, to be edited in the KG editor or aligned in the metadata.
- `existence_query: [fields]` replaces the fields fairgraph matches at push to find an existing node for an entity without a KG id (only when all of them have a value). Funding uses `[award_number]`: fairgraph's own check matches the funder alone and would merge two grants of one funder.
- Organizations are matched by full name and shared by all datasets, so only the institution is exported: a person's lab is not the institution's `short_name` (README and DataCite keep the lab in the affiliation).
- Digital identifiers (ORCID, DOI, RRID, ...) are one node per identifier, however often they are built (a PI as Custodian and as Contributor).

### Per-session options

`mode: per_session` schemas (SubjectState) build one entity per session and take a `per_session_config` block:

| Option | Effect |
|--------|--------|
| `session_date_field` | session property holding the date (default `date`) |
| `relative_time_unit: day` (or `week`) | `relative_time_indication` = time since the earliest dated session of the base section |

Sessions without a valid date get no relative time.

### How to extend

**Add a field to an existing entity:**

Add the field under `fields:` in the relevant schema. Use the appropriate type.

```yaml
Subject:
  fields:
    # ... existing fields ...
    weight: { odml: "weight" }           # simple property
    strain:                               # controlled term
      type: term
      odml_field: "genetic strain type"
      term_class: controlled_terms.Strain
```

**Add a new entity:**

1. Define the schema under `schemas:`
2. Add it to `build_order` (after its dependencies, before its dependents)
3. If it needs an odML section type, add to `odml_types`

```yaml
build_order:
  - ...
  - NewEntity       # after dependencies

schemas:
  NewEntity:
    fairgraph_class: core.NewThing
    odml_section: SomeSection
    mode: single
    fields:
      name: { odml: "name" }
```

**Add a nested entity (built within a parent context):**

```yaml
ParentEntity:
  fields:
    ref_field: { type: nested_ref, target: ChildThing }
  nested:
    ChildThing:
      fairgraph_class: core.Something
      condition: "source_field"              # skip if field is empty
      add_to_collection: false               # true = register separately in KG
      fields:
        value: { odml: "source_field" }
```

---

## bids_mappings.yaml

### Overview

Maps odML property paths to BIDS output files (JSON sidecars and TSV columns). Each top-level key corresponds to a BIDS output file.

### Path notation

`Section/Subsection.property` - dots separate the property name from the section path, slashes separate section levels. Wildcards (`*`) match any subsection (e.g., `Hardware/Probes/*.manufacturer`).

A list of paths is tried in order and the first non-empty value wins. For example, `TaskDescription` reads `[Sessions/*.task description, Behavioral Protocol.description, Sessions/*.description]`, so a session entry that recorded a task of its own (sessions.yaml `task description`, with its BIDS `task` label) describes its own runs, and otherwise the BIDS sidecars carry the same text as the openMINDS BehavioralProtocol description and fall back to the session description when the protocol has none.

### Sections

| Key | Output | Description |
|-----|--------|-------------|
| `spec` | - | Target BIDS version, BEP032 schema file and validator pin |
| `transforms` | - | Value mappings (e.g., "left" -> "L", "millimeter" -> "mm") |
| `coercions` | - | Type coercions applied after extraction |
| `probes_tsv` | `*_probes.tsv` | One row per Electrode Placement of the session |
| `electrodes_tsv` | `*_electrodes.tsv` | One row per contact of the session's ProbeInterface group |
| `channels_tsv` | `*_channels.tsv` | Recording channel metadata (from Neo) |
| `participants_tsv` | `participants.tsv` | Subject-level metadata |
| `dataset_description_json` | `dataset_description.json` | Top-level dataset metadata |
| `ecephys_json` | `*_ecephys.json` | Recording sidecar |
| `coordsystem_json` | `*_coordsystem.json` | Coordinate system of the probe positions |
| `events_json` | `*_events.json` | Stimulus presentation software |
| `eyetrack_json`, `motion_json`, `emg_json` | `*_physio.json`, `*_motion.json`, `*_emg.json` | Auxiliary device sidecars (the device as `Auxiliary Device` source) |
| `nwb_electrode_columns` | NWB electrode table | Column descriptions shared by the datasets |

### Column definition syntax

```yaml
some_tsv:
  required: [col_a, col_b]       # a required column with no value and no default is an error
  columns:
    col_a:
      path: Section.property     # odML path to read from
      default: "n/a"             # value when the path is empty
      transform: hemisphere      # apply value transform
    col_b:
      generated: true            # filled by code (e.g. from the ProbeInterface group)
    col_c:
      derived: true              # from what the caller passes (channel map, channel types)
```

A column with no value, no default and not required is written as `n/a`.

### How to extend

**Add a column to an existing TSV:**

```yaml
probes_tsv:
  columns:
    # ... existing columns ...
    new_column:
      path: Hardware/Probes/*.new property
      default: "n/a"
```

**Add a custom (non-BIDS-standard) column:**

Custom columns need a `sidecar_description` so the BIDS validator knows about them:

```yaml
channels_tsv:
  columns:
    quality:
      path: Sessions/*/Electrode Placements/*.quality
      sidecar_description: Signal quality rating (1-5)
```

Columns whose values are ontology terms also get `sidecar_term_url`, written as `TermURL` next to the description (e.g. `anatomical_location` values are UBERON term names, so BIDS tools can interpret them).

**Add a new JSON sidecar field:**

```yaml
ecephys_json:
  fields:
    NewField:
      path: Section.property
```

A sidecar field whose path holds no value is left out of the JSON; the only defaults are the n/a values BIDS prescribes (SoftwareFilters, HardwareFilters).

**Add a value transform:**

```yaml
transforms:
  my_transform:
    raw_value: bids_value
    another: mapped

some_tsv:
  columns:
    col:
      path: Section.property
      transform: my_transform
```

**Add a type coercion:**

```yaml
coercions:
  array_fields: [FieldName]       # wrap single values in list
  float_fields: [FieldName]       # cast to float
  string_fields: [FieldName]      # join arrays with ", "
  enum_fields:                    # allowed values (BIDS schema)
    FieldName: [val1, val2]
```

An enum field with a value outside its list is an error naming the field and the allowed values (never a substitute); a sidecar field with no value and no default is left out of the sidecar.
