#!/usr/bin/env python3
"""
pre-commit hook: finds the most recent Claude Code trajectory that edited the
same files (or a subset) as the current staged changes, checks reproducibility,
and POSTs the result to https://api.monperrus.com/trajectories.

Install as a git hook:
  ln -s ../../hooks/pre-commit-collect-trajectories.py .git/hooks/pre-commit
"""

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

try:
    from reproducible_trajectories.trajectory import (
        build_sequence,
        collect_modifications,
        get_session_info,
        parse_trajectory,
        _rel,
        _simulate_ops,
    )
except ImportError:
    _HOOKS_DIR = Path(__file__).resolve().parent
    sys.path.insert(0, str(_HOOKS_DIR.parent))
    from reproducible_trajectories.trajectory import (
        build_sequence,
        collect_modifications,
        get_session_info,
        parse_trajectory,
        _rel,
        _simulate_ops,
    )

API_URL = "https://api.monperrus.com/trajectories"
CLAUDE_DIR = Path.home() / ".claude"
N_RECENT = 10


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git(args):
    r = subprocess.run(["git"] + args, capture_output=True)
    return r.returncode, r.stdout.decode("utf-8", errors="replace")


def _repo_root():
    rc, out = _git(["rev-parse", "--show-toplevel"])
    return Path(out.strip()) if rc == 0 else Path.cwd()


def _git_context():
    """Return dict with remote URL, branch, latest commit SHA, and user email."""
    _, remote = _git(["remote", "get-url", "origin"])
    _, branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    _, commit = _git(["rev-parse", "HEAD"])
    _, email = _git(["config", "user.email"])
    return {
        "remote": remote.strip() or None,
        "branch": branch.strip() or None,
        "commit": commit.strip() or None,
        "email": email.strip() or None,
    }


def _staged_files():
    """Return set of repo-relative paths for staged files (added/copied/modified)."""
    rc, out = _git(["diff", "--cached", "--name-only", "--diff-filter=ACM"])
    if rc != 0:
        return set()
    return {line for line in out.splitlines() if line}


def _read_index(rel_path):
    """Return content of rel_path from the git index, or None."""
    rc, out = _git(["show", f":{rel_path}"])
    return out if rc == 0 else None


def _read_head(rel_path):
    """Return content of rel_path at HEAD, or None."""
    rc, out = _git(["show", f"HEAD:{rel_path}"])
    return out if rc == 0 else None


# ---------------------------------------------------------------------------
# Trajectory helpers
# ---------------------------------------------------------------------------

def _recent_trajectories(claude_dir=CLAUDE_DIR, n=N_RECENT):
    """Return the n most recent trajectory JSONL paths, newest first."""
    traj_files = list(Path(claude_dir).glob("projects/**/*.jsonl"))
    traj_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return traj_files[:n]


def _modified_rel_paths(traj_path):
    """
    Return the set of repo-relative paths that the trajectory modified,
    relative to its session cwd.  Returns empty set if cwd is unknown.
    """
    events = parse_trajectory(str(traj_path))
    _, session_cwd = get_session_info(events)
    if not session_cwd:
        return set()
    sequence, _ = build_sequence(events)
    modifications = collect_modifications(sequence)
    result = set()
    for fp in modifications:
        rel = _rel(fp, session_cwd)
        if rel:
            result.add(rel)
    return result


def _find_matching_trajectory(staged_files):
    """
    Among the 10 most recent trajectories, return the first whose set of
    modified files is a non-empty subset of staged_files.
    Returns (traj_path, events) or (None, None).
    """
    for traj_path in _recent_trajectories():
        modified = _modified_rel_paths(traj_path)
        if modified and modified.issubset(staged_files):
            events = parse_trajectory(str(traj_path))
            return traj_path, events
    return None, None


# ---------------------------------------------------------------------------
# Reproducibility check
# ---------------------------------------------------------------------------

def _check_reproducibility(events, repo_root):
    """
    Simulate Write/Edit ops from events against HEAD and compare with the index.

    Returns (status, file_results).
    status: 'reproducible' | 'not_reproducible' | 'no_operations'
    """
    sequence, _ = build_sequence(events)
    _, session_cwd = get_session_info(events)

    ops = []
    for s in sequence:
        if s["seq_type"] != "tool_use":
            continue
        item = s["item"]
        name = item.get("name")
        if name in ("Write", "Edit", "NotebookEdit"):
            ops.append({"tool": name, "input": item.get("input", {})})

    if not any(op["tool"] in ("Write", "Edit") for op in ops):
        return "no_operations", []

    # Classify files as notebook-only vs write/edit
    notebook_only = set()
    write_edit_files = set()
    for op in ops:
        tool = op["tool"]
        inp = op["input"]
        fp = inp.get("notebook_path" if tool == "NotebookEdit" else "file_path", "")
        if not fp:
            continue
        if tool == "NotebookEdit":
            if fp not in write_edit_files:
                notebook_only.add(fp)
        else:
            write_edit_files.add(fp)
            notebook_only.discard(fp)

    # Build initial states from HEAD
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
        initial_states[fp] = _read_head(rel) if rel else None

    simulated = _simulate_ops(ops, initial_states)

    file_results = []
    any_mismatch = False

    for fp, sim_content in simulated.items():
        if fp in notebook_only:
            file_results.append({"file": fp, "status": "unverifiable"})
            continue

        rel = file_rels.get(fp)

        if session_cwd is not None and rel is None:
            file_results.append({"file": fp, "status": "outside_repo"})
            continue

        if rel is None:
            file_results.append({"file": fp, "status": "unverifiable"})
            continue

        try:
            Path(fp).resolve().relative_to(repo_root)
        except ValueError:
            file_results.append({"file": rel, "status": "outside_repo"})
            continue

        actual = _read_index(rel)
        if sim_content == actual:
            file_results.append({"file": rel, "status": "match"})
        else:
            file_results.append({"file": rel, "status": "mismatch"})
            any_mismatch = True

    status = "not_reproducible" if any_mismatch else "reproducible"
    return status, file_results


# ---------------------------------------------------------------------------
# HTTP submission
# ---------------------------------------------------------------------------

def _post_trajectory(traj_path, events, repro_status, file_results, git_context):
    """POST trajectory + reproducibility metadata to API_URL."""
    traj_id = traj_path.stem  # UUID from filename

    payload = {
        "trajectory_id": traj_id,
        "trajectory": events,
        "reproducibility": {
            "status": repro_status,
            "files": file_results,
        },
        "git": git_context,
    }

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return None, str(exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    staged = _staged_files()
    if not staged:
        sys.exit(0)

    traj_path, events = _find_matching_trajectory(staged)
    if traj_path is None:
        print(
            "pre-commit-collect-trajectories: no matching trajectory found for staged files",
            file=sys.stderr,
        )
        sys.exit(0)

    repo_root = _repo_root()
    repro_status, file_results = _check_reproducibility(events, repo_root)
    git_context = _git_context()

    http_status, response = _post_trajectory(traj_path, events, repro_status, file_results, git_context)

    if http_status is None:
        print(
            f"pre-commit-collect-trajectories: failed to POST trajectory {traj_path.name}: {response}",
            file=sys.stderr,
        )
    else:
        print(
            f"pre-commit-collect-trajectories: submitted {traj_path.name} "
            f"(reproducibility={repro_status}, HTTP {http_status})",
            file=sys.stderr,
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
