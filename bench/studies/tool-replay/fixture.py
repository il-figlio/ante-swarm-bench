import json,hashlib
SYSTEM='Execute only the fixed synthetic file trace. Use the supplied write and read tools in the exact requested order.'
FINAL='TRACE_COMPLETE'
def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False)
def digest(x):return hashlib.sha256(canonical(x).encode()).hexdigest()
def payload(i):return f'fixture step {i:03d}\n'+'x'*1006+'\n' # exactly1024 ASCII bytes
USER='Complete the benchmark file trace.\n'+'x'*(4096-len('Complete the benchmark file trace.\n'))
def user(i):return USER if i==0 else f'Run fixed file trace {i}.'
def filename(i):return f'fixture-{i:03d}.txt'
def calls(family,i):
 return [{'id':f'call_{i:03d}_write','name':'write' if family=='pi' else 'write_file','arguments':{'path':filename(i),'content':payload(i)}},{'id':f'call_{i:03d}_read','name':'read' if family=='pi' else 'read_file','arguments':{'path':filename(i)}}]
def unwrap(value):
 # Only unwrap transport envelopes; actual native result objects remain intact.
 if isinstance(value,str):
  try:decoded=json.loads(value)
  except ValueError:return value
  if isinstance(decoded,(dict,list)):return unwrap(decoded)
  return value
 if isinstance(value,dict) and 'content' in value and set(value)<= {'content','isError'}:
  if value.get('isError'):raise ValueError('tool error envelope')
  if isinstance(value['content'],list):return unwrap(value['content'])
 if isinstance(value,list) and all(isinstance(x,dict) and x.get('type')=='text' and set(x)<= {'type','text','annotations'} for x in value):return unwrap(''.join(x['text'] for x in value))
 return value
