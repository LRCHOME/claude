// Explicit live check. Uses plan tokens and never sends project/session data.
import assert from 'node:assert/strict';
import {accessToken,resource} from './gpt-auth.mjs';
import {proxyFetch} from './gpt-network.mjs';
import {sseEvents} from './gpt-bridge.mjs';

const started=Date.now(),token=await accessToken();
let completed=false,text='',events=0;
try {
  const response=await proxyFetch(resource+'/responses',{
    method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},
    signal:AbortSignal.timeout(240000),
    body:JSON.stringify({model:process.env.GPT_CLAUDE_MODEL||'gpt-6.2-sol',store:false,stream:true,
      reasoning:{effort:'low'},input:[{role:'user',content:
        'Print every integer from 1 through 2000 in ascending order, separated by spaces. No code, no explanations, no omissions.'}]})
  });
  if(!response.ok)throw new Error('Stream check HTTP '+response.status);
  for await(const event of sseEvents(response.body)) {
    events++;
    if(event.type==='response.output_text.delta')text+=event.delta;
    if(['error','response.failed','response.incomplete'].includes(event.type))throw new Error('Stream check received '+event.type);
    if(event.type==='response.completed'){completed=true;break;}
  }
  assert.ok(completed,'Stream ended without response.completed');
  assert.deepEqual(text.trim().split(/\s+/),Array.from({length:2000},(_,i)=>String(i+1)),
    'Long answer was truncated or incomplete');
  console.log(JSON.stringify({completed,integers:2000,chars:text.length,events,elapsed_ms:Date.now()-started}));
}finally{await proxyFetch.close();}
