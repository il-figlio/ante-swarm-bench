import readline from 'node:readline';
import {dispatch} from './pi_tools.mjs';
for await(const line of readline.createInterface({input:process.stdin})){
 let msg;try{msg=JSON.parse(line)}catch{continue}if(msg.id==null)continue;
 try{process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:msg.id,result:await dispatch(msg)})+'\n')}
 catch(e){process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:msg.id,error:{code:-32602,message:e.message}})+'\n')}
}
