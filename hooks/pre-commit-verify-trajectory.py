#!/usr/bin/env python3
"""
commit-msg hook: ensures the commit message contains a valid trajectory identifier.

Valid formats:
  trajectory: {uuid}         (space after colon optional)
  trajectory: {filepath}
  <trajectory>{uuid}</trajectory>

A UUID is valid if a corresponding JSONL file exists in ~/.claude/projects/.
A filepath is valid if the file exists on disk.

Install as a git hook:
  ln -s ../../hooks/pre-commit .git/hooks/commit-msg
"""

import re
import sys
from pathlib import Path

UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)
TRAJECTORY_LABEL_RE = re.compile(r"trajectory\s*:\s*(\S+)", re.I | re.MULTILINE)
TRAJECTORY_TAG_RE = re.compile(r"<trajectory>([^<]+)</trajectory>", re.I)


def uuid_exists(uuid, claude_dir):
    return bool(list(Path(claude_dir).glob(f"projects/**/{uuid}.jsonl")))


def validate_identifier(identifier, claude_dir):
    if UUID_RE.fullmatch(identifier):
        return uuid_exists(identifier, claude_dir)
    return Path(identifier).exists()


def check_commit_message(msg, claude_dir):
    if re.search(r"\bno trajectory\b", msg, re.I):
        return True, None

    candidates = []
    for m in TRAJECTORY_LABEL_RE.finditer(msg):
        candidates.append(m.group(1))
    for m in TRAJECTORY_TAG_RE.finditer(msg):
        value = m.group(1).strip()
        if UUID_RE.fullmatch(value):
            candidates.append(value)

    if not candidates:
        return False, "no trajectory identifier found in commit message"

    for c in candidates:
        if validate_identifier(c, claude_dir):
            return True, None

    return False, f"none of the trajectory identifiers could be validated: {candidates}"


def main():
    if len(sys.argv) < 2:
        print("usage: pre-commit <commit-msg-file>", file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1]) as f:
        raw = f.read()

    # strip comment lines
    msg = "\n".join(l for l in raw.splitlines() if not l.startswith("#"))

    claude_dir = Path.home() / ".claude"
    ok, error = check_commit_message(msg, claude_dir)

    if not ok:
        print(f"commit rejected: {error}", file=sys.stderr)
        print("commit message must include a trajectory identifier, e.g.:", file=sys.stderr)
        print("  trajectory: <uuid>", file=sys.stderr)
        print("  trajectory: <path/to/trajectory.jsonl>", file=sys.stderr)
        print("  <trajectory><uuid></trajectory>", file=sys.stderr)
        print("or include 'no trajectory' to skip this check", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
