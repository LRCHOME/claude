// Read-only structural diagnostics. Does not print prompts, results or reasoning.
import fs from 'node:fs';
import {convertRequest} from './gpt-bridge.mjs';
const prefix='gpt_claude_v1.';
const args=process.argv.slice(2),targetOption=args.indexOf('--call-id');
let targetCall;
if(targetOption>=0){targetCall=args[targetOption+1];args.splice(targetOption,2);if(!targetCall)throw new Error('--call-id requires an ID');}
for(const filename of args) {
  const rows=fs.readFileSync(filename,'utf8').split(/\r?\n/).filter(Boolean).map(line=>JSON.parse(line));
  const messages=rows.filter(row=>['assistant','user'].includes(row.type)&&row.message);
  const calls=new Map(),outputs=new Set(),replayed=new Map();
  const describe=row=>{
    const blocks=Array.isArray(row.message.content)?row.message.content:[];
    const replay=blocks.filter(b=>b.type==='redacted_thinking'&&b.data?.startsWith(prefix)).flatMap(b=>JSON.parse(Buffer.from(b.data.slice(prefix.length),'base64url')).output);
    return {time:row.timestamp,id:row.message.id,uuid:row.uuid,parent:row.parentUuid,role:row.message.role,
      replay:replay.map(item=>({type:item.type,id:item.id,call_id:item.call_id,name:item.name})),
      tools:blocks.filter(b=>b.type==='tool_use').map(b=>({id:b.id,name:b.name})),
      results:blocks.filter(b=>b.type==='tool_result').map(b=>({id:b.tool_use_id,is_error:b.is_error}))};
  };
  for(const row of messages) {
    const d=describe(row);
    for(const tool of d.tools)calls.set(tool.id,(calls.get(tool.id)||0)+1);
    for(const output of d.results)outputs.add(output.id);
    for(const item of d.replay)if(item.type==='function_call')replayed.set(item.call_id,(replayed.get(item.call_id)||0)+1);
  }
  console.log(JSON.stringify({filename,messages:messages.length,lastMessageTime:messages.at(-1)?.timestamp,
    lastAssistantTime:messages.findLast(row=>row.type==='assistant')?.timestamp,
    recordedErrors:messages.filter(row=>row.error).map(row=>({time:row.timestamp,error:row.error})),
    visible_calls:calls.size,results:outputs.size,replay_calls:replayed.size,
    replay_without_visible_call:[...replayed.keys()].filter(id=>!calls.has(id)),
    replay_without_result:[...replayed.keys()].filter(id=>!outputs.has(id)),
    visible_without_result:[...calls.keys()].filter(id=>!outputs.has(id)),
    repeated_replay:[...replayed].filter(([id,count])=>count>1)}));
  const indices=messages.map((row,index)=>({d:describe(row),index})).filter(({d})=>targetCall&&[...d.replay.map(x=>x.call_id),...d.tools.map(x=>x.id),...d.results.map(x=>x.id)].includes(targetCall)).map(x=>x.index);
  const selected=new Set(indices.flatMap(index=>Array.from({length:7},(_,offset)=>index+offset-2)).filter(index=>index>=0&&index<messages.length));
  for(const index of [...selected].sort((a,b)=>a-b))console.log(JSON.stringify(describe(messages[index])));
  const targetResult=messages.findIndex(row=>targetCall&&describe(row).results.some(result=>result.id===targetCall));
  if(targetResult>=0) {
    const snapshot=messages.slice(0,targetResult).map(row=>row.message);
    const converted=convertRequest({messages:snapshot},'test-model').input;
    const outputs=new Set(converted.filter(item=>item.type==='function_call_output').map(item=>item.call_id));
    const calls=converted.filter(item=>item.type==='function_call').map(item=>item.call_id);
    console.log(JSON.stringify({snapshot:'before target result',forwardedCalls:calls.length,
      missingResults:calls.filter(id=>!outputs.has(id)),duplicateCalls:calls.filter((id,index)=>calls.indexOf(id)!==index)}));
  }
}
