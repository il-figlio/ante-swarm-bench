# CLAUDE.md

Working notes for Claude sessions in this repo.

## What this is

devpulse — a stdlib-only Python CLI for standup notes and changelog entries.
The package lives in `devpulse/` (entry point `python3 -m devpulse`), the
deterministic helper scripts in `scripts/`, sample notes in `.devpulse/`.

## Rules

- **Stdlib only.** No third-party dependencies, ever. No network calls.
- **Style:** 4-space indents, single quotes, docstrings on public functions.
  CLI logic stays in `devpulse/cli.py`; storage in `devpulse/store.py`;
  anything that prints markdown in `devpulse/render.py`.
- **Tests:** run `make check` before every commit. It wraps
  `python3 -m unittest discover -s tests -t . -p 'test_*.py'`.
- **Hands off:** `CHANGELOG.md` is written only by
  `scripts/bump_changelog.py` — never hand-edit a release section. Don't
  touch `scripts/` or `tests/` unless I ask.
- **Commits:** subject line `area: change`, lowercase, 72 chars max. No
  `Co-Authored-By` trailers.
- **Data:** notes live in `.devpulse/` — `DEVPULSE_DATA_DIR` overrides the
  location, `DEVPULSE_DEFAULT_DAYS` the default digest window.

## Commands

`/digest`, `/label-pr` and `/changelog` (see `.claude/commands/`) wrap the
scripts in `scripts/`. Use them instead of running the scripts ad hoc.
