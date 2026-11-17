import {spawn} from 'node:child_process';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

async function runClaude(args,env){
 const child=spawn(process.platform==='win32'?'claude.exe':'claude',args,{stdio:'inherit',env,windowsHide:false});
 const signals=['SIGINT','SIGTERM',...(process.platform==='win32'?[]:['SIGHUP'])];
 const stop=signal=>{if(child.exitCode===null)child.kill(signal);};
 const handlers=signals.map(signal=>{const handler=()=>stop(signal);process.on(signal,handler);return [signal,handler];});
 try{return await new Promise((resolve,reject)=>{child.on('error',reject);child.on('exit',(code,signal)=>resolve(code??(signal?130:1)));});}
 finally{for(const [signal,handler] of handlers)process.off(signal,handler);}
}

async function main(){
 const args=process.argv.slice(2);
 if(args.length===1 && ['--help','-h','--version','-v'].includes(args[0])){
  process.exitCode=await runClaude(args,process.env);return;
 }
 // Native help/version must work before npm dependencies or login exist.
 if(args[0]==='--login'){const {login}=await import('./gpt-auth.mjs');await login();return;}
 if(args[0]==='--launcher-info'){
  const {loadAuth}=await import('./gpt-auth.mjs');
  try{const a=loadAuth();console.log(JSON.stringify({authorization:'ChatGPT plan',signed_in:true,email:a.email,expires_at:new Date(a.expires_at).toISOString(),endpoint:'https://api.openai.com/v1/responses'},null,2));}
  catch(e){console.error(e.message);process.exitCode=1;}
  return;
 }
 const {modelCatalog,startBridge}=await import('./gpt-bridge.mjs');
 const {createDiagnostics}=await import('./gpt-diagnostics.mjs');
 const catalog=await modelCatalog();
 if(args[0]==='--models'){console.log(JSON.stringify(catalog.map(m=>({model:m.slug,name:m.display_name})),null,2));return;}
 const model=process.env.GPT_CLAUDE_MODEL || (catalog.some(m=>m.slug==='gpt-6.1-sol')?'gpt-6.1-sol':catalog[0]?.slug);
 if(!model || !catalog.some(m=>m.slug===model))throw new Error('Requested model unavailable. See gpt_claude --models.');
 const diagnostics=createDiagnostics();
 const bridge=await startBridge(model,catalog,{diagnostics});
 if(diagnostics.path)console.error('Диагностика запросов: '+diagnostics.path);
 const env={...process.env};
 for(const key of ['ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','CLAUDE_CODE_OAUTH_TOKEN','ANTHROPIC_CUSTOM_HEADERS',
  'CLAUDE_CODE_USE_BEDROCK','CLAUDE_CODE_USE_VERTEX','CLAUDE_CODE_USE_FOUNDRY','ANTHROPIC_DEFAULT_MODEL','ANTHROPIC_SMALL_FAST_MODEL'])delete env[key];
 env.ANTHROPIC_BASE_URL=bridge.url;
 env[args.includes('--bare')?'ANTHROPIC_API_KEY':'ANTHROPIC_AUTH_TOKEN']=bridge.token;
 env.ANTHROPIC_MODEL=model;
 for(const family of ['FABLE','OPUS','SONNET','HAIKU'])env['ANTHROPIC_DEFAULT_'+family+'_MODEL']=model;
 env.CLAUDE_CODE_SUBAGENT_MODEL=model;
 env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1';
 // The bridge owns retries. Do not multiply its budget by SDK-level retries.
 env.CLAUDE_CODE_MAX_RETRIES=process.env.CLAUDE_CODE_MAX_RETRIES??'0';
 // Let the bridge report its own deadline instead of a shorter native fetch timeout.
 env.API_TIMEOUT_MS=process.env.API_TIMEOUT_MS||String(Number(process.env.GPT_CLAUDE_RESPONSE_TIMEOUT_MS||900000)+60000);
 env.ENABLE_TOOL_SEARCH='false';
 // This bridge does not implement Anthropic's server-side permission review.
 // Keep the native client-side classifier and its permission rules active.
 env.CLAUDE_CODE_AUTO_MODE_SERVER='0';
 env.CLAUDE_CODE_AUTO_MODE_MODEL=process.env.CLAUDE_CODE_AUTO_MODE_MODEL || model;
 // Conservative window until the account catalog supplies a numeric limit.
 const selected=catalog.find(m=>m.slug===model);
 const context=Number(selected.context_window)||200000;
 env.CLAUDE_CODE_MAX_CONTEXT_TOKENS=String(context);
 env.CLAUDE_CODE_AUTO_COMPACT_WINDOW=String(Math.floor(context*.75));
 console.error(`Claude → ${model} через подписку ChatGPT`);
 console.error('Использование подписки: https://chatgpt.com/settings/usage');
 try{
  process.exitCode=await runClaude(args,env);
 }finally{bridge.close();}
}
main().catch(e=>{
 if(e.code==='ERR_MODULE_NOT_FOUND'&&e.message.includes('undici')){
  console.error('gpt_claude: missing npm dependency undici. Run npm ci in '+path.dirname(fileURLToPath(import.meta.url))+'.');
 }else if(e.code==='ENOENT'&&e.path==='claude.exe'){
  console.error('gpt_claude: native Claude Code (claude.exe) was not found in PATH. Install it and reopen the terminal.');
 }else console.error('gpt_claude: '+e.message);
 process.exitCode=1;
});
