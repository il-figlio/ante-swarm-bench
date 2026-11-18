# Integrity: cheating, leaks and isolation

What stops an agent from faking a result, what stops a page or export from leaking a host's data, and where the guarantees end. Code references are under [`swarm/src/swarm/`](../swarm/src/swarm/).

## Threat model in one paragraph

Agents are semi-trusted programs driven by a model. They may take shortcuts: edit the tests, read the answer, script around a rule. They are not assumed to attack the host. Every attempt runs as the same user, on the same filesystem, network and signals. So **the process sandbox is not a security boundary**. A hostile agent can read files, disturb other attempts or hide a process (`env -i` plus `setsid`). Run untrusted agents in a VM or container. Within that limit, the design makes shortcuts hard. Where they still happen, they show up in the record.

## Task checks (`runner.py`, `sandbox.py`)

- Hidden tests are copied in only after the agent exits. They go onto a private copy of the workdir and run with a fresh HOME and Python's user site-packages disabled.
- The change snapshot covers symlinks, modes and hidden files. Changing a path outside the allowed set, or changing anything after exit, fails the attempt.
- Processes still alive in the attempt's group, or found by its environment tag, fail the attempt and are killed.
- Each attempt gets an unguessable model URL. Another attempt's requests cannot be replayed onto it.
- Model, effort and output cap are checked on the wire. A request for another model is refused, not served.

## Maze eval: the answer is not on disk (`maze.py`)

The maze task measures coordination. So the maze itself has to be unreachable, except through the `./maze` client.

| Attack | Defence | Test |
|---|---|---|
| Read the maze from the task or plan and regenerate it | Each cell's seed is HMAC-SHA256 of `"<task seed>:<cell>:<task id>"` under a 32-byte per-run key from `os.urandom` that is never written anywhere. Regenerating from `task.toml` gives a different maze. | regenerate-from-task test |
| Read live positions from a log | The service keeps its call log in memory and writes it only when the cell ends. | log-not-on-disk test |
| Use a sibling's token (same uid, shared filesystem) | The service listens on a Unix socket in its own 0700 directory. It reads the peer pid (`SO_PEERCRED` on Linux, `LOCAL_PEERPID` on macOS) and honours a token only from the agent's root pid or a descendant. Others get 403, logged as `stolen_token` with owner and peer, and the owner's counters are untouched. Where peer pids cannot be read, the service does not start. | `test_a_token_from_a_non_descendant_is_refused_and_costs_its_owner_nothing` |
| Walk through walls, exceed the step limit, double-claim an edge | Every command is one logged call. Walls and limits are enforced server-side. Edge claims are atomic: exactly one of two simultaneous claims wins. | adversarial tests in `test_maze.py` |
| Oversized or malformed requests | Bodies over 4096 bytes or with a negative Content-Length are refused unread. | `test_maze.py` |

Results (time to exit, route, credit, repeat work) come from the service's call log. They never come from agent text. The effective seed is recorded after the cell ends. That means any cell can be re-simulated and checked call for call against `maze.simulate`. The Python generator and simulation, and their JS twins (`front-maze/maze-core.js`), are checked for parity on walls, calls, exit tick and credit.

## Live games: rules a script cannot shortcut (`play.py`, `arcade.py`, `gamesvc.py`, `hill.py`)

- The same token-and-peer-pid service backs `./trail` and `./hill`. Acting ops wait behind a gate until the round starts.
- During a maze round, rival positions never go to the page. Only `far|mid|near` buckets and counts do. At the end, the full maze, every path and the seed are revealed, along with a nonce. The `seed_hash = sha256("<seed>:<nonce>")` is sent when the round starts, which lets the page check the commitment.
- Live hill pieces carry a fresh random id from `secrets`, and only a fresh `look` shows it. An Ante's drops must be 1 s apart. This rule was added after a real-model Ante wrote a shell loop of 377 blind drops in 28 s.
- The passcode is compared in constant time, and refused before any cell starts. Tries are rate limited per address and globally. Only its SHA-256 is configured on the server.
- Players send only moves. No player text reaches a model.

## Nothing private leaves a lane

- **Replay page (`solo.py`).** Responses are built field by field from an allow-list. Free text goes through `clean()`, which drops absolute paths, long hex and base64-like strings, e-mail addresses and URLs. `tests/test_solo.py` feeds it hostile rows (home paths, tokens, an e-mail, hex secrets) and checks that none of them survive.
- **Front (`front.py`).** Everything relayed from a lane goes through `Front.scrub`. `tests/test_front.py` checks that host names, private addresses, home paths and key-shaped strings get removed.
- **Export (`export.py`).** Only numbers, codes, hashes and tool names get out, and only for keys on an allow-list. Paths, hostnames, logs, tool arguments, prompt text and client data are not exported. `tools/rebuild_export.py` recomputes every summary from the rows and has to produce the same bytes.
- **Relay and bench relay.** The harness's own auth headers, `x-api-key` and cookies are never forwarded. Audits record the model, settings, status and usage, but never prompts or responses.
- **Hub public API.** `/swarm/public/*` serves receipts and samples with cell host names and usage attribution removed. The console sits behind an authenticating proxy, a shared ingress secret and an e-mail allow-list. Without the secret, it fails closed.

## What `swarm verify` does and does not prove

It detects missing, changed or extra files, a broken attempt chain, cells or attempts that don't match the frozen plan, and a summary that doesn't follow from the records. Bundles aren't signed. Anyone with write access can build a consistent fake, so `verify` proves internal consistency, not authorship. Bundles also contain raw agent output and absolute paths, which is why only summaries and hashes are published here.

## Known gaps

- No namespaced or container sandbox. cgroup v2 limits (with delegation) cap CPU, memory and pids per maze cell or live round, but not file access.
- The Linux-only peer and cgroup paths have unit tests, but most test runs in the source history happened on macOS.
- Live cells can read every stored sign-in for their provider profile (see [swarm/docs/live.md](../swarm/docs/live.md#exposure)).
