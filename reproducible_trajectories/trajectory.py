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


def open_source_trajectories(claude_dir=None, codex_dir=None):
    """
    Scan all trajectories in ~/.claude/projects/ and ~/.codex/sessions/ and
    return those where:
      1. Every edit is within a single git repository.
      2. That git repository has at least one public GitHub remote.

    Returns list of dicts, one per unique (local_folder, github_repo) pair:
      {local_folder, github_repo, trajectories: [...], edited_files: [...]}
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
        """Shared logic: group trajectory by public-GitHub repo."""
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

        # Find a public GitHub remote for this repo
        github_urls = _get_github_remote_urls(repo_root)
        public_repo_url = None
        for url in sorted(github_urls):
            owner_repo = _parse_github_owner_repo(url)
            if owner_repo is None:
                continue
            if owner_repo not in _public_cache:
                _public_cache[owner_repo] = _is_public_github_repo(owner_repo)
            if _public_cache[owner_repo]:
                public_repo_url = f'https://github.com/{owner_repo}'
                break

        if public_repo_url is None:
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


# ---------------------------------------------------------------------------
# collection webhook
# ---------------------------------------------------------------------------

_COLLECTION_HOOK_MARKER = '# reproducible-trajectories collection hook'


def _install_collection_webhook(repo_paths):
    """Add/update a pre-commit git hook in each repo that shares trajectories."""
    hook_body = 'pre-commit-collect-trajectories\n'
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

    p_oss = subparsers.add_parser(
        "open-source-trajectories",
        help="List trajectories whose edits are all within a public GitHub repo",
    )
    p_oss.add_argument(
        "--claude-dir",
        default=None,
        help="Path to .claude directory (default: ~/.claude)",
    )
    p_oss.add_argument("--json", action="store_true", help="Output results as JSON")
    p_oss.add_argument("--yes", action="store_true", help="Auto-confirm sharing all repos (non-interactive)")
    p_oss.add_argument(
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

    subparsers.add_parser(
        "collect-trajectories",
        help="Find the most recent trajectory matching staged files, check reproducibility, and POST to API",
    )

    args = parser.parse_args(argv)
    claude_dir = Path(args.claude_dir or Path.home() / ".claude") if hasattr(args, "claude_dir") else Path.home() / ".claude"

    if args.command == "collect-trajectories":
        from reproducible_trajectories.hooks.collect_trajectories import run as collect_run
        collect_run()
        return

    if args.command == "open-source-trajectories":
        codex_dir = args.codex_dir if hasattr(args, 'codex_dir') else None
        results = open_source_trajectories(claude_dir=str(claude_dir), codex_dir=codex_dir)
        if args.json:
            print(json.dumps(results, indent=2))
            return
        n = sum(len(r['trajectories']) for r in results)
        print(f"🔍 Found {n} trajectories in open-source repos")
        if not results:
            return
        print()
        for r in results:
            print(f"{r['local_folder']}  {r['github_repo']}")
            for f in r['edited_files']:
                print(f"  {f}")
        print()
        import tempfile, zipfile, urllib.request
        _rc, git_email = _git(['config', '--global', 'user.email'], os.getcwd())
        git_email = git_email.strip()

        if args.yes:
            answer = 'y'
        else:
            answer = input("Do you agree to share them all with the KTH experiment on coding agents? [y/N] ").strip().lower()
        if answer == 'y':
            approved = results
        else:
            approved = []
            for r in results:
                a = input(f"  Share {r['github_repo']} ({len(r['trajectories'])} trajectories)? [y/N] ").strip().lower()
                if a == 'y':
                    approved.append(r)

        if not approved:
            print("Sharing cancelled.")
            return

        all_trajs = [t for r in approved for t in r['trajectories']]
        github_urls = sorted({r['github_repo'] for r in approved})
        metadata = {
            'git_email': git_email,
            'github_repos': github_urls,
            'trajectories': all_trajs,
        }
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp:
            zip_path = tmp.name
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('metadata.json', json.dumps(metadata, indent=2))
            for r in approved:
                repo_slug = r['github_repo'].replace('https://github.com/', '').replace('/', '_')
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
