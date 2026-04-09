import json
from pathlib import Path


def _write_jsonl(path, events):
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


class TestIsPiTrajectory:
    def test_detects_session_event(self):
        from reproducible_trajectories.pi_trajectory import is_pi_trajectory

        events = [{"type": "session", "id": "sess-1", "cwd": "/repo"}]
        assert is_pi_trajectory(events) is True

    def test_detects_tool_call_event(self):
        from reproducible_trajectories.pi_trajectory import is_pi_trajectory

        events = [
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "toolCall", "id": "tc1", "name": "read", "arguments": {"path": "a.py"}}],
                },
            }
        ]
        assert is_pi_trajectory(events) is True

    def test_returns_false_for_non_pi_events(self):
        from reproducible_trajectories.pi_trajectory import is_pi_trajectory

        events = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/repo/a.py"}}],
                },
            }
        ]
        assert is_pi_trajectory(events) is False


class TestNormalizeTrajectory:
    def test_normalizes_pi_events_to_claude_format(self):
        from reproducible_trajectories.pi_trajectory import normalize_trajectory

        events = [
            {"type": "session", "id": "sess-1", "cwd": "/repo"},
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "text": "skip me"},
                        {"type": "text", "text": "I will read the file."},
                        {"type": "toolCall", "id": "call-1", "name": "read", "arguments": {"path": "/repo/a.py"}},
                        {
                            "type": "toolCall",
                            "id": "call-2",
                            "name": "edit",
                            "arguments": {
                                "path": "/repo/a.py",
                                "oldText": "old",
                                "newText": "new",
                                "replace_all": True,
                            },
                        },
                    ],
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "call-1",
                    "content": [{"type": "text", "text": "file contents"}],
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "user",
                    "content": "please continue",
                },
            },
        ]

        normalized = normalize_trajectory(events)

        assert normalized[0] == {"sessionId": "sess-1", "cwd": "/repo"}
        assert normalized[1]["message"]["role"] == "assistant"
        assert normalized[1]["message"]["content"][0] == {"type": "text", "text": "I will read the file."}
        assert normalized[1]["message"]["content"][1] == {
            "type": "tool_use",
            "id": "call-1",
            "name": "Read",
            "input": {"file_path": "/repo/a.py"},
        }
        assert normalized[1]["message"]["content"][2] == {
            "type": "tool_use",
            "id": "call-2",
            "name": "Edit",
            "input": {
                "file_path": "/repo/a.py",
                "old_string": "old",
                "new_string": "new",
                "replace_all": True,
            },
        }
        assert normalized[2] == {
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "content": [{"type": "text", "text": "file contents"}],
                    }
                ],
            }
        }
        assert normalized[3] == {
            "message": {"role": "user", "content": "please continue"}
        }

    def test_parse_trajectory_normalizes_pi_file(self, tmp_path):
        from reproducible_trajectories.trajectory import parse_trajectory

        traj = tmp_path / "pi-session.jsonl"
        events = [
            {"type": "session", "id": "sess-1", "cwd": "/repo"},
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "toolCall", "id": "call-1", "name": "write", "arguments": {"path": "/repo/out.py", "content": "x = 1\n"}}
                    ],
                },
            },
            {
                "type": "message",
                "message": {"role": "toolResult", "toolCallId": "call-1", "content": "OK"},
            },
        ]
        _write_jsonl(traj, events)

        parsed = parse_trajectory(str(traj))

        assert parsed[0] == {"sessionId": "sess-1", "cwd": "/repo"}
        assert parsed[1]["message"]["content"][0] == {
            "type": "tool_use",
            "id": "call-1",
            "name": "Write",
            "input": {"file_path": "/repo/out.py", "content": "x = 1\n"},
        }
        assert parsed[2]["message"]["content"][0] == {
            "type": "tool_result",
            "tool_use_id": "call-1",
            "content": "OK",
        }


class TestResolvePiSessionPath:
    def test_resolves_from_pi_coding_agent_dir(self, tmp_path, monkeypatch):
        from reproducible_trajectories.pi_trajectory import resolve_session_path

        pi_dir = tmp_path / ".pi-agent"
        sessions = pi_dir / "sessions" / "project"
        sessions.mkdir(parents=True)
        traj = sessions / "abc123-session.jsonl"
        traj.write_text("{}\n")

        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_dir))

        assert resolve_session_path("abc123") == str(traj)

    def test_resolve_trajectory_path_uses_pi_session_lookup(self, tmp_path, monkeypatch):
        from reproducible_trajectories.trajectory import resolve_trajectory_path

        pi_dir = tmp_path / ".pi-agent"
        sessions = pi_dir / "sessions" / "project"
        sessions.mkdir(parents=True)
        traj = sessions / "pi-session-456.jsonl"
        traj.write_text("{}\n")

        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_dir))

        resolved = resolve_trajectory_path("pi-session-456", str(tmp_path / "claude"))
        assert resolved == str(traj)
