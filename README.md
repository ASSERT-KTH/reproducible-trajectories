# reproducible-trajectories

**Author: Martin Monperrus**

When Claude Code edits your codebase, it produces a *trajectory*: a complete, structured log of every file it read, every edit it made, and every tool it called. This repository provides tooling to embed trajectory references in git commits and to verify that replaying a trajectory faithfully reproduces the committed changes.

The core idea: a git commit produced by an AI agent should be as reproducible as one produced by a deterministic build system. Tag your commit with a trajectory ID, store the trajectory alongside the code, and anyone — human or machine — can replay the session step by step and check that the output matches. No black boxes.

**In short: source code has compilers; AI-generated commits should have trajectories.**

---

## Script

```
$ python reproducible_trajectories.py <command>

```

Commands could be

### extract-read-files

Extract all files read in a Claude Code trajectory, either by a tool call `Read` (or equivalent), or with a bash command (cat / head / tail / sed / awk). Telles whether the file has been fully or partially read. Support textual and json output.

### extract-modified-files

Extract all modified files from a Claude Code trajectory. For each file, reports one of:

- `fully` — a `Read` of the file appeared in the trace before the modification; full pre-edit content is recoverable
- `partially` — an `Edit` was made without a prior `Read`; only `old_string` is in the trace
- `not contained` — file existed before the trajectory but was overwritten with no prior `Read`
- `new file` — file was created fresh during the trajectory (no pre-existing content)

```
usage: extract-modified-files.py [-h] [--claude-dir CLAUDE_DIR] [--json] trajectory

positional arguments:
  trajectory       path to trajectory JSONL file, or session ID

options:
  --claude-dir     path to .claude directory (default: ~/.claude)
  --json           output results as JSON
```

With `--json` each entry includes `file_path`, `tool` (Write/Edit/NotebookEdit), and `containment`.

Session IDs are resolved by searching `~/.claude/projects/**/<id>.jsonl`.


### filter-trajectories

Produce a filtered copy of a trajectory, removing tool calls (`Read`, `Write`, `Edit`, `NotebookEdit`, `Glob`, `Grep`) that reference specified files or folders, along with their paired results. Events that become empty after filtering are dropped entirely, keeping the output a valid Claude trace.

```
usage: reproducible_trajectories.py filter-trajectories [-h]
                                                        [--claude-dir CLAUDE_DIR]
                                                        [--cwd CWD]
                                                        [--output OUTPUT]
                                                        trajectory [paths ...]

positional arguments:
  trajectory       path to trajectory JSONL file, or session ID
  paths            files/folders to exclude; if omitted, all tool calls
                   referencing files outside the working directory are removed

options:
  --claude-dir     path to .claude directory (default: ~/.claude)
  --cwd            base directory for the "outside" check (default: cwd
                   recorded in the trajectory, or the current directory)
  --output, -o     write filtered trajectory to this file (default: stdout)
```

The input file is never modified. Output is written as JSONL (one JSON object per line), matching the native Claude trace format.

**Example — strip all references to files outside the project:**
```
python reproducible_trajectories.py filter-trajectories <session-id> -o filtered.jsonl
```

**Example — strip references to a specific private directory:**
```
python reproducible_trajectories.py filter-trajectories trace.jsonl /home/user/private -o trace-public.jsonl
```

### verify-trajectories

Walk a Git repository, find commits that reference a trajectory, replay the trajectory's `Write`/`Edit` operations on the parent-commit file state, and check whether the result matches the actual commit.

```
usage: reproducible_trajectories.py verify-trajectories [-h]
                                                        [--claude-dir CLAUDE_DIR]
                                                        [--json]
                                                        repo

positional arguments:
  repo             path to the git repository to verify

options:
  --claude-dir     path to .claude directory (default: ~/.claude)
  --json           output results as JSON
```

Each trajectory-tagged commit is reported with one of four statuses:

- `reproducible` — all verifiable files produced by the simulation match the commit
- `not_reproducible` — at least one file differs between the simulation and the commit
- `no_operations` — the trajectory contains no `Write` or `Edit` operations (nothing to verify)
- `trajectory_not_found` — the trajectory reference in the commit message could not be resolved to a file

Files that cannot be verified are excluded from the pass/fail judgement:

- `outside_repo` — the file path is outside the repository root
- `unverifiable` — the file was only touched by `NotebookEdit` (which the simulator skips), or the session working directory is unknown so paths cannot be resolved

**Example — text output:**
```
$ python reproducible_trajectories.py verify-trajectories .
0d43870c17a0  reproducible            implementation of hook
859aba2ca3ca  reproducible            implementation of extract_read_files
b9f4ef111d9f  not_reproducible        first implementation of extract-modified-files.py
```

**Example — JSON output with per-file breakdown:**
```
$ python reproducible_trajectories.py verify-trajectories . --json
[
  {
    "commit": "0d43870c17a0",
    "short_message": "implementation of hook",
    "trajectory": "e9f50aed-ffcd-488b-bdd3-8e6f68539932",
    "status": "reproducible",
    "files": [
      { "file": "hooks/pre-commit", "status": "match" }
    ]
  },
  ...
]
```

### add-trajectories-to-repo

For each trajectory referred to in commits, copy it into the repo under `trajectories/`, provided it only reads files from within the repo (no private paths outside the repository root).

```
usage: reproducible_trajectories.py add-trajectories-to-repo [-h]
                                                              [--claude-dir CLAUDE_DIR]
                                                              [--json]
                                                              [--dry-run]
                                                              repo

positional arguments:
  repo             path to the git repository

options:
  --claude-dir     path to .claude directory (default: ~/.claude)
  --json           output results as JSON
  --dry-run        report what would be copied without writing anything
```

Each referenced trajectory is reported with one of four statuses:

- `added` — trajectory was copied to `trajectories/<id>.jsonl`
- `already_exists` — trajectory file was already present in `trajectories/`
- `skipped_private` — trajectory reads files outside the repository root; not copied
- `not_found` — trajectory reference could not be resolved to a file

**Example — copy all safe trajectories:**
```
python reproducible_trajectories.py add-trajectories-to-repo .
```

**Example — preview without writing:**
```
python reproducible_trajectories.py add-trajectories-to-repo . --dry-run
```

## Commit conventions

The commit message should contain:
- `trajectory: {uuid}` (space optional after column)
- `trajectory: {filepath}`
- `<trajectory>{uuid}</trajectory>`

## Hooks

`hooks/pre-commit`: contains a python script that checks that the commit message contains a valid trajectory identifier (either a UUID that can be found in `$HOME/.claude/` or a correct file path)

`hooks/pre-commit-verify-trajectory`: verifies that staged trajectory files under `trajectories/` are reproducible. Simulates the trajectory's `Write`/`Edit` operations against HEAD and checks that the result matches the staged content. Rejects the commit if any file mismatches.

```bash
ln -s ../../hooks/pre-commit-verify-trajectory .git/hooks/pre-commit
```

`hooks/pre-commit-collect-trajectories.py`: automatically finds the Claude Code trajectory that produced the current staged changes and submits it — along with reproducibility metadata — to `https://api.monperrus.com/trajectories`.

How it works:

1. Reads the set of staged files from `git diff --cached`.
2. Scans the 10 most recent trajectory files in `~/.claude/projects/` (sorted by modification time).
3. Selects the first trajectory whose modified-file set is a non-empty subset of the staged files.
4. Checks reproducibility by simulating the trajectory's `Write`/`Edit` operations on the HEAD state and comparing with the index.
5. POSTs the full trajectory events and reproducibility metadata as JSON to `https://api.monperrus.com/trajectories`.

The commit is never blocked by this hook — failures are printed to stderr and the hook exits 0.

```bash
ln -s ../../hooks/pre-commit-collect-trajectories.py .git/hooks/pre-commit
```

Payload format:

```json
{
  "trajectory_id": "<uuid>",
  "trajectory": [ ...events... ],
  "reproducibility": {
    "status": "reproducible | not_reproducible | no_operations",
    "files": [
      { "file": "path/to/file", "status": "match | mismatch | unverifiable | outside_repo" }
    ]
  },
  "git": {
    "remote": "https://github.com/owner/repo.git",
    "branch": "main",
    "commit": "<sha of HEAD at commit time>",
    "email": "user@example.com"
  }
}
```

## Trajectories:


`a8810dfd-8ae5-4678-a9cc-358727628077`:
  - implement `extract-modified-files.py`:
  - contains private files, so we only push the filtered version to the repository


`fb049bdf-8889-449f-a299-c11d48fe430b`: refactoring to `$ python reproducible_trajectories.py <command>`

`e9f50aed-ffcd-488b-bdd3-8e6f68539932`: implement the hook system

`743a0977-517f-4ad2-b409-a002c3f65a6e`: implement the extract-read-files command

`f959c661-891e-4292-92a3-d105b49e5244`: merge refactoring

`6e3a6daf-25aa-4a99-bdbb-2557149964cd`: implement filter-trajectories

`0c71e6af-ff2c-4819-aba3-4daf988dc668`: implement verify-trajectories

`f4f82a30-f6f4-452a-9f73-14a48a4d38f5`: add-trajectories-to-repo

`ca9f8f57-90ab-4eda-b6a9-b9fc9676b789`: add support for "no trajectory" in commit hoook