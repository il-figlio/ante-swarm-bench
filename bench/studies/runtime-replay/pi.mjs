import fs from 'node:fs';
import readline from 'node:readline';
const lines=readline.createInterface({input:process.stdin})[Symbol.asyncIterator]();
const base='/opt/swarm/pi/node_modules/@earendil-works/pi-coding-agent/node_modules/@earendil-works';
const {Agent}=await import(base+'/pi-agent-core/dist/index.js');
const {streamSimple}=await import(base+'/pi-ai/dist/api/openai-responses.js');
const cfg=JSON.parse(fs.readFileSync(process.argv[2]));
const mark=(kind,extra={})=>console.log(JSON.stringify({mark:kind,t:performance.now()/1000,...extra}));
const model={id:cfg.model,name:cfg.model,api:'openai-responses',provider:'openai',baseUrl:cfg.url,reasoning:true,input:['text'],cost:{input:0,output:0,cacheRead:0,cacheWrite:0},contextWindow:128000,maxTokens:16384};
const agent=new Agent({initialState:{systemPrompt:cfg.system,model,tools:[],thinkingLevel:'medium'},streamFn:(m,c,o)=>streamSimple(m,c,{...o,apiKey:'offline',maxTokens:16384,cacheRetention:'none'}),transport:'sse'});
mark('ready');
for(let i=0;i<cfg.turns;i++){
 await agent.prompt(i===0?cfg.user:`Continue fixed trace ${i}.`);
 const msg=agent.state.messages.at(-1),text=(msg.content||[]).filter(x=>x.type==='text').map(x=>x.text).join('');
 if(msg.role!=='assistant'||msg.stopReason==='error'||text!=='TRACE_COMPLETE')throw Error(JSON.stringify(msg));
 mark('turn',{index:i,text});
 if(i===0)await lines.next();
}
await lines.next();mark('done');process.exit(0);
