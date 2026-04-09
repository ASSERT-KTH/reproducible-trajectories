"""Comprehensive tests for reproducible_trajectories.trajectory."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"
SIMPLE_TRAJ = str(FIXTURES / "simple_trajectory.jsonl")


def _make_event(uuid, role, content, session_id="sess", cwd="/repo", **extra):
    """Build a minimal trajectory event dict."""
    base = {
        "parentUuid": None,
        "isSidechain": False,
        "type": role,
        "message": {"role": role, "content": content},
        "uuid": uuid,
        "timestamp": "2026-01-01T00:00:00.000Z",
        "sessionId": session_id,
        "cwd": cwd,
    }
    base.update(extra)
    return base


def _make_tool_use_event(uuid, tool_id, name, inp, session_id="sess", cwd="/repo"):
    return _make_event(
        uuid,
        "assistant",
        [{"type": "tool_use", "id": tool_id, "name": name, "input": inp}],
        session_id=session_id,
        cwd=cwd,
    )


def _make_tool_result_event(uuid, tool_use_id, text, session_id="sess", cwd="/repo"):
    return _make_event(
        uuid,
        "user",
        [{"type": "tool_result", "tool_use_id": tool_use_id,
          "content": [{"type": "text", "text": text}]}],
        session_id=session_id,
        cwd=cwd,
    )


def _write_jsonl(path, events):
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


# ---------------------------------------------------------------------------
# parse_trajectory
# ---------------------------------------------------------------------------

class TestParseTrajectory:
    def test_reads_all_lines(self):
        from reproducible_trajectories.trajectory import parse_trajectory
        events = parse_trajectory(SIMPLE_TRAJ)
        assert len(events) == 12

    def test_returns_list_of_dicts(self):
        from reproducible_trajectories.trajectory import parse_trajectory
        events = parse_trajectory(SIMPLE_TRAJ)
        for e in events:
            assert isinstance(e, dict)

    def test_skips_blank_lines(self, tmp_path):
        from reproducible_trajectories.trajectory import parse_trajectory
        p = tmp_path / "blank.jsonl"
        p.write_text('{"a": 1}\n\n{"b": 2}\n\n')
        events = parse_trajectory(str(p))
        assert len(events) == 2

    def test_empty_file(self, tmp_path):
        from reproducible_trajectories.trajectory import parse_trajectory
        p = tmp_path / "empty.jsonl"
        p.write_text("")
        assert parse_trajectory(str(p)) == []


# ---------------------------------------------------------------------------
# build_sequence
# ---------------------------------------------------------------------------

class TestBuildSequence:
    def test_returns_tuple(self):
        from reproducible_trajectories.trajectory import parse_trajectory, build_sequence
        events = parse_trajectory(SIMPLE_TRAJ)
        seq, uses = build_sequence(events)
        assert isinstance(seq, list)
        assert isinstance(uses, dict)

    def test_tool_uses_in_map(self):
        from reproducible_trajectories.trajectory import parse_trajectory, build_sequence
        events = parse_trajectory(SIMPLE_TRAJ)
        _seq, uses = build_sequence(events)
        # SIMPLE_TRAJ has several named tool calls
        assert "toolu_read_001" in uses
        assert "toolu_edit_001" in uses
        assert "toolu_write_001" in uses

    def test_sequence_types(self):
        from reproducible_trajectories.trajectory import parse_trajectory, build_sequence
        events = parse_trajectory(SIMPLE_TRAJ)
        seq, _uses = build_sequence(events)
        for item in seq:
            assert item["seq_type"] in ("tool_use", "tool_result")

    def test_empty_events(self):
        from reproducible_trajectories.trajectory import build_sequence
        seq, uses = build_sequence([])
        assert seq == []
        assert uses == {}

    def test_event_without_message(self):
        from reproducible_trajectories.trajectory import build_sequence
        events = [{"type": "file-history-snapshot"}]
        seq, uses = build_sequence(events)
        assert seq == []


# ---------------------------------------------------------------------------
# get_session_info
# ---------------------------------------------------------------------------

class TestGetSessionInfo:
    def test_returns_session_and_cwd(self):
        from reproducible_trajectories.trajectory import parse_trajectory, get_session_info
        events = parse_trajectory(SIMPLE_TRAJ)
        sid, cwd = get_session_info(events)
        assert sid == "test-session-1111-1111-1111-111111111111"
        assert cwd == "/repo"

    def test_returns_none_when_missing(self):
        from reproducible_trajectories.trajectory import get_session_info
        events = [{"type": "user", "message": {"role": "user", "content": "hi"}}]
        sid, cwd = get_session_info(events)
        assert sid is None
        assert cwd is None

    def test_picks_first_matching_event(self):
        from reproducible_trajectories.trajectory import get_session_info
        events = [
            {"sessionId": "s1", "cwd": "/a"},
            {"sessionId": "s2", "cwd": "/b"},
        ]
        sid, cwd = get_session_info(events)
        assert sid == "s1"
        assert cwd == "/a"


# ---------------------------------------------------------------------------
# collect_modifications
# ---------------------------------------------------------------------------

class TestCollectModifications:
    def test_collects_write_edit(self):
        from reproducible_trajectories.trajectory import parse_trajectory, build_sequence, collect_modifications
        events = parse_trajectory(SIMPLE_TRAJ)
        seq, _ = build_sequence(events)
        mods = collect_modifications(seq)
        assert "/repo/existing.py" in mods
        assert "/repo/new_file.py" in mods

    def test_collects_notebookedit(self):
        from reproducible_trajectories.trajectory import parse_trajectory, build_sequence, collect_modifications
        events = parse_trajectory(SIMPLE_TRAJ)
        seq, _ = build_sequence(events)
        mods = collect_modifications(seq)
        assert "/repo/notebook.ipynb" in mods

    def test_first_modification_wins(self, tmp_path):
        from reproducible_trajectories.trajectory import build_sequence, collect_modifications
        events = [
            _make_tool_use_event("u1", "t1", "Write", {"file_path": "/f.py", "content": "v1"}),
            _make_tool_use_event("u2", "t2", "Write", {"file_path": "/f.py", "content": "v2"}),
        ]
        seq, _ = build_sequence(events)
        mods = collect_modifications(seq)
        assert mods["/f.py"]["input"]["content"] == "v1"

    def test_ignores_non_write_tools(self, tmp_path):
        from reproducible_trajectories.trajectory import build_sequence, collect_modifications
        events = [
            _make_tool_use_event("u1", "t1", "Read", {"file_path": "/f.py"}),
            _make_tool_use_event("u2", "t2", "Bash", {"command": "ls"}),
        ]
        seq, _ = build_sequence(events)
        mods = collect_modifications(seq)
        assert mods == {}


# ---------------------------------------------------------------------------
# load_file_history
# ---------------------------------------------------------------------------

class TestLoadFileHistory:
    def test_returns_pre_existing(self, tmp_path):
        from reproducible_trajectories.trajectory import load_file_history
        events = [
            {
                "type": "file-history-snapshot",
                "messageId": "m1",
                "snapshot": {
                    "trackedFileBackups": {
                        "a.py": {"version": 1, "backupFileName": "backup_a.py"},
                        "b.py": {"version": 1, "backupFileName": None},
                    }
                },
            }
        ]
        pre = load_file_history("some-session", events, str(tmp_path))
        assert "a.py" in pre
        assert "b.py" not in pre

    def test_returns_empty_without_snapshot(self, tmp_path):
        from reproducible_trajectories.trajectory import load_file_history
        events = [{"type": "user", "message": {}}]
        pre = load_file_history("s", events, str(tmp_path))
        assert pre == set()

    def test_returns_empty_without_session_id(self, tmp_path):
        from reproducible_trajectories.trajectory import load_file_history
        pre = load_file_history(None, [], str(tmp_path))
        assert pre == set()


# ---------------------------------------------------------------------------
# determine_containment
# ---------------------------------------------------------------------------

class TestDetermineContainment:
    def _mod(self, seq_idx, tool, inp):
        return {"seq_idx": seq_idx, "tool": tool, "input": inp}

    def test_fully_with_prior_read(self):
        from reproducible_trajectories.trajectory import determine_containment
        reads = {"/repo/f.py": [{"seq_idx": 0, "content": "x"}]}
        mod = self._mod(1, "Edit", {"old_string": "x"})
        assert determine_containment("/repo/f.py", mod, reads, set(), "/repo") == "fully"

    def test_partially_edit_no_prior_read(self):
        from reproducible_trajectories.trajectory import determine_containment
        mod = self._mod(0, "Edit", {"old_string": "old"})
        assert determine_containment("/repo/f.py", mod, {}, set(), "/repo") == "partially"

    def test_not_contained_edit_no_old_string(self):
        from reproducible_trajectories.trajectory import determine_containment
        mod = self._mod(0, "Edit", {})
        assert determine_containment("/repo/f.py", mod, {}, set(), "/repo") == "not contained"

    def test_new_file_write(self):
        from reproducible_trajectories.trajectory import determine_containment
        mod = self._mod(0, "Write", {"file_path": "/repo/new.py"})
        assert determine_containment("/repo/new.py", mod, {}, set(), "/repo") == "new file"

    def test_not_contained_write_existing(self):
        from reproducible_trajectories.trajectory import determine_containment
        mod = self._mod(0, "Write", {"file_path": "/repo/existing.py"})
        pre = {"existing.py"}
        assert determine_containment("/repo/existing.py", mod, {}, pre, "/repo") == "not contained"


# ---------------------------------------------------------------------------
# analyze_modified_files
# ---------------------------------------------------------------------------

class TestAnalyzeModifiedFiles:
    def test_returns_list_of_dicts(self):
        from reproducible_trajectories.trajectory import analyze_modified_files
        results = analyze_modified_files(SIMPLE_TRAJ)
        assert isinstance(results, list)
        for r in results:
            assert "file_path" in r
            assert "tool" in r
            assert "containment" in r

    def test_containment_values(self):
        from reproducible_trajectories.trajectory import analyze_modified_files
        results = analyze_modified_files(SIMPLE_TRAJ)
        valid = {"fully", "partially", "not contained", "new file"}
        for r in results:
            assert r["containment"] in valid

    def test_existing_file_with_prior_read_is_fully(self):
        from reproducible_trajectories.trajectory import analyze_modified_files
        results = analyze_modified_files(SIMPLE_TRAJ)
        by_path = {r["file_path"]: r for r in results}
        # existing.py is read before edited, so first modification = Edit after Read
        assert by_path["/repo/existing.py"]["containment"] == "fully"

    def test_new_file_is_new_file(self):
        from reproducible_trajectories.trajectory import analyze_modified_files
        results = analyze_modified_files(SIMPLE_TRAJ)
        by_path = {r["file_path"]: r for r in results}
        assert by_path["/repo/new_file.py"]["containment"] == "new file"


# ---------------------------------------------------------------------------
# extract_bash_file_reads
# ---------------------------------------------------------------------------

class TestExtractBashFileReads:
    def test_cat_single_file(self):
        from reproducible_trajectories.trajectory import extract_bash_file_reads
        results = extract_bash_file_reads("cat /some/file.py")
        assert results == [("/some/file.py", "fully")]

    def test_head(self):
        from reproducible_trajectories.trajectory import extract_bash_file_reads
        results = extract_bash_file_reads("head -n 10 /file.py")
        assert results == [("/file.py", "partially")]

    def test_tail(self):
        from reproducible_trajectories.trajectory import extract_bash_file_reads
        results = extract_bash_file_reads("tail -20 /file.py")
        assert results == [("/file.py", "partially")]

    def test_sed(self):
        from reproducible_trajectories.trajectory import extract_bash_file_reads
        results = extract_bash_file_reads("sed 's/a/b/' /file.py")
        assert results == [("/file.py", "partially")]

    def test_awk(self):
        from reproducible_trajectories.trajectory import extract_bash_file_reads
        results = extract_bash_file_reads("awk '{print}' /file.py")
        assert results == [("/file.py", "partially")]

    def test_pipe_chain(self):
        from reproducible_trajectories.trajectory import extract_bash_file_reads
        results = extract_bash_file_reads("cat /a.py | head -n 5")
        paths = [r[0] for r in results]
        assert "/a.py" in paths

    def test_non_read_command(self):
        from reproducible_trajectories.trajectory import extract_bash_file_reads
        results = extract_bash_file_reads("ls /dir")
        assert results == []

    def test_empty_command(self):
        from reproducible_trajectories.trajectory import extract_bash_file_reads
        assert extract_bash_file_reads("") == []

    def test_invalid_shell_syntax(self):
        from reproducible_trajectories.trajectory import extract_bash_file_reads
        # shlex.split raises ValueError on unclosed quotes; should return []
        assert extract_bash_file_reads("cat 'unclosed") == []

    def test_multiple_files(self):
        from reproducible_trajectories.trajectory import extract_bash_file_reads
        results = extract_bash_file_reads("cat /a.py /b.py")
        assert ("/a.py", "fully") in results
        assert ("/b.py", "fully") in results


# ---------------------------------------------------------------------------
# analyze_read_files
# ---------------------------------------------------------------------------

class TestAnalyzeReadFiles:
    def test_returns_list(self):
        from reproducible_trajectories.trajectory import analyze_read_files
        results = analyze_read_files(SIMPLE_TRAJ)
        assert isinstance(results, list)

    def test_read_tool_full(self):
        from reproducible_trajectories.trajectory import analyze_read_files
        results = analyze_read_files(SIMPLE_TRAJ)
        by_path = {r["file_path"]: r for r in results}
        # existing.py is fully read (no offset/limit on first read)
        assert by_path["/repo/existing.py"]["read_type"] == "fully"

    def test_bash_cat_full(self):
        from reproducible_trajectories.trajectory import analyze_read_files
        # SIMPLE_TRAJ has 'cat /repo/existing.py'
        results = analyze_read_files(SIMPLE_TRAJ)
        by_path = {r["file_path"]: r for r in results}
        assert "/repo/existing.py" in by_path

    def test_partial_read_does_not_override_full(self, tmp_path):
        from reproducible_trajectories.trajectory import analyze_read_files
        # Full read first, then partial — should stay "fully"
        events = [
            _make_tool_use_event("u1", "t1", "Read",
                                 {"file_path": "/f.py"}),
            _make_tool_use_event("u2", "t2", "Read",
                                 {"file_path": "/f.py", "offset": 5}),
        ]
        p = tmp_path / "t.jsonl"
        _write_jsonl(str(p), events)
        results = analyze_read_files(str(p))
        by_path = {r["file_path"]: r for r in results}
        assert by_path["/f.py"]["read_type"] == "fully"

    def test_partial_upgrades_to_full(self, tmp_path):
        from reproducible_trajectories.trajectory import analyze_read_files
        events = [
            _make_tool_use_event("u1", "t1", "Read",
                                 {"file_path": "/f.py", "limit": 10}),
            _make_tool_use_event("u2", "t2", "Read",
                                 {"file_path": "/f.py"}),
        ]
        p = tmp_path / "t.jsonl"
        _write_jsonl(str(p), events)
        results = analyze_read_files(str(p))
        by_path = {r["file_path"]: r for r in results}
        assert by_path["/f.py"]["read_type"] == "fully"


# ---------------------------------------------------------------------------
# filter_trajectory
# ---------------------------------------------------------------------------

class TestFilterTrajectory:
    def test_returns_list(self):
        from reproducible_trajectories.trajectory import filter_trajectory
        result = filter_trajectory(SIMPLE_TRAJ)
        assert isinstance(result, list)

    def test_exclude_specific_path(self, tmp_path):
        from reproducible_trajectories.trajectory import filter_trajectory
        events = [
            _make_tool_use_event("u1", "t1", "Read", {"file_path": "/repo/a.py"}),
            _make_tool_use_event("u2", "t2", "Read", {"file_path": "/repo/b.py"}),
        ]
        p = tmp_path / "t.jsonl"
        _write_jsonl(str(p), events)
        result = filter_trajectory(str(p), exclude_paths=["/repo/a.py"])
        ids = {
            item["id"]
            for e in result
            for item in (e.get("message", {}).get("content") or [])
            if isinstance(item, dict) and item.get("type") == "tool_use"
        }
        assert "t1" not in ids
        assert "t2" in ids

    def test_paired_result_removed(self, tmp_path):
        from reproducible_trajectories.trajectory import filter_trajectory
        events = [
            _make_tool_use_event("u1", "t1", "Read", {"file_path": "/repo/a.py"}),
            _make_tool_result_event("u2", "t1", "content of a.py"),
        ]
        p = tmp_path / "t.jsonl"
        _write_jsonl(str(p), events)
        result = filter_trajectory(str(p), exclude_paths=["/repo/a.py"])
        all_items = [
            item
            for e in result
            for item in (e.get("message", {}).get("content") or [])
            if isinstance(item, dict)
        ]
        tool_use_ids = {i["id"] for i in all_items if i.get("type") == "tool_use"}
        tool_result_ids = {i["tool_use_id"] for i in all_items if i.get("type") == "tool_result"}
        assert "t1" not in tool_use_ids
        assert "t1" not in tool_result_ids

    def test_exclude_outside_cwd_by_default(self, tmp_path):
        from reproducible_trajectories.trajectory import filter_trajectory
        cwd = str(tmp_path / "project")
        events = [
            _make_tool_use_event("u1", "t1", "Read",
                                 {"file_path": str(tmp_path / "project" / "inside.py")},
                                 cwd=cwd),
            _make_tool_use_event("u2", "t2", "Read",
                                 {"file_path": "/other/outside.py"},
                                 cwd=cwd),
        ]
        p = tmp_path / "t.jsonl"
        _write_jsonl(str(p), events)
        result = filter_trajectory(str(p), cwd=cwd)
        ids = {
            item["id"]
            for e in result
            for item in (e.get("message", {}).get("content") or [])
            if isinstance(item, dict) and item.get("type") == "tool_use"
        }
        assert "t1" in ids
        assert "t2" not in ids

    def test_no_exclusion_if_no_match(self, tmp_path):
        from reproducible_trajectories.trajectory import filter_trajectory
        events = [
            _make_tool_use_event("u1", "t1", "Read", {"file_path": "/repo/a.py"}),
        ]
        p = tmp_path / "t.jsonl"
        _write_jsonl(str(p), events)
        result = filter_trajectory(str(p), exclude_paths=["/repo/other.py"])
        ids = {
            item["id"]
            for e in result
            for item in (e.get("message", {}).get("content") or [])
            if isinstance(item, dict) and item.get("type") == "tool_use"
        }
        assert "t1" in ids


# ---------------------------------------------------------------------------
# check_commit_message
# ---------------------------------------------------------------------------

class TestCheckCommitMessage:
    def test_no_trajectory_keyword_accepted(self, tmp_path):
        from reproducible_trajectories.trajectory import check_commit_message
        ok, err = check_commit_message("no trajectory: just a plain fix", str(tmp_path))
        assert ok is True
        assert err is None

    def test_no_identifier_rejected(self, tmp_path):
        from reproducible_trajectories.trajectory import check_commit_message
        ok, err = check_commit_message("Fix a bug", str(tmp_path))
        assert ok is False
        assert err is not None

    def test_valid_file_path_accepted(self, tmp_path):
        from reproducible_trajectories.trajectory import check_commit_message
        traj = tmp_path / "my.jsonl"
        traj.write_text("{}\n")
        msg = f"trajectory: {traj}"
        ok, err = check_commit_message(msg, str(tmp_path))
        assert ok is True

    def test_invalid_file_path_rejected(self, tmp_path):
        from reproducible_trajectories.trajectory import check_commit_message
        msg = "trajectory: /nonexistent/path/traj.jsonl"
        ok, err = check_commit_message(msg, str(tmp_path))
        assert ok is False

    def test_uuid_tag_format_no_file(self, tmp_path):
        from reproducible_trajectories.trajectory import check_commit_message
        msg = "<trajectory>aabbccdd-0000-0000-0000-000000000000</trajectory>"
        ok, err = check_commit_message(msg, str(tmp_path))
        # UUID doesn't exist in claude_dir, so validation fails
        assert ok is False

    def test_valid_uuid_in_claude_dir(self, tmp_path):
        from reproducible_trajectories.trajectory import check_commit_message
        uuid = "aabbccdd-1234-5678-abcd-ef0123456789"
        # Create the expected file structure
        proj = tmp_path / "projects" / "myproject"
        proj.mkdir(parents=True)
        (proj / f"{uuid}.jsonl").write_text("{}\n")
        msg = f"trajectory: {uuid}"
        ok, err = check_commit_message(msg, str(tmp_path))
        assert ok is True

    def test_ignores_comment_lines(self, tmp_path):
        from reproducible_trajectories.trajectory import pre_commit_verify_trajectory
        # The hook strips # lines; "no trajectory" should pass
        commit_msg_file = tmp_path / "COMMIT_EDITMSG"
        commit_msg_file.write_text("# comment\nno trajectory\n")
        # Should not raise SystemExit(1)
        try:
            pre_commit_verify_trajectory(["hook", str(commit_msg_file)])
        except SystemExit as e:
            assert e.code != 1, "Hook should not reject 'no trajectory'"


# ---------------------------------------------------------------------------
# _simulate_ops
# ---------------------------------------------------------------------------

class TestSimulateOps:
    def test_write_creates_file(self):
        from reproducible_trajectories.trajectory import _simulate_ops
        ops = [{"tool": "Write", "input": {"file_path": "/f.py", "content": "hello\n"}}]
        result = _simulate_ops(ops, {})
        assert result["/f.py"] == "hello\n"

    def test_edit_replaces_old_string(self):
        from reproducible_trajectories.trajectory import _simulate_ops
        ops = [{"tool": "Edit", "input": {
            "file_path": "/f.py",
            "old_string": "old",
            "new_string": "new",
        }}]
        result = _simulate_ops(ops, {"/f.py": "old content old"})
        assert result["/f.py"] == "new content old"

    def test_edit_replace_all(self):
        from reproducible_trajectories.trajectory import _simulate_ops
        ops = [{"tool": "Edit", "input": {
            "file_path": "/f.py",
            "old_string": "x",
            "new_string": "y",
            "replace_all": True,
        }}]
        result = _simulate_ops(ops, {"/f.py": "x and x"})
        assert result["/f.py"] == "y and y"

    def test_edit_noop_if_file_not_in_states(self):
        from reproducible_trajectories.trajectory import _simulate_ops
        ops = [{"tool": "Edit", "input": {
            "file_path": "/f.py",
            "old_string": "old",
            "new_string": "new",
        }}]
        result = _simulate_ops(ops, {})
        assert "/f.py" not in result

    def test_edit_noop_if_state_is_none(self):
        from reproducible_trajectories.trajectory import _simulate_ops
        ops = [{"tool": "Edit", "input": {
            "file_path": "/f.py",
            "old_string": "old",
            "new_string": "new",
        }}]
        result = _simulate_ops(ops, {"/f.py": None})
        assert result["/f.py"] is None

    def test_notebookedit_skipped(self):
        from reproducible_trajectories.trajectory import _simulate_ops
        ops = [{"tool": "NotebookEdit", "input": {
            "notebook_path": "/nb.ipynb",
            "cell_id": "1",
        }}]
        result = _simulate_ops(ops, {})
        assert "/nb.ipynb" not in result

    def test_sequential_operations(self):
        from reproducible_trajectories.trajectory import _simulate_ops
        ops = [
            {"tool": "Write", "input": {"file_path": "/f.py", "content": "a"}},
            {"tool": "Edit", "input": {
                "file_path": "/f.py", "old_string": "a", "new_string": "b"
            }},
        ]
        result = _simulate_ops(ops, {})
        assert result["/f.py"] == "b"

    def test_does_not_mutate_input_states(self):
        from reproducible_trajectories.trajectory import _simulate_ops
        states = {"/f.py": "original"}
        ops = [{"tool": "Write", "input": {"file_path": "/f.py", "content": "changed"}}]
        _simulate_ops(ops, states)
        assert states["/f.py"] == "original"


# ---------------------------------------------------------------------------
# _rel
# ---------------------------------------------------------------------------

class TestRel:
    def test_relative_within_cwd(self):
        from reproducible_trajectories.trajectory import _rel
        assert _rel("/repo/src/main.py", "/repo") == "src/main.py"

    def test_outside_cwd_returns_none(self):
        from reproducible_trajectories.trajectory import _rel
        assert _rel("/other/file.py", "/repo") is None

    def test_same_as_cwd(self):
        from reproducible_trajectories.trajectory import _rel
        result = _rel("/repo", "/repo")
        assert result == "."


# ---------------------------------------------------------------------------
# _extract_trajectory_ref
# ---------------------------------------------------------------------------

class TestExtractTrajectoryRef:
    def test_trajectory_colon(self):
        from reproducible_trajectories.trajectory import _extract_trajectory_ref
        ref = _extract_trajectory_ref("trajectory: abc-123")
        assert ref == "abc-123"

    def test_trajectory_xml_tag(self):
        from reproducible_trajectories.trajectory import _extract_trajectory_ref
        ref = _extract_trajectory_ref("<trajectory>abc-uuid</trajectory>")
        assert ref == "abc-uuid"

    def test_no_ref_returns_none(self):
        from reproducible_trajectories.trajectory import _extract_trajectory_ref
        assert _extract_trajectory_ref("plain commit message") is None

    def test_case_insensitive_colon(self):
        from reproducible_trajectories.trajectory import _extract_trajectory_ref
        ref = _extract_trajectory_ref("Trajectory: my-uuid")
        assert ref == "my-uuid"


# ---------------------------------------------------------------------------
# _parse_github_owner_repo
# ---------------------------------------------------------------------------

class TestParseGithubOwnerRepo:
    def test_https_url(self):
        from reproducible_trajectories.trajectory import _parse_github_owner_repo
        assert _parse_github_owner_repo("https://github.com/owner/repo") == "owner/repo"

    def test_https_url_with_git(self):
        from reproducible_trajectories.trajectory import _parse_github_owner_repo
        assert _parse_github_owner_repo("https://github.com/owner/repo.git") == "owner/repo"

    def test_ssh_url(self):
        from reproducible_trajectories.trajectory import _parse_github_owner_repo
        assert _parse_github_owner_repo("git@github.com:owner/repo.git") == "owner/repo"

    def test_non_github_url_returns_none(self):
        from reproducible_trajectories.trajectory import _parse_github_owner_repo
        assert _parse_github_owner_repo("https://gitlab.com/owner/repo") is None


# ---------------------------------------------------------------------------
# verify_trajectories  (integration-style with a temp git repo)
# ---------------------------------------------------------------------------

class TestVerifyTrajectories:
    """Integration tests that create a real git repo with trajectory-tagged commits."""

    def _init_repo(self, path):
        """Initialise a bare git repo and return its root path."""
        subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            check=True, capture_output=True, cwd=str(path),
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            check=True, capture_output=True, cwd=str(path),
        )
        return path

    def _commit(self, repo, message, files=None):
        """Stage `files` (dict path->content) and commit with `message`."""
        if files:
            for rel, content in files.items():
                fp = repo / rel
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(content)
                subprocess.run(
                    ["git", "add", rel],
                    check=True, capture_output=True, cwd=str(repo),
                )
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", message],
            check=True, capture_output=True, cwd=str(repo),
        )

    def test_no_trajectory_tagged_commits(self, tmp_path):
        from reproducible_trajectories.trajectory import verify_trajectories
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)
        self._commit(repo, "initial commit", {"a.py": "x = 1\n"})
        results = verify_trajectories(str(repo))
        assert results == []

    def test_trajectory_not_found(self, tmp_path):
        from reproducible_trajectories.trajectory import verify_trajectories
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)
        self._commit(repo, "initial", {"f.py": "x = 1\n"})
        self._commit(repo, "trajectory: nonexistent-uuid-0000-0000-0000000000")
        results = verify_trajectories(str(repo), claude_dir=str(tmp_path / "claude"))
        assert len(results) == 1
        assert results[0]["status"] == "trajectory_not_found"

    def test_reproducible_write(self, tmp_path):
        from reproducible_trajectories.trajectory import verify_trajectories
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)
        self._commit(repo, "initial", {"f.py": "original\n"})

        # Build a trajectory that writes f.py
        traj_uuid = "aaaaaaaa-0000-0000-0000-000000000001"
        traj_events = [
            {
                "type": "user",
                "message": {"role": "user", "content": "do stuff"},
                "uuid": "e1",
                "sessionId": traj_uuid,
                "cwd": str(repo),
                "timestamp": "2026-01-01T00:00:00.000Z",
            },
            _make_tool_use_event(
                "e2", "t1", "Write",
                {"file_path": str(repo / "f.py"), "content": "new content\n"},
                session_id=traj_uuid, cwd=str(repo),
            ),
        ]
        traj_dir = tmp_path / "claude" / "projects" / "proj"
        traj_dir.mkdir(parents=True)
        traj_path = traj_dir / f"{traj_uuid}.jsonl"
        _write_jsonl(str(traj_path), traj_events)

        self._commit(repo, f"trajectory: {traj_uuid}", {"f.py": "new content\n"})

        results = verify_trajectories(str(repo), claude_dir=str(tmp_path / "claude"))
        traj_results = [r for r in results if r["trajectory"] == traj_uuid]
        assert len(traj_results) == 1
        assert traj_results[0]["status"] == "reproducible"

    def test_not_reproducible_write(self, tmp_path):
        from reproducible_trajectories.trajectory import verify_trajectories
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)
        self._commit(repo, "initial", {"f.py": "original\n"})

        traj_uuid = "bbbbbbbb-0000-0000-0000-000000000002"
        traj_events = [
            {
                "type": "user",
                "message": {"role": "user", "content": "do stuff"},
                "uuid": "e1",
                "sessionId": traj_uuid,
                "cwd": str(repo),
                "timestamp": "2026-01-01T00:00:00.000Z",
            },
            _make_tool_use_event(
                "e2", "t1", "Write",
                {"file_path": str(repo / "f.py"), "content": "trajectory says X\n"},
                session_id=traj_uuid, cwd=str(repo),
            ),
        ]
        traj_dir = tmp_path / "claude" / "projects" / "proj"
        traj_dir.mkdir(parents=True)
        traj_path = traj_dir / f"{traj_uuid}.jsonl"
        _write_jsonl(str(traj_path), traj_events)

        # Actual commit has different content
        self._commit(repo, f"trajectory: {traj_uuid}", {"f.py": "actual Y\n"})

        results = verify_trajectories(str(repo), claude_dir=str(tmp_path / "claude"))
        traj_results = [r for r in results if r["trajectory"] == traj_uuid]
        assert len(traj_results) == 1
        assert traj_results[0]["status"] == "not_reproducible"

    def test_no_operations(self, tmp_path):
        from reproducible_trajectories.trajectory import verify_trajectories
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)
        self._commit(repo, "initial", {"f.py": "x\n"})

        traj_uuid = "cccccccc-0000-0000-0000-000000000003"
        # Trajectory with only a Read, no Write/Edit
        traj_events = [
            _make_tool_use_event(
                "e1", "t1", "Read",
                {"file_path": str(repo / "f.py")},
                session_id=traj_uuid, cwd=str(repo),
            ),
        ]
        traj_dir = tmp_path / "claude" / "projects" / "proj"
        traj_dir.mkdir(parents=True)
        traj_path = traj_dir / f"{traj_uuid}.jsonl"
        _write_jsonl(str(traj_path), traj_events)

        self._commit(repo, f"trajectory: {traj_uuid}")
        results = verify_trajectories(str(repo), claude_dir=str(tmp_path / "claude"))
        traj_results = [r for r in results if r["trajectory"] == traj_uuid]
        assert len(traj_results) == 1
        assert traj_results[0]["status"] == "no_operations"


# ---------------------------------------------------------------------------
# add_trajectories_to_repo
# ---------------------------------------------------------------------------

class TestAddTrajectoriesToRepo:
    def _init_repo(self, path):
        subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            check=True, capture_output=True, cwd=str(path),
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            check=True, capture_output=True, cwd=str(path),
        )

    def _commit(self, repo, message, files=None):
        if files:
            for rel, content in files.items():
                fp = repo / rel
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(content)
                subprocess.run(
                    ["git", "add", rel],
                    check=True, capture_output=True, cwd=str(repo),
                )
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", message],
            check=True, capture_output=True, cwd=str(repo),
        )

    def test_not_found(self, tmp_path):
        from reproducible_trajectories.trajectory import add_trajectories_to_repo
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)
        self._commit(repo, "trajectory: missing-uuid-0000-0000-0000000000")
        results = add_trajectories_to_repo(str(repo), claude_dir=str(tmp_path / "claude"))
        assert any(r["status"] == "not_found" for r in results)

    def test_added(self, tmp_path):
        from reproducible_trajectories.trajectory import add_trajectories_to_repo
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)
        self._commit(repo, "initial", {"src/main.py": "x = 1\n"})

        traj_uuid = "dddddddd-0000-0000-0000-000000000004"
        traj_events = [
            {
                "type": "user",
                "message": {"role": "user", "content": "task"},
                "uuid": "e1",
                "sessionId": traj_uuid,
                "cwd": str(repo),
                "timestamp": "2026-01-01T00:00:00.000Z",
            },
            _make_tool_use_event(
                "e2", "t1", "Write",
                {"file_path": str(repo / "src" / "main.py"), "content": "x = 2\n"},
                session_id=traj_uuid, cwd=str(repo),
            ),
        ]
        traj_dir = tmp_path / "claude" / "projects" / "proj"
        traj_dir.mkdir(parents=True)
        traj_path = traj_dir / f"{traj_uuid}.jsonl"
        _write_jsonl(str(traj_path), traj_events)

        self._commit(repo, f"trajectory: {traj_uuid}", {"src/main.py": "x = 2\n"})

        results = add_trajectories_to_repo(str(repo), claude_dir=str(tmp_path / "claude"))
        assert any(r["status"] == "added" for r in results)
        dest = repo / "trajectories" / f"{traj_uuid}.jsonl"
        assert dest.exists()

    def test_already_exists(self, tmp_path):
        from reproducible_trajectories.trajectory import add_trajectories_to_repo
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)

        traj_uuid = "eeeeeeee-0000-0000-0000-000000000005"
        traj_events = [
            {
                "type": "user",
                "message": {"role": "user", "content": "task"},
                "uuid": "e1",
                "sessionId": traj_uuid,
                "cwd": str(repo),
                "timestamp": "2026-01-01T00:00:00.000Z",
            },
            _make_tool_use_event(
                "e2", "t1", "Write",
                {"file_path": str(repo / "f.py"), "content": "y\n"},
                session_id=traj_uuid, cwd=str(repo),
            ),
        ]
        traj_dir = tmp_path / "claude" / "projects" / "proj"
        traj_dir.mkdir(parents=True)
        traj_path = traj_dir / f"{traj_uuid}.jsonl"
        _write_jsonl(str(traj_path), traj_events)

        # Pre-create the destination
        dest_dir = repo / "trajectories"
        dest_dir.mkdir()
        (dest_dir / f"{traj_uuid}.jsonl").write_text("{}\n")

        self._commit(repo, f"trajectory: {traj_uuid}", {"f.py": "y\n"})

        results = add_trajectories_to_repo(str(repo), claude_dir=str(tmp_path / "claude"))
        assert any(r["status"] == "already_exists" for r in results)

    def test_dry_run_does_not_write(self, tmp_path):
        from reproducible_trajectories.trajectory import add_trajectories_to_repo
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)
        self._commit(repo, "initial", {"f.py": "z\n"})

        traj_uuid = "ffffffff-0000-0000-0000-000000000006"
        traj_events = [
            {
                "type": "user",
                "message": {"role": "user", "content": "task"},
                "uuid": "e1",
                "sessionId": traj_uuid,
                "cwd": str(repo),
                "timestamp": "2026-01-01T00:00:00.000Z",
            },
            _make_tool_use_event(
                "e2", "t1", "Write",
                {"file_path": str(repo / "f.py"), "content": "z\n"},
                session_id=traj_uuid, cwd=str(repo),
            ),
        ]
        traj_dir = tmp_path / "claude" / "projects" / "proj"
        traj_dir.mkdir(parents=True)
        traj_path = traj_dir / f"{traj_uuid}.jsonl"
        _write_jsonl(str(traj_path), traj_events)

        self._commit(repo, f"trajectory: {traj_uuid}", {"f.py": "z\n"})

        results = add_trajectories_to_repo(
            str(repo), claude_dir=str(tmp_path / "claude"), dry_run=True
        )
        assert any(r["status"] == "added" for r in results)
        dest = repo / "trajectories" / f"{traj_uuid}.jsonl"
        assert not dest.exists()


# ---------------------------------------------------------------------------
# _get_codex_session_info
# ---------------------------------------------------------------------------

class TestGetCodexSessionInfo:
    def test_returns_id_and_cwd(self):
        from reproducible_trajectories.trajectory import _get_codex_session_info
        events = [
            {"id": "sess-abc", "cwd": "/home/user/project"},
        ]
        session_id, cwd = _get_codex_session_info(events)
        assert session_id == "sess-abc"
        assert cwd == "/home/user/project"

    def test_session_id_field_fallback(self):
        from reproducible_trajectories.trajectory import _get_codex_session_info
        events = [
            {"session_id": "sess-xyz", "cwd": "/repo"},
        ]
        session_id, cwd = _get_codex_session_info(events)
        assert session_id == "sess-xyz"
        assert cwd == "/repo"

    def test_skips_chat_messages_with_role(self):
        from reproducible_trajectories.trajectory import _get_codex_session_info
        events = [
            {"role": "user", "content": "hello", "cwd": "/wrong"},
            {"role": "assistant", "cwd": "/also-wrong"},
            {"id": "sess-1", "cwd": "/correct"},
        ]
        session_id, cwd = _get_codex_session_info(events)
        assert cwd == "/correct"

    def test_returns_none_when_no_cwd(self):
        from reproducible_trajectories.trajectory import _get_codex_session_info
        events = [
            {"role": "user", "content": "hello"},
        ]
        session_id, cwd = _get_codex_session_info(events)
        assert session_id is None
        assert cwd is None

    def test_empty_events(self):
        from reproducible_trajectories.trajectory import _get_codex_session_info
        session_id, cwd = _get_codex_session_info([])
        assert session_id is None
        assert cwd is None

    def test_session_meta_payload_format(self):
        from reproducible_trajectories.trajectory import _get_codex_session_info
        events = [
            {
                "type": "session_meta",
                "payload": {
                    "id": "sess-live",
                    "cwd": "/repo",
                },
            }
        ]
        session_id, cwd = _get_codex_session_info(events)
        assert session_id == "sess-live"
        assert cwd == "/repo"


# ---------------------------------------------------------------------------
# _collect_codex_modifications
# ---------------------------------------------------------------------------

def _make_codex_tool_call(name, args):
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args),
                },
            }
        ],
    }


def _make_codex_response_item(name, input_):
    return {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "name": name,
            "input": input_,
        },
    }


class TestCollectCodexModifications:
    def test_write_file_absolute_path(self):
        from reproducible_trajectories.trajectory import _collect_codex_modifications
        events = [_make_codex_tool_call("write_file", {"path": "/repo/src/main.py", "content": "x"})]
        mods = _collect_codex_modifications(events)
        assert "/repo/src/main.py" in mods
        assert mods["/repo/src/main.py"]["tool"] == "write_file"

    def test_write_file_relative_path_with_cwd(self):
        from reproducible_trajectories.trajectory import _collect_codex_modifications
        events = [_make_codex_tool_call("write_file", {"path": "src/main.py", "content": "x"})]
        mods = _collect_codex_modifications(events, session_cwd="/repo")
        assert "/repo/src/main.py" in mods

    def test_write_file_relative_path_no_cwd(self):
        from reproducible_trajectories.trajectory import _collect_codex_modifications
        events = [_make_codex_tool_call("write_file", {"path": "src/main.py", "content": "x"})]
        mods = _collect_codex_modifications(events, session_cwd=None)
        assert "src/main.py" in mods

    def test_apply_patch_openai_update_format(self):
        from reproducible_trajectories.trajectory import _collect_codex_modifications
        patch = "*** Begin Patch\n*** Update File: src/utils.py\n@@ -1 +1 @@\n-old\n+new\n*** End Patch"
        events = [_make_codex_tool_call("apply_patch", {"patch": patch})]
        mods = _collect_codex_modifications(events, session_cwd="/repo")
        assert "/repo/src/utils.py" in mods
        assert mods["/repo/src/utils.py"]["tool"] == "apply_patch"

    def test_apply_patch_openai_add_format(self):
        from reproducible_trajectories.trajectory import _collect_codex_modifications
        patch = "*** Begin Patch\n*** Add File: newfile.py\n+def foo(): pass\n*** End Patch"
        events = [_make_codex_tool_call("apply_patch", {"patch": patch})]
        mods = _collect_codex_modifications(events, session_cwd="/repo")
        assert "/repo/newfile.py" in mods

    def test_apply_patch_unified_diff_format(self):
        from reproducible_trajectories.trajectory import _collect_codex_modifications
        patch = "--- a/tests/test_main.py\n+++ b/tests/test_main.py\n@@ -1 +1 @@\n-old\n+new"
        events = [_make_codex_tool_call("apply_patch", {"patch": patch})]
        mods = _collect_codex_modifications(events, session_cwd="/repo")
        assert "/repo/tests/test_main.py" in mods

    def test_apply_patch_input_field_fallback(self):
        from reproducible_trajectories.trajectory import _collect_codex_modifications
        patch = "*** Begin Patch\n*** Update File: file.py\n@@ -1 +1 @@\n-a\n+b\n*** End Patch"
        events = [_make_codex_tool_call("apply_patch", {"input": patch})]
        mods = _collect_codex_modifications(events, session_cwd="/repo")
        assert "/repo/file.py" in mods

    def test_first_modification_wins(self):
        from reproducible_trajectories.trajectory import _collect_codex_modifications
        events = [
            _make_codex_tool_call("write_file", {"path": "/repo/file.py", "content": "v1"}),
            _make_codex_tool_call("write_file", {"path": "/repo/file.py", "content": "v2"}),
        ]
        mods = _collect_codex_modifications(events)
        assert mods["/repo/file.py"]["input"]["content"] == "v1"

    def test_empty_events(self):
        from reproducible_trajectories.trajectory import _collect_codex_modifications
        assert _collect_codex_modifications([]) == {}

    def test_ignores_unknown_tools(self):
        from reproducible_trajectories.trajectory import _collect_codex_modifications
        events = [_make_codex_tool_call("shell", {"command": "ls -la"})]
        mods = _collect_codex_modifications(events, session_cwd="/repo")
        assert mods == {}

    def test_malformed_arguments_ignored(self):
        from reproducible_trajectories.trajectory import _collect_codex_modifications
        events = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_bad",
                        "type": "function",
                        "function": {"name": "write_file", "arguments": "not-json"},
                    }
                ],
            }
        ]
        mods = _collect_codex_modifications(events)
        assert mods == {}

    def test_response_item_custom_tool_call_patch(self):
        from reproducible_trajectories.trajectory import _collect_codex_modifications
        patch = "*** Begin Patch\n*** Update File: src/live.py\n@@\n-old\n+new\n*** End Patch"
        events = [_make_codex_response_item("apply_patch", patch)]
        mods = _collect_codex_modifications(events, session_cwd="/repo")
        assert "/repo/src/live.py" in mods
        assert mods["/repo/src/live.py"]["tool"] == "apply_patch"

    def test_patch_apply_end_changes_format(self):
        from reproducible_trajectories.trajectory import _collect_codex_modifications
        events = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "patch_apply_end",
                    "success": True,
                    "changes": {
                        "/repo/reproducible_trajectories/__init__.py": {
                            "type": "update"
                        }
                    },
                },
            }
        ]
        mods = _collect_codex_modifications(events, session_cwd="/repo")
        assert "/repo/reproducible_trajectories/__init__.py" in mods
        assert mods["/repo/reproducible_trajectories/__init__.py"]["tool"] == "apply_patch"
