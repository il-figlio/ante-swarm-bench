#!/usr/bin/env python3
"""Style gate for the tally package.

The rules, concretely:

  * indentation is spaces only, in multiples of 4 — no tabs
  * no trailing whitespace
  * every file ends with exactly one newline
  * string literals are double-quoted (single quotes are tolerated only
    when the string itself contains a double quote)

Usage: ``python3 tools/check_style.py [path ...]``. With no arguments it
checks the ``tally/`` package. Prints one ``path:LINE: message`` per
violation and exits 1 when any are found, 0 when the tree is clean.
"""
import io
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRING_RE = re.compile(r"(?i)([rbuf]*)('''|\"\"\"|'|\")")


def display(path):
    """Repo-relative path when possible, for readable violations."""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def iter_paths(args):
    """Files to check: every .py under each directory argument."""
    for arg in args or [str(ROOT / "tally")]:
        path = Path(arg)
        if path.is_dir():
            yield from sorted(path.rglob("*.py"))
        elif path.is_file():
            yield path


def logical_start_lines(tokens):
    """Line numbers that begin a logical line — the only places where
    indentation depth is meaningful. Lines inside brackets are
    continuation lines and may indent however they like."""
    starts = set()
    expect = True
    for tok in tokens:
        if tok.type in (tokenize.COMMENT, tokenize.NL):
            continue
        if expect and tok.type not in (tokenize.INDENT, tokenize.DEDENT,
                                       tokenize.NEWLINE,
                                       tokenize.ENDMARKER):
            starts.add(tok.start[0])
            expect = False
        if tok.type == tokenize.NEWLINE:
            expect = True
    return starts


def single_quoted(tok_string):
    """True when the literal is single-quoted without needing to be —
    a ``"`` inside the string makes single quotes legitimate."""
    m = STRING_RE.match(tok_string)
    if not m or not m.group(2).startswith("'"):
        return False
    inner = tok_string[m.end():-len(m.group(2))]
    return '"' not in inner


def violations(path):
    """All style violations in one file as (lineno, message) pairs."""
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [(0, "file is not UTF-8")]
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError) as exc:
        return [(0, "file does not tokenize: %s" % exc)]
    starts = logical_start_lines(tokens)

    out = []
    for i, line in enumerate(text.split("\n"), 1):
        leading = line[: len(line) - len(line.lstrip(" \t"))]
        if line != line.rstrip():
            out.append((i, "trailing whitespace"))
        if "\t" in leading:
            out.append((i, "tab indentation"))
        elif i in starts and len(leading) % 4:
            out.append((i, "indent of %d is not a multiple of 4"
                        % len(leading)))
    for tok in tokens:
        if tok.type == tokenize.STRING and single_quoted(tok.string):
            out.append((tok.start[0], "single-quoted string literal"))

    if not data.endswith(b"\n"):
        out.append((len(text.split("\n")), "no newline at end of file"))
    elif data.endswith(b"\n\n"):
        out.append((len(text.split("\n")) - 1,
                    "extra blank line at end of file"))
    if "\r" in text:
        out.append((0, "carriage-return line endings"))
    return out


def main(argv):
    problems = []
    for path in iter_paths(argv):
        for lineno, msg in violations(path):
            problems.append((path, lineno, msg))
    for path, lineno, msg in problems:
        print("%s:%d: %s" % (display(path), lineno, msg))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
