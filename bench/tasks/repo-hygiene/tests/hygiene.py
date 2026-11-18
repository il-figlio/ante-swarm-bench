"""Shared helpers for the repo-hygiene checks.

Two things live here:

* Source fingerprints used by the "behaviour unchanged" checks — an
  ``ast.dump`` hash, plus a normalized token-stream hash as a fallback
  for interpreters whose AST dumps differ (node schemas shift between
  Python versions; the token stream for this code does not).

* A deliberately small YAML-subset parser — nested maps, lists, flow
  collections, ``|``/``>`` block scalars, quoted scalars, comments —
  enough to check a GitHub workflow file semantically without PyYAML.
"""
import ast
import hashlib
import io
import json
import re
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_MANIFEST_PATH = ROOT / "tests" / "manifest.json"
MANIFEST = (json.loads(_MANIFEST_PATH.read_text())
            if _MANIFEST_PATH.is_file() else {})


def rel(path):
    return Path(path).resolve().relative_to(ROOT).as_posix()


def function_names(path):
    """Top-level def/async-def names in a module, in source order."""
    tree = ast.parse(Path(path).read_text())
    return [n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def ast_hash(path):
    """sha256 of the module's ast.dump — structure + string values."""
    tree = ast.parse(Path(path).read_text())
    return hashlib.sha256(ast.dump(tree).encode()).hexdigest()


_STRUCT = {tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE,
           tokenize.ENDMARKER}
_SKIP = {tokenize.NL, tokenize.COMMENT, tokenize.ENCODING}
_QUOTED = re.compile(r"(?i)([rbuf]*)('''|\"\"\"|'|\")(.*)\1\s*$", re.S)


def token_fingerprint(text):
    """Normalized token stream: whitespace/comments/quote style ignored,
    block structure (INDENT/DEDENT/NEWLINE events) and literal values
    preserved. A pure reformat produces an identical fingerprint."""
    parts = []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        ttype = tok.type
        if ttype in _SKIP:
            continue
        if ttype in _STRUCT:
            parts.append(tokenize.tok_name[ttype])
        elif ttype in (tokenize.STRING, tokenize.NUMBER):
            try:
                parts.append(repr(ast.literal_eval(tok.string)))
            except (ValueError, SyntaxError):
                m = _QUOTED.match(tok.string)
                parts.append(m.group(3)[:-len(m.group(2))]
                             if m else tok.string)
        else:
            parts.append(tok.string)
    return "|".join(parts)


def token_hash(path):
    return hashlib.sha256(
        token_fingerprint(Path(path).read_text()).encode()).hexdigest()


def run_suite():
    """Run the repo's own test suite (the same command CI should use)."""
    return subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests",
         "-t", ".", "-p", "test_*.py", "-q"],
        cwd=ROOT, capture_output=True, text=True, timeout=180)


# --- YAML subset --------------------------------------------------------

def _strip_comment(line):
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote is not None:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        else:
            out.append(ch)
    return "".join(out)


def _unquote(text):
    text = text.strip()
    if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
        return text[1:-1]
    return text


def _split_flow(text):
    """Split a flow collection body on top-level commas."""
    parts, cur, depth, quote = [], [], 0, None
    for ch in text:
        if quote is not None:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch in "[{":
            depth += 1
            cur.append(ch)
        elif ch in "]}":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts]


def _scalar(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == "[" and text[-1] == "]":
        inner = text[1:-1].strip()
        return [_scalar(p) for p in _split_flow(inner)] if inner else []
    if len(text) >= 2 and text[0] == "{" and text[-1] == "}":
        inner = text[1:-1].strip()
        out = {}
        for part in _split_flow(inner) if inner else []:
            key, _, val = part.partition(":")
            out[_unquote(key)] = _scalar(val)
        return out
    return _unquote(text)


def _split_key(content):
    """``key: value`` -> (key, value) at top level, else None."""
    depth, quote = 0, None
    for i, ch in enumerate(content):
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif (ch == ":" and depth == 0
              and (i + 1 == len(content) or content[i + 1] in " \t")):
            return content[:i].strip(), content[i + 1:].strip()
    return None


def _is_item(content):
    return content == "-" or content.startswith("- ") or \
        content.startswith("-\t")


def parse_yaml(text):
    """Parse the workflow-YAML subset into nested dicts/lists/strs.

    Raises ValueError on tab indentation (illegal in YAML anyway)."""
    lines = []
    for raw in text.splitlines():
        s = _strip_comment(raw).rstrip()
        if not s.strip() or s.strip() in ("---", "..."):
            continue
        if "\t" in s[: len(s) - len(s.lstrip())]:
            raise ValueError("tab in indentation")
        stripped = s.lstrip(" ")
        lines.append((len(s) - len(stripped), stripped))
    if not lines:
        return {}
    obj, _ = _node(lines, 0, lines[0][0])
    return obj


def _node(lines, i, indent):
    if i < len(lines) and lines[i][0] == indent and _is_item(lines[i][1]):
        return _list(lines, i, indent)
    return _map(lines, i, indent)


def _map(lines, i, indent):
    out = {}
    while i < len(lines):
        ind, content = lines[i]
        if ind < indent or (ind == indent and _is_item(content)):
            break
        if ind > indent:
            i += 1  # continuation of a plain scalar; ignore the tail
            continue
        kv = _split_key(content)
        if kv is None:
            i += 1
            continue
        key, val = _unquote(kv[0]), kv[1]
        i += 1
        if val in ("|", "|-", "|+", ">", ">-", ">+"):
            buf = []
            while i < len(lines) and lines[i][0] > indent:
                buf.append(lines[i][1])
                i += 1
            out[key] = ("\n" if val.startswith("|") else " ").join(buf)
        elif val:
            out[key] = _scalar(val)
        elif i < len(lines) and lines[i][0] > indent:
            out[key], i = _node(lines, i, lines[i][0])
        else:
            out[key] = None
    return out, i


def _list(lines, i, indent):
    out = []
    while i < len(lines):
        ind, content = lines[i]
        if ind != indent or not _is_item(content):
            break
        rest = content[1:].lstrip(" ")
        inner = ind + (len(content) - len(rest))
        if not rest:
            i += 1
            if i < len(lines) and lines[i][0] > indent:
                val, i = _node(lines, i, lines[i][0])
                out.append(val)
            else:
                out.append(None)
        elif _split_key(rest) is not None:
            lines[i] = (inner, rest)  # re-enter the item as a map
            val, i = _map(lines, i, inner)
            out.append(val)
        else:
            out.append(_scalar(rest))
            i += 1
    return out, i


def workflow_events(on):
    """Trigger names from a parsed ``on:`` value, whatever its shape."""
    if isinstance(on, dict):
        return {str(k) for k in on}
    if isinstance(on, list):
        return {str(v) for v in on}
    if isinstance(on, str):
        return {on}
    return set()


def workflow_steps(wf):
    """Every step dict across every job in a parsed workflow."""
    jobs = wf.get("jobs")
    if not isinstance(jobs, dict):
        return []
    steps = []
    for job in jobs.values():
        if isinstance(job, dict) and isinstance(job.get("steps"), list):
            steps.extend(s for s in job["steps"] if isinstance(s, dict))
    return steps
