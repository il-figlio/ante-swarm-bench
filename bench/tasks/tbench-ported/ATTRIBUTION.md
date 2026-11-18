# Attribution

The tasks in this pack are adapted from **Terminal-Bench** (tbench.ai), licensed under the **Apache License 2.0**
(http://www.apache.org/licenses/LICENSE-2.0). Task instructions, input files, test logic and reference answers were
ported from the upstream task trees. Changes: checks converted from pytest to `unittest`, `/app/` paths rewritten to
repository-relative ones, and hidden expected answers moved into `tests/manifest.json` as hashes.

| Task | Upstream task | Upstream author (as listed in the task metadata; contact details there) |
|---|---|---|
| t01 | `tasks/constraints-scheduling` in github.com/harbor-framework/terminal-bench-2-1 | Derek Pham |
| t02 | `tasks/sqlite-db-truncate` in github.com/harbor-framework/terminal-bench-2-1 | Nicholas Carlini |
| t03 | `original-tasks/count-call-stack` in github.com/harbor-framework/terminal-bench-1 | Li Zhong |
| t04 | `original-tasks/fibonacci-server` in github.com/harbor-framework/terminal-bench-1 | Alex Shaw |
| t05 | `tasks/fix-git` in github.com/harbor-framework/terminal-bench-2-1 | TheMikeMerrill |

## Inputs

Committed:

- `alice_calendar.ics`, `bob_calendar.ics`, `carol_calendar.ics`: verbatim copies of
  `tasks/constraints-scheduling/environment/inputs/*` (git blob ids match terminal-bench-2-1 at `12b7565`).

Not committed; `python3 fetch_inputs.py` downloads them from the upstream trees at pinned commits and checks each
against a recorded sha256:

- `trunc.db`: verbatim copy of `tasks/sqlite-db-truncate/environment/trunc.db`.
- `log.stack`: the `log.stack` member of `original-tasks/count-call-stack/log.stack.zip` (the upstream Dockerfile
  unzips it into the task directory).
- `personal-site.tar.gz`: upstream `fix-git` ships no archive. Its `environment/setup.sh` clones a personal website
  repository, resets it to a fixed commit, commits the two files in `environment/resources/patch_files/` on a
  detached HEAD ("Move to Stanford") and checks out `master`. `fetch_inputs.py` repeats those steps with the patch
  files fetched and verified, and packs the result. As of 2026-11-18 the cloned repository
  (github.com/TheMikeMerrill/personal-site) is not publicly reachable, so this input cannot currently be rebuilt
  from the upstream source. The copy used in the original runs was a reduced rebuild of the same state (the
  `resources/` binaries dropped), later repacked with the synthetic identity `Test User <test@example.com>`, the
  identity upstream's setup script uses. It is not distributed here; `fetch_inputs.py --archive FILE` accepts it
  only if its sha256 matches the recorded value.

The expected file contents for t05 (`tests/manifest.json`, `t05.file_md5`) equal the md5 of the upstream patch
files, so a rebuild against any faithful mirror of the website repository is checked the same way.

When an input is missing, its check (`tests/check_t02.py`, `check_t03.py`, `check_t05.py`) skips instead of
failing. `unittest` then prints `OK (skipped=N)`; a runner that treats any line starting with `OK` as a pass (the
hub runner does) must not run these tasks without the inputs.

- `tests/check_tNN.py`: `unittest` ports of each task's `tests/test_outputs.py`.
