#!/usr/bin/env python3
"""
Analyze Claude Code trajectories.

Subcommands:
  modified-files  Extract modified files and their pre-edit containment status.
  read-files      Extract files read during the trajectory.
"""

import argparse
import json
import os
import shlex
import sys
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def parse_trajectory(path):
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def build_sequence(events):
    """
    Return flat sequence of tool_use and tool_result items in order,
    and a mapping of tool_use_id -> tool_use item.
    """
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


def resolve_trajectory_path(trajectory_arg, claude_dir):
    if os.path.exists(trajectory_arg):
        return trajectory_arg
    matches = list(Path(claude_dir).glob(f"projects/**/{trajectory_arg}.jsonl"))
    if matches:
        return str(matches[0])
    return trajectory_arg


# ---------------------------------------------------------------------------
# modified-files
# ---------------------------------------------------------------------------

def get_session_info(events):
    for e in events:
        if "sessionId" in e and "cwd" in e:
            return e["sessionId"], e["cwd"]
    return None, None


def collect_read_results(sequence, tool_uses):
    """Map file_path -> list of {seq_idx, content} for Read tool results."""
    reads = defaultdict(list)
    for seq_idx, s in enumerate(sequence):
        if s["seq_type"] != "tool_result":
            continue
        item = s["item"]
        tool_use = tool_uses.get(item.get("tool_use_id"), {})
        if tool_use.get("name") != "Read":
            continue
        file_path = tool_use.get("input", {}).get("file_path", "")
        raw = item.get("content", "")
        text = (
            "".join(c.get("text", "") for c in raw if isinstance(c, dict))
            if isinstance(raw, list)
            else str(raw)
        )
        reads[file_path].append({"seq_idx": seq_idx, "content": text})
    return reads


def collect_modifications(sequence):
    """Return first modification per file path (in sequence order)."""
    seen = {}
    for seq_idx, s in enumerate(sequence):
        if s["seq_type"] != "tool_use":
            continue
        item = s["item"]
        name = item.get("name")
        if name not in ("Write", "Edit", "NotebookEdit"):
            continue
        inp = item.get("input", {})
        file_path = inp.get("file_path") or inp.get("notebook_path", "")
        if file_path and file_path not in seen:
            seen[file_path] = {"seq_idx": seq_idx, "tool": name, "input": inp}
    return seen


def load_file_history(session_id, events, claude_dir):
    """
    Return set of relative file paths that existed before the trajectory started.

    A file is pre-existing if its version=1 backup has a non-null backupFileName.
    version=1 with backupFileName=null means Claude Code created the file fresh.
    """
    if not session_id:
        return set()
    pre_existing = set()
    for e in events:
        if e.get("type") != "file-history-snapshot":
            continue
        backups = e.get("snapshot", {}).get("trackedFileBackups", {})
        for rel_path, binfo in backups.items():
            if binfo.get("version") == 1 and binfo.get("backupFileName"):
                pre_existing.add(rel_path)
    return pre_existing


def _relative_path(abs_path, cwd):
    try:
        return str(Path(abs_path).relative_to(cwd))
    except ValueError:
        return abs_path


def determine_containment(file_path, mod, reads, pre_existing_rel_paths, cwd):
    seq_idx = mod["seq_idx"]
    tool = mod["tool"]
    inp = mod["input"]

    prior_reads = [r for r in reads.get(file_path, []) if r["seq_idx"] < seq_idx]

    if prior_reads:
        return "fully"

    if tool == "Edit":
        return "partially" if inp.get("old_string") else "not contained"

    if tool in ("Write", "NotebookEdit"):
        rel = _relative_path(file_path, cwd) if cwd else file_path
        if rel in pre_existing_rel_paths:
            return "not contained"
        return "new file"

    return "not contained"


def analyze_modified_files(trajectory_path, claude_dir=None):
    if claude_dir is None:
        claude_dir = Path.home() / ".claude"

    events = parse_trajectory(trajectory_path)
    session_id, cwd = get_session_info(events)

    sequence, tool_uses = build_sequence(events)
    reads = collect_read_results(sequence, tool_uses)
    modifications = collect_modifications(sequence)
    pre_existing = load_file_history(session_id, events, claude_dir)

    results = []
    for file_path, mod in modifications.items():
        containment = determine_containment(
            file_path, mod, reads, pre_existing, cwd
        )
        results.append(
            {"file_path": file_path, "tool": mod["tool"], "containment": containment}
        )
    return results


# ---------------------------------------------------------------------------
# read-files
# ---------------------------------------------------------------------------

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
            continue
        if tok.startswith("-") and len(tok) > 1:
            for ch in tok[1:]:
                if ch in flags_with_arg:
                    if tok == f"-{ch}":
                        skip_next = True
                    break
            continue
        positional.append(tok)

    if first_is_program and positional:
        positional = positional[1:]

    return cmd, positional, read_type


def extract_bash_file_reads(command):
    """Parse a bash command string and return list of (file_path, read_type)."""
    results = []
    try:
        tokens = shlex.split(command)
    except ValueError:
        return results

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


def analyze_read_files(trajectory_path):
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_common_args(parser):
    parser.add_argument("trajectory", help="Path to trajectory JSONL file, or session ID")
    parser.add_argument(
        "--claude-dir",
        default=None,
        help="Path to .claude directory (default: ~/.claude)",
    )
    parser.add_argument("--json", action="store_true", help="Output results as JSON")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Analyze Claude Code trajectories",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_mod = subparsers.add_parser(
        "modified-files",
        help="Extract modified files and their pre-edit containment status",
    )
    _add_common_args(p_mod)

    p_read = subparsers.add_parser(
        "read-files",
        help="Extract files read during the trajectory",
    )
    _add_common_args(p_read)

    args = parser.parse_args(argv)
    claude_dir = Path(args.claude_dir or Path.home() / ".claude")
    trajectory_path = resolve_trajectory_path(args.trajectory, str(claude_dir))

    if args.command == "modified-files":
        results = analyze_modified_files(trajectory_path, claude_dir)
        if args.json:
            print(json.dumps(results, indent=2))
            return
        if not results:
            print("No file modifications found in trajectory.")
            return
        for r in results:
            print(f"{r['file_path']}: {r['containment']}")

    elif args.command == "read-files":
        results = analyze_read_files(trajectory_path)
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
