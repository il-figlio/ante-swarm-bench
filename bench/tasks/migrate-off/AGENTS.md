# AGENTS.md

Notes for Codex sessions in this repo. Agent config: `.codex/config.toml`.

## Repo

devpulse — a stdlib-only Python CLI (`python3 -m devpulse`) for standup
notes and changelog entries. Package: `devpulse/`. Helper scripts:
`scripts/`. Sample data: `.devpulse/notes.jsonl`.

## Conventions

- No third-party dependencies; keep it stdlib-only.
- Run the unit tests before committing.
- Commit subjects in imperative mood, 72 chars or fewer.
- CHANGELOG.md is generated — don't edit release sections by hand.
- Keep changes small; ask before restructuring anything.
