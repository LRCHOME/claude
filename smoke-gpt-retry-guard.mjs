// Offline native Claude check. Fake upstream; no plan tokens or real tools.
import http from 'node:http';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {startBridge} from './gpt-bridge.mjs';
const execute=promisify(execFile);
let calls=0;
const upstream=http.createServer((req,res)=>{
  calls++;res.writeHead(200,{'Content-Type':'text/event-stream'});
  res.write('data: {"type":"response.created"}\n\n');
});
await new Promise(resolve=>upstream.listen(0,'127.0.0.1',resolve));
const model='gpt-6.1-sol';
const bridge=await startBridge(model,[{slug:model}],{
  accessToken:async()=>'fixture-token',idleTimeout:40,responseTimeout:1000,retryDelay:1,
  fetch:(_,init)=>fetch(`http://127.0.0.1:${upstream.address().port}`,init),log:()=>{}
});
try {
  const env={...process.env};
  for(const key of ['CLAUDE_CODE_USE_BEDROCK','CLAUDE_CODE_USE_VERTEX','CLAUDE_CODE_USE_FOUNDRY','CLAUDE_CODE_OAUTH_TOKEN','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_CUSTOM_HEADERS'])delete env[key];
  Object.assign(env,{ANTHROPIC_BASE_URL:bridge.url,ANTHROPIC_API_KEY:bridge.token,ANTHROPIC_MODEL:model,
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC:'1',NO_PROXY:'127.0.0.1,localhost',no_proxy:'127.0.0.1,localhost'});
  let stdout='',exit=0;
  try {
    const pending=execute(process.platform==='win32'?'claude.exe':'claude',[
      '--bare','--setting-sources','','--strict-mcp-config','--mcp-config','{"mcpServers":{}}',
      '--no-session-persistence','--tools','','--system-prompt','Follow the user request.',
      '--output-format','json','-p','Reply OK'
    ],{env,timeout:15000,maxBuffer:2*1024*1024});
    pending.child?.stdin?.end();
    ({stdout}=await pending);
  }catch(error){
    if(error.killed)throw new Error('Native Claude did not stop after the retry budget was exhausted');
    stdout=error.stdout||'';exit=error.code;
  }
  const result=stdout.trim().split(/\r?\n/).filter(Boolean).map(line=>JSON.parse(line)).findLast(row=>row.type==='result');
  if(calls!==2||!result?.is_error)throw new Error('Retry guard native check was inconclusive: '+JSON.stringify({calls,exit,result}));
  console.log(JSON.stringify({upstream_calls:calls,native_stopped:true,is_error:result.is_error,real_tools_executed:0}));
}finally{bridge.close();upstream.closeAllConnections();upstream.close();}
