# Bench protocol shared-v3

This is the live-model task protocol for the 2026 Q4 cohort. The Ante, Pi, Hermes and Ante-Pi matrix (GPT-6.1 Sol and Terra, `shared-v2-20261030`) used shared-v2. The Codex CLI matrix (`shared-v3-codex-20261031`) used shared-v3. It keeps shared-v2's worker, Responses relay route, tasks, timeouts, RSS sampling, checker gate and per-run home cleanup, and adds the changes listed below. The Claude matrix (`shared-v3-claude-20261031`) is defined here too. Results for these matrices are not in this repository; see the root README. The protocol runs on one dedicated 2 vCPU, 8 GB Linux VM, as an unprivileged bench user. It is installed under `/opt/ante-bench/`, with run data under `/var/lib/ante-bench/`.

- **Claude cohort:** Claude Sonnet 5.5 across Ante 0.3.0, Pi 1.4.0, Hermes v0.22.5 and Claude Code 2.1.312, through a new Anthropic Messages route on the relay. 4 harnesses x 2 levels (x1, x8) x 3 repeats = 24 cells.
- **Codex cohort:** Codex CLI 0.165.0 on `openai/gpt-6.1-sol` and `openai/gpt-6.1-terra`, medium, through the existing Responses route. 2 models x 2 levels x 3 repeats = 12 cells.

Each cell runs 32 attempts (4 toolbox tasks x 8), serially. It uses the shared-v2 timeouts (600 s task, 30 s checker), RSS sampling, the checker gate, a disk preflight (6 GiB), telemetry off, and per-run home cleanup. The `homes-manifest.json` is retained.

## What is new compared with shared-v2

- `model_gate.py` v3 adds `/<run>/<task>/v1/messages` and `/v1/messages/count_tokens`. Both are forwarded, and an upstream 404 is passed through. The Responses route works as in v2. The relay pins the exact model. A mismatch returns 422, with no fallback, and the request is never forwarded. The relay also forces the effort controls and the output cap. For Messages, it sets `max_tokens=16384`, `thinking={"type":"adaptive"}`, and `output_config.effort="medium"`. It removes `temperature`, `top_p` and `top_k`. If the client omits `anthropic-version`, the relay sets it to `2023-06-01`. It forwards `anthropic-beta` as sent, and it never forwards `x-api-key` or `Authorization`. SSE streams through without buffering. Audit rows record the model, requested and forced values, status, usage (including cache tokens), echoed model and stop reason. They never record prompts. The gateway URL comes from `BENCH_MODEL_UPSTREAM`. A relay port can be set with `--gate-port`. A fake upstream can be injected for tests only, with `--test-upstream`. The worker accepts that flag only when the matrix id contains `-test`.
- `worker.py` v3 supports harnesses `ante|pi|hermes|claude-code|codex` and protocols `shared-v3-claude` and `shared-v3-codex`. Cohort definitions live in `protocol.json`. It also reads the per-harness configs below and `provider_route` per cohort. Token totals are normalised to "total input including cached tokens" for both wires. Receipts also record `protocol`, `wire`, `effort_mapping` and `protocol_sha256`. `ante-pi` is not part of v3.
- `usage_tracker.py`: v2 copy that also counts Anthropic cache tokens.
- `install-v3.sh`, `probe-claude.sh`, `gate-v3.sh`, `run-v3-matrix.sh`, `summarize_run.py`, and the relay tests, now at `../../tests/bench/`.

## Effort mapping for Claude ("medium")

Sonnet 5.5 supports adaptive thinking only. `thinking: {type: "enabled", budget_tokens: N}` gets an HTTP 400, and so do `{type: "disabled"}` and non-default sampling values. The only depth control is `output_config.effort` (`low|medium|high|xhigh|max`, default `high`). So for every Claude harness, "medium" means:

    thinking = {"type": "adaptive"}      output_config.effort = "medium"      max_tokens = 16384

`medium` is the same label the GPT cohorts use. It is also the level Anthropic suggests as a starting point for agentic coding on Sonnet 5.5. But it is only a label, not an equal amount of reasoning. The Claude and GPT effort scales differ. The relay overwrites these fields on every request, whatever the harness sent. The audit keeps both values. In local tests, Pi asked for `enabled` with an 8192 budget. That would get a 400 on Sonnet 5.5, and the relay corrects it. Hermes and Ante sent adaptive. Claude Code sent `max_tokens=128000`, plus adaptive and its own effort. Thinking display stays at the default (omitted). Display does not change thinking or billing.

## Provider routes and billing

Claude runs go through the managed model gateway, `anthropic(gateway)`, and are billed from gateway credits. GPT runs use `openai(chatgpt)`, which is the ChatGPT subscription. These are separate routes with different rate limits and backends. Each receipt records its route in `provider_route`: "managed model gateway · anthropic(gateway)" or "· openai(chatgpt)". The original receipts show the platform's name for the gateway. Compare harnesses only within a cohort. Claude and GPT timings are not a pure model comparison.

## Per-harness configuration

| Harness | Messages (Claude) | Responses (GPT) |
| --- | --- | --- |
| Ante 0.3.0 | `.ante/catalog.json` provider `exe` (the gateway's catalog name): `wire_style: AnthropicMessage`, `base_url = <relay>/<run>/<task>/v1`, `credential: null`. Flags same as v2 (`--effort medium --no-skills --disable-auto-memory --no-session-save --output-format json`) | Unchanged from v2 (`OpenAiResponse`) |
| Pi 1.4.0 | `.pi/agent/models.json`: `api: anthropic-messages`, `baseUrl = <relay>/<run>/<task>` (the SDK appends `/v1/messages`), `apiKey: exe-integration` (a placeholder the relay strips). Flags same as v2. | Unchanged from v2 (`openai-responses`) |
| Hermes v0.22.5 | `config.yaml` provider `custom:exe`, `transport: anthropic_messages`, `base_url = <relay>/<run>/<task>`. Flags same as v2. | Unchanged from v2 (`codex_responses`) |
| Claude Code 2.1.312 | `ANTHROPIC_BASE_URL=<relay>/<run>/<task>`, dummy `ANTHROPIC_API_KEY`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_TELEMETRY=1`, `DISABLE_AUTOUPDATER=1`, fresh `HOME` and `CLAUDE_CONFIG_DIR`. Run with `claude -p --dangerously-skip-permissions --output-format json --model <id> --effort medium --no-session-persistence --disable-slash-commands --strict-mcp-config <prompt>` | n/a |
| Codex CLI 0.165.0 | n/a | A fresh `CODEX_HOME` per task with a `config.toml`. `model_providers.relay` sets `base_url = <relay>/<run>/<task>/v1`, `wire_api = "responses"`, `requires_openai_auth = false`, `supports_websockets = false`. `model_reasoning_effort = "medium"`. Analytics, feedback, and the update check are off. Run with `codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox --ephemeral --ignore-rules --json -C <workspace> -m <model> <prompt>` |

## Fair-play flags

Claude Code: every model slot (`ANTHROPIC_MODEL`, the Sonnet/Opus/Haiku/Fable defaults, `ANTHROPIC_SMALL_FAST_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL`) is pinned to the benchmark model, so no background request names a different one. If one did, the relay would return 422 and the task would fail. `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` matches Ante's `--disable-auto-memory`. `--disable-slash-commands` matches `--no-skills`. `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` limits the `anthropic-beta` set. Its system prompt, tool set (Bash, Edit, Read, Write, Grep, Glob, Task and others), and sub-agents stay as shipped. It runs with permissions bypassed. Subscription/OAuth login is not used. It runs on a dummy API key against the relay.

Codex: `--dangerously-bypass-approvals-and-sandbox` is on because each task is already isolated by workspace and home. Request compression is off because the relay has to read the JSON. Websockets are off, and the run uses `--ephemeral` and `--ignore-rules`. `web_search` is disabled. Also off: project docs (`AGENTS.md` discovery, `project_doc_max_bytes = 0`), bundled system skills (`skills.bundled.enabled = false`), apps, plugins, memories, goals, browser/computer use, image generation, fast modes, hooks, the sleep tool, and multi-agent features.

Observed with 0.165.0: for these GPT-6 ids, Codex exposes its tools through "code mode," a single JavaScript `exec` tool that calls the shell (`tools.exec_command`), plus `wait`, `request_user_input`, and `collaboration.*` tools. This version does not let config turn them off. So Codex's tool surface and system prompt are as shipped, and they differ from the plain terminal and file tools in the other harnesses. Treat Codex results as "Codex as shipped," not as a controlled tool-parity comparison.

All harnesses: the task pack, prompts, 600 s deadline, checker gate, and 16384 output cap are identical.

## Third-party CLIs

Codex CLI (`@openai/codex`) and Claude Code (`@anthropic-ai/claude-code`) are third-party products used unmodified at pinned versions, each under its own license and terms (Claude Code: "SEE LICENSE IN README.md" in its package; Codex: see its repository). The versions are 0.165.0 and 2.1.312. They were the newest releases at least three days old on 2026-10-30 (npm `min-release-age`); newer releases 0.166.0 and 2.1.314 to 2.1.316 existed.

## Local proof (fake upstream, no model calls)

From the repository root, `python3 tests/bench/test_relay.py` runs the relay against `tests/bench/fake_upstream.py`. The fake upstream supports Messages and Responses streaming, with a scripted tool call followed by an end of turn. The test covers wrong-model 422s with no forwarding, forced `max_tokens` / thinking / effort, sampling removal, no auth headers forwarded, the `anthropic-version` default, SSE arriving before the stream ends, count_tokens forwarding, 404s, and an audit file without prompts. The worker was also run end to end through the relay on toolbox tasks with each harness (Ante 0.2.9 locally, since 0.3.0 is Linux-only here; Pi 1.4.0; Hermes v0.22.3; Claude Code 2.1.312; Codex 0.165.0), and `gate-v3.sh` was run against the fake upstream.

## Setup fix found by the first gate

The first gate showed Hermes making no relay requests on the Messages route. The shared-v2 Hermes venv was built with `uv sync --frozen` without the `anthropic` extra, so `transport: anthropic_messages` had no SDK to import. The fix affects setup only; the frozen shared-v2 venv is untouched. `install-v3.sh` installs the two upstream-locked packages `anthropic==0.87.0` and `docstring-parser==0.18.0` (`--no-deps`, versions from Hermes v0.22.5's `uv.lock`) into `hermes-extra/`, and `worker.py` appends that directory to Hermes's `PYTHONPATH` when it exists. The Responses path is unaffected.

## Sequence

```sh
./install-v3.sh                                   # pinned Codex CLI + Claude Code; refuses while another bench runs
BENCH_MODEL_UPSTREAM=https://<gateway> ./probe-claude.sh   # one 16-token request: model id and effort mapping
./gate-v3.sh both                                 # 6 harness/model pairs x 4 tasks; every pair must pass 4/4
./run-v3-matrix.sh claude && ./run-v3-matrix.sh codex       # serial; progress logs are rotated, never overwritten
```

Then `../build-cohorts.py --runs <receipt package> --log <progress log> --out cohorts.json` turns the run receipts and progress logs into a cohort summary. It cross-checks every cell and stops on any disagreement.

## Receipts and these files

The receipts record `worker_sha256`, `relay_sha256` and `protocol_sha256`. Three files here differ from the frozen copies by a redaction only: the gateway hostname became `BENCH_MODEL_UPSTREAM`, and provider-route labels name a "managed model gateway". The frozen hashes were:

| File | sha256 of the frozen copy |
|---|---|
| `worker.py` | `6c59c1a38c174fde840f66cdec73d73b82e5e9af095aad58b838c499f361c66f` |
| `model_gate.py` | `6da39d9c611b9d7ddade94c617d12e7c32bdd55d000626076363ab68369b6e55` |
| `protocol.json` | `23ba12bfbe07ed52990169199658b817c768c6d24192a3f59f39614d73540999` |
| `usage_tracker.py` | `9f93a69f0a269657de7af815449ae3ea5d1c45c39c0b25762a35c4c49fb690d5` |
