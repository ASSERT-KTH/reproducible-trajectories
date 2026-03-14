#!/usr/bin/env python3
"""
reproducible-trajectories — CLI entry point.

Usage:
    python reproducible_trajectories.py <command> [args...]

Commands:
    extract-modified-files   Extract modified files from a trajectory
"""

import sys
import os

COMMANDS = {
    "extract-modified-files": "src.extract_modified_files",
    "extract-read-files": "src.extract_read_files",
}


def usage():
    print(__doc__.strip())
    print()
    print("Available commands:")
    for name in COMMANDS:
        print(f"  {name}")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        usage()
        sys.exit(0 if len(sys.argv) >= 2 else 1)

    command = sys.argv[1]
    if command not in COMMANDS:
        print(f"Unknown command: {command!r}", file=sys.stderr)
        print(f"Run with --help to see available commands.", file=sys.stderr)
        sys.exit(1)

    # Add src/ to path so modules are importable
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

    module_path = COMMANDS[command]
    # Strip "src." prefix since we added src/ to sys.path
    module_name = module_path.removeprefix("src.")

    import importlib
    mod = importlib.import_module(module_name)
    mod.main(sys.argv[2:])


if __name__ == "__main__":
    main()
