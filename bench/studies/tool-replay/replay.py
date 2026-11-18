"""Offline Responses replay. Rejects semantic differences; preserves full synthetic wire."""
import json,time,threading,socket
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from fixture import SYSTEM,calls,user,unwrap,canonical
from pathlib import Path
GOLD=json.loads((Path(__file__).resolve().parent/'goldens.json').read_text())
def norm_result(value,cfg):
 value=unwrap(value)
 return json.loads(canonical(value).replace(cfg['workspace'],'$WORKSPACE'))
def tool_name(name,cfg):
 if name.startswith('mcp__'):
  prefix='mcp__'+cfg['family']+'__'
  if not name.startswith(prefix):raise ValueError('unexpected tool namespace')
  return name[len(prefix):]
 return name
def normalize(body,cfg):
 normalized=[];system=body.get('instructions','');inp=body.get('input',[])
 for m in inp:
  role=m.get('role');typ=m.get('type')
  if role in ('system','developer'):system+=text(m.get('content',''))
  elif typ=='function_call':normalized.append(['call',m['call_id'],tool_name(m['name'],cfg),json.loads(m['arguments'])])
  elif typ=='function_call_output':normalized.append(['result',m['call_id'],norm_result(m['output'],cfg)])
  elif role in ('user','assistant'):
   value=text(m.get('content',''))
   if value:normalized.append([role,value])
  elif typ=='reasoning':
   if m.get('summary'):raise ValueError('unexpected reasoning')
  else:raise ValueError('unexpected input '+str(m))
 if system!=cfg['system']:raise ValueError('system prompt mismatch')
 schemas=[]
 for t in body.get('tools',[]):
  f=t.get('function',t);schemas.append({'name':tool_name(f['name'],cfg),'description':f.get('description',''),'inputSchema':f['parameters']})
 schemas.sort(key=lambda x:x['name']);return normalized,schemas
def history(cfg,turn,stage):
 out=[]
 for i in range(turn+1):
  out.append(['user',user(i)])
  for k,c in enumerate(calls(cfg['family'],i)):
   if i==turn and k>=stage:break
   out += [['call',c['id'],c['name'],c['arguments']],['result',c['id'],GOLD[cfg['family']][i][k]]]
  if i<turn:out.append(['assistant','TRACE_COMPLETE'])
 return out
def text(v):
 if isinstance(v,str):return v
 if isinstance(v,list):return ''.join(text(x) for x in v)
 if isinstance(v,dict):return v.get('text','')
 return ''
class Server(ThreadingHTTPServer):
 daemon_threads=True
 def __init__(self):super().__init__(('127.0.0.1',0),Handler);self.cases={};self.lock=threading.Lock()
class Handler(BaseHTTPRequestHandler):
 def setup(self):
  super().setup();self.connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
 protocol_version='HTTP/1.1'
 def log_message(self,*a):pass
 def do_POST(self):
  raw=self.rfile.read(int(self.headers.get('Content-Length',0)));b=json.loads(raw);key=self.path.split('/')[1]
  with self.server.lock:
   case=self.server.cases[key]
   if not self.path.endswith('/responses'):
    case.setdefault('probes',[]).append({'path':self.path,'body':b});self.send_response(404);self.send_header('Content-Length','0');self.end_headers();return
   j=len(case['requests']);cfg=case['cfg'];turn,stage=divmod(j,3)
   errors=[]
   try:normalized,schemas=normalize(b,cfg)
   except (ValueError,KeyError,TypeError) as e:normalized=[];schemas=[];errors.append('invalid_wire:'+str(e))
   expected=history(cfg,turn,stage)
   if normalized!=expected:errors.append('history_or_tool_result_mismatch')
   if schemas!=cfg['schemas']:errors.append('tool_schema_mismatch')
   if b.get('model')!=cfg['model']:errors.append('model_mismatch')
   if b.get('reasoning',{}).get('effort')!='medium':errors.append('effort_mismatch')
   if b.get('max_output_tokens')!=16384:errors.append('output_budget_mismatch')
   if b.get('stream') is not True:errors.append('stream_mismatch')
   if turn>=cfg['turns']:errors.append('extra_request')
   audit={'index':j,'errors':errors,'body':b,'normalized':normalized,'expected':expected if errors else None,'schemas':schemas if errors else None,'bytes':len(raw),'at':time.monotonic()};case['requests'].append(audit)
  if errors:
   payload=json.dumps({'error':{'message':','.join(errors),'type':'invalid_request_error'}}).encode();self.send_response(400);self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload);return
  rid='resp_'+str(j);mid='msg_'+str(j);s='TRACE_COMPLETE'
  if stage<2:
   call=calls(cfg['family'],turn)[stage];name=call['name'] if not cfg['engine'].startswith('ante-') else 'mcp__'+cfg['family']+'__'+call['name'];args=json.dumps(call['arguments'],separators=(',',':'))
   msg={'id':mid,'type':'function_call','call_id':call['id'],'name':name,'arguments':args,'status':'completed'}
   events=[('response.created',{}),('response.output_item.added',{'output_index':0,'item':{**msg,'status':'in_progress','arguments':''}}),('response.function_call_arguments.delta',{'item_id':mid,'output_index':0,'delta':args}),('response.function_call_arguments.done',{'item_id':mid,'output_index':0,'arguments':args}),('response.output_item.done',{'output_index':0,'item':msg}),('response.completed',{})]
  else:
   msg={'id':mid,'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':s,'annotations':[]}]}
   events=[('response.created',{}),('response.output_item.added',{'output_index':0,'item':{**msg,'status':'in_progress','content':[]}}),('response.content_part.added',{'item_id':mid,'output_index':0,'content_index':0,'part':{'type':'output_text','text':'','annotations':[]}}),('response.output_text.delta',{'item_id':mid,'output_index':0,'content_index':0,'delta':s}),('response.output_text.done',{'item_id':mid,'output_index':0,'content_index':0,'text':s}),('response.content_part.done',{'item_id':mid,'output_index':0,'content_index':0,'part':msg['content'][0]}),('response.output_item.done',{'output_index':0,'item':msg}),('response.completed',{})]
  resp={'id':rid,'object':'response','created_at':0,'status':'completed','model':cfg['model'],'output':[msg],'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2}}
  events[0][1]['response']={**resp,'status':'in_progress','output':[]};events[-1][1]['response']=resp
  if b.get('stream'):
   payload=''.join('event: '+kind+'\ndata: '+json.dumps({'type':kind,'sequence_number':i,**data})+'\n\n' for i,(kind,data) in enumerate(events)).encode();ctype='text/event-stream'
  else:payload=json.dumps(resp).encode();ctype='application/json'
  self.send_response(200);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
