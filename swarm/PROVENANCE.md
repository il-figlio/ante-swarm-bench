# Provenance

Exported from a private monorepo at commit `9f6ae71a`. Only the committed files of the swarm directory are included, with no history.

The export was sanitized in two passes. The first set of edits went onto an earlier commit of the same directory (`da104be5`). The `da104be5..9f6ae71a` diff (team play) was then applied on top, with one hunk in `front-solo/trail.html` done by hand. A diff against the archive of `9f6ae71a` shows only the edits listed below.

The technical documentation and the public-ready edits were exported from a second private repository at commit `5bde926`: `docs/{evaluation,gateway,maze,solo,reference,live}.md`, `README.md`, `DESIGN.md`, `AGENTS.md` and `serve/*.example`, written from source commits `d7fc8200`/`dfb846ca`. Its hostname and wording edits to code and pages were re-applied as a patch (three hunks by hand).

## Added from the source working tree

The source repository ignores `*.log`, so 81 synthetic task fixtures were not tracked there. The tasks and tests need them. They are `tasks/log-triage/workspace/app.log`, `tasks/triage-20/workspace/logs/*.log` (20) and `tasks/triage-60/workspace/logs/*.log` (60). The triage logs are generated deterministically by `tools/make_tier0_tasks.py` and checked by `tests/test_tier0.py`. `.gitignore` here re-includes them.

## Excluded

- Dated results documents.
- Non-technical documents under `docs/`. Their technical content is in `docs/`.
- Deployment-specific service and nginx files. They are replaced by `serve/swarm-serve.service.example` and `serve/nginx.conf.example`.

## Changed from the source

- `package.json` is new: `{"type": "commonjs"}` only, so the Node-based tests can `require()` the browser scripts.
- `README.md`, `DESIGN.md`, `AGENTS.md`, `serve/README.md`: technical rewrites (see above).
- Hostnames and deployment names were removed from page text, help strings and comments. "Owner passkey/passcode" became "operator" in page text. Code identifiers (`Front.owner()`, `owner-lock.json`) are unchanged.
- `swarm solo`: `--play-upstream` has no default, since it named a private gateway, and it is required for `--play-provider relay`. The built-in weekly sign-in reset times (deployment data) were removed, so the fallback uses only `--play-signin-reset` values. No other behaviour changed.
- For this repository: page titles and cartridge brands say "swarm" instead of "ante". Ante is named only as the product under test. Every served page carries "Independent work, not an Antigma release." Comments that named a private desktop client and its source path were generalised. A comment quoting another harness's memory figure was made neutral. Comments, help text and docs describing the model path were made provider-neutral; behaviour is unchanged. The relay-path tokens in `tests/test_live.py` and `tests/test_solo.py` became obviously synthetic hex (`feedface…`, `deadbeef…`).

## License and notices

The project is Apache License 2.0, like the rest of this repository. See [`../LICENSE`](../LICENSE) and [`../NOTICE`](../NOTICE). The NOTICE file lists the third-party material kept here: spec-01 design values (MIT), Spec Mono (SIL OFL 1.1), and the Practical Data credit for `front/heat.js`. The full spec-01 notice is in `front-solo/NOTICE.txt`. The font licence is in `front-solo/fonts/OFL.txt`. The harness adapters configure third-party CLIs (Ante, Codex, Claude Code, Hermes, nanocode, Pi) but contain none of their code.
