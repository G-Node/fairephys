# Template Schemas

JSON Schema validation for the YAML templates, one schema per template file.

```
_resources/schemas/
  collection.v1.yaml
  datacite.v1.yaml
  dataset.v1.yaml
  hardware.v1.yaml
  processing.v1.yaml
  protocol.v1.yaml
  recordings.v1.yaml
  sessions.v1.yaml
  subject.v1.yaml
```

## Template Declaration

```yaml
_schema: hardware.v1
Hardware:
  ...
```

A template without `_schema` is validated against `<file name>.v1`; a template with no schema is an error.

## Versioning

- **MAJOR** (v1 to v2): Breaking changes (field removal, type change)
- **MINOR** (v1 to v1.1): New optional fields

## Validation

Compiling validates strictly: a schema error stops the pipeline.

```bash
fairephys validate templates/                                  # templates only
fairephys compile templates/ -o out/name                       # strict (fail on error)
fairephys compile templates/ -o out/name --validate warn       # warn only
fairephys compile templates/ -o out/name --validate skip       # skip
uv run python datasets/NAME/curate.py --compile                # strict, with enrichment
```
