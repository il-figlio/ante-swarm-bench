// MCP transport around the pinned Pi tool implementations. No Pi/LLM agent loop.
import fs from 'node:fs';
import readline from 'node:readline';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const root=path.dirname(fileURLToPath(import.meta.url));
console.log=(...v)=>console.error(...v);
if(process.env.BRIDGE_CGROUP)fs.writeFileSync(process.env.BRIDGE_CGROUP+'/cgroup.procs',String(process.pid));
const {createCodingTools}=await import('/opt/swarm/pi/node_modules/@earendil-works/pi-coding-agent/dist/core/tools/index.js');
const tools=createCodingTools(process.cwd());
const schemas=JSON.parse(fs.readFileSync(path.join(root,'pi-schemas-all.json'),'utf8'));
if(JSON.stringify(tools.map(t=>({name:t.name,description:t.description,inputSchema:t.parameters})))!==JSON.stringify(schemas))throw Error('Pinned Pi tool schema drift');
const selected=tools.filter(t=>['read','write'].includes(t.name)).map(t=>({...t,execute:async(...args)=>{const began=performance.now(),cpu=process.cpuUsage();const result=await t.execute(...args);const used=process.cpuUsage(cpu);audit('pi_body_end',{tool:t.name,wall_ns:(performance.now()-began)*1e6,cpu_ns:(used.user+used.system)*1e3,result:result.content});return result}}));
const allowed=new Map(selected.map(t=>[t.name,t]));const active=new Map();
const audit=(event,fields={})=>{if(process.env.ANTE_PI_AUDIT)fs.appendFileSync(process.env.ANTE_PI_AUDIT,JSON.stringify({event,t:Date.now()/1000,...fields})+'\n')};
async function dispatch(msg){const p=msg.params||{};
 if(msg.method==='initialize')return {protocolVersion:'2024-11-05',capabilities:{tools:{}},serverInfo:{name:'ante-pi-native-tools',version:'0.1.0'}};
 if(msg.method==='ping')return {};
 if(msg.method==='tools/list')return {tools:schemas.filter(t=>['read','write'].includes(t.name))};
 if(msg.method==='tools/call'){
  const tool=allowed.get(p.name);if(!tool)throw Error('Tool outside native Pi grant');
  const controller=new AbortController();active.set(msg.id,controller);audit('pi_tool_start',{tool:p.name});
  const began=performance.now(),cpu=process.cpuUsage();
  try{const result=await tool.execute(String(msg.id),p.arguments||{},controller.signal);const used=process.cpuUsage(cpu);audit('pi_tool_end',{tool:p.name,wall_ns:(performance.now()-began)*1e6,cpu_ns:(used.user+used.system)*1e3,result:result.content});return {content:result.content||[{type:'text',text:JSON.stringify(result)}]};}
  catch(error){audit('pi_tool_error',{tool:p.name,error_type:error.name});return {isError:true,content:[{type:'text',text:error.message}]};}
  finally{active.delete(msg.id)}
 }
 throw Error('Unsupported MCP method');
}

export {dispatch,selected};
