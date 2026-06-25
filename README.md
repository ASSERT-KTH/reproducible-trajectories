# the reproducible-trajectories project

[![PyPI](https://img.shields.io/pypi/v/reproducible-trajectories)](https://pypi.org/project/reproducible-trajectories/)

When a coding agent edits your codebase, it produces a *trajectory*: a complete, structured log of every file it read, every edit it made, and every tool it called. 

Trajectories represent **critical data for understanding and improving how AI coding agents work**. Yet this data is almost never captured or shared. With trajectory data, we could conduct unprecedented research:

- **Agent reasoning patterns**: How do different agents plan, navigate large codebases, and recover from errors?
- **Code quality outcomes**: What trajectory characteristics (e.g., number of reads, edit order, tool sequences) correlate with high-quality commits?
- **Human-AI collaboration**: How do human developers interact with agent trajectories and what edits matter most to them?
- **Optimization**: What makes agents efficient? Can we predict trajectory complexity from the problem statement?
- **Testing and verification**: Do agents that read more tests write better code? What's the relationship between exploration and correctness?

Without trajectory data, software engineering researchers like us can only observe the final commit. We're analyzing outcomes without understanding the process.

This repository provides tooling and a central database for storing trajectories. It is designed to be collaborative and crowd-sourced — so you can contribute to science!

## Join the movement!

**How it works**: Simply add a commit hook to your open source / open science repository. 

```sh
wget https://raw.githubusercontent.com/ASSERT-KTH/reproducible-trajectories/refs/heads/main/hooks/pre-commit-collect-trajectories.py > .git/hooks/pre-commit
chmod 755 .git/hooks/pre-commit
``` 

When you commit, the trajectory is automatically captured and contributed to our shared database.

In case you accidentally pushed a trajectory for a private repo or one containing private data, shoot us [an email](mailto:monperrus@kth.se?subject=reproducible-trajectories).

In any case, we'll do serious privacy checks before publishing the dataset.

---

## Installation

The package is available on [PyPI](https://pypi.org/project/reproducible-trajectories/):

```sh
pip install reproducible-trajectories
```

## CLI

```
$ python -m reproducible_trajectories <command>
```

Or, after `pip install`:

```
$ reproducible-trajectories <command>
```

Commands:

### read-files

Extract all files read in a Claude Code trajectory, either by a tool call `Read` (or equivalent), or with a bash command (cat / head / tail / sed / awk). Tells whether the file has been fully or partially read. Supports textual and JSON output.

### modified-files

Extract all modified files from a Claude Code trajectory. For each file, reports one of:

- `fully` — a `Read` of the file appeared in the trace before the modification; full pre-edit content is recoverable
- `partially` — an `Edit` was made without a prior `Read`; only `old_string` is in the trace
- `not contained` — file existed before the trajectory but was overwritten with no prior `Read`
- `new file` — file was created fresh during the trajectory (no pre-existing content)

```
usage: python -m reproducible_trajectories modified-files [-h] [--claude-dir CLAUDE_DIR] [--json] trajectory

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
usage: python -m reproducible_trajectories filter-trajectories [-h]
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
python -m reproducible_trajectories filter-trajectories <session-id> -o filtered.jsonl
```

**Example — strip references to a specific private directory:**
```
python -m reproducible_trajectories filter-trajectories trace.jsonl /home/user/private -o trace-public.jsonl
```

### verify-trajectories

The core idea: a git commit produced by an AI agent should be as reproducible as one produced by a deterministic build system. Tag your commit with a trajectory ID, store the trajectory alongside the code, and anyone — human or machine — can replay the session step by step and check that the output matches.

`verify-trajectories` walks a Git repository, finds commits that reference a trajectory, replays the trajectory's `Write`/`Edit` operations on the parent-commit file state, and checks whether the result matches the actual commit.

```
usage: python -m reproducible_trajectories verify-trajectories [-h]
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
$ python -m reproducible_trajectories verify-trajectories .
0d43870c17a0  reproducible            implementation of hook
859aba2ca3ca  reproducible            implementation of extract_read_files
b9f4ef111d9f  not_reproducible        first implementation of extract-modified-files.py
```

**Example — JSON output with per-file breakdown:**
```
$ python -m reproducible_trajectories verify-trajectories . --json
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

### share-trajectories

`share-trajectories` is the easiest way to contribute your local Claude Code, pi coding agent, and OpenAI Codex CLI trajectories to science. It scans `~/.claude/projects/` (Claude Code), `~/.pi/agent/sessions/` (pi coding agent), and `~/.codex/sessions/` (OpenAI Codex CLI), then lets you choose whether to share all trajectories or only the ones from open-source repositories.

**Why it matters for science**: Trajectories produced on open-source projects are themselves open data — the code they touch is public, the repository is public, and the agent's reasoning process is therefore safe to share. Collecting these trajectories at scale enables empirical studies that are otherwise impossible: How do AI agents navigate real codebases? Which tool-use patterns lead to correct, mergeable commits? How does agent behaviour vary across programming languages or project sizes? Every trajectory you share is a data point that helps answer these questions.

**How to run it** (interactive, one-time contribution):

```sh
pip install reproducible-trajectories
python -m reproducible_trajectories share-trajectories
```

The command will:
1. Start by asking whether you want to share all trajectories or only the ones from open-source repositories.
2. Reassure you that all collected data will be properly filtered and anonymized.
3. Scan all local trajectories (Claude Code, pi coding agent, and Codex CLI) and keep those whose edits stay within a single git repository.
4. Display the repos and edited files found for the selected scope.
5. Ask whether you agree to share all of them, or step through them repo by repo.
6. Zip and upload the approved trajectories together with a `metadata.json` containing your git email and any public GitHub repo URLs that were found.
7. Ask whether to install a `pre-commit` hook in each repo so future trajectories are shared automatically.
8. Print a summary: trajectories uploaded, hooks added.

```
$ python -m reproducible_trajectories share-trajectories
Do you want to:
  1) share all trajectories
  2) only share the ones from open-source repositories
All collected data will be properly filtered and anonymized. [1/2/N] 2

🔍 Found 20 trajectories in open-source repos

/home/user/myproject  https://github.com/org/myproject
  /home/user/myproject/src/foo.py
  /home/user/myproject/tests/test_foo.py
...

Do you agree to share them all with the KTH experiment on coding agents? [y/N]
```

**Non-interactive mode** (for use in scripts or hooks):

```sh
python -m reproducible_trajectories share-trajectories --scope open-source --yes
python -m reproducible_trajectories share-trajectories --scope all --yes
```

`open-source-trajectories` remains available as a backward-compatible alias.

**Custom agent directories**:

```sh
PI_CODING_AGENT_DIR=/path/to/.pi/agent \
python -m reproducible_trajectories open-source-trajectories \
  --claude-dir /path/to/.claude \
  --codex-dir /path/to/.codex
```

Supported agent trace formats:
- **Claude Code** (`~/.claude/projects/**/*.jsonl`) — uses `Write`, `Edit`, `NotebookEdit` tool calls
- **pi coding agent** (`~/.pi/agent/sessions/**/*.jsonl`, or `$PI_CODING_AGENT_DIR/sessions/**/*.jsonl`) — normalized to Claude-style `Read`/`Write`/`Edit`/`Bash` tool calls
- **OpenAI Codex CLI** (`~/.codex/sessions/**/*.jsonl`) — uses `write_file` and `apply_patch` tool calls (both OpenAI custom patch format and standard unified diff)

### collect-trajectories

`collect-trajectories` performs the same work as the `pre-commit-collect-trajectories` hook but can be invoked manually from the command line. It finds the most recent Claude Code trajectory whose modified files are a subset of the current staged files, checks reproducibility, and POSTs the result to `https://api.monperrus.com/trajectories`.

```sh
reproducible-trajectories collect-trajectories
```

The command never exits with a non-zero status — failures are printed to stderr.

### add-trajectories-to-repo

For each trajectory referred to in commits, copy it into the repo under `trajectories/`, provided it only reads files from within the repo (no private paths outside the repository root).

```
usage: python -m reproducible_trajectories add-trajectories-to-repo [-h]
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
python -m reproducible_trajectories add-trajectories-to-repo .
```

**Example — preview without writing:**
```
python -m reproducible_trajectories add-trajectories-to-repo . --dry-run
```

### replay-trajectories

`replay-trajectories` replays file mutation events from trajectories discoverable for a repository via `trajectoriz` local search semantics, orders them by event timestamp, applies them to the target repository, and creates one git commit per replayed event.

Supported mutation types:
- whole-file writes
- Claude / pi `Edit`
- Codex `apply_patch` (OpenAI patch format and unified diff)

Safety constraints:
- the repository must have a clean worktree before replay starts
- only mutations targeting files inside the target repository are replayed
- duplicate replay events discovered from mirrored trajectory stores are ignored
- replay events that originally failed in the source trajectory are skipped
- edits or patches that cannot be cleanly replayed on the reconstructed state are skipped with a warning instead of aborting the whole run
- commit messages include the agent name, replayed file path(s), and total staged line delta, e.g. `replay: claude mc_repl.py (42 lines changed)`
- author and committer dates are both set to the original write-event timestamp

```
usage: python -m reproducible_trajectories replay-trajectories [-h]
                                                     [repo]

positional arguments:
  repo                  path to the git repository (default: current directory)
```

**Example — replay the repository's stored trajectories:**
```
python -m reproducible_trajectories replay-trajectories .
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


`fb049bdf-8889-449f-a299-c11d48fe430b`: refactoring to `$ python -m reproducible_trajectories <command>`

`e9f50aed-ffcd-488b-bdd3-8e6f68539932`: implement the hook system

`743a0977-517f-4ad2-b409-a002c3f65a6e`: implement the extract-read-files command

`f959c661-891e-4292-92a3-d105b49e5244`: merge refactoring

`6e3a6daf-25aa-4a99-bdbb-2557149964cd`: implement filter-trajectories

`0c71e6af-ff2c-4819-aba3-4daf988dc668`: implement verify-trajectories

`f4f82a30-f6f4-452a-9f73-14a48a4d38f5`: add-trajectories-to-repo

`ca9f8f57-90ab-4eda-b6a9-b9fc9676b789`: add support for "no trajectory" in commit hoook

## License

MIT
Authors: Martin Monperrus and the Assert team
