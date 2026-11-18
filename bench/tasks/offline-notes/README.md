# fieldnotes

Personal knowledge base — markdown notes, local files only, no sync
service, no cloud. Everything worth remembering lives under `notes/`
as plain `.md` files I can read with any editor on any machine.

## Layout

```
notes/
  projects/    things with an end state — builds, trips, the zine, exams
  areas/       standing responsibilities — health, money, house, dog
  resources/   reference material — recipes, cheat sheets, book notes
  daily/       dated journal + log entries, YYYY-MM-DD.md
tests/         ground-truth manifest and the bench checks
tools/         gen_kb.py — the corpus generator
```

## Conventions

- **Links between notes are relative markdown links**:
  `[text](../areas/moss-terrarium.md)`, resolved against the linking
  note's folder. One convention only — no wiki-links (`[[...]]`), no
  absolute paths, and link targets always end in `.md`. Files are
  linked, headings are not.
- **Tags** are inline `#tag` tokens, lowercase, one or two per note.
  Folders carry the real structure; tags are just threads.
- **Filenames** are lowercase-with-dashes; the note's title is its `#`
  heading.
- The tree is a git repo mirrored to the home Pi — see
  [the home-network note](notes/projects/home-network.md) and
  [the markdown conventions note](notes/resources/markdown-notes.md).

## Tooling

`tools/gen_kb.py` regenerates the whole corpus deterministically
(fixed seed, authored content) — it rewrites `notes/` and
`tests/manifest.json` byte-identically:

```
python3 tools/gen_kb.py
```

`tests/manifest.json` carries the ground truth the checks read: the
note list, a sha256 of each note with link targets blanked, and the
planted broken links with their intended targets. Agents can read the
manifest — every harness faces the same temptation; that is documented,
not prevented.

---

Bench pack: this repo is also a task pack. `tasks.json` maps tasks to
machine checks; each check runs from the repo root as
`python3 -m unittest tests.check_nNN -q` and must end in `OK`.
Standard library only — do not add dependencies, and nothing touches
the network.
