"""swarm: probe | plan | run | verify | report | serve | front."""
import argparse
import json
import sys
from pathlib import Path

from . import VERSION, host


def _binaries(values):
    out = {}
    for value in values or []:
        name, _, path = value.partition("=")
        if not path:
            raise SystemExit(f"--binary expects NAME=PATH, got {value!r}")
        out[name] = path
    return out


def _harness_options(items) -> dict:
    out: dict[str, dict] = {}
    for item in items or []:
        name_key, sep, value = item.partition("=")
        name, dot, key = name_key.partition(".")
        if not (sep and dot and name and key):
            raise SystemExit(f"--harness-option wants NAME.KEY=VALUE, got {item!r}")
        out.setdefault(name, {})[key] = value
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(prog="swarm", description="Measure coding agents at concurrency, with verifiable bundles.")
    parser.add_argument("--version", action="version", version=f"swarm {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe", help="host fingerprint and current memory pressure")
    p = sub.add_parser("plan", help="validate a spec and freeze it into a plan")
    p.add_argument("spec")
    p.add_argument("-o", "--out", default="plan.json")
    p.add_argument("--binary", action="append", metavar="NAME=PATH", help="harness binary override")
    r = sub.add_parser("run", help="execute a frozen plan into a new bundle directory")
    r.add_argument("plan")
    r.add_argument("-o", "--out", required=True)
    v = sub.add_parser("verify", help="check a bundle's manifest, attempt chain and summary")
    v.add_argument("bundle")
    rep = sub.add_parser("report", help="render a bundle's report to a file outside the bundle")
    rep.add_argument("bundle")
    rep.add_argument("-o", "--out")
    srv = sub.add_parser("serve", help="live run service for preset use cases (scripted model only)")
    srv.add_argument("--presets", required=True, help="presets.json (see serve/presets.json)")
    srv.add_argument("--data", required=True, help="directory for run records and bundles")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8080)
    srv.add_argument("--max-queue", type=int, default=5)
    srv.add_argument("--per-client-hour", type=int, default=3)
    srv.add_argument("--keep", type=int, default=40, help="finished runs to keep on disk")
    srv.add_argument("--telemetry", choices=["on", "off"], default="on")
    srv.add_argument("--binary", help="ante binary override")
    srv.add_argument("--harness-binary", action="append", metavar="NAME=PATH",
                     help="binary for another harness a preset offers (codex, claude-code); one that is not found is not offered")
    srv.add_argument("--harness", default="ante", help="this lane's own harness: the default for a launch that names none")
    srv.add_argument("--max-size", type=int, help="refuse launches with more agents than this machine holds")
    srv.add_argument("--relay-upstream", help="real models: the HTTPS gateway the relay forwards to (holds the credentials)")
    srv.add_argument("--live-model", action="append", default=[], help="a real model launches may name (repeat)")
    srv.add_argument("--gateway-upstream", help="the swarm gateway (provider adapters for subscriptions) the relay may use")
    srv.add_argument("--gateway-model", action="append", default=[], help="a real model served through --gateway-upstream (repeat)")
    srv.add_argument("--gateway-key-env", help="env var holding this lane's gateway token (none when the gateway knows lanes by VM)")
    srv.add_argument("--live-max-size", type=int, default=None, help="most agents in a real-model run (default: no cap)")
    srv.add_argument("--live-max-tokens", type=int, default=2_000_000, help="input+output token budget per real-model run")
    srv.add_argument("--live-agent-tokens", type=int, default=0,
                     help="token budget per agent; when set, a real-model run's budget is this times its size")
    srv.add_argument("--live-effort", default="medium", choices=["low", "medium", "high"])
    srv.add_argument("--harness-option", action="append", metavar="NAME.KEY=VALUE",
                     help="a required harness option written into every run's spec, e.g. hermes.home=prepared")
    srv.add_argument("--reset-min-free-mb", type=int, default=2048,
                     help="free disk a lane must have after its post-run reset; below it launches are refused")
    srv.add_argument("--reset-tolerance-mb", type=int, default=512,
                     help="how far below its idle level available memory may stay after a reset")
    srv.add_argument("--reserve-disk-mb", type=int, default=1024,
                     help="free disk admission keeps during a run; agents are held, not launched, below it")
    srv.add_argument("--gate", choices=["off", "observe", "enforce"], default="observe",
                     help="quiet gate before the control and the run: observe records, enforce refuses a busy lane")
    srv.add_argument("--gate-quiet-s", type=float, default=5, help="seconds of CPU the gate averages over")
    srv.add_argument("--gate-cpu-cores", type=float, default=0.10, help="most other CPU (cores) a quiet lane may show")
    srv.add_argument("--gate-max-swap-mb", type=int, default=0, help="most swap in use a quiet lane may show")
    srv.add_argument("--gate-wait-s", type=float, default=120, help="how long a busy lane is waited for before it refuses")
    srv.add_argument("--gate-allow-unknown", action="store_true", help="let unknown readings pass the gate")
    srv.add_argument("--control", choices=["on", "off"], default="on",
                     help="run the lane control (preset kind control) with every run")
    lr = sub.add_parser("lane-reset", help="on a lane: remove runner sandboxes, stop leftover agent processes, check the machine")
    lr.add_argument("--data", required=True, help="the lane service's data directory (the record is written there)")
    lr.add_argument("--min-free-mb", type=int, default=2048)
    lr.add_argument("--tmp", help="where the runner makes its sandbox roots: the service's TMPDIR when it sets one (default: the temp dir)")
    lr.add_argument("--force", action="store_true",
                    help="also take sandboxes and agents of a runner that looks alive (only when no run is in progress)")
    gw = sub.add_parser("gateway", help="provider adapters for subscriptions (Claude via Claude Code, OpenCode Go) behind per-lane tokens")
    gw.add_argument("--config", required=True, help="gateway.json: routes (upstream, auth, secret_env, apis, models) and clients")
    gw.add_argument("--log", required=True, help="jsonl usage log (also restores today's spend on restart)")
    gw.add_argument("--host", default="127.0.0.1")
    gw.add_argument("--port", type=int, default=9300)
    fr = sub.add_parser("front", help="public front end that races one preset across several private serve lanes")
    fr.add_argument("--lanes", required=True, help="lanes.json: id, label, harness, url, secret_env per lane")
    fr.add_argument("--data", required=True, help="directory for the race log")
    fr.add_argument("--static", required=True, help="directory holding index.html and app.js")
    fr.add_argument("--host", default="127.0.0.1")
    fr.add_argument("--port", type=int, default=8090)
    fr.add_argument("--per-client-hour", type=int, default=3)
    so = sub.add_parser("solo", help="replay-only page for stored Ante runs, drawn as a handheld game")
    so.add_argument("--data", required=True, help="a serve data directory holding runs/<id>/ (read only)")
    so.add_argument("--static", required=True, help="directory holding the solo page (front-solo/)")
    so.add_argument("--maze", help="directory holding the maze race pages (front-maze/), served under /maze/")
    so.add_argument("--host", default="127.0.0.1")
    so.add_argument("--port", type=int, default=8000)
    so.add_argument("--play-ante", help="live maze (/api/play/): the Ante binary its rounds run; without it live is off (404)")
    so.add_argument("--play-passcode-env", default="SWARM_PLAY_PASSCODE_SHA256",
                    help="env var holding the SHA-256 hex digest of the live passcode (never the passcode itself)")
    so.add_argument("--play-provider", choices=["subscription", "relay", "standin"], default="subscription",
                    help="subscription: each Ante runs on the provider and sign-in its Ante home is configured with (one per round); "
                         "relay: through the swarm relay to --play-upstream; standin: no model (same as --play-standin)")
    so.add_argument("--play-signin", default="auto",
                    help="subscription: which configured sign-in to use: one label such as 'personal', or 'auto' (picked from "
                         "`ante usage --json` among those under 95%% used); a round's catalog names only that one")
    so.add_argument("--play-signin-order", default="work,personal",
                    help="subscription fallback when `ante usage --json` cannot be read: labels, comma-separated")
    so.add_argument("--play-signin-reset", action="append", metavar="LABEL=ISO",
                    help="subscription fallback: a weekly reset time with its zone (repeatable); rolled forward weekly. "
                         "Default: none (without usage or reset times the order alone decides)")
    so.add_argument("--play-ante-home", help="subscription: the Ante home holding the sign-ins (default ~/.ante); its "
                                             "auth/credentials/oauth/anthropic must be writable by this service")
    so.add_argument("--play-upstream",
                    help="relay: the model gateway the relay forwards to; it holds the credential (required for relay)")
    so.add_argument("--play-model", help="model id (default: subscription = the Ante home's settings.json model; "
                                         "relay/standin = claude-haiku-5-5)")
    so.add_argument("--play-ante-provider", choices=["anthropic-subscription", "openai-subscription"],
                    help="subscription: Ante's provider (default: the Ante home's settings.json provider)")
    so.add_argument("--play-wire", choices=["messages", "responses", "chat"], default="messages")
    so.add_argument("--play-wire-style", help="Ante catalog wire_style override (default from --play-wire: messages -> AnthropicMessage)")
    so.add_argument("--play-effort", default="low", choices=["low", "medium", "high"])
    so.add_argument("--play-max-output-tokens", type=int, default=4096)
    so.add_argument("--play-standin", action="store_true",
                    help="no model: the scripted model drives every Ante with the maze Explorer (acceptance runs)")
    so.add_argument("--play-round-timeout", type=float, default=300.0, help="seconds")
    so.add_argument("--play-round-tokens", type=int, default=400_000, help="input+output tokens per round")
    so.add_argument("--play-daily-tokens", type=int, default=5_000_000, help="input+output tokens per UTC day")
    so.add_argument("--play-max-antes", type=int, default=16, help="most rival Antes in a round")
    so.add_argument("--play-queue", type=int, default=3, help="rounds that may wait behind the running one")
    so.add_argument("--play-max-requests", type=int, default=400, help="model requests per Ante per round")
    so.add_argument("--play-data", help="writable directory for rounds.jsonl and the day's token count (default: memory only)")
    so.add_argument("--play-cpu", type=float, help="cgroup cpu.max (cores) for a round's Antes (needs Delegate=yes)")
    so.add_argument("--play-memory-mb", type=int, help="cgroup memory.max for a round's Antes")
    so.add_argument("--play-pids", type=int, help="cgroup pids.max for a round's Antes")
    so.add_argument("--play-trust-forwarded", action="store_true",
                    help="rate-limit passcode tries by X-Forwarded-For's last hop (only behind a proxy that sets it)")
    un = sub.add_parser("front-unlock", help="on the front's machine: show or clear the operator-passkey lock with the reset PIN")
    un.add_argument("--data", required=True, help="the front's data directory")
    un.add_argument("--env", default="/etc/swarm-front-pin.env",
                    help="root-only file holding SWARM_FRONT_RESET_PIN; the front service never loads it")
    un.add_argument("--status", action="store_true", help="only show wrong passkeys counted and whether a lock is on")
    ex = sub.add_parser("export", help="write the numbers-only export tree from a front data directory")
    ex.add_argument("--front-data", required=True, help="the front's data directory (races.jsonl and export/)")
    ex.add_argument("-o", "--out", required=True, help="directory for the export tree (v1/...)")
    a = parser.parse_args(argv)

    if a.command == "probe":
        from .limits import Limits, probe as probe_limits
        reason = probe_limits(Limits(cpu=1.0, memory_mb=1024, pids=1024))  # read-only: moves and enables nothing
        print(json.dumps({"fingerprint": host.fingerprint(), "memory": host.memory_state(), "tasks": host.task_headroom(),
                          "cgroup_limits": {"available": reason is None, "reason": reason}}, indent=2))
        return 0
    if a.command == "plan":
        from .plan import freeze
        try:
            plan = freeze(Path(a.spec), _binaries(a.binary))
        except ValueError as error:
            print(f"spec rejected: {error}", file=sys.stderr)
            return 2
        Path(a.out).write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        versions = ", ".join(f"{name} {info['version']}" for name, info in plan["harnesses"].items())
        print(f"plan {plan['plan_sha256'][:12]} · {len(plan['cells'])} cells · {len(plan['tasks'])} tasks · "
              f"{versions} -> {a.out}")
        return 0
    if a.command == "run":
        from .runner import run
        try:
            bundle = run(Path(a.plan), Path(a.out))
        except (RuntimeError, FileExistsError, ValueError) as error:
            print(f"run refused: {error}", file=sys.stderr)
            return 2
        summary = json.loads((bundle / "summary.json").read_text())
        for group in sorted(summary["groups"].values(), key=lambda g: (g["harness"], g["level"])):
            print(f"{group['harness']:>8} n={group['level']:<3} passed {group['passes']}/{group['attempts']}  "
                  f"succ/min {group['successes_per_minute']}  peak MB p50 {group['peak_rss_mb_median']}  "
                  f"cpu s/success {group['cpu_s_per_success']}  failures {group['failures'] or '-'}")
        state = "complete" if summary["complete"] else ("CRASHED: " + summary["crashed"]) if summary.get("crashed") else \
            "CANCELLED" if summary.get("cancelled") else "INCOMPLETE"
        print(f"bundle {bundle} ({state})")
        return 0 if summary["complete"] else 1
    if a.command == "gateway":
        from .gateway import serve_gateway
        serve_gateway(Path(a.config), Path(a.log), a.host, a.port)
        return 0
    if a.command == "serve":
        from .serve import serve
        serve(Path(a.presets), Path(a.data), a.host, a.port, max_queue=a.max_queue, per_client_hour=a.per_client_hour,
              keep=a.keep, telemetry=a.telemetry, binary=a.binary, binaries=_binaries(a.harness_binary), harness=a.harness,
              harness_options=_harness_options(a.harness_option), max_size=a.max_size,
              live={"models": {**{m: {"upstream": a.relay_upstream, "key_env": None} for m in a.live_model},
                               **{m: {"upstream": a.gateway_upstream, "key_env": a.gateway_key_env} for m in a.gateway_model}},
                    "max_size": a.live_max_size,
                    "max_tokens": a.live_max_tokens, "agent_tokens": a.live_agent_tokens, "effort": a.live_effort},
              reset_min_free_mb=a.reset_min_free_mb, reset_tolerance_mb=a.reset_tolerance_mb, reserve_disk_mb=a.reserve_disk_mb,
              gate=a.gate, control=a.control == "on",
              gate_limits={"quiet_s": a.gate_quiet_s, "max_cpu_cores": a.gate_cpu_cores, "max_swap_mb": a.gate_max_swap_mb,
                           "wait_s": a.gate_wait_s, "allow_unknown": a.gate_allow_unknown})
        return 0
    if a.command == "lane-reset":
        from .reset import reset
        record = reset(Path(a.data), a.min_free_mb, force=a.force, tmp=Path(a.tmp) if a.tmp else None)
        print(json.dumps(record, indent=2, sort_keys=True))
        print(("lane clean" if record["ok"] else "lane NOT clean: " + ", ".join(record["problems"])), file=sys.stderr)
        return 0 if record["ok"] else 1
    if a.command == "front":
        from .front import serve_front
        serve_front(Path(a.lanes), Path(a.data), Path(a.static), a.host, a.port, per_client_hour=a.per_client_hour)
        return 0
    if a.command == "solo":
        # Weekly reset times are deployment data: pass them with --play-signin-reset LABEL=ISO. Only used when
        # `ante usage` cannot be read; each rolls forward a week at a time.
        DEFAULT_SIGNIN_RESETS = []
        from .solo import serve_solo
        play_config = None
        if a.play_ante:
            from .play import PlayConfig, parse_resets, passcode_hash_from_env
            if a.play_provider == "relay" and not a.play_standin and not a.play_upstream:
                print("--play-provider relay needs --play-upstream (the model gateway that holds the credential)", file=sys.stderr)
                return 2
            digest, problem = passcode_hash_from_env(a.play_passcode_env)
            if problem:
                print(problem, file=sys.stderr)
            limits = {k: v for k, v in (("cpu", a.play_cpu), ("memory_mb", a.play_memory_mb), ("pids", a.play_pids))
                      if v is not None}
            play_config = PlayConfig(
                ante=Path(a.play_ante), passcode_sha256=digest, upstream=a.play_upstream or "", model=a.play_model,
                wire=a.play_wire, wire_style=a.play_wire_style, effort=a.play_effort,
                max_output_tokens=a.play_max_output_tokens, standin=a.play_standin,
                round_timeout_s=a.play_round_timeout, round_tokens=a.play_round_tokens,
                daily_tokens=a.play_daily_tokens, max_antes=a.play_max_antes, queue=a.play_queue,
                max_requests=a.play_max_requests, data=Path(a.play_data) if a.play_data else None, limits=limits,
                trust_forwarded=a.play_trust_forwarded, provider="standin" if a.play_standin else a.play_provider,
                signin=a.play_signin, ante_provider=a.play_ante_provider, signin_order=tuple(x.strip() for x in a.play_signin_order.split(",") if x.strip()),
                signin_resets=parse_resets(a.play_signin_reset or DEFAULT_SIGNIN_RESETS),
                ante_home=Path(a.play_ante_home) if a.play_ante_home else None)
        serve_solo(Path(a.data), Path(a.static), a.host, a.port, Path(a.maze) if a.maze else None, play_config)
        return 0
    if a.command == "front-unlock":
        import getpass
        import hmac
        import os
        from .front import PASSKEY_TRIES, lock_state, write_lock
        failures, locked = lock_state(Path(a.data))
        print(f"{'locked' if locked else 'open'} · {failures} of {PASSKEY_TRIES} wrong passkeys counted")
        if a.status:
            return 0
        pin = next((line.split("=", 1)[1].strip() for line in Path(a.env).read_text().splitlines()
                    if line.startswith("SWARM_FRONT_RESET_PIN=")), "")
        if not pin:
            print("no SWARM_FRONT_RESET_PIN set; nothing unlocked")
            return 1
        if not hmac.compare_digest(getpass.getpass("Reset PIN: ").encode(), pin.encode()):
            print("wrong PIN; nothing changed")
            return 1
        write_lock(Path(a.data), 0, False)
        st = os.stat(a.data)  # keep the file the front's own user can rewrite
        os.chown(Path(a.data) / "owner-lock.json", st.st_uid, st.st_gid)
        print("unlocked · count reset")
        return 0
    if a.command == "export":
        from .export import build
        got = build(Path(a.front_data), Path(a.out))
        print(f"exported {got['races']} races ({got['interrupted']} interrupted, {got['stale']} stale, {got['superseded']} superseded) "
              f"· {got['rows']} rows · {got['requests']} requests · {got['gates']} gates -> {got['out']}")
        return 0
    if a.command == "verify":
        from .bundle import verify
        problems = verify(Path(a.bundle))
        for problem in problems:
            print(f"FAIL {problem}")
        print("verified" if not problems else f"{len(problems)} problem(s)")
        return 0 if not problems else 1
    if a.command == "report":
        from .report import render
        out = Path(a.out) if a.out else Path(f"{Path(a.bundle).resolve().name}-report.html")
        if Path(a.bundle).resolve() in out.resolve().parents:
            print("report refused: write it outside the bundle, or the bundle stops verifying", file=sys.stderr)
            return 2
        out.write_text(render(Path(a.bundle)))
        print(out)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
