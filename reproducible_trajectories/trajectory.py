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
import re
import shlex
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

try:
    from . import pi_trajectory
except ImportError:  # pragma: no cover - allows direct script execution
    import pi_trajectory

import trajectoriz as tz
from trajectoriz.cli import _local_records as trajectoriz_local_records
from trajectoriz.cli import _parse_record as trajectoriz_parse_record


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
    if pi_trajectory.is_pi_trajectory(events):
        events = pi_trajectory.normalize_trajectory(events)
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
    pi_match = pi_trajectory.resolve_session_path(trajectory_arg)
    if pi_match:
        return pi_match
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
# filter-trajectories
# ---------------------------------------------------------------------------

def _tool_use_file_path(item):
    """Return the primary file/directory path from a tool_use item, or None."""
    name = item.get("name")
    inp = item.get("input", {})
    if name in ("Read", "Write", "Edit"):
        return inp.get("file_path")
    if name == "NotebookEdit":
        return inp.get("notebook_path")
    if name in ("Glob", "Grep"):
        return inp.get("path")
    return None


def filter_trajectory(trajectory_path, exclude_paths=None, cwd=None):
    """
    Return filtered events with tool calls referencing excluded paths removed.

    exclude_paths: list of file/folder paths to exclude.
                   If None, exclude everything outside `cwd`.
    cwd:           Base directory for the "outside" check.
                   Defaults to the session cwd, then os.getcwd().
    """
    events = parse_trajectory(trajectory_path)
    _session_id, session_cwd = get_session_info(events)
    effective_cwd = cwd or session_cwd or os.getcwd()

    if exclude_paths is None:
        # Exclude files that are NOT under effective_cwd
        cwd_path = Path(effective_cwd).resolve()

        def should_exclude(file_path):
            if not file_path:
                return False
            try:
                Path(file_path).resolve().relative_to(cwd_path)
                return False
            except ValueError:
                return True
    else:
        excl_resolved = [str(Path(p).resolve()) for p in exclude_paths]

        def should_exclude(file_path):
            if not file_path:
                return False
            resolved = str(Path(file_path).resolve())
            return any(
                resolved == e or resolved.startswith(e + os.sep)
                for e in excl_resolved
            )

    # First pass: collect IDs of tool_use items that reference excluded paths
    excluded_ids = set()
    for event in events:
        msg = event.get("message", {})
        for item in msg.get("content") or []:
            if not isinstance(item, dict) or item.get("type") != "tool_use":
                continue
            fp = _tool_use_file_path(item)
            if fp and should_exclude(fp):
                excluded_ids.add(item["id"])

    # Second pass: rebuild events without excluded tool_use / tool_result pairs
    filtered = []
    for event in events:
        msg = event.get("message")
        if not msg:
            filtered.append(event)
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            filtered.append(event)
            continue

        new_content = [
            item for item in content
            if not isinstance(item, dict) or (
                not (item.get("type") == "tool_use"
                     and item.get("id") in excluded_ids)
                and not (item.get("type") == "tool_result"
                         and item.get("tool_use_id") in excluded_ids)
            )
        ]

        if new_content == content:
            filtered.append(event)
        elif new_content:
            new_event = {**event, "message": {**msg, "content": new_content}}
            filtered.append(new_event)
        # else: all content items were filtered out — drop the event entirely

    return filtered


# ---------------------------------------------------------------------------
# pre-commit-verify-trajectory hook
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)
_TRAJECTORY_LABEL_RE = re.compile(r"trajectory\s*:\s*(\S+)", re.I | re.MULTILINE)
_TRAJECTORY_TAG_RE = re.compile(r"<trajectory>([^<]+)</trajectory>", re.I)


def _uuid_exists(uuid, claude_dir):
    return bool(list(Path(claude_dir).glob(f"projects/**/{uuid}.jsonl")))


def _validate_identifier(identifier, claude_dir):
    if _UUID_RE.fullmatch(identifier):
        return _uuid_exists(identifier, claude_dir)
    return Path(identifier).exists()


def check_commit_message(msg, claude_dir):
    if re.search(r"\bno trajectory\b", msg, re.I):
        return True, None

    candidates = []
    for m in _TRAJECTORY_LABEL_RE.finditer(msg):
        candidates.append(m.group(1))
    for m in _TRAJECTORY_TAG_RE.finditer(msg):
        value = m.group(1).strip()
        if _UUID_RE.fullmatch(value):
            candidates.append(value)

    if not candidates:
        return False, "no trajectory identifier found in commit message"

    for c in candidates:
        if _validate_identifier(c, claude_dir):
            return True, None

    return False, f"none of the trajectory identifiers could be validated: {candidates}"


def pre_commit_verify_trajectory(argv=None):
    """Entry point for the commit-msg git hook."""
    import sys as _sys
    args = (argv if argv is not None else _sys.argv)[1:]
    if not args:
        print("usage: pre-commit <commit-msg-file>", file=_sys.stderr)
        _sys.exit(1)

    with open(args[0]) as f:
        raw = f.read()

    msg = "\n".join(l for l in raw.splitlines() if not l.startswith("#"))
    claude_dir = Path.home() / ".claude"
    ok, error = check_commit_message(msg, claude_dir)

    if not ok:
        print(f"commit rejected: {error}", file=_sys.stderr)
        print("commit message must include a trajectory identifier, e.g.:", file=_sys.stderr)
        print("  trajectory: <uuid>", file=_sys.stderr)
        print("  trajectory: <path/to/trajectory.jsonl>", file=_sys.stderr)
        print("  <trajectory><uuid></trajectory>", file=_sys.stderr)
        print("or include 'no trajectory' to skip this check", file=_sys.stderr)
        _sys.exit(1)


# ---------------------------------------------------------------------------
# verify-trajectories
# ---------------------------------------------------------------------------

_TRAJ_PATTERNS = [
    re.compile(r'<trajectory>([^<\s]+)</trajectory>'),
    re.compile(r'trajectory:\s*(\S+)', re.IGNORECASE),
]


def _git(args, cwd):
    r = subprocess.run(['git'] + args, capture_output=True, cwd=cwd)
    return r.returncode, r.stdout.decode('utf-8', errors='replace')


def _get_commits(repo):
    rc, out = _git(['log', '--format=%H%x00%B%x01', '--reverse'], repo)
    if rc != 0:
        return []
    commits = []
    for block in out.split('\x01'):
        if '\x00' not in block:
            continue
        hash_, _, message = block.partition('\x00')
        hash_ = hash_.strip()
        if hash_:
            commits.append((hash_, message))
    return commits


def _get_parent(repo, h):
    rc, out = _git(['rev-parse', f'{h}^'], repo)
    if rc != 0:
        return None
    return out.strip()


def _file_at(repo, h, rel):
    rc, out = _git(['show', f'{h}:{rel}'], repo)
    if rc != 0:
        return None
    return out


def _changed_files(repo, h):
    rc, out = _git(['diff-tree', '--no-commit-id', '-r', '--name-only', h], repo)
    if rc != 0:
        return []
    return [line for line in out.splitlines() if line]


def _extract_trajectory_ref(message):
    for pattern in _TRAJ_PATTERNS:
        m = pattern.search(message)
        if m:
            return m.group(1)
    return None


def _simulate_ops(ops, states):
    """
    Simulate Write/Edit operations on file states.
    ops: list of {tool, input} for Write / Edit / NotebookEdit
    states: {abs_path: str|None}  (None = file didn't exist)
    NotebookEdit is skipped (complex JSON; can't reliably simulate).
    Returns mutated copy of states.
    """
    states = dict(states)
    for op in ops:
        tool = op['tool']
        inp = op['input']
        if tool == 'Write':
            fp = inp.get('file_path', '')
            content = inp.get('content', '')
            if fp:
                states[fp] = content
        elif tool == 'Edit':
            fp = inp.get('file_path', '')
            old_string = inp.get('old_string', '')
            new_string = inp.get('new_string', '')
            replace_all = inp.get('replace_all', False)
            if fp and fp in states and states[fp] is not None:
                current = states[fp]
                if replace_all:
                    states[fp] = current.replace(old_string, new_string)
                else:
                    states[fp] = current.replace(old_string, new_string, 1)
        # NotebookEdit: skip
    return states


def _rel(abs_path, session_cwd):
    try:
        return str(Path(abs_path).relative_to(session_cwd))
    except ValueError:
        return None


def verify_trajectories(repo_path, claude_dir=None):
    """
    Walk repo commits, find those with trajectory refs, simulate their
    Write/Edit ops on the parent-commit state, and check whether the result
    matches the actual commit.

    Returns list of dicts:
      commit, short_message, trajectory, status, files (optional)

    status values:
      'trajectory_not_found'  — ref resolved to non-existent file
      'no_operations'         — trajectory has no Write/Edit ops
      'reproducible'          — all verifiable files match
      'not_reproducible'      — at least one file mismatches
    """
    if claude_dir is None:
        claude_dir = str(Path.home() / '.claude')
    repo_path = str(Path(repo_path).resolve())

    results = []

    for commit_hash, message in _get_commits(repo_path):
        ref = _extract_trajectory_ref(message)
        if not ref:
            continue

        short_msg = message.splitlines()[0][:72] if message else ''

        traj_path = resolve_trajectory_path(ref, claude_dir)
        if not os.path.exists(traj_path):
            results.append({
                'commit': commit_hash[:12],
                'short_message': short_msg,
                'trajectory': ref,
                'status': 'trajectory_not_found',
            })
            continue

        events = parse_trajectory(traj_path)
        sequence, _tool_uses = build_sequence(events)
        _, session_cwd = get_session_info(events)

        # Collect all Write/Edit/NotebookEdit ops in sequence order
        ops = []
        for s in sequence:
            if s['seq_type'] != 'tool_use':
                continue
            item = s['item']
            name = item.get('name')
            if name not in ('Write', 'Edit', 'NotebookEdit'):
                continue
            ops.append({'tool': name, 'input': item.get('input', {})})

        # no_operations means no Write/Edit ops (NotebookEdit doesn't count)
        if not any(op['tool'] in ('Write', 'Edit') for op in ops):
            results.append({
                'commit': commit_hash[:12],
                'short_message': short_msg,
                'trajectory': ref,
                'status': 'no_operations',
            })
            continue

        # Determine which files are touched only by NotebookEdit (unverifiable)
        notebook_only = set()
        write_edit_files = set()
        for op in ops:
            tool = op['tool']
            inp = op['input']
            fp = inp.get('notebook_path' if tool == 'NotebookEdit' else 'file_path', '')
            if not fp:
                continue
            if tool == 'NotebookEdit':
                if fp not in write_edit_files:
                    notebook_only.add(fp)
            else:
                write_edit_files.add(fp)
                notebook_only.discard(fp)

        # Get parent commit
        parent = _get_parent(repo_path, commit_hash)

        # Build initial states for all files touched by ops
        initial_states = {}
        file_rels = {}  # fp -> rel path string or None

        for op in ops:
            tool = op['tool']
            inp = op['input']
            fp = inp.get('notebook_path' if tool == 'NotebookEdit' else 'file_path', '')
            if not fp or fp in initial_states:
                continue
            rel = _rel(fp, session_cwd) if session_cwd else None
            file_rels[fp] = rel
            if rel is not None and parent:
                initial_states[fp] = _file_at(repo_path, parent, rel)
            else:
                initial_states[fp] = None

        # Simulate Write/Edit ops (NotebookEdit is silently skipped)
        simulated = _simulate_ops(ops, initial_states)

        # Compare simulated states with actual commit
        file_results = []
        any_mismatch = False

        for fp, sim_content in simulated.items():
            rel = file_rels.get(fp)

            # Files only modified by NotebookEdit are unverifiable
            if fp in notebook_only:
                file_results.append({'file': rel or fp, 'status': 'unverifiable'})
                continue

            # Path outside session cwd
            if session_cwd is not None and rel is None:
                file_results.append({'file': fp, 'status': 'outside_repo'})
                continue

            # session_cwd unknown — can't map to repo-relative path
            if rel is None:
                file_results.append({'file': fp, 'status': 'unverifiable'})
                continue

            # Check whether the absolute path falls within the repo
            try:
                Path(fp).resolve().relative_to(repo_path)
            except ValueError:
                file_results.append({'file': rel, 'status': 'outside_repo'})
                continue

            actual_content = _file_at(repo_path, commit_hash, rel)

            if sim_content == actual_content:
                file_results.append({'file': rel, 'status': 'match'})
            else:
                file_results.append({'file': rel, 'status': 'mismatch'})
                any_mismatch = True

        status = 'not_reproducible' if any_mismatch else 'reproducible'
        results.append({
            'commit': commit_hash[:12],
            'short_message': short_msg,
            'trajectory': ref,
            'status': status,
            'files': file_results,
        })

    return results


# ---------------------------------------------------------------------------
# check-execution-reproducible
# ---------------------------------------------------------------------------

def _extract_tool_result_text(item):
    raw = item.get("content", "")
    if isinstance(raw, list):
        return "".join(c.get("text", "") for c in raw if isinstance(c, dict))
    return str(raw)


def collect_bash_commands(sequence, tool_uses):
    """
    Return list of {command, recorded_output} for every Bash tool_use that has
    a paired tool_result in the sequence.
    """
    result_by_id = {}
    for s in sequence:
        if s["seq_type"] == "tool_result":
            item = s["item"]
            result_by_id[item.get("tool_use_id", "")] = _extract_tool_result_text(item)

    commands = []
    for s in sequence:
        if s["seq_type"] != "tool_use":
            continue
        item = s["item"]
        if item.get("name") != "Bash":
            continue
        tid = item.get("id", "")
        cmd = item.get("input", {}).get("command", "")
        if not cmd:
            continue
        if tid in result_by_id:
            commands.append({"command": cmd, "recorded_output": result_by_id[tid]})
    return commands


def check_execution_reproducible(trajectory_path, repo_path=None, exec_cwd=None):
    """
    Check whether a trajectory satisfies both execution-reproducibility criteria:

    1. Edit criterion  — all Write/Edit ops, replayed from the commit that
       references this trajectory, reproduce the actual committed file states.
       Requires repo_path; skipped (status 'no_repo') when omitted.

    2. Command criterion — every Bash command, re-executed verbatim in exec_cwd
       (or the trajectory's recorded cwd when exec_cwd is None), produces
       byte-identical stdout+stderr to the recorded tool_result output.

    Returns:
      {
        "edit_criterion":    {"status": str, "files": [...]},
        "command_criterion": {"status": str, "commands": [...]},
        "execution_reproducible": bool | None,   # None = no criterion applicable
      }

    Status values (edit):   no_repo | no_commit | no_operations | reproducible | not_reproducible
    Status values (command): no_commands | reproducible | not_reproducible
    """
    events = parse_trajectory(trajectory_path)
    sequence, tool_uses = build_sequence(events)
    _, session_cwd = get_session_info(events)
    run_cwd = exec_cwd or session_cwd or os.getcwd()

    # ── criterion 2: command re-execution ────────────────────────────────────
    bash_entries = collect_bash_commands(sequence, tool_uses)
    command_results = []
    for entry in bash_entries:
        cmd = entry["command"]
        expected = entry["recorded_output"]
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, cwd=run_cwd,
        )
        actual = proc.stdout + proc.stderr
        command_results.append({
            "command": cmd,
            "expected": expected,
            "actual": actual,
            "match": actual == expected,
        })

    if not command_results:
        cmd_status = "no_commands"
    elif all(r["match"] for r in command_results):
        cmd_status = "reproducible"
    else:
        cmd_status = "not_reproducible"

    # ── criterion 1: edit replay from commit ─────────────────────────────────
    edit_status = "no_repo"
    edit_files = []

    if repo_path:
        repo_path = str(Path(repo_path).resolve())
        traj_abs = str(Path(trajectory_path).resolve())
        claude_dir = str(Path.home() / ".claude")
        edit_status = "no_commit"

        for commit_hash, message in _get_commits(repo_path):
            ref = _extract_trajectory_ref(message)
            if not ref:
                continue
            ref_path = resolve_trajectory_path(ref, claude_dir)
            if not os.path.exists(ref_path):
                continue
            if str(Path(ref_path).resolve()) != traj_abs:
                continue

            # Collect ops
            ops = []
            for s in sequence:
                if s["seq_type"] != "tool_use":
                    continue
                item = s["item"]
                if item.get("name") not in ("Write", "Edit", "NotebookEdit"):
                    continue
                ops.append({"tool": item["name"], "input": item.get("input", {})})

            if not any(op["tool"] in ("Write", "Edit") for op in ops):
                edit_status = "no_operations"
                break

            parent = _get_parent(repo_path, commit_hash)
            initial_states = {}
            file_rels = {}
            for op in ops:
                tool = op["tool"]
                inp = op["input"]
                fp = inp.get("notebook_path" if tool == "NotebookEdit" else "file_path", "")
                if not fp or fp in initial_states:
                    continue
                rel = _rel(fp, session_cwd) if session_cwd else None
                file_rels[fp] = rel
                initial_states[fp] = (_file_at(repo_path, parent, rel)
                                       if (rel and parent) else None)

            simulated = _simulate_ops(ops, initial_states)
            any_mismatch = False
            for fp, sim_content in simulated.items():
                rel = file_rels.get(fp)
                if rel is None:
                    edit_files.append({"file": fp, "status": "unverifiable"})
                    continue
                try:
                    Path(fp).resolve().relative_to(repo_path)
                except ValueError:
                    edit_files.append({"file": rel, "status": "outside_repo"})
                    continue
                actual_content = _file_at(repo_path, commit_hash, rel)
                if sim_content == actual_content:
                    edit_files.append({"file": rel, "status": "match"})
                else:
                    edit_files.append({"file": rel, "status": "mismatch"})
                    any_mismatch = True

            edit_status = "not_reproducible" if any_mismatch else "reproducible"
            break

    # ── overall verdict ───────────────────────────────────────────────────────
    _pass = {"reproducible"}
    _neutral = {"no_repo", "no_commit", "no_operations", "no_commands"}
    _fail = {"not_reproducible"}

    criteria = [edit_status, cmd_status]
    if any(s in _fail for s in criteria):
        execution_reproducible = False
    elif any(s in _pass for s in criteria):
        execution_reproducible = True
    else:
        execution_reproducible = None  # no applicable criterion

    return {
        "edit_criterion": {"status": edit_status, "files": edit_files},
        "command_criterion": {"status": cmd_status, "commands": command_results},
        "execution_reproducible": execution_reproducible,
    }


# ---------------------------------------------------------------------------
# find-reproducible-trajectories-claude (and future variants)
# ---------------------------------------------------------------------------

def _parse_iso(ts):
    return datetime.fromisoformat(ts.replace('Z', '+00:00'))


def _get_commit_timeline(repo_root):
    """Return sorted list of (datetime, hash) for all commits across all refs."""
    rc, out = _git(['log', '--format=%H %aI', '--all'], repo_root)
    if rc != 0:
        return []
    commits = []
    for line in out.strip().splitlines():
        h, _, ts = line.partition(' ')
        if h and ts:
            try:
                commits.append((_parse_iso(ts), h))
            except ValueError:
                pass
    commits.sort()
    return commits


def _commit_time(repo_root, commit):
    rc, out = _git(['log', '-1', '--format=%aI', commit], repo_root)
    if rc != 0:
        return None
    try:
        return _parse_iso(out.strip())
    except ValueError:
        return None


def _end_commit_by_content(repo_root, rels, simulated, traj_end):
    """
    Search commits that touched each file; return the one whose content matches
    the simulated output, choosing the commit closest to traj_end when multiple
    candidates exist.  Only considers commits at or after traj_end - 10 min.
    """
    earliest = traj_end - timedelta(minutes=10)
    candidates = {}  # hash -> (datetime, match_count)
    for fp, rel in rels.items():
        expected = simulated.get(fp)
        if expected is None:
            continue
        rc, out = _git(['log', '--all', '--format=%H %aI', '--', rel], repo_root)
        if rc != 0:
            continue
        for line in out.strip().splitlines():
            h, _, ts_str = line.partition(' ')
            if not h or not ts_str:
                continue
            try:
                dt = _parse_iso(ts_str)
            except ValueError:
                continue
            if dt < earliest:
                continue
            actual = _file_at(repo_root, h, rel)
            if actual == expected:
                if h not in candidates:
                    candidates[h] = (dt, 0)
                candidates[h] = (dt, candidates[h][1] + 1)
    if not candidates:
        return None
    ranked = sorted(
        candidates.items(),
        key=lambda kv: (-kv[1][1], abs((kv[1][0] - traj_end).total_seconds())),
    )
    return ranked[0][0]


def _end_commit_by_time(commits, traj_end, window_hours=24):
    """Fallback: earliest commit within [traj_end, traj_end + window_hours]."""
    limit = traj_end + timedelta(hours=window_hours)
    for dt, h in commits:
        if traj_end <= dt <= limit:
            return h
    return None


def find_reproducible_trajectories_claude(claude_dir=None, window_hours=24):
    """
    Scan all Claude Code trajectories in claude_dir/projects/ and return those
    whose Write/Edit ops reproduce the file state at the nearest commit after
    the trajectory.

    End-commit discovery order:
      1. Content match: find a commit where the written file content matches.
      2. Timestamp fallback: first commit within window_hours after trajectory end.

    Returns list of dicts: {cwd, trajectory, commit_before, commit_after,
                             strategy, files}
    """
    if claude_dir is None:
        claude_dir = Path.home() / '.claude'
    claude_dir = Path(claude_dir)

    timelines = {}
    results = []

    for jsonl in sorted(claude_dir.glob('projects/**/*.jsonl')):
        try:
            events = parse_trajectory(str(jsonl))
        except (OSError, json.JSONDecodeError):
            continue

        _, cwd = get_session_info(events)
        if not cwd or not Path(cwd).exists():
            continue

        timestamps = []
        for e in events:
            ts = e.get('timestamp')
            if ts:
                try:
                    timestamps.append(_parse_iso(ts))
                except ValueError:
                    pass
        if not timestamps:
            continue
        traj_start = min(timestamps)
        traj_end = max(timestamps)

        sequence, _tool_uses = build_sequence(events)
        ops = []
        for s in sequence:
            if s['seq_type'] != 'tool_use':
                continue
            item = s['item']
            if item.get('name') in ('Write', 'Edit'):
                ops.append({'tool': item['name'], 'input': item.get('input', {})})
        if not ops:
            continue

        rc, out = _git(['rev-parse', '--show-toplevel'], cwd)
        if rc != 0:
            continue
        repo_root = out.strip()

        if repo_root not in timelines:
            timelines[repo_root] = _get_commit_timeline(repo_root)
        commits = timelines[repo_root]

        fps_seen = set()
        fps_ordered = []
        for op in ops:
            fp = op['input'].get('file_path', '')
            if fp and fp not in fps_seen:
                fps_ordered.append(fp)
                fps_seen.add(fp)

        rels = {}
        skip = False
        for fp in fps_ordered:
            rel = _rel(fp, repo_root)
            if rel is None:
                skip = True
                break
            rels[fp] = rel
        if skip:
            continue

        start_commit = None
        for dt, h in commits:
            if dt <= traj_start:
                start_commit = h

        initial = {
            fp: (_file_at(repo_root, start_commit, rel) if start_commit else None)
            for fp, rel in rels.items()
        }
        simulated = _simulate_ops(ops, initial)

        end_commit = _end_commit_by_content(repo_root, rels, simulated, traj_end)
        strategy = 'content'
        if end_commit is None:
            end_commit = _end_commit_by_time(commits, traj_end, window_hours=window_hours)
            strategy = 'timestamp'
        if end_commit is None:
            continue

        parent = _get_parent(repo_root, end_commit)
        if parent:
            parent_time = _commit_time(repo_root, parent)
            if parent_time and parent_time <= traj_start:
                start_commit = parent
                initial = {fp: _file_at(repo_root, start_commit, rel) for fp, rel in rels.items()}
                simulated = _simulate_ops(ops, initial)

        matched_files = []
        any_mismatch = False
        for fp, sim in simulated.items():
            rel = rels.get(fp)
            if not rel:
                continue
            actual = _file_at(repo_root, end_commit, rel)
            if actual is None:
                continue
            if sim == actual:
                matched_files.append(rel)
            else:
                any_mismatch = True

        if not matched_files or any_mismatch:
            continue

        results.append({
            'cwd': cwd,
            'trajectory': jsonl.name,
            'commit_before': start_commit[:12] if start_commit else None,
            'commit_after': end_commit[:12],
            'strategy': strategy,
            'files': matched_files,
        })

    return results


def _parse_copilot_session(events_path):
    """
    Parse a Copilot session JSONL and return {cwd, start, end, ops} or None.

    Handles three file-editing tools:
      edit         → Edit op  {file_path, old_string, new_string}
      create       → Write op {file_path, content}
      apply_patch  → delegated to _normalize_codex_tool_call (same format as Codex)
    """
    events = []
    try:
        with open(events_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        return None

    cwd = None
    timestamps = []
    ops = []

    for e in events:
        ts = e.get('timestamp')
        if ts:
            try:
                timestamps.append(_parse_iso(ts))
            except ValueError:
                pass

        if e.get('type') == 'session.start':
            cwd = e.get('data', {}).get('context', {}).get('cwd')

        if e.get('type') == 'tool.execution_start':
            d = e.get('data', {})
            tool_name = d.get('toolName', '')
            args = d.get('arguments', {})
            if not isinstance(args, dict):
                continue

            if tool_name == 'edit':
                path = args.get('path', '')
                if path:
                    ops.append({
                        'tool': 'Edit',
                        'input': {
                            'file_path': path,
                            'old_string': args.get('old_str', ''),
                            'new_string': args.get('new_str', ''),
                        },
                    })

            elif tool_name == 'create':
                path = args.get('path', '')
                if path:
                    ops.append({
                        'tool': 'Write',
                        'input': {
                            'file_path': path,
                            'content': args.get('file_text', ''),
                        },
                    })

            elif tool_name == 'apply_patch':
                patch = args.get('patch') or args.get('input', '')
                normalized = _normalize_codex_tool_call('apply_patch', patch, cwd)
                if normalized:
                    path, _, patch_args = normalized
                    ops.append({
                        'tool': 'Write',
                        'input': {'file_path': path, '_apply_patch': patch_args},
                    })

    if not cwd or not timestamps or not ops:
        return None

    return {
        'path': str(events_path),
        'cwd': cwd,
        'start': min(timestamps),
        'end': max(timestamps),
        'ops': ops,
    }


def find_reproducible_trajectories_copilot(copilot_dir=None, window_hours=24):
    """
    Scan all Copilot agent sessions in copilot_dir/session-state/ and return
    those whose file edits reproduce the file state at the nearest commit.

    Uses the same content-match + timestamp-fallback strategy as the Claude variant.

    Returns list of dicts: {cwd, trajectory, commit_before, commit_after,
                             strategy, files}
    """
    if copilot_dir is None:
        copilot_dir = Path.home() / '.copilot'
    copilot_dir = Path(copilot_dir)

    timelines = {}
    results = []

    for events_path in sorted(copilot_dir.glob('session-state/*/events.jsonl')):
        info = _parse_copilot_session(events_path)
        if info is None:
            continue

        cwd = info['cwd']
        if not Path(cwd).exists():
            continue

        rc, out = _git(['rev-parse', '--show-toplevel'], cwd)
        if rc != 0:
            continue
        repo_root = out.strip()

        if repo_root not in timelines:
            timelines[repo_root] = _get_commit_timeline(repo_root)
        commits = timelines[repo_root]

        ops = [op for op in info['ops'] if op['tool'] in ('Write', 'Edit')]
        # apply_patch ops are stored as Write with _apply_patch key — skip them
        # for simulation (content is not directly usable); keep only plain Write/Edit
        ops = [op for op in ops if '_apply_patch' not in op['input']]
        if not ops:
            continue

        fps_seen = set()
        fps_ordered = []
        for op in ops:
            fp = op['input'].get('file_path', '')
            if fp and fp not in fps_seen:
                fps_ordered.append(fp)
                fps_seen.add(fp)

        rels = {}
        skip = False
        for fp in fps_ordered:
            rel = _rel(fp, repo_root)
            if rel is None:
                skip = True
                break
            rels[fp] = rel
        if skip:
            continue

        traj_start = info['start']
        traj_end = info['end']

        start_commit = None
        for dt, h in commits:
            if dt <= traj_start:
                start_commit = h

        initial = {
            fp: (_file_at(repo_root, start_commit, rel) if start_commit else None)
            for fp, rel in rels.items()
        }
        simulated = _simulate_ops(ops, initial)

        end_commit = _end_commit_by_content(repo_root, rels, simulated, traj_end)
        strategy = 'content'
        if end_commit is None:
            end_commit = _end_commit_by_time(commits, traj_end, window_hours=window_hours)
            strategy = 'timestamp'
        if end_commit is None:
            continue

        parent = _get_parent(repo_root, end_commit)
        if parent:
            parent_time = _commit_time(repo_root, parent)
            if parent_time and parent_time <= traj_start:
                start_commit = parent
                initial = {fp: _file_at(repo_root, start_commit, rel) for fp, rel in rels.items()}
                simulated = _simulate_ops(ops, initial)

        matched_files = []
        any_mismatch = False
        for fp, sim in simulated.items():
            rel = rels.get(fp)
            if not rel:
                continue
            actual = _file_at(repo_root, end_commit, rel)
            if actual is None:
                continue
            if sim == actual:
                matched_files.append(rel)
            else:
                any_mismatch = True

        if not matched_files or any_mismatch:
            continue

        results.append({
            'cwd': cwd,
            'trajectory': events_path.parent.name,
            'commit_before': start_commit[:12] if start_commit else None,
            'commit_after': end_commit[:12],
            'strategy': strategy,
            'files': matched_files,
        })

    return results


def find_reproducible_trajectories_opencode(window_hours=24):
    """Placeholder: find reproducible trajectories from OpenCode sessions."""
    raise NotImplementedError("find_reproducible_trajectories_opencode is not yet implemented")


# ---------------------------------------------------------------------------
# add-trajectories-to-repo
# ---------------------------------------------------------------------------

def add_trajectories_to_repo(repo_path, claude_dir=None, dry_run=False):
    """
    For each trajectory referenced in commits, copy it to <repo>/trajectories/
    if and only if all files it reads are within the repo.

    Returns list of dicts: {trajectory, status, dest (optional)}
    status values:
      'added'           — trajectory copied to trajectories/
      'already_exists'  — trajectory file already present in trajectories/
      'skipped_private' — trajectory reads files outside the repo
      'not_found'       — trajectory file could not be resolved
    """
    if claude_dir is None:
        claude_dir = str(Path.home() / '.claude')
    repo_path = str(Path(repo_path).resolve())
    repo_root = Path(repo_path)

    results = []
    seen_refs = set()

    for _commit_hash, message in _get_commits(repo_path):
        ref = _extract_trajectory_ref(message)
        if not ref or ref in seen_refs:
            continue
        seen_refs.add(ref)

        traj_path = resolve_trajectory_path(ref, claude_dir)
        if not os.path.exists(traj_path):
            results.append({'trajectory': ref, 'status': 'not_found'})
            continue

        dest_name = Path(traj_path).name
        dest = repo_root / 'trajectories' / dest_name

        if dest.exists():
            results.append({
                'trajectory': ref,
                'status': 'already_exists',
                'dest': str(dest.relative_to(repo_root)),
            })
            continue

        # Check that every file read is within the repo
        events = parse_trajectory(traj_path)
        _, session_cwd = get_session_info(events)
        read_files = analyze_read_files(traj_path)

        all_in_repo = True
        for r in read_files:
            fp = r['file_path']
            if not os.path.isabs(fp):
                if session_cwd:
                    fp = str(Path(session_cwd) / fp)
                else:
                    all_in_repo = False
                    break
            try:
                Path(fp).resolve().relative_to(repo_root)
            except ValueError:
                all_in_repo = False
                break

        if not all_in_repo:
            results.append({'trajectory': ref, 'status': 'skipped_private'})
            continue

        dest_rel = str(dest.relative_to(repo_root))
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(traj_path, dest)
        results.append({'trajectory': ref, 'status': 'added', 'dest': dest_rel})

    return results


# ---------------------------------------------------------------------------
# open-source-trajectories
# ---------------------------------------------------------------------------

def _find_git_root(file_path):
    """Return the git repo root for the given file path, or None."""
    d = Path(file_path).parent
    # Walk up to find an existing ancestor directory
    while d != d.parent:
        if d.exists():
            break
        d = d.parent
    else:
        return None
    rc, out = _git(['rev-parse', '--show-toplevel'], str(d))
    if rc != 0:
        return None
    return out.strip()


def _get_github_remote_urls(repo_path):
    """Return set of GitHub remote URLs for the given git repo."""
    rc, out = _git(['remote', '-v'], repo_path)
    if rc != 0:
        return set()
    urls = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and 'github.com' in parts[1]:
            urls.add(parts[1])
    return urls


def _parse_github_owner_repo(url):
    """Parse a GitHub remote URL and return 'owner/repo', or None."""
    m = re.match(
        r'(?:https?://github\.com/|git@github\.com:)([^/]+/[^/\s]+?)(?:\.git)?$',
        url,
    )
    return m.group(1) if m else None


def _is_public_github_repo(owner_repo):
    """Return True if the GitHub repo is publicly accessible (no API rate limit)."""
    import urllib.request
    import urllib.error
    url = f'https://github.com/{owner_repo}'
    req = urllib.request.Request(
        url, method='HEAD', headers={'User-Agent': 'reproducible-trajectories'}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        return False
    except Exception:
        return False


def _public_github_repo_url(repo_root, public_cache):
    """Return a public GitHub repo URL for repo_root, or None."""
    github_urls = _get_github_remote_urls(repo_root)
    for url in sorted(github_urls):
        owner_repo = _parse_github_owner_repo(url)
        if owner_repo is None:
            continue
        if owner_repo not in public_cache:
            public_cache[owner_repo] = _is_public_github_repo(owner_repo)
        if public_cache[owner_repo]:
            return f'https://github.com/{owner_repo}'
    return None


def _get_codex_session_info(events):
    """
    Extract (session_id, cwd) from OpenAI Codex CLI session events.

    Codex sessions include a metadata object with a 'cwd' field, typically
    the first event in the JSONL file. Only events that look like session
    metadata (i.e. contain 'cwd' but no 'role' field used by chat messages)
    are considered.
    """
    for e in events:
        if not isinstance(e, dict):
            continue
        if e.get('type') == 'session_meta':
            payload = e.get('payload') or {}
            if payload.get('cwd'):
                return payload.get('id') or payload.get('session_id'), payload['cwd']
        # Chat messages (user/assistant/tool) carry a 'role' field — skip them.
        # Session metadata events have 'cwd' but no 'role'.
        if e.get('role'):
            continue
        if e.get('cwd'):
            return e.get('id') or e.get('session_id'), e['cwd']
    return None, None


def _normalize_codex_tool_call(name, raw_input, session_cwd):
    """Return (absolute_path, tool_name, input_dict) for a Codex edit call."""
    if name == 'write_file':
        args = raw_input if isinstance(raw_input, dict) else {}
        path = args.get('path', '')
        if not path:
            return None
        if not os.path.isabs(path) and session_cwd:
            path = str(Path(session_cwd) / path)
        return path, 'write_file', args

    if name != 'apply_patch':
        return None

    if isinstance(raw_input, dict):
        args = raw_input
        patch = args.get('patch') or args.get('input', '')
    elif isinstance(raw_input, str):
        patch = raw_input
        args = {'input': raw_input}
    else:
        return None

    if not isinstance(patch, str):
        return None

    for line in patch.splitlines():
        for prefix in ('*** Update File: ', '*** Add File: '):
            if line.startswith(prefix):
                path = line[len(prefix):].strip()
                if path:
                    if not os.path.isabs(path) and session_cwd:
                        path = str(Path(session_cwd) / path)
                    return path, 'apply_patch', args
        if line.startswith('+++ '):
            path = line[4:].strip()
            if path.startswith('b/'):
                path = path[2:]
            if path and path != '/dev/null':
                if not os.path.isabs(path) and session_cwd:
                    path = str(Path(session_cwd) / path)
                return path, 'apply_patch', args

    return None


def _collect_codex_modifications(events, session_cwd=None):
    """
    Extract first file modification per path from OpenAI Codex CLI session events.

    Handles two tool call styles:
      - write_file: {"path": "...", "content": "..."}
      - apply_patch: {"patch": "..."} or {"input": "..."}
        Supports both the OpenAI custom patch format
        ("*** Update File: path" / "*** Add File: path") and
        standard unified diff format ("+++ b/path" / "+++ path").

    Returns dict: {absolute_file_path: {tool, input}}
    """
    seen = {}
    for e in events:
        if not isinstance(e, dict):
            continue

        payload = e.get('payload') or {}
        if e.get('type') == 'patch_apply_end' or payload.get('type') == 'patch_apply_end':
            patch_payload = payload if payload.get('type') == 'patch_apply_end' else e.get('payload') or {}
            if patch_payload.get('success'):
                for path in (patch_payload.get('changes') or {}):
                    if path and path not in seen:
                        seen[path] = {'tool': 'apply_patch', 'input': patch_payload}

        if e.get('type') == 'response_item' and payload.get('type') in ('function_call', 'custom_tool_call'):
            name = payload.get('name', '')
            raw_input = payload.get('input')
            if raw_input is None and isinstance(payload.get('arguments'), str):
                try:
                    raw_input = json.loads(payload.get('arguments', '{}'))
                except (json.JSONDecodeError, TypeError, ValueError):
                    raw_input = {}
            normalized = _normalize_codex_tool_call(name, raw_input, session_cwd)
            if normalized is not None:
                path, tool, args = normalized
                if path not in seen:
                    seen[path] = {'tool': tool, 'input': args}

        tool_calls = e.get('tool_calls') or []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            func = tc.get('function', {})
            name = func.get('name', '')
            try:
                args = json.loads(func.get('arguments', '{}'))
            except (json.JSONDecodeError, TypeError, ValueError):
                args = {}
            normalized = _normalize_codex_tool_call(name, args, session_cwd)
            if normalized is not None:
                path, tool, normalized_args = normalized
                if path not in seen:
                    seen[path] = {'tool': tool, 'input': normalized_args}
    return seen


def collect_shareable_trajectories(claude_dir=None, codex_dir=None, public_only=False):
    """
    Scan Claude Code, pi, and Codex trajectories and return those where every
    edit is within a single git repository.

    Returns list of dicts, one per unique (local_folder, github_repo) pair:
      {local_folder, github_repo, trajectories: [...], edited_files: [...]}

    If public_only is True, only keep repositories with a public GitHub remote.
    """
    if claude_dir is None:
        claude_dir = Path.home() / '.claude'
    claude_dir = Path(claude_dir)

    if codex_dir is None:
        codex_dir = Path.home() / '.codex'
    codex_dir = Path(codex_dir)

    # key: (local_folder, github_repo) -> {trajectories: set, edited_files: set}
    groups = {}
    _public_cache = {}

    def _process_modifications(traj_path, modifications):
        """Shared logic: group a trajectory by git repo and public GitHub URL."""
        if not modifications:
            return

        # Require all edits to fall under a single git repo root
        git_roots = set()
        all_in_git = True
        for file_path in modifications:
            root = _find_git_root(file_path)
            if root is None:
                all_in_git = False
                break
            git_roots.add(root)

        if not all_in_git or len(git_roots) != 1:
            return

        repo_root = next(iter(git_roots))

        public_repo_url = _public_github_repo_url(repo_root, _public_cache)
        if public_only and public_repo_url is None:
            return

        key = (repo_root, public_repo_url)
        if key not in groups:
            groups[key] = {'trajectories': [], 'edited_files': set()}
        groups[key]['trajectories'].append(str(traj_path))
        groups[key]['edited_files'].update(modifications.keys())

    # --- Claude Code trajectories ---
    for traj_path in sorted(claude_dir.glob('projects/**/*.jsonl')):
        try:
            events = parse_trajectory(str(traj_path))
        except (json.JSONDecodeError, OSError):
            continue
        sequence, _tool_uses = build_sequence(events)
        modifications = collect_modifications(sequence)
        _process_modifications(traj_path, modifications)

    # --- pi trajectories ---
    for traj_path in pi_trajectory.iter_session_paths():
        try:
            events = parse_trajectory(str(traj_path))
        except (json.JSONDecodeError, OSError):
            continue
        sequence, _tool_uses = build_sequence(events)
        modifications = collect_modifications(sequence)
        _process_modifications(traj_path, modifications)

    # --- Codex CLI trajectories ---
    for traj_path in sorted(codex_dir.glob('sessions/**/*.jsonl')):
        try:
            events = []
            with open(traj_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
        except (json.JSONDecodeError, OSError):
            continue
        _, session_cwd = _get_codex_session_info(events)
        modifications = _collect_codex_modifications(events, session_cwd)
        _process_modifications(traj_path, modifications)

    return [
        {
            'local_folder': local_folder,
            'github_repo': github_repo,
            'trajectories': sorted(info['trajectories']),
            'edited_files': sorted(info['edited_files']),
        }
        for (local_folder, github_repo), info in sorted(groups.items())
    ]


def open_source_trajectories(claude_dir=None, codex_dir=None):
    """Return shareable trajectories limited to public GitHub repositories."""
    return collect_shareable_trajectories(
        claude_dir=claude_dir,
        codex_dir=codex_dir,
        public_only=True,
    )


# ---------------------------------------------------------------------------
# share-trajectories CLI helpers
# ---------------------------------------------------------------------------

def _prompt_share_scope():
    """Ask whether to share all trajectories or only open-source ones."""
    print("Do you want to:")
    print("  1) share all trajectories")
    print("  2) only share the ones from open-source repositories")
    answer = input(
        "All collected data will be properly filtered and anonymized. [1/2/N] "
    ).strip().lower()
    if answer == '1':
        return 'all'
    if answer == '2':
        return 'open-source'
    return None


def _share_scope_results(all_results, scope):
    """Return the repository groups matching the selected sharing scope."""
    if scope == 'all':
        return all_results
    return [r for r in all_results if r['github_repo']]


def _share_scope_label(scope):
    """Return a human-readable label for the selected sharing scope."""
    if scope == 'all':
        return 'all repos'
    return 'open-source repos'


def _share_target_label(result):
    """Return the best label for a share target."""
    return result['github_repo'] or f"{result['local_folder']}  (private/non-GitHub repo)"


def _share_target_slug(result, index):
    """Return a stable archive folder name for a share target."""
    if result['github_repo']:
        return result['github_repo'].replace('https://github.com/', '').replace('/', '_')
    return f"repo_{index:03d}"


# ---------------------------------------------------------------------------
# replay-trajectories
# ---------------------------------------------------------------------------

_REPLAY_WRITE_TOOLS = {
    "Write": ("file_path", "content"),
    "write": ("path", "content"),
    "write_file": ("path", "content"),
    "create_or_update_file": ("path", "content"),
}

_REPLAY_EDIT_TOOLS = {
    "Edit": ("file_path", "old_string", "new_string", "replace_all"),
    "edit": ("path", "oldText", "newText", "replace_all"),
}


def _normalize_write_path(raw_path, session_cwd):
    if not raw_path:
        return None
    path = Path(raw_path)
    if not path.is_absolute() and session_cwd:
        path = Path(session_cwd) / path
    return path.resolve()


def _result_content_indicates_error(content):
    if not isinstance(content, str):
        return False
    lowered = content.lower()
    return "<tool_use_error>" in lowered or "is_error" in lowered and "true" in lowered


def _successful_tool_calls(step):
    """Yield tool calls whose paired tool results do not indicate failure."""
    results = {
        result.get("source_call_id"): result.get("content", "")
        for result in (step.get("observation") or {}).get("results", [])
        if isinstance(result, dict)
    }
    for call in step.get("tool_calls", []):
        call_id = call.get("tool_call_id")
        if call_id and _result_content_indicates_error(results.get(call_id, "")):
            continue
        yield call


def _path_in_repo(abs_path, repo_root):
    if abs_path is None:
        return None
    try:
        return Path(abs_path).resolve().relative_to(repo_root)
    except ValueError:
        return None


def _join_lines(lines):
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _find_subsequence(haystack, needle):
    if not needle:
        return 0
    limit = len(haystack) - len(needle) + 1
    for index in range(max(limit, 0)):
        if haystack[index : index + len(needle)] == needle:
            return index
    return -1


def _replace_between_anchors(current_text, old_string, new_string):
    old_lines = [line for line in old_string.splitlines() if line.strip()]
    if len(old_lines) < 2:
        return None
    first_line = old_lines[0]
    last_line = old_lines[-1]
    start = current_text.find(first_line)
    if start == -1:
        return None
    end_marker = current_text.find(last_line, start + len(first_line))
    if end_marker == -1:
        return None
    line_end = current_text.find("\n", end_marker)
    if line_end == -1:
        line_end = len(current_text)
    else:
        line_end += 1
    return current_text[:start] + new_string + current_text[line_end:]


def _apply_line_hunks(current_text, hunks, file_label):
    lines = current_text.splitlines()
    trailing_newline = current_text.endswith("\n")
    for hunk in hunks:
        old_lines = hunk["old_lines"]
        new_lines = hunk["new_lines"]
        index = _find_subsequence(lines, old_lines)
        if index == -1:
            if _find_subsequence(lines, new_lines) != -1:
                continue
            raise RuntimeError(f"could not apply patch hunk to {file_label}")
        lines[index : index + len(old_lines)] = new_lines
        trailing_newline = True
    if not lines:
        return ""
    return "\n".join(lines) + ("\n" if trailing_newline else "")


def _parse_openai_patch(patch_text):
    lines = patch_text.splitlines()
    changes = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("*** Update File: "):
            path = line[len("*** Update File: ") :].strip()
            move_to = None
            index += 1
            if index < len(lines) and lines[index].startswith("*** Move to: "):
                move_to = lines[index][len("*** Move to: ") :].strip()
                index += 1
            hunks = []
            while index < len(lines) and not lines[index].startswith("*** "):
                if lines[index].startswith("@@"):
                    index += 1
                    old_lines = []
                    new_lines = []
                    while index < len(lines) and not lines[index].startswith("@@") and not lines[index].startswith("*** "):
                        prefix = lines[index][:1]
                        content = lines[index][1:] if lines[index] else ""
                        if prefix == " ":
                            old_lines.append(content)
                            new_lines.append(content)
                        elif prefix == "-":
                            old_lines.append(content)
                        elif prefix == "+":
                            new_lines.append(content)
                        index += 1
                    hunks.append({"old_lines": old_lines, "new_lines": new_lines})
                    continue
                index += 1
            changes.append({"kind": "patch", "path": path, "move_to": move_to, "hunks": hunks})
            continue
        if line.startswith("*** Add File: "):
            path = line[len("*** Add File: ") :].strip()
            index += 1
            added_lines = []
            while index < len(lines) and not lines[index].startswith("*** "):
                if lines[index].startswith("+"):
                    added_lines.append(lines[index][1:])
                index += 1
            changes.append({"kind": "write", "path": path, "content": _join_lines(added_lines)})
            continue
        if line.startswith("*** Delete File: "):
            path = line[len("*** Delete File: ") :].strip()
            changes.append({"kind": "delete", "path": path})
        index += 1
    return changes


def _parse_unified_diff_patch(patch_text):
    lines = patch_text.splitlines()
    changes = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- "):
            index += 1
            continue
        old_path = lines[index][4:].strip()
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            break
        new_path = lines[index][4:].strip()
        index += 1
        hunks = []
        while index < len(lines) and not lines[index].startswith("--- "):
            if lines[index].startswith("@@"):
                index += 1
                old_lines = []
                new_lines = []
                while index < len(lines) and not lines[index].startswith("@@") and not lines[index].startswith("--- "):
                    if lines[index].startswith("\\"):
                        index += 1
                        continue
                    prefix = lines[index][:1]
                    content = lines[index][1:] if lines[index] else ""
                    if prefix == " ":
                        old_lines.append(content)
                        new_lines.append(content)
                    elif prefix == "-":
                        old_lines.append(content)
                    elif prefix == "+":
                        new_lines.append(content)
                    index += 1
                hunks.append({"old_lines": old_lines, "new_lines": new_lines})
                continue
            index += 1
        old_path = old_path[2:] if old_path.startswith("a/") else old_path
        new_path = new_path[2:] if new_path.startswith("b/") else new_path
        if old_path == "/dev/null":
            changes.append({"kind": "write", "path": new_path, "content": _apply_line_hunks("", hunks, new_path)})
        elif new_path == "/dev/null":
            changes.append({"kind": "delete", "path": old_path})
        else:
            changes.append({"kind": "patch", "path": old_path, "move_to": None, "hunks": hunks})
    return changes


def _parse_codex_patch_changes(patch_text, session_cwd, repo_root):
    if not isinstance(patch_text, str) or not patch_text.strip():
        return []
    raw_changes = (
        _parse_openai_patch(patch_text)
        if "*** Begin Patch" in patch_text
        else _parse_unified_diff_patch(patch_text)
    )
    changes = []
    for change in raw_changes:
        abs_path = _normalize_write_path(change.get("path"), session_cwd)
        rel_path = _path_in_repo(abs_path, repo_root)
        if rel_path is None:
            continue
        normalized = dict(change)
        normalized["path"] = abs_path
        normalized["rel_path"] = str(rel_path)
        if change.get("move_to"):
            move_abs = _normalize_write_path(change["move_to"], session_cwd)
            move_rel = _path_in_repo(move_abs, repo_root)
            if move_rel is None:
                continue
            normalized["move_to"] = move_abs
            normalized["move_to_rel_path"] = str(move_rel)
        changes.append(normalized)
    return changes


def _operation_fingerprint(operation):
    serializable_changes = []
    for change in operation["changes"]:
        item = {"kind": change["kind"], "rel_path": change.get("rel_path")}
        for key in (
            "content",
            "old_string",
            "new_string",
            "replace_all",
            "move_to_rel_path",
            "hunks",
        ):
            if key in change:
                item[key] = change[key]
        serializable_changes.append(item)
    return json.dumps(
        {
            "timestamp_raw": operation["timestamp_raw"],
            "agent": operation["agent"],
            "tool": operation["tool"],
            "changes": serializable_changes,
        },
        sort_keys=True,
    )


def _collect_replay_operations(repo_root):
    """Collect replayable file mutations from trajectories discoverable for repo_root."""
    repo_root = Path(repo_root).resolve()
    operations = []
    for record in trajectoriz_local_records(str(repo_root)):
        agent_name = record.agent
        traj = trajectoriz_parse_record(record)
        if traj is None:
            continue
        traj_source = str(record.source)
        for step in traj.steps:
            timestamp = step.get("timestamp")
            if not timestamp:
                continue
            for call in _successful_tool_calls(step):
                tool_name = call.get("function_name")
                args = call.get("arguments") or {}
                changes = []
                write_spec = _REPLAY_WRITE_TOOLS.get(tool_name)
                edit_spec = _REPLAY_EDIT_TOOLS.get(tool_name)
                if write_spec is not None:
                    path_key, content_key = write_spec
                    raw_path = args.get(path_key) or args.get("file_path")
                    content = args.get(content_key)
                    abs_path = _normalize_write_path(raw_path, traj.cwd)
                    rel_path = _path_in_repo(abs_path, repo_root)
                    if rel_path is None or content is None:
                        continue
                    changes = [{
                        "kind": "write",
                        "path": abs_path,
                        "rel_path": str(rel_path),
                        "content": content,
                    }]
                elif edit_spec is not None:
                    path_key, old_key, new_key, replace_key = edit_spec
                    raw_path = args.get(path_key) or args.get("file_path")
                    abs_path = _normalize_write_path(raw_path, traj.cwd)
                    rel_path = _path_in_repo(abs_path, repo_root)
                    if rel_path is None:
                        continue
                    changes = [{
                        "kind": "edit",
                        "path": abs_path,
                        "rel_path": str(rel_path),
                        "old_string": args.get(old_key, ""),
                        "new_string": args.get(new_key, ""),
                        "replace_all": bool(args.get(replace_key, False)),
                    }]
                elif tool_name == "apply_patch":
                    patch_text = args.get("patch") or args.get("input", "")
                    changes = _parse_codex_patch_changes(patch_text, traj.cwd, repo_root)
                if not changes:
                    continue
                operations.append({
                    "timestamp": _parse_iso(timestamp),
                    "timestamp_raw": timestamp,
                    "agent": agent_name,
                    "trajectory": traj_source,
                    "tool": tool_name,
                    "changes": changes,
                })
    operations.sort(key=lambda item: (item["timestamp"], item["trajectory"], item["tool"]))
    deduped = []
    seen = set()
    for operation in operations:
        fingerprint = _operation_fingerprint(operation)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduped.append(operation)
    return deduped


def _apply_replay_change(change):
    kind = change["kind"]
    path = Path(change["path"])
    if kind == "write":
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = path.read_text() if path.exists() else None
        if previous == change["content"]:
            return False, [change["rel_path"]]
        path.write_text(change["content"])
        return True, [change["rel_path"]]
    if kind == "edit":
        if not path.exists():
            raise RuntimeError(f"cannot edit missing file {change['rel_path']}")
        current = path.read_text()
        old_string = change["old_string"]
        if old_string not in current:
            if change["new_string"] in current:
                return False, [change["rel_path"]]
            replaced = _replace_between_anchors(current, old_string, change["new_string"])
            if replaced is None:
                print(
                    f"warning: skipping unreplayable edit for {change['rel_path']}",
                    file=sys.stderr,
                )
                return False, [change["rel_path"]]
            if replaced == current:
                return False, [change["rel_path"]]
            path.write_text(replaced)
            return True, [change["rel_path"]]
        updated = (
            current.replace(old_string, change["new_string"])
            if change["replace_all"]
            else current.replace(old_string, change["new_string"], 1)
        )
        if updated == current:
            return False, [change["rel_path"]]
        path.write_text(updated)
        return True, [change["rel_path"]]
    if kind == "patch":
        current = path.read_text() if path.exists() else ""
        try:
            updated = _apply_line_hunks(current, change["hunks"], change["rel_path"])
        except RuntimeError:
            print(
                f"warning: skipping unreplayable patch for {change['rel_path']}",
                file=sys.stderr,
            )
            return False, [change["rel_path"]]
        if change.get("move_to"):
            target_path = Path(change["move_to"])
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                path.unlink()
            target_path.write_text(updated)
            return True, [change["rel_path"], change["move_to_rel_path"]]
        if updated == current:
            return False, [change["rel_path"]]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated)
        return True, [change["rel_path"]]
    if kind == "delete":
        if not path.exists():
            return False, [change["rel_path"]]
        path.unlink()
        return True, [change["rel_path"]]
    raise RuntimeError(f"unsupported replay change kind: {kind}")


def _git_run(repo_root, args, env=None):
    return subprocess.run(
        ["git"] + args,
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
    )


def _staged_replay_commit_message(repo_root, agent, staged_paths):
    diff = _git_run(repo_root, ["diff", "--cached", "--numstat", "--", *staged_paths])
    if diff.returncode != 0:
        raise RuntimeError(diff.stderr.strip() or "git diff --cached failed")
    added = 0
    deleted = 0
    for line in diff.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        a, d, _path = parts[:3]
        added += int(a) if a.isdigit() else 0
        deleted += int(d) if d.isdigit() else 0
    line_count = added + deleted
    if len(staged_paths) == 1:
        path_label = staged_paths[0]
    else:
        path_label = ", ".join(staged_paths[:2])
        if len(staged_paths) > 2:
            path_label += f", +{len(staged_paths) - 2} more"
    return f"replay: {agent} {path_label} ({line_count} lines changed)"


def _ensure_clean_worktree(repo_root):
    status = _git_run(repo_root, ["status", "--porcelain"])
    if status.returncode != 0:
        raise RuntimeError(status.stderr.strip() or "git status failed")
    if status.stdout.strip():
        raise RuntimeError("git worktree is not clean; replay-trajectories requires a clean repository")


def replay_trajectories(repo_path, trajectory_dir=None):
    """
    Replay file mutation events from stored trajectories and commit them chronologically.

    Supports whole-file writes, Edit operations, and Codex apply_patch calls.
    The repository must be clean. Returns a list of created commits, one per
    effective mutation event.
    """
    repo_root = Path(repo_path).resolve()
    if trajectory_dir is not None:
        raise ValueError("--trajectory-dir is no longer supported; replay-trajectories now follows trajectoriz local discovery")

    _ensure_clean_worktree(repo_root)
    operations = _collect_replay_operations(repo_root)
    if not operations:
        return []

    commits = []
    for operation in operations:
        changed = False
        staged_paths = []
        for change in operation["changes"]:
            change_changed, change_paths = _apply_replay_change(change)
            changed = changed or change_changed
            staged_paths.extend(change_paths)
        staged_paths = list(dict.fromkeys(staged_paths))
        if not changed:
            continue
        add_result = _git_run(repo_root, ["add", "-A", "--", *staged_paths])
        if add_result.returncode != 0:
            raise RuntimeError(add_result.stderr.strip() or f"git add failed for {staged_paths}")

        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = operation["timestamp_raw"]
        env["GIT_COMMITTER_DATE"] = operation["timestamp_raw"]
        commit_message = _staged_replay_commit_message(repo_root, operation["agent"], staged_paths)
        commit_result = _git_run(repo_root, ["commit", "-m", commit_message], env=env)
        if commit_result.returncode != 0:
            raise RuntimeError(commit_result.stderr.strip() or f"git commit failed for {staged_paths}")

        rev_parse = _git_run(repo_root, ["rev-parse", "HEAD"])
        commit_hash = rev_parse.stdout.strip() if rev_parse.returncode == 0 else None
        commits.append({
            "commit": commit_hash,
            "agent": operation["agent"],
            "file": staged_paths[0] if len(staged_paths) == 1 else f"{len(staged_paths)} files",
            "files": staged_paths,
            "timestamp": operation["timestamp_raw"],
            "trajectory": operation["trajectory"],
        })
    return commits


# ---------------------------------------------------------------------------
# collection webhook
# ---------------------------------------------------------------------------

_COLLECTION_HOOK_MARKER = '# reproducible-trajectories collection hook'


def _install_collection_webhook(repo_paths):
    """Add/update a pre-commit git hook in each repo that shares trajectories."""
    binary = shutil.which('pre-commit-collect-trajectories') or 'pre-commit-collect-trajectories'
    hook_body = f'{binary}\n'
    installed = 0
    for repo_path in repo_paths:
        hook_path = Path(repo_path) / '.git' / 'hooks' / 'pre-commit'
        marker_line = _COLLECTION_HOOK_MARKER + '\n'
        hook_snippet = marker_line + hook_body

        if hook_path.exists():
            existing = hook_path.read_text()
            if _COLLECTION_HOOK_MARKER in existing:
                print(f"  already installed: {hook_path}")
                continue
            # Append to existing hook
            new_content = existing.rstrip('\n') + '\n\n' + hook_snippet
        else:
            new_content = '#!/bin/sh\n\n' + hook_snippet

        hook_path.write_text(new_content)
        hook_path.chmod(0o755)
        print(f"  ✅ installed: {hook_path}")
        installed += 1
    return installed


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

    p_verify = subparsers.add_parser(
        "verify-trajectories",
        help="Check whether trajectory ops reproduce the actual commit",
    )
    p_verify.add_argument("repo", help="Path to the git repository")
    p_verify.add_argument(
        "--claude-dir",
        default=None,
        help="Path to .claude directory (default: ~/.claude)",
    )
    p_verify.add_argument("--json", action="store_true", help="Output results as JSON")

    p_add = subparsers.add_parser(
        "add-trajectories-to-repo",
        help="Copy repo-safe trajectories into <repo>/trajectories/",
    )
    p_add.add_argument("repo", help="Path to the git repository")
    p_add.add_argument(
        "--claude-dir",
        default=None,
        help="Path to .claude directory (default: ~/.claude)",
    )
    p_add.add_argument("--json", action="store_true", help="Output results as JSON")
    p_add.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be copied without writing anything",
    )

    p_share = subparsers.add_parser(
        "share-trajectories",
        aliases=["open-source-trajectories"],
        help="Share local trajectories from all repos or only open-source ones",
    )
    p_share.add_argument(
        "--claude-dir",
        default=None,
        help="Path to .claude directory (default: ~/.claude)",
    )
    p_share.add_argument("--json", action="store_true", help="Output results as JSON")
    p_share.add_argument(
        "--yes",
        action="store_true",
        help="Auto-confirm sharing all trajectories for the selected scope",
    )
    p_share.add_argument(
        "--scope",
        choices=["all", "open-source"],
        default=None,
        help="Sharing scope for non-interactive use (default: ask interactively)",
    )
    p_share.add_argument(
        "--codex-dir",
        default=None,
        help="Path to Codex CLI sessions directory (default: ~/.codex)",
    )

    p_filter = subparsers.add_parser(
        "filter-trajectories",
        help="Remove tool calls related to specified files/folders",
    )
    p_filter.add_argument("trajectory", help="Path to trajectory JSONL file, or session ID")
    p_filter.add_argument(
        "paths",
        nargs="*",
        help="Files/folders to exclude (if omitted, exclude files outside cwd)",
    )
    p_filter.add_argument(
        "--claude-dir",
        default=None,
        help="Path to .claude directory (default: ~/.claude)",
    )
    p_filter.add_argument(
        "--cwd",
        default=None,
        help="Base directory for 'outside' check (default: session cwd or current dir)",
    )
    p_filter.add_argument(
        "--output", "-o",
        default=None,
        help="Output file (default: stdout)",
    )

    p_replay = subparsers.add_parser(
        "replay-trajectories",
        help="Replay stored write events in chronological order and commit them",
    )
    p_replay.add_argument(
        "repo",
        nargs="?",
        default=".",
        help="Path to the git repository (default: current directory)",
    )

    p_exec = subparsers.add_parser(
        "check-execution-reproducible",
        help="Check whether a trajectory satisfies both execution-reproducibility criteria",
    )
    p_exec.add_argument("trajectory", help="Path to trajectory JSONL file, or session ID")
    p_exec.add_argument(
        "--repo",
        default=None,
        help="Git repo to verify the edit criterion against (optional)",
    )
    p_exec.add_argument(
        "--cwd",
        default=None,
        help="Working directory for command re-execution (default: trajectory cwd)",
    )
    p_exec.add_argument(
        "--claude-dir",
        default=None,
        help="Path to .claude directory (default: ~/.claude)",
    )
    p_exec.add_argument("--json", action="store_true", help="Output results as JSON")

    subparsers.add_parser(
        "collect-trajectories",
        help="Find the most recent trajectory matching staged files, check reproducibility, and POST to API",
    )

    p_find = subparsers.add_parser(
        "find-reproducible-trajectories-claude",
        help="Scan ~/.claude/projects for trajectories whose edits reproduce a nearby commit",
    )
    p_find.add_argument(
        "--claude-dir",
        default=None,
        help="Path to .claude directory (default: ~/.claude)",
    )
    p_find.add_argument("--json", action="store_true", help="Output results as JSON")
    p_find.add_argument(
        "--output", "-o",
        default=None,
        help="Write JSON results to this file",
    )
    p_find.add_argument(
        "--window-hours",
        type=float,
        default=24.0,
        help="Hours after trajectory end to search for a commit via timestamp fallback (default: 24)",
    )
    p_find.add_argument(
        "--yes",
        action="store_true",
        help="Auto-confirm submission to the server",
    )

    p_find_copilot = subparsers.add_parser(
        "find-reproducible-trajectories-copilot",
        help="Scan ~/.copilot/session-state for trajectories whose edits reproduce a nearby commit",
    )
    p_find_copilot.add_argument(
        "--copilot-dir",
        default=None,
        help="Path to .copilot directory (default: ~/.copilot)",
    )
    p_find_copilot.add_argument("--json", action="store_true", help="Output results as JSON")
    p_find_copilot.add_argument(
        "--output", "-o",
        default=None,
        help="Write JSON results to this file",
    )
    p_find_copilot.add_argument(
        "--window-hours",
        type=float,
        default=24.0,
        help="Hours after session end to search for a commit via timestamp fallback (default: 24)",
    )
    p_find_copilot.add_argument(
        "--yes",
        action="store_true",
        help="Auto-confirm submission to the server",
    )

    args = parser.parse_args(argv)
    claude_dir = Path(args.claude_dir or Path.home() / ".claude") if hasattr(args, "claude_dir") else Path.home() / ".claude"

    if args.command == "collect-trajectories":
        from reproducible_trajectories.hooks.collect_trajectories import run as collect_run
        collect_run()
        return

    if args.command in ("share-trajectories", "open-source-trajectories"):
        codex_dir = args.codex_dir if hasattr(args, 'codex_dir') else None
        all_results = collect_shareable_trajectories(
            claude_dir=str(claude_dir),
            codex_dir=codex_dir,
        )
        selected_scope = args.scope
        if selected_scope is None:
            if args.command == "open-source-trajectories" or args.yes or args.json:
                selected_scope = "open-source"
            else:
                selected_scope = _prompt_share_scope()
                if selected_scope is None:
                    print("Sharing cancelled.")
                    return
        results = _share_scope_results(all_results, selected_scope)
        if args.json:
            print(json.dumps(results, indent=2))
            return
        n = sum(len(r['trajectories']) for r in results)
        print(f"🔍 Found {n} trajectories in {_share_scope_label(selected_scope)}")
        if not results:
            print("No trajectories available for the selected scope.")
            return
        print()
        for r in results:
            print(f"{r['local_folder']}  {_share_target_label(r)}")
            for f in r['edited_files']:
                print(f"  {f}")
        print()
        import tempfile, zipfile, urllib.request
        _rc, git_email = _git(['config', '--global', 'user.email'], os.getcwd())
        git_email = git_email.strip()

        if args.yes:
            answer = 'y'
        else:
            answer = input(
                "Do you agree to share all of these trajectories with the KTH experiment on coding agents? [y/N] "
            ).strip().lower()
        if answer == 'y':
            approved = results
        else:
            approved = []
            for r in results:
                a = input(
                    f"  Share {_share_target_label(r)} ({len(r['trajectories'])} trajectories)? [y/N] "
                ).strip().lower()
                if a == 'y':
                    approved.append(r)

        if not approved:
            print("Sharing cancelled.")
            return

        all_trajs = [t for r in approved for t in r['trajectories']]
        github_urls = sorted({r['github_repo'] for r in approved})
        metadata = {
            'git_email': git_email,
            'share_scope': selected_scope,
            'github_repos': github_urls,
            'trajectories': all_trajs,
        }
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp:
            zip_path = tmp.name
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('metadata.json', json.dumps(metadata, indent=2))
            for index, r in enumerate(approved, start=1):
                repo_slug = _share_target_slug(r, index)
                for t in r['trajectories']:
                    zf.write(t, f"{repo_slug}/{Path(t).name}")
        print(f"Uploading {len(all_trajs)} trajectories ...")
        zip_data = Path(zip_path).read_bytes()
        req = urllib.request.Request(
            'https://www.monperrus.net/martin/transfer-sh.py/trajectories',
            data=zip_data,
            method='PUT',
            headers={
                'User-Agent': 'reproducible-trajectories',
                'Content-Length': str(len(zip_data)),
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            url = resp.read().decode().strip()
        print(f"Shared: {url}")
        print()
        print("🙏 Thank you so much for contributing to the KTH experiment on coding agents!")
        print("   Your trajectories will help advance research on AI-assisted software engineering.")
        print()
        hooks_added = 0
        hook_answer = input("Do you agree to install a collection webhook in each repo to automatically share future trajectories? [y/N] ").strip().lower()
        if hook_answer == 'y':
            hooks_added = _install_collection_webhook([r['local_folder'] for r in approved])
        print()
        print("── Summary ──────────────────────────────")
        print(f"  Trajectories uploaded : {len(all_trajs)}")
        print(f"  Hooks added           : {hooks_added}")
        print("─────────────────────────────────────────")
        return

    if args.command in ("find-reproducible-trajectories-claude",
                        "find-reproducible-trajectories-copilot"):
        if args.command == "find-reproducible-trajectories-claude":
            results = find_reproducible_trajectories_claude(
                claude_dir=str(claude_dir),
                window_hours=args.window_hours,
            )
            traj_lookup = lambda r: list(claude_dir.glob(f"projects/**/{r['trajectory']}"))
        else:
            copilot_dir = Path(args.copilot_dir or Path.home() / '.copilot')
            results = find_reproducible_trajectories_copilot(
                copilot_dir=str(copilot_dir),
                window_hours=args.window_hours,
            )
            traj_lookup = lambda r: [copilot_dir / 'session-state' / r['trajectory'] / 'events.jsonl']

        if args.output:
            Path(args.output).write_text(json.dumps(results, indent=2))
            print(f"Wrote {len(results)} entries to {args.output}")
            return
        if args.json:
            print(json.dumps(results, indent=2))
            return
        if not results:
            print("No reproducible trajectories found.")
            return
        for r in results:
            print(f"{r['trajectory']:<45} {r['commit_before']} -> {r['commit_after']}  [{r['strategy']}]")
            for f in r['files']:
                print(f"  {f}")
        print()
        if args.yes:
            answer = 'y'
        else:
            answer = input(
                f"Submit these {len(results)} reproducible trajectories to the KTH server? [y/N] "
            ).strip().lower()
        if answer != 'y':
            print("Submission cancelled.")
            return
        import tempfile, zipfile, urllib.request
        _rc, git_email = _git(['config', '--global', 'user.email'], os.getcwd())
        git_email = git_email.strip()
        metadata = {
            'git_email': git_email,
            'trajectories': results,
        }
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp:
            zip_path = tmp.name
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('metadata.json', json.dumps(metadata, indent=2))
            for r in results:
                traj_files = traj_lookup(r)
                if traj_files and Path(traj_files[0]).exists():
                    zf.write(str(traj_files[0]), r['trajectory'])
        print(f"Uploading {len(results)} trajectories ...")
        zip_data = Path(zip_path).read_bytes()
        req = urllib.request.Request(
            'https://www.monperrus.net/martin/transfer-sh.py/trajectories',
            data=zip_data,
            method='PUT',
            headers={
                'User-Agent': 'reproducible-trajectories',
                'Content-Length': str(len(zip_data)),
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            url = resp.read().decode().strip()
        print(f"Submitted: {url}")
        print("Thank you for contributing to the KTH experiment on coding agents!")
        return

    if args.command == "verify-trajectories":
        results = verify_trajectories(args.repo, claude_dir=str(claude_dir))
        if args.json:
            print(json.dumps(results, indent=2))
            return
        if not results:
            print("No trajectory-tagged commits found.")
            return
        for r in results:
            print(f"{r['commit']}  {r['status']:<22}  {r['trajectory']}  {r['short_message']}")
        return

    if args.command == "check-execution-reproducible":
        traj_path = resolve_trajectory_path(args.trajectory, str(claude_dir))
        result = check_execution_reproducible(
            traj_path,
            repo_path=args.repo,
            exec_cwd=args.cwd,
        )
        if args.json:
            print(json.dumps(result, indent=2))
            return
        ec = result["edit_criterion"]
        cc = result["command_criterion"]
        print(f"Edit criterion:    {ec['status']}")
        for f in ec.get("files", []):
            mark = "PASS" if f["status"] == "match" else f["status"].upper()
            print(f"  [{mark}] {f['file']}")
        print(f"Command criterion: {cc['status']}")
        for c in cc.get("commands", []):
            mark = "PASS" if c["match"] else "FAIL"
            cmd_preview = c["command"][:60] + ("…" if len(c["command"]) > 60 else "")
            print(f"  [{mark}] {cmd_preview}")
            if not c["match"]:
                print(f"    expected: {c['expected']!r}")
                print(f"    actual:   {c['actual']!r}")
        er = result["execution_reproducible"]
        verdict = "True" if er else ("False" if er is False else "unknown (no criteria applicable)")
        print(f"Execution reproducible: {verdict}")
        return

    if args.command == "add-trajectories-to-repo":
        results = add_trajectories_to_repo(
            args.repo,
            claude_dir=str(claude_dir),
            dry_run=args.dry_run,
        )
        if args.json:
            print(json.dumps(results, indent=2))
            return
        if not results:
            print("No trajectory-tagged commits found.")
            return
        for r in results:
            dest = r.get('dest', '')
            print(f"{r['trajectory'][:36]:<38}  {r['status']:<16}  {dest}")
        return

    if args.command == "replay-trajectories":
        results = replay_trajectories(args.repo)
        if not results:
            print("No write events were replayed.")
            return
        for r in results:
            print(f"{r['commit'][:12]}  {r['timestamp']}  {r['agent']:<12}  {r['file']}")
        return

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

    elif args.command == "filter-trajectories":
        filtered = filter_trajectory(
            trajectory_path,
            exclude_paths=args.paths or None,
            cwd=args.cwd,
        )
        out = open(args.output, "w") if args.output else sys.stdout
        try:
            for event in filtered:
                out.write(json.dumps(event) + "\n")
        finally:
            if args.output:
                out.close()


if __name__ == "__main__":
    main()
