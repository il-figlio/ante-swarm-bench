# Git cheat sheet

The six commands that cover 95% of what I do, plus the Pi remote
setup that pairs with [the home netwrok](../projects/home-netwrok.md).

## Daily

- `git status` / `git diff` / `git add -p` — stage hunks, not files
- `git log --oneline -15` — recent history
- `git stash` / `git stash pop` — rarely but precious

## Fixes

- `git checkout main -- path/` — restore a tracked subtree, e.g.
  after mangling generated files
- `git reset --soft HEAD~1` — uncommit, keep the work
- `git reflog` — the actual undo button

## Pi remote

- `git push pi main` — the NAS-adjacent mirror
- clone from it on a new machine; offline-first per
  [backup routine](../areas/backup-routine.md)

## Rule

Commit small, message in imperative, and never keep work uncommitted
overnight on a machine with one disk. #tech
