# Swarm — agent notes

Read DESIGN.md first. Principles there are requirements, not style.

- A frozen plan, its tasks and harness binaries must not change silently. New behaviour needs a new plan; never edit a sealed bundle or a sealed task.
- Never retry an attempt or drop a failure. Unknown values stay null.
- Any harness setting that changes results (telemetry, prepared homes, effort) must be a required, recorded option.
- A change to measurement code (`runner`, `sandbox`, `host`, `scripted`, `relay`, `gate`, `stats`) requires a `VERSION` bump; see docs/evaluation.md.
- Write bundles and serve data outside the source tree. Never commit run output, bundles, logs or telemetry.
- Tests and local runs use the scripted model only: no real model calls, no credentials, no network access for agents.
- Fixtures stay synthetic. Generated tasks (`tools/make_tier0_tasks.py`, `tools/make_maze_tasks.py`) are checked against their committed copies; regenerate rather than hand-edit them.
