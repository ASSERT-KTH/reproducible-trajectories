#!/usr/bin/env python3
"""
Extract modified files from a Claude Code trajectory.

For each file, states whether the file before edit is fully or partially
contained in the trace.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path


def parse_trajectory(path):
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def get_session_info(events):
    for e in events:
        if "sessionId" in e and "cwd" in e:
            return e["sessionId"], e["cwd"]
    return None, None


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
    """Return list of first modification per file path (in sequence order)."""
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
            seen[file_path] = {
                "seq_idx": seq_idx,
                "tool": name,
                "input": inp,
            }
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


def relative_path(abs_path, cwd):
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
        rel = relative_path(file_path, cwd) if cwd else file_path
        if rel in pre_existing_rel_paths:
            return "not contained"
        return "new file"

    return "not contained"


def analyze(trajectory_path, claude_dir=None):
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
            {
                "file_path": file_path,
                "tool": mod["tool"],
                "containment": containment,
            }
        )
    return results


def resolve_trajectory_path(trajectory_arg, claude_dir):
    if os.path.exists(trajectory_arg):
        return trajectory_arg
    # Try to look up by session ID in ~/.claude/projects/
    matches = list(Path(claude_dir).glob(f"projects/**/{trajectory_arg}.jsonl"))
    if matches:
        return str(matches[0])
    return trajectory_arg


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Extract modified files from a Claude Code trajectory"
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
    trajectory_path = resolve_trajectory_path(args.trajectory, claude_dir)

    results = analyze(trajectory_path, claude_dir)

    if args.json:
        print(json.dumps(results, indent=2))
        return

    if not results:
        print("No file modifications found in trajectory.")
        return

    for r in results:
        print(f"{r['file_path']}: {r['containment']}")


if __name__ == "__main__":
    main()
