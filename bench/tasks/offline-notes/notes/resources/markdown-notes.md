# Markdown notes

How this whole notes folder is wired — conventions, not opinions.

## Links

- one convention only: relative markdown links —
  [like this one to health](../areas/health.md) — resolved relative
  to the note's folder
- no wiki-links (`[[...]]`), no absolute paths, no bare filenames —
  mixed conventions broke things once, once was enough
- link targets end in `.md`; headings are not linked, files are

## Tags

- `#tag` inline, lowercase, one or two per note max — they're for
  the tag index, not decoration
- folders carry the real structure; tags are just threads

## Files

- filename: lowercase-with-dashes.md, the title is the `#` heading
- daily notes: `YYYY-MM-DD.md` in `daily/`, freeform
- when a note outgrows its folder, split it — don't nest folders

## History

The whole tree is a git repo synced to the Pi — see
[home network](../projects/home-network.md) and
[git cheat sheet](git-cheat-sheet.md). #tech
