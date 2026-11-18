# swarm-hub

This is the service behind `/swarm/*`, minus the Swarm harness itself (that lives in [`../swarm`](../swarm)). It's a small, dependency-free hub that starts swarm runs on exe.dev VMs and shows their records.

| Directory | Service | Language | Serves |
|---|---|---|---|
| `hub/` | **Swarm hub**: owner console for runs (lifecycle chart, memory, live events, receipts) and an unauthenticated read-only receipts API | Python 3 stdlib | `/swarm/console/` (the console, via a rewrite to `/swarm/`), `/swarm/public/*`, `/swarm/healthz`, and anything else under `/swarm/` that the static pages don't claim |
| `runner/` | **Runner** (`swarm_run.py`) that the hub starts for a run, plus a read-only annotation triage CLI (`swarm_annotations.py`) | Python 3 stdlib | nothing directly; writes the run records the hub reads |
| `tests/` | Unit tests for hub and runner, and a synthetic run fixture (`tests/fixtures/mini-run`) | Python `unittest` | |
| `deploy/examples/` | Placeholder nginx locations, systemd units, and env-file templates | | |

Not in this directory:

- `/swarm/` (the landing page) and `/swarm/bench/` (benchmark tables) are static HTML/JS published with the main site. Neither service serves them. `/swarm/live/api/*` is a reverse proxy to `swarm serve` from [`../swarm`](../swarm).

## Architecture

```
 browser ──► authenticating HTTPS proxy ──► nginx ──┬─ /swarm/public/*  (no identity, no secret) ──► hub (127.0.0.1:8003)
             (sets X-ExeDev-Email)                  │
                                                    ├─ /swarm/console/*  ─ rewrite to /swarm/* ──► hub
                                                    └─ /swarm/*          (legacy console paths) ──► hub
                                                                                                    │ starts
 run records:  <runs dir>/<run id>/  ◄── written by runner/swarm_run.py ◄────────────────────────────┘
               read by the hub (read-write for new runs and cancel/kill markers)
```

The console is mounted at `/swarm/console/`. Its page fetches relative URLs (`api/runs`, `api/status`,
`api/runs/<id>/events`), which resolve under `/swarm/console/` and are rewritten to the hub's own `/swarm/api/*`.

### Run records

Each run has a directory under the runs dir. Hub run ids are `r` + number (`r01`, `r142`); the hub sorts them
numerically and allocates the next one.

| File | Written by | Meaning |
|---|---|---|
| `meta.json` | runner | run spec: mode, cells, effort, model, harness, pack, started, runner `pid` |
| `tasks.json` | hub (custom runs) / pack | the exact task list sent |
| `log.jsonl`, `log.json` | runner | phase log rows (live, then final) |
| `samples.jsonl`, `samples.json` | runner | memory and load samples (live, then final) |
| `events.jsonl` | runner | the harness's own JSON event stream, tailed over SSH |
| `receipts.json` | runner | the final receipt; its presence marks a run complete |
| `runner.out` | hub | runner stdout/stderr |
| `cancel`, `kill-<label>` | hub | control markers the runner watches |

A run is "live" when it has no `receipts.json` and the `pid` in `meta.json` is alive; otherwise it is complete or
aborted.

### Hub (`hub/server.py`)

- Owner console page (`hub/static/index.html`, `app.js`): run list, cell lifecycle (Gantt), harness memory during
  the task, live event tail, receipts, and a form to start, cancel, or kill a run.
- Owner API: `GET /swarm/api/runs`, `/swarm/api/runs/<id>`, `/swarm/api/runs/<id>/events?after=N`,
  `/swarm/api/status` (packs, checks, models, harnesses, per-pack token history).
  `POST /swarm/api/swarms` (start), `/swarm/api/swarms/<id>/cancel`, `/swarm/api/swarms/<id>/kill`
  (one instance), `/swarm/api/annotations` (sink for an on-page feedback widget; one JSON file per POST).
  Every POST needs the `X-Swarm-Action: 1` header, a JSON body (64 KiB; 256 KiB for annotations).
- Public API (no identity, `Access-Control-Allow-Origin: *`): `GET /swarm/public/runs` (summary fields of
  completed runs), `/swarm/public/runs/<id>` (receipt and samples, with cell host names and usage attribution
  stripped), `/swarm/public/live/<id>` (last 120 samples). No logs and no controls.
- Optional "shadow cost": tokens priced at a configured list rate (`SWARM_USD_PER_MTOK_*`).
- One run at a time: a start is refused while another run is live.

### Runner (`runner/swarm_run.py`)

Drives Ante (or the `pi` / `hermes` harnesses) on exe.dev VMs through the `ssh exe.dev` control plane: clones
VMs from a golden image (`SWARM_GOLDEN`, built by a separate image script that is not part of this app), names
them `<SWARM_CELL_PREFIX>-NN`, reaches them at `<name>.<SWARM_CELL_DOMAIN>`, ships the task pack, runs one harness
process per task (`local` mode: N instances on one VM; `vm` mode: one VM per task), collects tokens, checks and
memory, writes the receipt, and destroys every VM it created on every exit path. Task packs are directories with a
`tasks.json` and `tests/check_*.py` (`SWARM_PACKS_DIR`; this repository's [`bench/tasks`](../bench/tasks)).

## Access gate

The hub trusts an identity only when it arrives through nginx. If that check fails, it fails closed.

1. **Who is asking.** An authenticating HTTPS proxy sits in front of nginx (exe.dev's proxy in the reference setup). It signs the visitor in and sets `X-ExeDev-Email`, stripping any value the client sent. nginx passes that to the hub as `X-ExeDev-Email`. An optional SSO verifier can sit in between. nginx runs `auth_request` against it. If the verifier says the owner profile is signed in, nginx swaps in the owner's email through an nginx `map`. Otherwise the proxy's email passes through unchanged.
2. **That nginx sent it.** nginx adds a shared secret header (`X-Swarm-Ingress`) from an include file that is not in git. The hub compares it to `SWARM_INGRESS_TOKEN` in constant time. With no secret configured, every authenticated route returns 403. That keeps another local process on the host from forging the identity header against the loopback port.
3. **Allow-list.** The email must appear in `SWARM_ALLOWED_EMAILS` (comma or space separated, case-insensitive). The hub refuses to start with an empty list.
4. **Anonymous visitors** get a 303 to the login URL (`SWARM_LOGIN_URL`). The default is the exe.dev proxy's `/__exe.dev/login?redirect=...`.
5. **Writes.** Hub POSTs need `X-Swarm-Action: 1`. The example nginx config runs the hub read-only, so non-GET requests get 409. Remove that line to allow starting runs.

`/swarm/public/*` and the hub's `/swarm/healthz` need neither identity nor secret. nginx clears both headers on the public location.

## Configuration

Hub (`hub/server.py`, read at import):

| Variable | Default | Purpose |
|---|---|---|
| `SWARM_ALLOWED_EMAILS` | required | allow-list for the console and owner API |
| `SWARM_INGRESS_TOKEN` | empty (all authed routes 403) | shared secret nginx injects as `X-Swarm-Ingress` |
| `SWARM_HUB_PORT` | `8003` | loopback port |
| `SWARM_RUNS_DIR` | `./runs` | run records |
| `SWARM_STATIC_DIR` | `hub/static` | console page |
| `SWARM_ANNOTATIONS_DIR` | sibling `annotations/` of the runs dir | annotation sink |
| `SWARM_LOGIN_URL` | `/__exe.dev/login?redirect=%2Fswarm%2F` | where anonymous visitors go |
| `SWARM_RUNNER` | `runner/swarm_run.py` | runner script the hub starts |
| `SWARM_PACKS_DIR` | `../bench/tasks` (this repository's task packs) | task packs |
| `SWARM_MAX_VMS`, `SWARM_MAX_LOCAL` | `2`, `32` | per-run caps for `vm` and `local` mode |
| `SWARM_USD_PER_MTOK_IN`, `_CACHED`, `_OUT`, `SWARM_PRICE_SOURCE` | unset | shadow-cost rate card and its label |

The runner inherits the hub's environment. It reads `SWARM_GOLDEN` (golden image name, `ante-golden`), `SWARM_CELL_PREFIX` (`bench-c`), `SWARM_CELL_DOMAIN` (`exe.xyz`), `SWARM_EFFORT`, and `HOME`. `HOME` must hold the SSH key for `ssh exe.dev`. The hub sets it to the runs dir's parent by default. `swarm_annotations.py` reads `SWARM_ANNOTATIONS_DIR`, which defaults to `./annotations`.

Template: `deploy/examples/hub.env.example`.

## Run locally with synthetic data

You don't need model calls, VMs, or secrets to look at the hub. Run this from the directory:

```sh
mkdir -p .local/runs
cp -R tests/fixtures/mini-run .local/runs/r01      # synthetic run; hub ids must be r<number>

# hub on :28703 (identity is a header you send yourself; nginx does this in production)
SWARM_HUB_PORT=28703 SWARM_RUNS_DIR=.local/runs SWARM_ALLOWED_EMAILS=dev@example.com \
SWARM_INGRESS_TOKEN=dev-token python3 hub/server.py &

curl -s localhost:28703/swarm/public/runs
curl -s -H 'X-Swarm-Ingress: dev-token' -H 'X-ExeDev-Email: dev@example.com' localhost:28703/swarm/api/runs
```

To open the console in a browser, put a local proxy in front that adds the two headers. A browser can't add them itself. You can also use the example nginx config with a fixed `X-ExeDev-Email`. Don't start runs locally unless you have an exe.dev account, a golden image, and task packs. A start clones real VMs.

`.local/` is scratch space. Delete it when you're done.

## Tests

```sh
python3 -m unittest discover -s tests -v     # hub + runner + annotations + import coverage
```

The Python tests start the hub in-process on a random loopback port, using temp directories. They use `/bin/true` as the runner.

## Deploy (generic)

1. Copy this directory to the host (for example `/opt/swarm-hub`). Make it owned by root and read-only to the services.
2. Create a service user. Then create a runs directory that user can write to.
3. Install `deploy/examples/swarm-hub.service` and adjust the paths.
4. Create the env file from the template with mode 600, and put a random ingress secret in it. Then write a matching nginx include file that sets the `X-Swarm-Ingress` header (also mode 600).
5. Add the locations from `deploy/examples/nginx-swarm.conf` to your server block. Run `nginx -t`, then reload.
6. Check that `/swarm/healthz` returns 200 and `/swarm/public/runs` returns 200. `/swarm/console/` should return 303 when signed out and 200 for an allow-listed identity.

## Known technical limitations

- Hub run ids must be `r<number>`. `run_dirs()` sorts with `int(name[1:])`, so any other directory under the runs dir that still matches the id pattern makes the run list fail.
- The hub's page and `app.js` are served only to an authenticated owner. The page loads an optional feedback widget from `/agentation/loader.js`. That file is not part of this app, so it 404s unless you supply it.
- The example nginx config effectively disables the console's start form (read-only). The start path also needs the golden image and an exe.dev SSH key. Neither ships here.
- Model aliases (`terra`, `luna`) and harness names are hard-coded in `hub/server.py` (`MODELS`, `HARNESSES`).
- The hub polls the filesystem (no inotify). Large run directories cost CPU on every request. It caches peak samples by mtime.

## Not included

- The static `/swarm/` pages. This repository keeps only the live launch page, `../site/live/`.
- The golden image build scripts the runner needs to start runs.

## Provenance

This is a snapshot with no history. It was exported from a private monorepo at commit `5bde926`, which had itself imported the hub, the runner and their tests from an earlier private monorepo. The import made these changes. `control/` became `runner/`. Host-specific defaults were swapped for neutral ones (runs dir, annotations dir, hub `HOME`). New settings `SWARM_LOGIN_URL` and `SWARM_CELL_DOMAIN` were added, and their defaults keep the previous behaviour. Test fixtures use example.com hosts. In this repository, the default `SWARM_PACKS_DIR` resolves to `../bench/tasks`.

The hub and runner integrate with the exe.dev VM platform. Identity arrives in the platform proxy's `X-ExeDev-Email` header. The runner drives VMs through the platform's `ssh exe.dev` control plane and reaches them at `<name>.exe.xyz`. Those names are the platform's public integration contract. No specific host appears here. The cell domain is configuration (`SWARM_CELL_DOMAIN`, default `exe.xyz`), and so is the login URL (`SWARM_LOGIN_URL`).

In this repository, the console's title is "Swarm Hub." Ante appears only as the product under test. The page carries the line "Independent work, not an Antigma release."

Excluded: run data and receipts, logs, annotations, build output, secrets and env files, host-specific deploy scripts (replaced by `deploy/examples/`), golden image scripts, and the run triage board.
