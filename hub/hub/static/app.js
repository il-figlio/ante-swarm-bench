const esc = s => String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
const DATA = {};
const CELLS = ["bench-c-01","bench-c-02","bench-c-03","bench-c-04"], COL = ["var(--c1)","var(--c2)","var(--c3)","var(--c4)"];
// Per-run display defaults: a run's own meta.json supplies the model; no run carries a hard-coded note.
const META_FIXED = {};
const META = new Proxy(META_FIXED, {get:(o,k)=> o[k] || {model:"—", note:""}});
let cur = null;
function normalize(raw){
  const meta = raw.meta || {};
  const log = raw.log || []; const sec = t => { const [h,m,s]=t.split(":").map(Number); return h*3600+m*60+s; };
  const t0 = log.length ? Math.min(...log.map(x=>sec(x.t)-Number(x.secs))) : 0;
  const phases = log.map(x=>({cell:x.cell,action:x.action,start:+(sec(x.t)-Number(x.secs)-t0).toFixed(1),end:+(sec(x.t)-t0).toFixed(1),result:x.result,notes:x.notes}));
  const sm = raw.samples || []; const s0 = sm.length ? Math.min(...sm.map(v=>v.t)) : 0;
  const samples = sm.map(v=>({cell:v.cell,t:+(v.t-s0).toFixed(1),rss_mb:+(v.ante_rss_kb/1024).toFixed(1),cpu:v.ante_cpu_pct,procs:v.ante_procs,tokens_in:v.tokens_in,tokens_out:v.tokens_out,vm_mb:Math.round(v.vm_used_kb/1024)}));
  const rc = raw.receipts || {results:[],merged:[],checks:"running…",total_secs:log.length?Math.max(...phases.map(p=>p.end)):0,live:true};
  if(!raw.receipts){ rc.results = phases.filter(p=>p.action.startsWith("ante task")).map(p=>({task:p.action.split(" ").pop(),cell:p.cell,ante_secs:+(p.end-p.start).toFixed(2),rusage:p.notes,files:(p.notes.match(/files=(.*)$/)||[,""])[1],ante_rc:p.result==="ok"?"0":p.result})); }
  return {phases, samples, receipts: rc, meta};
}
const RAW = {};
async function load(id){
  const prev = RAW[id]; const q = prev && prev.live ? `?log_after=${prev.log_total||0}&samples_after=${prev.samples_total||0}` : "";
  const r = await fetch("api/runs/"+id+q,{cache:"no-store"}); const inc = await r.json();
  const raw = prev && q ? {...prev, ...inc, log:(prev.log||[]).concat(inc.log||[]), samples:(prev.samples||[]).concat(inc.samples||[])} : inc;
  RAW[id]=raw; DATA[id]=normalize(raw); DATA[id].live = !!raw.live && !raw.receipts; DATA[id].meta = raw.meta; return DATA[id]; }
async function refreshList(){ const runs = await (await fetch("api/runs",{cache:"no-store"})).json();
  $("#runs").innerHTML = runs.slice().reverse().map(r=>`<button role="tab" data-run="${r.id}" id="run-${r.id}" aria-pressed="${r.id===cur}">${r.id} <span class="eyebrow" style="margin-left:6px">${r.harness||"ante"}${r.mode==="local"?" ×"+(r.cells||""):""}</span>${r.live?' <span class="eyebrow" style="color:var(--accent)">live</span>':''}</button>`).join("");
  if(!cur && runs.length){ cur = runs[runs.length-1].id; }
  runs.forEach(r=>{ if(r.aborted){ const b=document.getElementById("run-"+r.id); if(b) b.title="runner exited without receipts"; }});
  return runs; }
async function tick(){ await refreshList(); if(cur){ await load(cur); render(); if(DATA[cur].live) setTimeout(tick, 2000); } }
const $ = s => document.querySelector(s);
function fmt(n,d=1){return Number(n).toFixed(d)}
function render(){
  const d = DATA[cur], rc = d.receipts, ok = rc.checks.startsWith("OK");
  document.querySelectorAll("#runs button").forEach(b=>b.setAttribute("aria-pressed", b.dataset.run===cur));
  const taskSecs = rc.results.map(r=>r.ante_secs), peak = Math.max(...rc.results.map(r=>+(r.rusage.match(/peak_rss_kb=(\d+)/)||[0,0])[1]))/1024;
  const density = (d.meta||{}).mode==="local"; const passed = rc.passed!=null ? rc.passed+"/"+rc.attempted : null;
  const lastTok = [...(d.samples||[])].reverse().find(v=>v.tokens_in>0);
  $("#tiles").innerHTML = [
    ["Total wall time", fmt(rc.total_secs,1), "s", "clone → merge → destroy", ""],
    ["Slowest cell task", (taskSecs.length?fmt(Math.max(...taskSecs),1):"—"), "s", "fastest "+(taskSecs.length?fmt(Math.min(...taskSecs),1):"—")+" s", ""],
    ["Peak "+((d.meta||{}).harness||"ante")+" memory", fmt(peak,1), "MB", "per instance, model in the loop", ""],
    density ? ["Instances on one VM", (d.meta||{}).cells||"—", "", "peak "+((rc.peak_ante_procs)||"—")+" ante processes", ""] : ["Branches merged", (rc.merged||[]).filter(m=>m.merged).length+"/"+(rc.merged||[]).length, "", "by the reviewer", ok?"ok":"bad"],
    passed!=null ? ["Tasks passed", passed, "", "each task's own check", rc.passed===rc.attempted&&rc.attempted?"ok":"bad"] : ["Checks", String(rc.checks||"running…"), "", "", ok?"ok":"bad"],
    density ? [((d.meta||{}).harness||"ante")+" total on VM", rc.peak_vm_ante_rss_kb?fmt(rc.peak_vm_ante_rss_kb/1024,0):"—", "MB", "peak summed RSS of all instances", ""] : ["Model", (d.meta||{}).model||META[cur].model||"—", "", ((d.meta||{}).effort||"")+" · exe.dev integration", ""],
    rc.tokens ? ["Tokens", (rc.tokens.input+rc.tokens.output).toLocaleString(), "", rc.tokens.input.toLocaleString()+" in ("+(rc.tokens.cache_read||0).toLocaleString()+" cached) · "+rc.tokens.output.toLocaleString()+" out"+(rc.max_tokens?" · ceiling "+rc.max_tokens.toLocaleString():""), ""]
      : (d.live && lastTok) ? ["Tokens so far", (lastTok.tokens_in+lastTok.tokens_out).toLocaleString(), "", "live from the harness event stream"+((d.meta||{}).max_tokens?" · ceiling "+(d.meta||{}).max_tokens.toLocaleString():""), ""] : null,
    rc.shadow_cost_usd!=null ? ["Shadow cost", "$"+rc.shadow_cost_usd.toFixed(2), "", "list price for these tokens; actual run was a subscription", ""] : null
  ].filter(Boolean).map(([k,v,u,s,c])=>`<div class="tile ${c}"><div class="k">${k}</div><div class="v">${v}${u?`<small>${u}</small>`:""}</div><div class="eyebrow" style="margin-top:6px">${s}</div></div>`).join("");
  gantt(d.phases); mem(d.samples); receipts(rc); events(cur); $("#foot").textContent = (META[cur].note||"") + (d.live ? " Live: refreshing every 2 s." : " Source: runs/"+cur+" on the hub host.");
}
const EVCUR = {}, EVBUF = {};
function evLine(l){ try { const j = JSON.parse(l);
  const kind = j.type || j.event || j.kind || "";
  const detail = j.name || j.tool_name || j.tool || (j.data && (j.data.name || j.data.tool_name)) || j.file || j.path || "";
  return (kind + (detail ? " " + detail : "")) || l.slice(0, 100);
} catch(e){ return l.slice(0, 100); } }
async function events(id){
  const after = EVCUR[id] || 0;
  let j = {}; try { j = await (await fetch("api/runs/"+id+"/events?after="+after,{cache:"no-store"})).json(); } catch(e){ return; }
  if(j.events==null){ $("#events").innerHTML = `<span class="eyebrow">no event stream for this run</span>`; return; }
  EVCUR[id] = (j.after != null ? j.after : after) + (j.events || []).length;  // advance only by delivered rows so >200-event bursts aren't skipped
  const buf = EVBUF[id] = EVBUF[id] || [];
  for(const e of j.events){
    const lines = (e.data||"").split("\n").filter(l=>l.trim());
    const last = lines.length ? evLine(lines[lines.length-1]) : "";
    if(last) buf.push({cell:(e.cell||"").replace(/^[^/]+\//,""), task:e.task||"", text:last});
  }
  if(buf.length>400) buf.splice(0, buf.length-400);
  $("#events").innerHTML = buf.slice(-16).map(r=>`<span style="color:var(--ink-3)">${esc((r.cell+" ").padEnd(6))}</span><span style="color:var(--accent-ink)">${esc((r.task+" ").padEnd(10))}</span>${esc(r.text.slice(0,120))}`).join("\n") || `<span class="eyebrow">no events yet</span>`;
  const el = $("#events"); el.scrollTop = el.scrollHeight;
}
function gantt(ph){
  const rows = ph.filter(p=>p.cell!=="brain"&&p.cell!=="reviewer"); const names=[...new Set(rows.map(p=>p.cell))]; const W=920,L=130,R=20,rowH=names.length>8?14:22,gap=names.length>8?2:4,T=18;
  const groups = names.map(c=>rows.filter(p=>p.cell===c)); const H=T+names.length*(rowH+gap)+30;
  const tmax = Math.ceil(Math.max(...rows.map(p=>p.end))/10)*10, x=t=>L+(t/tmax)*(W-L-R);
  let s=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Cell lifecycle timeline">`;
  for(let t=0;t<=tmax;t+=tmax>60?20:10){s+=`<line x1="${x(t)}" x2="${x(t)}" y1="${T-6}" y2="${H-24}" stroke="var(--grid)"/><text x="${x(t)}" y="${H-8}" text-anchor="middle">${t}s</text>`}
  groups.forEach((g,i)=>{const y=T+i*(rowH+gap); s+=`<text x="${L-8}" y="${y+rowH/2+4}" text-anchor="end" fill="var(--ink)" style="font-size:${rowH<20?9:11}px">${esc(names[i].replace(/^[^/]+\//,""))}</text>`;
    g.forEach(p=>{const w=Math.max(1.5,x(p.end)-x(p.start)); const strong=/ante task|ssh ready/.test(p.action); s+=`<rect x="${x(p.start)}" y="${y}" width="${w}" height="${rowH}" rx="3" fill="${COL[i%COL.length]}" opacity="${strong?1:.45}" data-tip="${esc(names[i]+" · "+p.action+" · "+fmt(p.end-p.start,1)+" s · "+p.result)}"/>`;
      if(w>52&&rowH>=20) s+=`<text x="${x(p.start)+6}" y="${y+rowH/2+4}" fill="var(--paper)" style="font-size:10px">${esc(p.action)}</text>`;});});
  s+=`</svg><div class="tip"></div>`; $("#gantt").innerHTML=s; hover($("#gantt"));
}
function mem(sm){
  const W=920,L=56,R=20,T=14,B=28,H=230; const tmax=Math.max(10,...sm.map(v=>v.t)); const maxv=Math.max(0,...sm.map(v=>v.rss_mb)); const ymax=Math.max(40, Math.ceil(maxv/50)*50);
  const x=t=>L+(t/tmax)*(W-L-R), y=v=>T+(1-v/ymax)*(H-T-B);
  let s=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Ante resident memory per cell">`;
  for(let v=0;v<=ymax;v+=(ymax>40?ymax/5:10)){s+=`<line x1="${L}" x2="${W-R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/><text x="${L-8}" y="${y(v)+4}" text-anchor="end">${v} MB</text>`}
  for(let t=0;t<=tmax;t+=10){s+=`<text x="${x(t)}" y="${H-8}" text-anchor="middle">${t}s</text>`}
  const cellsHere = [...new Set(sm.map(v=>v.cell))]; const CL = cellsHere.length?cellsHere:CELLS;
  CL.forEach((c,i)=>{const pts=sm.filter(v=>v.cell===c&&v.rss_mb>0).sort((a,b)=>a.t-b.t); if(!pts.length)return;
    s+=`<path d="${pts.map((p,j)=>(j?"L":"M")+x(p.t).toFixed(1)+" "+y(p.rss_mb).toFixed(1)).join(" ")}" fill="none" stroke="${COL[i%COL.length]}" stroke-width="2" stroke-linejoin="round"/>`;
    pts.forEach(p=>s+=`<circle cx="${x(p.t)}" cy="${y(p.rss_mb)}" r="4" fill="${COL[i%COL.length]}" stroke="var(--paper)" stroke-width="2" data-tip="${esc(c+" · "+p.rss_mb+" MB"+(p.procs?" · "+p.procs+" procs":"")+" · "+p.t+" s")}"/>`);
    const last=pts[pts.length-1]; s+=`<text x="${x(last.t)+8}" y="${y(last.rss_mb)+4}" fill="var(--ink)">${last.rss_mb} MB</text>`;});
  s+=`</svg><div class="tip"></div>`; $("#mem").innerHTML=s; hover($("#mem"));
  $("#memlegend").innerHTML=CL.map((c,i)=>`<span style="--sw:${COL[i%COL.length]}">${esc(c)}${cellsHere.length===1?" · summed over all ante processes":""}</span>`).join("");
}
async function killInstance(label){ await act("api/swarms/"+cur+"/kill",{label}); }
function receipts(rc){
  const d = DATA[cur]||{}; const live = d.live;
  if(live){ const running = [...new Set(d.phases.filter(p=>p.action==="ship base repo"&&p.result==="ok").map(p=>p.cell))].filter(c=>!d.phases.some(p=>p.cell===c&&p.action.startsWith("ante task")));
    if(running.length){ $("#receipts").innerHTML = `<thead><tr><th>Running instance</th><th></th></tr></thead><tbody>${running.map(c=>`<tr><td class="mono">${esc(c)}</td><td><button class="killbtn" data-label="${esc(c)}">Kill</button></td></tr>`).join("")}</tbody>`;
      $("#receipts").querySelectorAll(".killbtn").forEach(b=>b.addEventListener("click",()=>killInstance(b.dataset.label))); return; } }
  const rows=rc.results.map(r=>{const m=(rc.merged||[]).find(x=>x.task===r.task&&(!x.cell||x.cell===r.cell))||{}; const ru=Object.fromEntries([...r.rusage.matchAll(/(\w+)=([\d.]+)/g)].map(a=>[a[1],a[2]]));
    return `<tr><td class="mono">${esc(r.task)}</td><td class="mono">${esc(r.cell)}</td><td class="n mono">${fmt(r.ante_secs,1)}</td><td class="n mono">${fmt(ru.peak_rss_kb/1024,1)}</td><td class="n mono">${fmt(+ru.user_s+ +ru.sys_s,2)}</td><td class="mono">${esc(r.files||"—")}</td><td class="${r.ante_rc==="0"?"ok":"bad"}">${r.ante_rc==="0"?"exit 0":"rc "+esc(r.ante_rc)}</td><td class="${(r.check||"").startsWith("OK")?"ok":"bad"}">${esc(r.check||"—")}</td><td class="${m.merged?"ok":(m.task?"bad":"")}">${m.task?(m.merged?"merged":"not merged"):"—"}</td></tr>`}).join("");
  $("#receipts").innerHTML=`<thead><tr><th>Task</th><th>Cell</th><th class="n">Task s</th><th class="n">Peak RSS MB</th><th class="n">CPU s</th><th>Files changed</th><th>Ante</th><th>Check</th><th>Reviewer</th></tr></thead><tbody>${rows}</tbody>`;
}
function hover(root){const tip=root.querySelector(".tip"); root.querySelectorAll("[data-tip]").forEach(el=>{el.addEventListener("mousemove",e=>{const b=root.getBoundingClientRect(); tip.textContent=el.dataset.tip; tip.style.display="block"; tip.style.left=(e.clientX-b.left+root.scrollLeft)+"px"; tip.style.top=(e.clientY-b.top)+"px"}); el.addEventListener("mouseleave",()=>tip.style.display="none")})}
$("#runs").addEventListener("click",async e=>{const b=e.target.closest("button"); if(b){cur=b.dataset.run; await load(cur); render(); if(DATA[cur].live) setTimeout(tick,2000);}});
let STATUS = {active:[]}, SRC = "pack";
async function status(){ try { STATUS = await (await fetch("api/status",{cache:"no-store"})).json(); } catch(e){}
  const active = STATUS.active||[]; $("#stop").disabled = !active.length; $("#start").disabled = !!active.length;
  const pk = $("#pack"); if(STATUS.packs && STATUS.packs.join() !== [...pk.options].map(o=>o.value).join()){ const cur_ = pk.value; pk.innerHTML = STATUS.packs.map(p=>`<option${p===cur_?" selected":""}>${p}</option>`).join(""); }
  const ck = $("#check"); if(STATUS.checks){ const opts = STATUS.checks[pk.value]||[]; const curC = ck.value; ck.innerHTML = `<option value="">no check — run only</option>`+opts.map(c=>`<option${c===curC?" selected":""}>${c}</option>`).join(""); }
  const txt = active.length ? "running "+active.join(", ") : "idle · max "+STATUS.max_vms+" VMs · "+STATUS.max_local+" instances";
  $("#status").textContent = txt; $("#statustext").textContent = active.length ? "live · "+active.join(",") : "idle";
  $("#statuspill").classList.toggle("live", !!active.length);
  const modeSel=$("#mode"), cells=$("#cells"); cells.max = modeSel.value==="vm" ? STATUS.max_vms : STATUS.max_local; if(+cells.value>+cells.max) cells.value=cells.max; estimate(); }
function estimate(){ const n=+$("#cells").value, el=$("#estimate");
  if(SRC==="custom"){ const lines=$("#prompts").value.split("\n").map(l=>l.trim()).filter(Boolean);
    el.textContent = lines.length ? `${lines.length} task${lines.length>1?"s":""} from the prompt list · ${n} cell${n>1?"s":""} · no history-based estimate for ad-hoc tasks` : "write at least one task prompt — one per line";
    return; }
  const h=(STATUS.history||{})[$("#harness").value+":"+$("#pack").value];
  if(!h){ el.textContent="no history yet for this harness and pack; the first run sets the baseline"; return; }
  const med=h.median_tokens_per_task*n, p90=h.p90_tokens_per_task*n; const p=STATUS.usd_per_mtok||{};
  const usd = (p.input&&p.output) ? ` · shadow ≈$${((med*0.93*p.input+med*0.07*p.output)/1e6).toFixed(2)}–$${((p90*n/med*med*0.93*p.input+p90*n/med*med*0.07*p.output)/1e6).toFixed(2)} at list price` : " · $ needs a rate card";
  el.textContent = `estimate for ${n} tasks: ≈${med.toLocaleString()} tokens (median), up to ≈${p90.toLocaleString()} (p90) from ${h.samples} past tasks${usd}. Suggested ceiling: ${Math.round(p90*1.5/10000)*10000}`;
  if(+$("#maxtok").value===0) $("#maxtok").placeholder = String(Math.round(p90*1.5/10000)*10000); }
["harness","pack","cells","prompts"].forEach(id=>$("#"+id).addEventListener("input",estimate));
$("#src").addEventListener("click",e=>{ const b=e.target.closest("button"); if(!b) return; SRC=b.dataset.src;
  $("#src").querySelectorAll("button").forEach(x=>x.setAttribute("aria-pressed",x===b));
  const custom = SRC==="custom";
  $("#row-pack").querySelector("span").textContent = custom ? "Base repo" : "Task pack";
  $("#row-check").hidden = !custom; $("#row-prompts").hidden = !custom; estimate(); });
$("#mode").addEventListener("change",()=>{ $("#cells").value = $("#mode").value==="vm" ? 2 : 8; status(); });
async function act(url, body){ const r = await fetch(url,{method:"POST",headers:{"Content-Type":"application/json","X-Swarm-Action":"1"},body:JSON.stringify(body||{})}); const j = await r.json().catch(()=>({})); if(!r.ok){ $("#status").textContent = j.error||("error "+r.status); } return j; }
$("#start").addEventListener("click", async ()=>{
  const body={mode:$("#mode").value,cells:+$("#cells").value,effort:$("#effort").value,pack:$("#pack").value,model:$("#model").value,harness:$("#harness").value,max_tokens:+$("#maxtok").value};
  if(SRC==="custom"){ body.custom=true; body.prompts=$("#prompts").value.split("\n").map(l=>l.trim()).filter(Boolean); body.check=$("#check").value;
    if(!body.prompts.length){ $("#status").textContent="write at least one task prompt first"; return; } }
  const j = await act("api/swarms",body); if(j.id){ cur=j.id; await status(); setTimeout(tick, 800); } });
$("#stop").addEventListener("click", async ()=>{ for(const id of (STATUS.active||[])) await act("api/swarms/"+id+"/cancel"); await status(); });
setInterval(status, 3000); status(); tick();
