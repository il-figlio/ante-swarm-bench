import fs from 'node:fs';import readline from 'node:readline';import {spawn} from 'node:child_process';
const lines=readline.createInterface({input:process.stdin})[Symbol.asyncIterator](),cfg=JSON.parse(fs.readFileSync(process.argv[2])),root=new URL('.',import.meta.url).pathname;
const base='/opt/swarm/pi/node_modules/@earendil-works/pi-coding-agent/node_modules/@earendil-works';
const mark=(kind,extra={})=>process.stdout.write(JSON.stringify({mark:kind,t:performance.now()/1000,...extra})+'\n');
const payload=i=>`fixture step ${String(i).padStart(3,'0')}\n`+'x'.repeat(1006)+'\n';
const user=i=>i===0?cfg.user:`Run fixed file trace ${i}.`;
const done=async i=>{if(fs.readFileSync(`fixture-${String(i).padStart(3,'0')}.txt`,'utf8')!==payload(i))throw Error('file state mismatch');mark('turn',{index:i,text:'TRACE_COMPLETE'});if(i===0||i===cfg.turns-1)await lines.next()};
if(cfg.engine==='pi'){
 delete process.env.BRIDGE_CGROUP;
 const {selected}=await import('./pi_tools.mjs'),{Agent}=await import(base+'/pi-agent-core/dist/index.js'),{streamSimple}=await import(base+'/pi-ai/dist/api/openai-responses.js');
 const model={id:cfg.model,name:cfg.model,api:'openai-responses',provider:'openai',baseUrl:cfg.url,reasoning:true,input:['text'],cost:{input:0,output:0,cacheRead:0,cacheWrite:0},contextWindow:128000,maxTokens:16384};
 const agent=new Agent({initialState:{systemPrompt:cfg.system,model,tools:selected,thinkingLevel:'medium'},streamFn:(m,c,o)=>streamSimple(m,c,{...o,apiKey:'offline',maxTokens:16384,cacheRetention:'none'}),transport:'sse'});
 mark('ready');for(let i=0;i<cfg.turns;i++){await agent.prompt(user(i));const m=agent.state.messages.at(-1),text=(m.content||[]).filter(x=>x.type==='text').map(x=>x.text).join('');if(m.stopReason==='error'||text!=='TRACE_COMPLETE')throw Error(JSON.stringify(m));await done(i)}
}else{
 let call,p;
 if(cfg.engine==='pi-direct'){delete process.env.BRIDGE_CGROUP;const {dispatch}=await import('./pi_tools.mjs');call=dispatch;}
 else{p=spawn('/opt/swarm/node/bin/node',[root+'pi_bridge.mjs'],{stdio:['pipe','pipe','inherit']});const out=readline.createInterface({input:p.stdout})[Symbol.asyncIterator]();let id=0;call=async m=>{p.stdin.write(JSON.stringify({jsonrpc:'2.0',id:++id,...m})+'\n');const r=JSON.parse((await out.next()).value);if(r.error)throw Error(JSON.stringify(r));return r.result}}
 await call({method:'initialize',params:{}});await call({method:'tools/list'});mark('ready');
 for(let i=0;i<cfg.turns;i++){for(const [name,args] of [['write',{path:`fixture-${String(i).padStart(3,'0')}.txt`,content:payload(i)}],['read',{path:`fixture-${String(i).padStart(3,'0')}.txt`}]]){const r=await call({method:'tools/call',params:{name,arguments:args}});if(r.isError)throw Error(JSON.stringify(r));mark('tool_result',{index:i,name,result:r.content.map(x=>x.text).join('')})}await done(i)}
 if(p){p.stdin.end();await new Promise(resolve=>p.on('exit',resolve))}
}
mark('done');process.exit(0);
