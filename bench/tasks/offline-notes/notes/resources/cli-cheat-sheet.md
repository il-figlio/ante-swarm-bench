# CLI cheat sheet

Commands I keep re-googling; now I re-grep instead.

## Files

- `rsync -a --delete src/ dst/` — mirror; trailing slashes matter
- `rsync -an --delete src/ dst/` — dry run first, always
- `find . -name '*.md' -newer marker.txt` — changed since
- `du -sh */ | sort -h` — who's eating the disk

## Text

- `grep -rn "pattern" .` — the whole toolbox
- `sed -n '10,20p' file` — print a range
- `sort | uniq -c | sort -rn` — instant histogram

## System

- `python3 -m http.server 8000` — local preview (used by
  [studio website](../projects/studio-website.md))
- `systemctl --user status x` / `journalctl --user -u x -f`
- `sudo ufw status numbered` / `sudo ufw deny out to any` variants —
  firewall one-liners referenced from [home network](../projects/home-network.md)

## rsync-to-NAS

The Sunday backup command block lives in
[backup routine](../areas/backup-routine.md). #tech
