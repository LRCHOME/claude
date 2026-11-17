import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';

// No free-form API messages, request bodies, headers, tool arguments or results.
const fields=new Set(['event','trace','model','attempt','elapsed_ms','input_bytes','input_items','tool_count',
  'request_id','response_id','status','error_type','error_code','phase','last_event','received_bytes',
  'since_last_chunk_ms','since_last_event_ms','retryable','stop_reason']);
export function createDiagnostics({directory=path.join(os.homedir(),'.config','gpt_claude','logs'),
  enabled=process.env.GPT_CLAUDE_LOGGING!=='0',maxBytes=5*1024*1024,warn=console.error}={}) {
  const filename=path.join(directory,`gpt-${new Date().toISOString().slice(0,10)}-${process.pid}-${crypto.randomUUID().slice(0,8)}.jsonl`);
  let written=0,disabled=!enabled;
  const record=event=>{
    if(disabled)return;
    const data={time:new Date().toISOString(),pid:process.pid};
    for(const [key,value] of Object.entries(event)) {
      if(!fields.has(key)||value===undefined)continue;
      if(typeof value==='string')data[key]=value.replace(/[^a-zA-Z0-9_.:/ -]/g,'').slice(0,200);
      else if(typeof value==='number'||typeof value==='boolean')data[key]=value;
    }
    const line=JSON.stringify(data)+'\n';
    const bytes=Buffer.byteLength(line);
    if(written+bytes>maxBytes){disabled=true;warn('gpt_claude: diagnostic file size limit reached; logging disabled for this process.');return;}
    try {
      if(!written)fs.mkdirSync(directory,{recursive:true,mode:0o700});
      fs.appendFileSync(filename,line,{mode:0o600});written+=bytes;
    } catch {disabled=true;warn('gpt_claude: could not write diagnostic metadata; requests will continue.');}
  };
  record.path=enabled?filename:undefined;
  return record;
}
