#!/usr/bin/env python3
"""
Extract all files read in a Claude Code trajectory.

For each file, tells whether it was fully or partially read.
Detects reads via the Read tool and via bash commands (cat/head/tail/sed/awk).
"""

import argparse
import json
import os
import shlex
import sys
from pathlib import Path


def parse_trajectory(path):
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def build_sequence(events):
    tool_uses = {}
    sequence = []
    for i, e in enumerate(events):
        msg = e.get("message")
        if not msg:
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "tool_use":
                tool_uses[item["id"]] = item
                sequence.append({"seq_type": "tool_use", "event_idx": i, "item": item})
            elif itype == "tool_result":
                sequence.append(
                    {"seq_type": "tool_result", "event_idx": i, "item": item}
                )
    return sequence, tool_uses


# Flags that consume the next token for each command
_FLAGS_WITH_ARG = {
    "head": set("nqvcbz"),
    "tail": set("nqvcbzs"),
    "sed": {"e", "f", "n"},
    "awk": {"f", "v", "F"},
    "cat": set(),
}

# Whether the first positional arg is a program/script (not a file)
_FIRST_POSITIONAL_IS_PROGRAM = {"sed", "awk"}


def _bash_files_in_part(tokens):
    """
    Given a list of tokens for a single command (no pipes/semicolons),
    return (command_name, file_paths, read_type) or None if not a read command.
    """
    if not tokens:
        return None

    READ_COMMANDS = {
        "cat": "fully",
        "head": "partially",
        "tail": "partially",
        "sed": "partially",
        "awk": "partially",
    }

    cmd = os.path.basename(tokens[0])
    if cmd not in READ_COMMANDS:
        return None

    read_type = READ_COMMANDS[cmd]
    flags_with_arg = _FLAGS_WITH_ARG.get(cmd, set())
    first_is_program = cmd in _FIRST_POSITIONAL_IS_PROGRAM

    positional = []
    skip_next = False
    for tok in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if tok == "--":
            # Everything after -- is positional
            continue
        if tok.startswith("-") and len(tok) > 1:
            # Check if any flag char consumes the next token
            # Handle combined short flags like -n10 vs -n 10
            for ch in tok[1:]:
                if ch in flags_with_arg:
                    # If the flag value isn't attached (e.g. -n 10 not -n10)
                    # We check: if the flag is the last char in the token
                    if tok == f"-{ch}":
                        skip_next = True
                    break
            continue
        positional.append(tok)

    # For sed/awk, the first positional is the program text, not a file
    if first_is_program and positional:
        positional = positional[1:]

    return cmd, positional, read_type


def extract_bash_file_reads(command):
    """
    Parse a bash command string and return list of (file_path, read_type).
    """
    results = []
    try:
        tokens = shlex.split(command)
    except ValueError:
        return results

    # Split on pipeline/sequence operators
    parts = []
    current = []
    for tok in tokens:
        if tok in ("|", "&&", "||", ";"):
            if current:
                parts.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        parts.append(current)

    for part in parts:
        info = _bash_files_in_part(part)
        if info is None:
            continue
        _cmd, files, read_type = info
        for f in files:
            if f:
                results.append((f, read_type))

    return results


_READ_PRIORITY = {"fully": 1, "partially": 0}


def analyze(trajectory_path):
    events = parse_trajectory(trajectory_path)
    sequence, tool_uses = build_sequence(events)

    reads = {}  # file_path -> record dict

    def record(file_path, tool, read_type):
        if not file_path:
            return
        existing = reads.get(file_path)
        if existing is None or _READ_PRIORITY[read_type] > _READ_PRIORITY[existing["read_type"]]:
            reads[file_path] = {"file_path": file_path, "tool": tool, "read_type": read_type}

    for s in sequence:
        if s["seq_type"] != "tool_use":
            continue
        item = s["item"]
        name = item.get("name")
        inp = item.get("input", {})

        if name == "Read":
            file_path = inp.get("file_path", "")
            has_partial = inp.get("offset") is not None or inp.get("limit") is not None
            record(file_path, "Read", "partially" if has_partial else "fully")

        elif name == "Bash":
            command = inp.get("command", "")
            for file_path, read_type in extract_bash_file_reads(command):
                record(file_path, "Bash", read_type)

    return list(reads.values())


def resolve_trajectory_path(trajectory_arg, claude_dir):
    if os.path.exists(trajectory_arg):
        return trajectory_arg
    matches = list(Path(claude_dir).glob(f"projects/**/{trajectory_arg}.jsonl"))
    if matches:
        return str(matches[0])
    return trajectory_arg


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Extract read files from a Claude Code trajectory"
    )
    parser.add_argument(
        "trajectory",
        help="Path to trajectory JSONL file, or session ID",
    )
    parser.add_argument(
        "--claude-dir",
        default=None,
        help="Path to .claude directory (default: ~/.claude)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    args = parser.parse_args(argv)

    claude_dir = Path(args.claude_dir or Path.home() / ".claude")
    trajectory_path = resolve_trajectory_path(args.trajectory, str(claude_dir))

    results = analyze(trajectory_path)

    if args.json:
        print(json.dumps(results, indent=2))
        return

    if not results:
        print("No file reads found in trajectory.")
        return

    for r in results:
        print(f"{r['file_path']}: {r['read_type']}")


if __name__ == "__main__":
    main()
