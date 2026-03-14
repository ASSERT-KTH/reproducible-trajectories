#!/usr/bin/env python3
"""
reproducible-trajectories — CLI entry point.

Usage:
    python reproducible_trajectories.py <command> [args...]

Commands:
    modified-files       Extract modified files from a trajectory
    read-files           Extract files read during a trajectory
    filter-trajectories  Remove tool calls related to specified files/folders
    verify-trajectories  Check whether trajectory ops reproduce git commits
"""

import sys
import os

# Map external command names to the subcommand passed to src/trajectory.py
COMMANDS = {
    "modified-files": "modified-files",
    "read-files": "read-files",
    "filter-trajectories": "filter-trajectories",
    "verify-trajectories": "verify-trajectories",
    "add-trajectories-to-repo": "add-trajectories-to-repo",
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

    import importlib
    mod = importlib.import_module("trajectory")
    mod.main([COMMANDS[command]] + sys.argv[2:])


if __name__ == "__main__":
    main()
