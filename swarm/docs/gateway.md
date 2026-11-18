# Real models: relay and gateway

The scripted model measures runtime cost without spending anything. To race harnesses on a real model, requests still go through Swarm, so each run leaves a receipt. The planned model, effort, and output cap are enforced. Each request's real token usage is recorded, and spend is capped per run.

## Shape

```
harness ──/s/<token>/v1/{responses,messages,chat/completions}──▶ relay (on the lane, 127.0.0.1)
                                                  │ checks model, effort, cap, request budget, run token budget
                                                  │ records status, usage, timing per request (same rows as scripted)
                                                  ▼
                                   upstream gateway (--relay-upstream / --gateway-upstream, HTTPS)
                                                  │ holds the provider credentials; the lane holds none
                                                  ▼
                                               provider
```

- **Relay** (`relay.py`, spec `model.source = "relay"` with `model.upstream`): serves the scripted model's per-attempt routes, so harness adapters stay as they are. Admission uses the scripted model's own checks: route, request budget, body size, and planned model, effort, and output cap. An admitted body is forwarded unchanged. The harness's auth header, `x-api-key`, and cookies are never forwarded. Only `content-type`, `accept`, `anthropic-version`, `anthropic-beta`, and `openai-beta` pass through. Streaming replies go back as they arrive. Usage comes from the final SSE event: `response.completed`, `message_delta`, or the Chat Completions usage chunk. For that last one, the relay sets `stream_options.include_usage` itself. `model.wire` picks the wire.
- **Gateway** (`gateway.py`, `swarm gateway --config gateway.json --log usage.jsonl`): provider adapters for credentials that can't sit behind a plain HTTPS proxy. The gateway holds the credentials. Each client (one per lane) presents its own token, and that token only unlocks the routes listed for it. Requests are routed by API (`responses`, `messages`, `count_tokens`, `chat`) and model. Bodies pass through unchanged, and replies stream back. Every request is logged with its token usage. Each client also has a daily token cap (`max_tokens_per_day`, default 5,000,000). On restart, today's spend is restored from the log.
- Both reuse upstream connections (`upstream.py`), so only the first request pays for TLS.

### `gateway.json`

```json
{
  "routes": {
    "<route>": {"upstream": "https://provider.example/v1-base", "auth": "bearer", "secret_env": "PROVIDER_KEY",
                "apis": ["responses", "chat"], "models": ["model-a"]}
  },
  "clients": [{"name": "lane-a", "token_sha256": "<sha256 of the lane's token>", "routes": ["<route>"],
               "max_tokens_per_day": 5000000}],
  "trust_source_vm": false
}
```

- `auth`: one of `claude-oauth` (a Claude sign-in token from `claude setup-token`; the example configuration gives this route only to the Claude Code lane), `bearer`, `anthropic-key` (`x-api-key`), or `integration` (the upstream is a proxy that injects the provider key itself, so the gateway adds no auth header).
- `upstream` must be `https://` (or `http://127.0.0.1` for tests). A route whose `secret_env` is unset is disabled, not fatal.
- `trust_source_vm`: turn this on only for a gateway reachable through a platform that sets a trusted, unforgeable `X-Exedev-Source-Vm` header. Clients are then matched by `source_vm` and hold no token.

## Guardrails

- Only the operator can launch real models, and only with the passkey in `SWARM_FRONT_PASSKEY`. Visitors get the scripted model. Wrong passkeys are counted. The last allowed miss locks real-model launches until `swarm front-unlock` clears it, and that needs a reset PIN the front service never loads.
- Per lane: `--live-model` / `--gateway-model` allowlists, `--live-max-size`, and `--live-max-tokens` (default 2,000,000 input + output tokens per run), or `--live-agent-tokens` per agent. Once a run is over budget, the relay answers 429 without calling upstream and records `run_token_budget_exhausted`.
- Per attempt: the request budget and timeout apply.
- A wrong model or effort is refused (422) before any upstream call. A cap above the plan's is refused. A lower cap is served and flagged (`cap_lowered`).
- No harness credentials or operator environment reach an attempt.
- Claude Code may send sub-agent work to a smaller "helper" model (`model.helper`). Those requests are admitted and count against the same run budget. They are marked `helper` in their rows. No other harness has a helper model.

## How each harness reaches the relay

Every adapter points its harness at the same per-attempt local relay. It is set up as a custom provider with a placeholder key. No harness uses its native login.

| Harness | Adapter setting (file) | Wire | Native alternative it does not use |
|---|---|---|---|
| Ante | `catalog.json` provider `swarm`, `wire_style` OpenAiResponse / OpenAiCompatible (`harnesses/ante.py`) | Responses, Chat | Ante catalog providers |
| Codex | `config.toml` `model_provider = "swarm"`, `wire_api = "responses"` (`codex.py`) | Responses | ChatGPT login (`auth.json`) |
| Claude Code | `ANTHROPIC_BASE_URL` + placeholder `ANTHROPIC_API_KEY` (`claude_code.py`) | Messages | Claude subscription OAuth |
| nanocode | `NANOCODE_OPENAI_BASE_URL` + placeholder key (`nanocode.py`) | Responses | its vendor providers |
| Hermes | `custom:swarm`, `api_mode` codex_responses / chat_completions (`hermes.py`) | Responses, Chat | its built-in providers |
| Pi | provider `swarm`, `api` openai-responses / openai-completions (`pi.py`) | Responses, Chat | its built-in providers |

## Known limitations of the method

1. **Native providers are bypassed for everyone.** Harnesses with built-in providers shape requests their own way: headers, caching hints, retries. On the custom provider they fall back to their generic path. No per-harness "native provider, base URL overridden" mode exists.
2. **Wire coverage differs.** Codex speaks only Responses and Claude Code only Messages, so Chat-only models cannot run on them. The relay does not translate between wires. A translator would be Swarm code inside the harness's loop.
3. **Strict settings checks catch real harness bugs.** For example, Ante 0.3.0 sends `reasoning_effort: "low"` when asked for `medium` on its OpenAI-compatible wire. The relay refuses that (422).
4. **Startup grows with contention.** On 2 vCPU, startup at size 8 is far slower than at size 1. Report size-1 numbers next to swarm numbers so contention does not get read as harness speed.
5. Hermes teardown (stopping its gateway) runs inside `execute`, after `wall_s` and before the escaped-process check. It is recorded as `teardown_s`, so it is not part of `after_final_s`.

`catalog.py` holds the provider and model menu the front offers. Edit it for your own upstreams.
