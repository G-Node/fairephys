"""The `fairephys` command (also `python -m fairephys`): init, compile, validate."""

import sys

from . import init
from .metadata import compiler
from .validation import schema

COMMANDS = {
    "init": (init.main, "scaffold a dataset directory (curate.py and templates)"),
    "compile": (compiler.main, "compile YAML templates to odML (no enrichment)"),
    "validate": (schema.main, "validate YAML templates against their schemas"),
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in COMMANDS:
        return COMMANDS[argv[0]][0](argv[1:]) or 0
    asked_for_help = bool(argv) and argv[0] in ("-h", "--help")
    usage = "usage: fairephys <command> [options]\n\ncommands:\n" + "".join(
        f"  {name:10}{description}\n" for name, (_, description) in COMMANDS.items())
    print(usage, file=sys.stdout if asked_for_help else sys.stderr)
    return 0 if asked_for_help else 2


if __name__ == "__main__":
    sys.exit(main())
