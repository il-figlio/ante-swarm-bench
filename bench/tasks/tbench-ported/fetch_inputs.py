#!/usr/bin/env python3
"""Fetch and verify the third-party inputs of the tbench-ported pack. Standard library plus `git` and `tar`.

The pack's large or binary inputs are not committed. They come from the public Terminal-Bench task trees
(Apache-2.0, see ATTRIBUTION.md), pinned by commit, and every download is checked against a sha256 recorded here:

  trunc.db              t02  verbatim: terminal-bench-2-1 tasks/sqlite-db-truncate/environment/trunc.db
  log.stack             t03  verbatim: the log.stack member of terminal-bench-1 original-tasks/count-call-stack/log.stack.zip
  personal-site.tar.gz  t05  built: upstream fix-git has no archive; its setup.sh clones a personal website
                             repository and commits two patch files on a detached HEAD. This script repeats that
                             setup (patch files fetched and verified) and packs the result.

personal-site.tar.gz cannot be fetched faithfully at the moment: the repository that upstream fix-git clones
(github.com/TheMikeMerrill/personal-site) returned "Repository not found" on 2026-11-18. When the clone fails the
script says so and stops. If you hold the copy that shipped with the private source of this pack (a reduced rebuild
of the upstream state, repacked with the synthetic identity "Test User <test@example.com>"), pass it with
--archive: it is accepted only if its sha256 matches REPACKED_SHA256 below. Without the archive, check_t05 skips.

    python3 fetch_inputs.py                       # all three; t05 needs the upstream clone to work
    python3 fetch_inputs.py --only trunc.db log.stack
    python3 fetch_inputs.py --archive /path/to/personal-site.tar.gz
    python3 fetch_inputs.py --repo URL --rev SHA  # rebuild t05 from a mirror of the upstream repository
"""
import argparse
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TB2 = "https://raw.githubusercontent.com/harbor-framework/terminal-bench-2-1/12b7565679ce5006230b820666353673a133db93/tasks"
TB1 = "https://raw.githubusercontent.com/harbor-framework/terminal-bench-1/d28711d0da2675d0bb1d56de45ae5df6082438a3/original-tasks"

# sha256 of the upstream files at the pinned commits, recorded 2026-11-18 (their git blob ids matched the upstream trees).
TRUNC_DB = (TB2 + "/sqlite-db-truncate/environment/trunc.db",
            "a7f00ee232fe621629dc014eb2619ac793131843ede1fd892f3b8b7e17332a5b")
LOG_ZIP = (TB1 + "/count-call-stack/log.stack.zip",
           "b5feedd84f28b28e3a76d469fe3afb6b9984ee075c4a035c3c5904197ec3b6c1")
LOG_STACK_SHA256 = "6f73619ff6956bc1ee1ae2a9c6813cfb792c4b058eeb42e54a26da6e4339ed40"
PATCH_FILES = {  # fix-git/environment/resources/patch_files/<name> -> path in the site
    "about.md": ("_includes/about.md", "2e09260b011012f9f7f491cd02809903449924cf8f825acd369fb8990b04e768"),
    "default.html": ("_layouts/default.html", "fb3fad17b83338002f5fd6a99ea1ec3a662cea570f9fff9240b8abc1861be655"),
}
SITE_REPO = "https://github.com/TheMikeMerrill/personal-site.git"  # from upstream fix-git/environment/setup.sh
SITE_REV = "d7d3e4b"                                                # `git reset --hard d7d3e4b` in that script
# The archive that shipped with the private source of this pack: not an upstream file. It is a reduced rebuild of
# the upstream state (resources/ binaries dropped), repacked with a synthetic git identity during curation.
REPACKED_SHA256 = "c15f0ce5a5fca4ee0a54d2401e9cf48103106079913e31cf6087094aeca56b5f"
IDENTITY = {"GIT_AUTHOR_NAME": "Test User", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test User", "GIT_COMMITTER_EMAIL": "test@example.com"}  # as upstream setup.sh


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def get(url: str, want: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "fetch-inputs"}), timeout=60) as r:
        data = r.read()
    got = sha256(data)
    if got != want:
        raise SystemExit(f"sha256 mismatch for {url}\n  expected {want}\n  got      {got}")
    return data


def write(name: str, data: bytes):
    tmp = HERE / (name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, HERE / name)
    print(f"{name}: ok ({len(data)} bytes, sha256 {sha256(data)})")


def fetch_trunc_db():
    write("trunc.db", get(*TRUNC_DB))


def fetch_log_stack():
    with zipfile.ZipFile(io.BytesIO(get(*LOG_ZIP))) as z:
        data = z.read("log.stack")
    if sha256(data) != LOG_STACK_SHA256:
        raise SystemExit("log.stack inside the upstream zip does not match its recorded sha256")
    write("log.stack", data)


def git(cwd, *args, env=None):
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       env={**os.environ, **IDENTITY, **(env or {})})
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()[-300:]}")
    return p.stdout


def build_site(repo: str, rev: str):
    """Repeat upstream fix-git's setup.sh in a temporary directory and pack personal-site/ as personal-site.tar.gz."""
    patches = {name: get(f"{TB2}/fix-git/environment/resources/patch_files/{name}", want)
               for name, (_, want) in PATCH_FILES.items()}
    with tempfile.TemporaryDirectory(prefix="fetch-inputs-") as tmp:
        site = Path(tmp) / "personal-site"
        clone = subprocess.run(["git", "clone", "--quiet", repo, str(site)], capture_output=True, text=True,
                               env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if clone.returncode != 0:
            raise SystemExit(
                f"personal-site.tar.gz: cannot clone {repo} ({clone.stderr.strip().splitlines()[-1] if clone.stderr.strip() else 'error'}).\n"
                "The repository that upstream fix-git clones is not publicly available, so the t05 input cannot be\n"
                "rebuilt from the upstream source. Use --archive with the repacked copy, or --repo with a mirror.\n"
                "Without it, tests/check_t05.py skips.")
        git(site, "remote", "rm", "origin")
        git(site, "reset", "--hard", rev)
        git(site, "checkout", "HEAD~1")
        for name, (dest, _) in PATCH_FILES.items():
            (site / dest).write_bytes(patches[name])
        git(site, "add", "-A")
        git(site, "commit", "-m", "Move to Stanford")
        git(site, "checkout", "master")
        out = HERE / "personal-site.tar.gz.part"
        subprocess.run(["tar", "-czf", str(out), "-C", tmp, "personal-site"], check=True)
        os.replace(out, HERE / "personal-site.tar.gz")
    data = (HERE / "personal-site.tar.gz").read_bytes()
    print(f"personal-site.tar.gz: built from {repo} at {rev} ({len(data)} bytes, sha256 {sha256(data)}; "
          "a fresh build does not match REPACKED_SHA256, which is expected)")


def install_archive(path: str):
    data = Path(path).read_bytes()
    if sha256(data) != REPACKED_SHA256:
        raise SystemExit(f"{path}: sha256 {sha256(data)} is not the recorded repacked archive {REPACKED_SHA256}")
    write("personal-site.tar.gz", data)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="+", choices=["trunc.db", "log.stack", "personal-site.tar.gz"])
    ap.add_argument("--archive", help="a local copy of the repacked personal-site.tar.gz (verified by sha256)")
    ap.add_argument("--repo", default=SITE_REPO, help="repository to clone for t05 (default: upstream's)")
    ap.add_argument("--rev", default=SITE_REV, help="commit to reset to before the detached commit")
    a = ap.parse_args()
    want = a.only or ["trunc.db", "log.stack", "personal-site.tar.gz"]
    if "trunc.db" in want:
        fetch_trunc_db()
    if "log.stack" in want:
        fetch_log_stack()
    if "personal-site.tar.gz" in want:
        if a.archive:
            install_archive(a.archive)
        else:
            if not shutil.which("git"):
                raise SystemExit("personal-site.tar.gz: git is required to rebuild it")
            build_site(a.repo, a.rev)


if __name__ == "__main__":
    sys.exit(main())
