# Backup routine

The 3-2-1 thing, done with local hardware. Covers everything on the
NAS including the [family photos](../projects/family-photos.md)
project and this notes repo.

## Layout

- primary: NAS mirror (2x4 TB)
- local copy: USB disk, weekly rsync, Sunday evening
- off-site: second USB disk at parents', swapped on visits
  (~every 6–8 weeks)

## Schedule

- Sun 21:00: `rsync -a --delete nas/ usb/` — commands in
  [cli cheat sheet](../resources/cli-cheat-sheet.md)
- monthly: verify by reading back a random folder, not just trusting
  rsync's exit code
- quarterly: spin up the off-site disk and check SMART

## Tested restores

- 2026-05: restored the whole notes dir from USB after a fat-fingered
  `rm -rf` — 4 min, everything back. Worth every habit above.
#tech #local
