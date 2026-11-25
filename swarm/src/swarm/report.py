"""Static, self-contained HTML report for a bundle."""
import html
import json
from pathlib import Path

from .util import read_jsonl

# Colour follows the harness, never its position. First three are the
# validated all-pairs slots (dataviz reference palette); Ante keeps orange.
SERIES = {
    "ante": ("#eb6834", "#d95926"),
    "hermes": ("#2a78d6", "#3987e5"),
    "mini": ("#1baf7a", "#199e70"),
    "pi": ("#eda100", "#c98500"),
    "claude-code": ("#e87ba4", "#d55181"),
    "codex": ("#4a3aa7", "#9085e9"),
}
FALLBACK = ("#6b6b6b", "#9a9a9a")

CSS = """
:root{--page:#F9F9F9;--card:#FFFFFF;--ink:#252525;--muted:#5f5f5f;--hair:#e4e4e2;--grid:#ececea;--brand:#EA5518;
--ok:#1f7a4d;--bad:#b3261e;--warnbg:#fff4e5;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--page:#141413;--card:#1c1c1b;--ink:#ededeb;--muted:#a3a3a0;
--hair:#2e2e2c;--grid:#262624;--ok:#5cc28f;--bad:#f08a80;--warnbg:#2a2118;color-scheme:dark}}
:root[data-theme="dark"]{--page:#141413;--card:#1c1c1b;--ink:#ededeb;--muted:#a3a3a0;--hair:#2e2e2c;--grid:#262624;--ok:#5cc28f;
--bad:#f08a80;--warnbg:#2a2118;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);font:15px/1.5 "Space Grotesk",system-ui,sans-serif}
main{max-width:1080px;margin:0 auto;padding:32px 16px 64px}
.mono,td.n,th.n{font-family:"IBM Plex Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}
h1{font-size:22px;margin:0 0 4px;font-weight:600}h2{font-size:15px;margin:32px 0 12px;font-weight:600}
.eyebrow{color:var(--brand);font-size:12px;letter-spacing:.08em;text-transform:uppercase;font-weight:600}
.muted{color:var(--muted)}.card{background:var(--card);border:1px solid var(--hair);border-radius:18px;padding:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}
.pill{display:inline-block;border-radius:6px;padding:1px 8px;font-size:12px;font-weight:600;border:1px solid var(--hair)}
.pill.ok{color:var(--ok)}.pill.bad{color:var(--bad)}
.note{background:var(--warnbg);border-radius:10px;padding:10px 14px;font-size:13px;margin-top:12px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--hair)}
th{font-weight:600;color:var(--muted)}td.n,th.n{text-align:right}td.n{white-space:nowrap}.unk{border-bottom:1px dashed var(--muted);color:var(--muted)}
.wrap{overflow-x:auto}.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;margin-bottom:8px}
.sw{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin-right:6px}
svg text{fill:var(--muted);font:11px "IBM Plex Mono",ui-monospace,monospace}svg .lbl{fill:var(--ink)}
#tip{position:fixed;pointer-events:none;background:var(--card);border:1px solid var(--hair);border-radius:8px;padding:6px 10px;
font:12px "IBM Plex Mono",ui-monospace,monospace;display:none;z-index:9}
.dot{cursor:default}.dot:hover{r:6}
"""

JS = """
const tip=document.getElementById('tip');
document.querySelectorAll('.dot').forEach(d=>{
 d.addEventListener('mouseenter',e=>{tip.textContent=d.dataset.tip;tip.style.display='block'});
 d.addEventListener('mousemove',e=>{tip.style.left=(e.clientX+12)+'px';tip.style.top=(e.clientY+12)+'px'});
 d.addEventListener('mouseleave',()=>{tip.style.display='none'})});
"""


def _fmt(value, digits=2):
    if value is None:
        return '<span class="unk" title="not measured">—</span>'
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return html.escape(str(value))


def _chart(groups, metric, title, unit):
    harnesses = sorted({g["harness"] for g in groups})
    levels = sorted({g["level"] for g in groups})
    values = [g[metric] for g in groups if g.get(metric) is not None]
    width, height, left, right, top, bottom = 460, 240, 48, 70, 16, 36
    top_value = (max(values) * 1.15 if values else 0) or 1
    x = lambda level: left + (levels.index(level) + 0.5) * (width - left - right) / len(levels)  # noqa: E731
    y = lambda value: top + (height - top - bottom) * (1 - value / top_value)  # noqa: E731
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}" width="100%">']
    for i in range(5):
        value = top_value * i / 4
        parts.append(f'<line x1="{left}" x2="{width - right}" y1="{y(value):.1f}" y2="{y(value):.1f}" stroke="var(--grid)" stroke-width="1"/>')
        parts.append(f'<text x="{left - 6}" y="{y(value) + 4:.1f}" text-anchor="end">{value:.3g}</text>')
    for level in levels:
        parts.append(f'<text x="{x(level):.1f}" y="{height - 14}" text-anchor="middle">{level}</text>')
    parts.append(f'<text x="{(left + width - right) / 2:.1f}" y="{height - 1}" text-anchor="middle">concurrent instances</text>')
    for harness in harnesses:
        colour = f"var(--s-{harness})"
        points = sorted((g["level"], g[metric]) for g in groups if g["harness"] == harness and g.get(metric) is not None)
        if not points:
            continue
        path = " ".join(f"{'M' if i == 0 else 'L'}{x(l):.1f},{y(v):.1f}" for i, (l, v) in enumerate(points))
        parts.append(f'<path d="{path}" fill="none" stroke="{colour}" stroke-width="2" stroke-linejoin="round"/>')
        for level, value in points:
            tip = f"{harness} · n={level} · {value:.3g} {unit}"
            parts.append(f'<circle class="dot" cx="{x(level):.1f}" cy="{y(value):.1f}" r="4" fill="{colour}" '
                         f'stroke="var(--card)" stroke-width="2" data-tip="{html.escape(tip)}"/>')
        last_level, last_value = points[-1]
        parts.append(f'<text class="lbl" x="{x(last_level) + 10:.1f}" y="{y(last_value) + 4:.1f}">{html.escape(harness)}</text>')
    parts.append("</svg>")
    return f'<div class="card"><div class="muted" style="font-size:13px;margin-bottom:6px">{html.escape(title)}</div>{"".join(parts)}</div>'


def _options(spec):
    stated = spec.get("harness_options") or {}
    parts = [f"{h}: " + (", ".join(f"{k}={v}" for k, v in sorted(stated.get(h, {}).items())) or "no options")
             for h in spec["harnesses"]]
    return "Harness options · " + " · ".join(parts)


def render(root: Path) -> str:
    root = Path(root)
    summary = json.loads((root / "summary.json").read_text())
    plan = json.loads((root / "plan.json").read_text())
    host = json.loads((root / "host.json").read_text())
    attempts = read_jsonl(root / "attempts.jsonl")
    admission = read_jsonl(root / "admission.jsonl")
    groups = sorted(summary["groups"].values(), key=lambda g: (g["harness"], g["level"]))
    spec = plan["spec"]
    harnesses = sorted({g["harness"] for g in groups})
    series_css = ":root{" + "".join(f"--s-{h}:{SERIES.get(h, FALLBACK)[0]};" for h in harnesses) + "}"
    series_css += "@media (prefers-color-scheme:dark){:root:not([data-theme=\"light\"]){" + \
        "".join(f"--s-{h}:{SERIES.get(h, FALLBACK)[1]};" for h in harnesses) + "}}"
    series_css += ":root[data-theme=\"dark\"]{" + "".join(f"--s-{h}:{SERIES.get(h, FALLBACK)[1]};" for h in harnesses) + "}"
    fp = host["fingerprint"]
    machine = f"{fp.get('model') or fp.get('os') or fp['system']} · {fp['machine']} · {fp['cpus']} CPU · " \
              f"{fp['memory'].get('total_mb') or '?'} MB"
    complete = summary.get("complete")
    status = '<span class="pill ok">complete</span>' if complete else '<span class="pill bad">incomplete</span>'
    held = [a for a in admission if a.get("held_for")]
    legend = "".join(f'<span><span class="sw" style="background:var(--s-{h})"></span>{html.escape(h)}</span>' for h in harnesses)
    rows = "".join(
        f"<tr><td>{html.escape(g['harness'])}</td><td class='n'>{g['level']}</td><td class='n'>{g['passes']}/{g['attempts']}</td>"
        f"<td class='n'>{_fmt(g['pass_rate_wilson95'][0] if g['pass_rate_wilson95'] else None)}–{_fmt(g['pass_rate_wilson95'][1] if g['pass_rate_wilson95'] else None)}</td>"
        f"<td class='n'>{_fmt(g['successes_per_minute'])}</td><td class='n'>{_fmt(g['wall_s_median'])}</td>"
        f"<td class='n'>{_fmt(g['peak_rss_mb_median'], 1)}</td><td class='n'>{_fmt(g['tree_rss_mb_median'], 1)} <span class='muted'>({g['tree_rss_sampled']})</span></td>"
        f"<td class='n'>{g['escaped_processes']}{(' · ' + _fmt(g['escaped_rss_mb_median'], 0) + ' MB') if g['escaped_processes'] else ''}</td><td class='n'>{_fmt(g['cpu_s_per_success'], 3)}</td>"
        f"<td class='n'>{_fmt(g['max_running'])}</td><td class='n'>{_fmt(g['admission_wait_s_total'])}</td>"
        f"<td>{html.escape(', '.join(f'{k}×{v}' for k, v in g['failures'].items())) or '—'}</td></tr>"
        for g in groups)
    timed = [g for g in groups if "first_request_s_median" in g]
    timing_rows = "".join(
        f"<tr><td>{html.escape(g['harness'])}</td><td class='n'>{g['level']}</td><td class='n'>{_fmt(g['first_request_s_median'], 3)}</td>"
        f"<td class='n'>{_fmt(g['first_request_s_p90'], 3)}</td><td class='n'>{_fmt(g['step_gap_s_median'], 3)}</td>"
        f"<td class='n'>{_fmt(g.get('step_gap_s_p90'), 3)}</td>"
        f"<td class='n'>{_fmt(g.get('step_gap_s_early_median'), 3)}</td><td class='n'>{_fmt(g.get('step_gap_s_late_median'), 3)}</td>"
        f"<td class='n'>{_fmt(g['after_final_s_median'], 3)}</td>"
        f"<td class='n'>{_fmt(g.get('context_slope_median'), 1)}</td><td class='n'>{_fmt(g.get('context_bytes_last_median'), 0)}</td>"
        f"<td class='n'>{_fmt(g.get('model_turns_median'), 0)}</td><td class='n'>{_fmt(g.get('probes_median'), 0)}</td></tr>"
        for g in timed)
    timing_html = ("<h2>Startup and per-step overhead</h2><div class='card wrap'><table><tr><th>Harness</th><th class='n'>N</th>"
                   "<th class='n'>Launch → 1st p50 s</th><th class='n'>p90 s</th><th class='n'>Step gap p50 s</th><th class='n'>p90 s</th>"
                   "<th class='n'>Early gap s</th><th class='n'>Late gap s</th><th class='n'>Final → exit s</th>"
                   "<th class='n'>Context slope B/step</th><th class='n'>Context growth B</th>"
                   "<th class='n'>Model turns</th><th class='n'>Probes</th></tr>" + timing_rows + "</table></div>"
                   "<div class='card muted'>Read from the scripted model's clock. The step gap includes the tool the reply asked for, "
                   "so it is harness overhead on tasks whose tools are trivial. Early and late gaps are the first and last 25 gaps, "
                   "and stay blank until a session has 25 gaps. Context growth subtracts the first request, so tool schemas are not "
                   "read as resent file bytes. A dash under successes per minute means the wave did not reach its size or a launch waited. "
                   "mini is the floor: compare harnesses with it, and do not read a scripted pass as a solved task.</div>") if timed else ""
    failures = [a for a in attempts if not a["passed"]]
    fail_rows = "".join(
        f"<tr><td class='mono'>{html.escape(a['attempt'])}</td><td>{html.escape(a['task'])}</td><td>{html.escape(a['failure'])}</td>"
        f"<td class='n'>{_fmt(a['agent'].get('exit_code'))}</td><td>{html.escape(', '.join(a['requests'].get('errors', [])) or '—')}</td></tr>"
        for a in failures[:200])
    synthetic = spec["model"]["source"] == "scripted"
    primary = spec.get("primary_outcome") or "successes_per_minute"
    chart_field, chart_title, chart_unit = {
        "successes_per_minute": ("successes_per_minute", "Verified successes per minute, only when the wave filled", "per min"),
        "first_request_s": ("first_request_s_median", "Launch to first request, median seconds", "s"),
        "after_final_s": ("after_final_s_median", "Final reply to exit, median seconds", "s"),
        "step_gap_s": ("step_gap_s_median", "Step gap, median seconds", "s"),
        "context_slope": ("context_slope_median", "Context growth per step, median bytes", "B"),
        "wall_s": ("wall_s_median", "Wall time, median seconds", "s"),
        "pass_rate": ("pass_rate", "Pass rate", ""),
        "peak_rss_mb": ("peak_rss_mb_median", "Peak memory per instance, median MB", "MB"),
        "cpu_s_per_success": ("cpu_s_per_success", "CPU seconds per verified success", "s"),
    }.get(primary, ("successes_per_minute", "Verified successes per minute, only when the wave filled", "per min"))
    maze_html = ""
    if any(g.get("maze_runs") for g in groups):
        mrows = "".join(
            f"<tr><td>{html.escape(g['harness'])}</td><td class='n'>{g['level']}</td>"
            f"<td class='n'>{g.get('maze_exits', 0)}/{g.get('maze_runs', 0)}</td>"
            f"<td class='n'>{_fmt(g.get('maze_exit_s_median'), 3)}</td>"
            f"<td class='n'>{_fmt(g.get('maze_calls_median'), 1)}</td>"
            f"<td class='n'>{_fmt(g.get('maze_unique_cells_median'), 1)}</td>"
            f"<td class='n'>{_fmt(g.get('maze_repeat_pct_median'), 1)}</td>"
            f"<td class='n'>{_fmt(g.get('maze_credit_agents_median'), 1)}</td></tr>"
            for g in groups if g.get("maze_runs"))
        maze_html = (
            "<h2>Maze coordination</h2><div class='card wrap'><table>"
            "<tr><th>Harness</th><th class='n'>N</th><th class='n'>Exits</th><th class='n'>Exit t p50 s</th>"
            "<th class='n'>Calls p50</th><th class='n'>Unique cells p50</th><th class='n'>Repeat % p50</th>"
            "<th class='n'>Credited agents p50</th></tr>"
            f"{mrows}</table></div>"
        )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Swarm run report</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Space+Grotesk:wght@400;600&display=swap" rel="stylesheet">
<style>{CSS}{series_css}</style></head><body><main>
<div class="eyebrow">Swarm · run report</div>
<h1>{html.escape(spec['question'])}</h1>
<div class="muted">{status} · primary outcome <b>{html.escape(spec['primary_outcome'])}</b> · protocol <b>{html.escape(spec.get('protocol') or 'naive')}</b> · plan <span class="mono">{plan['plan_sha256'][:12]}</span> · {summary['cells_run']}/{summary['cells_planned']} cells</div>
<div class="muted">{html.escape(_options(spec))}</div>
<div class="muted">{html.escape(machine)} · sandbox <b>{html.escape(host['sandbox'])}</b> ({html.escape(host['isolation'])})</div>
{'<div class="note">Scripted model, protocol ' + html.escape(spec.get('protocol') or 'naive') + ': the tool steps are fixed. A pass means the harness followed that script and the hidden tests matched. It does not mean the harness chose the steps or solved the task. Token counts are synthetic.</div>' if synthetic else ''}
<h2>Primary outcome</h2><div class="legend">{legend}</div>
<div class="grid">{_chart(groups, chart_field, chart_title, chart_unit)}
{_chart(groups, 'peak_rss_mb_median', 'Peak memory per instance, median MB (largest process, rusage; lower is better)', 'MB')}</div>
<h2>Results by harness and level</h2><div class="card wrap"><table>
<tr><th>Harness</th><th class="n">N</th><th class="n">Passed</th><th class="n">95% CI</th><th class="n">Succ/min</th><th class="n">Wall p50 s</th>
<th class="n">Peak MB p50</th><th class="n">Tree MB p50</th><th class="n">Escaped</th><th class="n">CPU s/success</th><th class="n">Max running</th><th class="n">Admit wait s</th><th>Failures</th></tr>{rows}</table></div>
{timing_html}{maze_html}
<h2>Admission</h2><div class="card muted">{len(admission)} launch decisions, {len(held)} held at least once
(reserve {spec['admission']['reserve_mb']} MB and {spec['admission'].get('reserve_tasks', 0)} task slots, max pressure {html.escape(spec['admission']['max_pressure'])}).
N is requested concurrency per cell, Max running is the peak number of agents alive at once, and Passed is verified completions. Succ/min divides verified passes by total cell wall time (sandbox setup, agent run, hidden check and cleanup), and stays blank if the wave did not reach N simultaneously or if any launch was held by admission. Peak MB is the largest single process (kernel rusage, complete for every attempt). Tree MB is process-tree RSS sampled every {spec['sample_interval_s']} s; it double-counts shared pages and misses attempts shorter than the interval (sample count in brackets). Escaped counts processes that detached from the attempt and outlived it (found by an environment tag); they are recorded with their memory, stopped, and fail the attempt as descendant_leak. CPU is rusage of each agent and the descendants it reaped.</div>
<h2>Failures ({len(failures)})</h2><div class="card wrap">{'<table><tr><th>Attempt</th><th>Task</th><th>Class</th><th class="n">Exit</th><th>Request errors</th></tr>' + fail_rows + '</table>' if failures else '<span class="muted">None.</span>'}</div>
<p class="muted" style="font-size:12px;margin-top:24px">Verify this bundle offline with <span class="mono">swarm verify &lt;bundle&gt;</span>. Stop rule: {html.escape(spec['stop_rule'])}</p>
</main><div id="tip"></div><script>{JS}</script></body></html>"""


def write_report(root: Path):
    (Path(root) / "report.html").write_text(render(root))
