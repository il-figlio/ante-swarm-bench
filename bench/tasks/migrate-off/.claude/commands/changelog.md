---
description: Cut a release by rolling CHANGELOG.md's Unreleased section into a version
argument-hint: "--version X.Y.Z [--date YYYY-MM-DD]"
allowed-tools: Bash(python3 scripts/bump_changelog.py:*), Bash(git diff:*)
---

Run the changelog bumper with the arguments I passed:

!`python3 scripts/bump_changelog.py $ARGUMENTS`

Then show me `git diff CHANGELOG.md` so I can eyeball the release notes.
CHANGELOG.md is only ever written by the script — never hand-edit it.
