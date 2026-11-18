"""YAML template and odML document validation."""

import logging
from datetime import date, datetime
from pathlib import Path

import yaml
from jsonschema import Draft7Validator, validators

logger = logging.getLogger(__name__)

# Extend Draft7Validator to recognize "date" as a JSON Schema type,
# so {type: [date, string]} accepts both datetime.date and strings.
_type_checker = Draft7Validator.TYPE_CHECKER.redefine(
    "date", lambda checker, instance: isinstance(instance, (date, datetime))
)
Validator = validators.extend(Draft7Validator, type_checker=_type_checker)

SCHEMAS_DIR = Path(__file__).parent.parent / "_resources" / "schemas"


def load_schema(name: str) -> dict | None:
    """Load schema by name (e.g., 'hardware.v1'); None when there is none."""
    path = SCHEMAS_DIR / f"{name}.yaml"
    return yaml.safe_load(path.read_text()) if path.exists() else None


def validate_semantic(data: dict, schema_ref: str) -> list[str]:
    """Cross-field semantic checks that JSON Schema alone cannot express."""
    errors = []

    if schema_ref == "hardware.v1":
        for i, probe in enumerate(data.get("Hardware", {}).get("Probes", []), 1):
            ids = probe.get("electrode identifiers")
            n = probe.get("number of contacts")
            # the empty default leaves the ids to ProbeInterface
            if ids and isinstance(ids, list) and isinstance(n, int) and len(ids) != n:
                errors.append(
                    f"Hardware.Probes.{i}: electrode identifiers count "
                    f"({len(ids)}) != number of contacts ({n})"
                )

    return errors


def validate_template(data: dict, template_name: str) -> list[str]:
    """Validate template data against its JSON schema (the '_schema' it
    names, else <name>.v1). Returns error messages; a template without a
    schema is an error."""
    schema_ref = data.get("_schema", template_name.replace(".yaml", ".v1"))
    schema = load_schema(schema_ref)
    if not schema:
        return [f"no schema {schema_ref!r} in {SCHEMAS_DIR} (name it in '_schema')"]

    errors = []
    for e in Validator(schema).iter_errors(data):
        path = ".".join(str(p) for p in e.absolute_path) or "(root)"
        errors.append(f"{path}: {e.message}")

    errors.extend(validate_semantic(data, schema_ref))
    return errors


def validate_templates(templates_dir: Path) -> dict[str, list[str]]:
    """Validate all YAML templates in a directory. Returns {filename: [errors]}."""
    templates_dir = Path(templates_dir)
    all_errors = {}

    for fpath in sorted(templates_dir.glob("*.yaml")):
        if fpath.name.startswith("_"):
            continue

        with open(fpath) as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            continue

        errors = validate_template(data, fpath.name)
        if errors:
            all_errors[fpath.name] = errors

    return all_errors


def validate_document(doc) -> None:
    """Run odML's built-in validation on a compiled document, logging each
    error at error level and each warning once at info level."""
    from odml.validation import Validation

    for issue in Validation(doc).errors:
        (logger.error if issue.is_error else logger.info)("odML: %s", issue)


def main(argv: list[str] | None = None) -> int:
    """CLI for validating templates."""
    import argparse

    parser = argparse.ArgumentParser(prog="fairephys validate",
                                     description="Validate YAML templates against schemas")
    parser.add_argument("templates_dir", type=Path, help="Directory with YAML templates")
    args = parser.parse_args(argv)

    errors = validate_templates(args.templates_dir)

    if not errors:
        print(f"All templates valid in {args.templates_dir}")
        return 0

    print(f"Schema validation errors in {args.templates_dir}:\n")
    for fname, errs in errors.items():
        print(f"{fname}:")
        for e in errs:
            print(f"  - {e}")
        print()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
