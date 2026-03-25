"""
Pi coding agent trajectory support.

Detects and normalizes pi trajectories into the Claude Code format
used by the rest of the codebase.
"""

import os
from pathlib import Path


def is_pi_trajectory(events):
    """Detect pi coding agent trajectory format."""
    for e in events:
        if e.get("type") == "session" and "cwd" in e:
            return True
        if e.get("type") == "message" and e.get("message", {}).get("role") == "toolResult":
            return True
        if e.get("type") == "message":
            for c in e.get("message", {}).get("content", []):
                if isinstance(c, dict) and c.get("type") == "toolCall":
                    return True
    return False


# Map pi tool names/args to Claude Code equivalents.
_TOOL_NAMES = {"read": "Read", "write": "Write", "edit": "Edit", "bash": "Bash"}
_ARG_MAP = {
    "read":  {"path": "file_path"},
    "write": {"path": "file_path", "content": "content"},
    "edit":  {"path": "file_path", "oldText": "old_string", "newText": "new_string",
              "replace_all": "replace_all"},
}


def _normalize_tool_call(item):
    """Convert a pi toolCall content item to Claude Code tool_use format."""
    name = item.get("name", "")
    cc_name = _TOOL_NAMES.get(name, name.capitalize())
    args = item.get("arguments", {})
    arg_map = _ARG_MAP.get(name, {})
    cc_input = {}
    for pi_key, val in args.items():
        cc_key = arg_map.get(pi_key, pi_key)
        cc_input[cc_key] = val
    return {
        "type": "tool_use",
        "id": item.get("id", ""),
        "name": cc_name,
        "input": cc_input,
    }


def normalize_trajectory(events):
    """Convert pi trajectory events to Claude Code format."""
    normalized = []

    # Extract session info and emit it as a Claude-style event.
    session_id = None
    cwd = None
    for e in events:
        if e.get("type") == "session":
            session_id = e.get("id")
            cwd = e.get("cwd")
            break

    if session_id or cwd:
        normalized.append({"sessionId": session_id, "cwd": cwd})

    for e in events:
        if e.get("type") != "message":
            continue
        msg = e.get("message", {})
        role = msg.get("role")

        if role == "assistant":
            new_content = []
            for c in msg.get("content", []):
                if isinstance(c, dict) and c.get("type") == "toolCall":
                    new_content.append(_normalize_tool_call(c))
                elif isinstance(c, dict) and c.get("type") == "thinking":
                    continue
                else:
                    new_content.append(c)
            normalized.append({"message": {"role": "assistant", "content": new_content}})

        elif role == "toolResult":
            tool_call_id = msg.get("toolCallId", "")
            content = msg.get("content", "")
            normalized.append({
                "message": {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_call_id,
                        "content": content,
                    }],
                }
            })

        elif role == "user":
            normalized.append({"message": msg})

    return normalized


def resolve_session_path(trajectory_arg):
    """Search pi session directories for a trajectory by ID fragment."""
    pi_agent_dir = os.environ.get("PI_CODING_AGENT_DIR")
    if pi_agent_dir:
        pi_sessions = Path(pi_agent_dir) / "sessions"
    else:
        pi_sessions = Path.home() / ".pi" / "agent" / "sessions"
    if pi_sessions.is_dir():
        matches = [p for p in pi_sessions.rglob("*.jsonl") if trajectory_arg in p.name]
        if matches:
            return str(matches[0])
    return None
