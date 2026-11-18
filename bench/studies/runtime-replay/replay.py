"""Offline Responses replay. Rejects semantic differences; preserves full synthetic wire."""
import json,time,threading,socket
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
SYSTEM='Execute only the fixed synthetic trace.'
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
   j=len(case['requests']);cfg=case['cfg']
   inp=b.get('input',[]);inp=[{'role':'user','content':inp}] if isinstance(inp,str) else inp
   sysmsg=b.get('instructions','')
   messages=[]
   for m in inp:
    role=m.get('role')
    if role in ('system','developer'):sysmsg+=text(m.get('content',''))
    elif role in ('user','assistant'):messages.append((role,text(m.get('content',''))))
   wanted=[]
   for i in range(j+1):
    wanted.append(('user',cfg['user'] if i==0 else f'Continue fixed trace {i}.'))
    if i<j:wanted.append(('assistant','TRACE_COMPLETE'))
   errors=[]
   if sysmsg!=cfg['system']:errors.append('system_prompt_mismatch')
   if messages!=wanted:errors.append('conversation_mismatch')
   if b.get('tools'):errors.append('unexpected_tools')
   if b.get('model')!=cfg['model']:errors.append('model_mismatch')
   if b.get('reasoning',{}).get('effort')!='medium':errors.append('effort_mismatch')
   if b.get('max_output_tokens')!=16384:errors.append('output_budget_mismatch')
   if j>=cfg['turns']:errors.append('extra_request')
   audit={'index':j,'errors':errors,'body':b,'bytes':len(raw),'at':time.monotonic()};case['requests'].append(audit)
  if errors:
   payload=json.dumps({'error':{'message':','.join(errors),'type':'invalid_request_error'}}).encode();self.send_response(400);self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload);return
  rid='resp_'+str(j);mid='msg_'+str(j);s='TRACE_COMPLETE'
  msg={'id':mid,'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':s,'annotations':[]}]}
  resp={'id':rid,'object':'response','created_at':0,'status':'completed','model':cfg['model'],'output':[msg],'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2}}
  events=[('response.created',{'response':{**resp,'status':'in_progress','output':[]}}),('response.output_item.added',{'output_index':0,'item':{**msg,'status':'in_progress','content':[]}}),('response.content_part.added',{'item_id':mid,'output_index':0,'content_index':0,'part':{'type':'output_text','text':'','annotations':[]}}),('response.output_text.delta',{'item_id':mid,'output_index':0,'content_index':0,'delta':s}),('response.output_text.done',{'item_id':mid,'output_index':0,'content_index':0,'text':s}),('response.content_part.done',{'item_id':mid,'output_index':0,'content_index':0,'part':msg['content'][0]}),('response.output_item.done',{'output_index':0,'item':msg}),('response.completed',{'response':resp})]
  if b.get('stream'):
   payload=''.join('event: '+kind+'\ndata: '+json.dumps({'type':kind,'sequence_number':i,**data})+'\n\n' for i,(kind,data) in enumerate(events)).encode();ctype='text/event-stream'
  else:payload=json.dumps(resp).encode();ctype='application/json'
  self.send_response(200);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
