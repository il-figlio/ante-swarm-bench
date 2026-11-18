---
description: Turn the last week of devpulse notes into a standup digest
argument-hint: "[--days N]"
allowed-tools: Bash(python3 scripts/weekly_digest.py:*)
---

Run the digest script, forwarding any arguments I passed:

!`python3 scripts/weekly_digest.py $ARGUMENTS`

Turn the output into my standup update: blockers first, then one line per
theme grouped by tag. Keep the whole update under 10 lines.
