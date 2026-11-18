"""Shared helpers for the offline-notes checks.

The KB's linking convention (see README): links between notes are
relative markdown links ``[text](path.md)`` resolved against the linking
file's folder. No wiki-links, no absolute paths; targets end in ``.md``.
tags are inline ``#tag`` tokens.

The same regexes live in tools/gen_kb.py — keep them in sync.
"""
import hashlib
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTES_DIR = ROOT / "notes"
MANIFEST = json.loads((ROOT / "tests" / "manifest.json").read_text())

LINK_RE = re.compile(
    r"!?\[[^\]\n]*\]\(\s*(?:<([^>]+)>|([^)\s]+))(?:\s+[^)]*)?\)")
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
NORM_RE = re.compile(r"(\]\()\s*(?:<[^>]+>|[^)\s]+)((?:\s+[^)]*)?\))")
TAG_RE = re.compile(r"(?<![\w/])#([a-z0-9][a-z0-9_-]*)")
HEADING_RE = re.compile(r"^#{1,6}\s+(\S.*?)\s*$")


def link_target(m):
    """The raw target string of a LINK_RE match."""
    return m.group(1) if m.group(1) is not None else m.group(2)


def is_file_link(target):
    """True if the target should resolve to a file (not an external
    scheme: link or a bare #anchor)."""
    t = target.split("#", 1)[0]
    return bool(t) and not SCHEME_RE.match(t)


def link_path(src, target):
    """Repo-relative Path a file link points at, or None if the link
    escapes the repo entirely. The path may not exist — caller checks."""
    t = target.split("#", 1)[0]
    p = (src.parent / t) if not t.startswith("/") else (
        ROOT / t.lstrip("/"))
    p = Path(os.path.normpath(p))
    try:
        return p.relative_to(ROOT)
    except ValueError:
        return None


def note_files():
    """Every note under notes/, repo-relative, sorted."""
    return sorted(NOTES_DIR.rglob("*.md"))


def iter_links(path):
    """(lineno, match) for every markdown link in a file."""
    for i, line in enumerate(
            path.read_text(errors="replace").splitlines(), 1):
        for m in LINK_RE.finditer(line):
            yield i, m


def broken_links(paths):
    """(repo_rel_file, lineno, target) for every file link that does
    not resolve to an existing file under the repo."""
    out = []
    for f in paths:
        for i, m in iter_links(f):
            t = link_target(m)
            if not is_file_link(t):
                continue
            p = link_path(f, t)
            if p is None or not (ROOT / p).is_file():
                out.append((f.relative_to(ROOT).as_posix(), i, t))
    return out


def resolved_links(path):
    """{repo_rel_target: lineno} — the file links in `path` that do
    resolve (first occurrence wins on duplicates)."""
    out = {}
    for i, m in iter_links(path):
        t = link_target(m)
        if not is_file_link(t):
            continue
        p = link_path(path, t)
        if p is not None and (ROOT / p).is_file():
            out.setdefault(p.as_posix(), i)
    return out


def normalized(text):
    """Note content with every inline-link target blanked."""
    return NORM_RE.sub(r"\1\2", text)


def content_hash(path):
    return hashlib.sha256(
        normalized(path.read_text(errors="replace")).encode()
    ).hexdigest()


def tag_truth():
    """{tag: {repo_rel_file}} — the live #tag index over notes/."""
    truth = {}
    for f in note_files():
        for tag in TAG_RE.findall(f.read_text(errors="replace")):
            truth.setdefault(tag, set()).add(
                f.relative_to(ROOT).as_posix())
    return truth


def index_sections(path):
    """Parse a generated index-style file: [(section_name, lineno,
    link_match)] — every link paired with the `##` section it sits in.
    Section names are normalised to their last path component,
    casefolded."""
    rows = []
    section = None
    for i, line in enumerate(
            path.read_text(errors="replace").splitlines(), 1):
        h = HEADING_RE.match(line)
        if h:
            section = (h.group(1).rstrip("/").rsplit("/", 1)[-1]
                       .split()[0].casefold())
            continue
        for m in LINK_RE.finditer(line):
            rows.append((section, i, m))
    return rows
