"""Hashing, canonical JSON and durable writes shared by every module."""
import hashlib
import json
import os
import time
from pathlib import Path

SKIP_NAMES = {".DS_Store", "__pycache__"}


def canonical(value) -> bytes:
    # surrogateescape keeps non-UTF-8 filenames (as Python decodes them) hashable.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "surrogateescape")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def tree_files(root: Path) -> dict[str, str]:
    """Relative path -> sha256 for every regular file under root."""
    root = Path(root)
    found = {}
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_NAMES for part in path.parts) or path.name.startswith("._"):
            continue
        if path.is_file() and not path.is_symlink():
            found[str(path.relative_to(root))] = sha256_file(path)
    return found


def snapshot(root: Path) -> dict[str, str]:
    """Every entry under root, nothing skipped: files by content and mode,
    symlinks by target, directories by mode. Used to detect what an agent changed."""
    root = Path(root)
    found = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            relative = str(path.relative_to(root))
            info = path.lstat()
            mode = oct(info.st_mode & 0o7777)
            if path.is_symlink():
                found[relative] = "link:" + os.readlink(path)
            elif path.is_dir():
                found[relative] = "dir:" + mode
            elif path.is_file():
                found[relative] = f"file:{mode}:{sha256_file(path)}"
            else:
                found[relative] = "special:" + oct(info.st_mode)
    return found


def path_matches(relative: str, pattern: str) -> bool:
    """Glob where * and ? stay inside one path segment and ** spans segments."""
    import fnmatch

    parts, globs = relative.split("/"), pattern.split("/")

    def match(i: int, j: int) -> bool:
        if j == len(globs):
            return i == len(parts)
        if globs[j] == "**":
            return any(match(k, j + 1) for k in range(i, len(parts) + 1))
        return i < len(parts) and fnmatch.fnmatchcase(parts[i], globs[j]) and match(i + 1, j + 1)

    return match(0, 0)


def tree_hash(root: Path) -> str:
    return sha256_bytes(canonical(tree_files(root)))


def write_json(path: Path, value) -> None:
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def append_jsonl(path: Path, value) -> None:
    with Path(path).open("a") as stream:
        stream.write(json.dumps(value, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_jsonl(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
