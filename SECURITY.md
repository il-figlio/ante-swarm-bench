# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**. Do not open a public issue for a security problem.

Include what you found, how to reproduce it, and what an attacker could do with it.

## Scope

In scope:

- The run hub (`hub/`): its HTTP API, console and login handling.
- The swarm services (`swarm/`): lanes, the relay, the gateway, the front page and live play, including their per-lane tokens and passcode checks.

The hub and every swarm service bind to `127.0.0.1` by default. Exposing one beyond localhost (for example behind a reverse proxy) is an operator decision; see the deploy and `serve/` examples.

Out of scope: the process sandbox is not a security boundary (same user, filesystem and network). Run untrusted agents in a VM. Third-party harnesses configured by the adapters (Ante, Claude Code, Codex CLI, Hermes, Pi, nanocode) should be reported to their owners.

## Supported versions

Only `main` is supported. Fixes land on `main`; there are no release branches.

## Pre-release scan

Every export is scanned before release. Procedure: every tracked file is grepped for the patterns below, then scanned with detect-secrets; each remaining hit is reviewed by hand. The numbers come from the latest scan, on 2026-11-18 over this commit's tree:

- **Patterns:** private-key headers; `sk-`/`ghp_`/`github_pat_`/`xox`/AKIA tokens; 64-hex values next to "passcode"; e-mail addresses; `/Users/`, `/home/`, and `/Volumes`; platform and private repository names; personal names; and planning file names. The remaining hits are deliberate. They include hostile strings in redaction tests (synthetic home paths such as `/home/exedev/...` and `/Users/someone`, `sk-ant-api03-abcdefghijklmnop`-style fake keys, and `*.exe.xyz` host names); placeholder keys the adapters hand to agents (`sk-ant-swarm-local-no-key` and `sk-ant-bench-dummy-not-a-credential`); `example.com` addresses only; and exe.dev platform names (`X-ExeDev-Email`, `ssh exe.dev`, `*.exe.xyz`, `/__exe.dev/login`) that `hub/` integrates with. The cell domain and login URL are configuration (`SWARM_CELL_DOMAIN`, `SWARM_LOGIN_URL`). Upstream Terminal-Bench authors are credited by name in `bench/tasks/tbench-ported/ATTRIBUTION.md`, without e-mail addresses.
- **detect-secrets 1.5.0** (`detect-secrets scan --all-files`, run from a throwaway virtualenv outside the tree): 155 findings in 25 files, all reviewed as false positives. These comprise 137 hex strings (sha256 answer manifests, protocol pins, the fetch script's recorded hashes, and synthetic test ids), 13 keyword hits (placeholder keys and environment-variable names such as `secret_env`), and 5 base64 hits (ULID alphabets).
- **Archives and media:** no archives. The binary files are the two Spec Mono `.woff2` fonts and the GIFs and PNGs in `docs/media/`, screen captures of the pages in this repository and of the public bench and race pages, written without metadata (no EXIF, XMP or comments). The Terminal-Bench `personal-site.tar.gz` (a third party's website with a nested `.git`) is no longer committed; `fetch_inputs.py` fetches or rebuilds it.
- **Size:** 649 files; the largest are the README media in `docs/media/` (up to 1.5 MB); the largest text file is `bench/studies/tool-replay/goldens.json` (88 KB).

