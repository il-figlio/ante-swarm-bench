---
description: Suggest labels for a PR from its title and body
argument-hint: "--title \"...\" [--body \"...\"]"
allowed-tools: Bash(python3 scripts/label_pr.py:*)
model: claude-haiku-4-5
---

Run the labeller with the PR details I gave you:

!`python3 scripts/label_pr.py $ARGUMENTS`

Print the suggested labels, then one sentence each on why they apply. If the
script prints `needs-triage`, say so plainly and ask me which label to use.
