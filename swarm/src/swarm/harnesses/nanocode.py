"""nanocode (`nanocode exec`), an unmodified third-party binary, pointed at the scripted model.

nanocode (github.com/nanocodexyz/nanocode, CLI `nanocode`/`nc`) drives Codex-family models on the OpenAI
Responses wire. NANOCODE_OPENAI_BASE_URL points it at the attempt's scripted URL: it lists `<base>/models` once
at startup (and refuses to start on an empty list), then streams `<base>/responses`. A model it does not know
runs on its Codex backend, which is the same wire every other OpenAI-wire harness here uses. No OpenAI or
nanocode service is contacted.

Tools: nanocode runs the model in Code Mode. The only top-level tools are `exec` (a freeform tool whose input is a
JavaScript program run in embedded QuickJS) and `wait`; the shell (`exec_command`), `apply_patch` and the rest
are nested tools the program calls as `tools.<name>(...)`. nanocode 0.7.0 has no switch to turn Code Mode off,
so the spec must say `code_mode = "on"`: a later build that can turn it off becomes a different option value,
not a silent change. The scripted model answers each step with a one-call program that runs the same shell
command Codex CLI gets (cat to read, printf to write), so the file effects are identical.

Fair-play settings, each mirroring an off-switch the other harnesses get: no session persistence (--ephemeral),
OpenTelemetry off (--telemetry off, no collector endpoint), no Tempo payment tools or their local proxy
(--no-tempo-tools), no subagent tools unless the spec asks for them (a required option, since they change the
tool catalog), and a fresh NANOCODE_HOME. nanocode has no update check. Computer use cannot be switched off by
flag; it needs Node.js 22 and a cua-driver install, neither of which the reference worker has, and nanocode
says "computer use unavailable" on stderr and carries on.
"""
from .base import Harness


class Nanocode(Harness):
    name = "nanocode"
    binary_names = ("nanocode",)
    required_options = {
        # Code Mode is always on in nanocode 0.7.x; stating it keeps the run's tool shape on the record.
        "code_mode": ("on",),
        # Subagent tools join the Code Mode catalog (bigger prompt, more tools); "off" matches the other harnesses.
        "subagents": ("off", "on"),
    }

    def prepare(self, home, base_url, model, effort, max_output_tokens):
        state = home / ".nanocode"
        state.mkdir(parents=True, exist_ok=True)
        return {
            # The client appends /models and /responses itself.
            "NANOCODE_OPENAI_BASE_URL": base_url,
            "NANOCODE_OPENAI_API_KEY": "swarm-local-no-key",
            "NANOCODE_HOME": str(state),
            "NANOCODE_TELEMETRY": "off",
            "NANOCODE_NO_TEMPO_TOOLS": "true",  # a clap bool: "1" is rejected
        }

    def argv(self, binary, prompt, workdir, model, effort):
        argv = [str(binary), "exec", "--model", model, "--thinking", effort, "--ephemeral", "--telemetry", "off",
                "--no-tempo-tools", "--cd", str(workdir)]
        if self.options.get("subagents") != "on":  # validated specs say "off"; live.py builds argv without options
            argv.append("--no-subagents")
        return argv + ["--", prompt]
