#!/usr/bin/env python3
"""
commit-msg hook: ensures the commit message contains a valid trajectory identifier.

Install as a git hook:
  ln -s ../../hooks/pre-commit-verify-trajectory.py .git/hooks/commit-msg
"""

import subprocess
import sys

def _pip_supports_break_system_packages():
    try:
        from pip import __version__ as v
        major, minor = int(v.split(".")[0]), int(v.split(".")[1])
        return (major, minor) >= (23, 1)
    except Exception:
        return False

_pip_cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet"]
if _pip_supports_break_system_packages():
    _pip_cmd.append("--break-system-packages")
_pip_cmd.append("reproducible-trajectories")

subprocess.check_call(_pip_cmd)

from reproducible_trajectories.trajectory import pre_commit_verify_trajectory

pre_commit_verify_trajectory()
