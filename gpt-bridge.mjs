import http from 'node:http';
import crypto from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';
import {accessToken,resource} from './gpt-auth.mjs';
import {proxyFetch} from './gpt-network.mjs';
const replayPrefix='gpt_claude_v1.';
const textOf=c=>typeof c==='string'?c:(c||[]).filter(b=>b.type==='text').map(b=>b.text).join('\n\n');

export function convertRequest(body,model) {
  const input=[],instructions=[textOf(body.system)];
  // Classifier requests and interrupted turns may contain only some results
  // from a parallel batch. Opaque replay must not resurrect pending calls.
  const resultIDs=new Set((body.messages||[]).flatMap(message=>
    Array.isArray(message.content)?message.content.filter(b=>b.type==='tool_result').map(b=>b.tool_use_id):[]));
  const emittedCalls=new Set();
  const pushCall=item=>{
    if(emittedCalls.has(item.call_id))return;
    emittedCalls.add(item.call_id);
    if(resultIDs.has(item.call_id)){input.push(item);return;}
    // Preserve the uncertainty without fabricating a successful tool result.
    input.push({role:'assistant',content:[{type:'output_text',text:
      `[Tool call ${item.name} (${item.call_id}) has no result in the supplied history. Its execution status is unknown. Verify state before repeating the action.]`}]});
  };
  for(const message of body.messages||[]) {
    if(message.role==='system'){instructions.push(textOf(message.content));continue;}
    const blocks=typeof message.content==='string'?[{type:'text',text:message.content}]:message.content||[];
    const replay=message.role==='assistant' && blocks.find(b=>b.type==='redacted_thinking' && b.data?.startsWith(replayPrefix));
    if(replay){
      const saved=JSON.parse(Buffer.from(replay.data.slice(replayPrefix.length),'base64url'));
      if(!Array.isArray(saved.output))throw new Error('Invalid GPT replay state');
      const partial=saved.output.some(item=>item.type==='function_call'&&!resultIDs.has(item.call_id));
      for(const item of saved.output){
        if(item.type==='function_call')pushCall(item);
        // Reasoning belongs to the original complete output; don't replay it
        // when its call batch has been changed for a partial history snapshot.
        else if(!(partial&&item.type==='reasoning'))input.push(item);
      }
      // Claude may put the visible blocks in this message or separate records.
      // Replay above already supplies these blocks; visible tool records below
      // are deduplicated by call_id if they arrive as separate messages.
      continue;
    }
    let content=[];
    const flush=()=>{if(content.length){input.push({role:message.role,content});content=[];}};
    for(const block of blocks) {
      if(block.type==='text')content.push({type:message.role==='assistant'?'output_text':'input_text',text:block.text});
      else if(block.type==='image' && message.role==='user') {
        const s=block.source;
        content.push({type:'input_image',image_url:s.type==='base64'?`data:${s.media_type};base64,${s.data}`:s.url});
      } else if(block.type==='document' && message.role==='user') {
        const s=block.source;
        if(s.type==='text')content.push({type:'input_text',text:s.data});
        else if(s.type==='base64')content.push({type:'input_file',filename:block.title||'attachment.pdf',file_data:`data:${s.media_type};base64,${s.data}`});
        else if(s.type==='url')content.push({type:'input_file',file_url:s.url});
        else throw new Error('Unsupported document source');
      } else if(block.type==='tool_use') {
        flush();pushCall({type:'function_call',call_id:block.id,name:block.name,namespace:'claude',arguments:JSON.stringify(block.input)});
      } else if(block.type==='tool_result') {
        flush();
        const output=typeof block.content==='string'?block.content:(block.content||[]).map(b=>{
          if(b.type==='text')return {type:'input_text',text:b.text};
          if(b.type==='image'){const s=b.source;return {type:'input_image',image_url:s.type==='base64'?`data:${s.media_type};base64,${s.data}`:s.url};}
          throw new Error('Unsupported tool result content');
        });
        input.push({type:'function_call_output',call_id:block.tool_use_id,output});
      } else if(['thinking','redacted_thinking'].includes(block.type)) {
        // Thinking from another model cannot be replayed as OpenAI encrypted state.
      } else throw new Error('Unsupported Anthropic content block: '+block.type);
    }
    flush();
  }
  const tools=(body.tools||[]).map(t=>{
    if(!t.name || !t.input_schema)throw new Error('Only client-executed function tools are supported');
    return {type:'function',name:t.name,description:t.description||'',parameters:t.input_schema,strict:false};
  });
  const request={model,instructions:instructions.filter(Boolean).join('\n\n'),input,store:false,stream:true};
  if(tools.length)request.tools=[{type:'namespace',name:'claude',description:'Tools executed by Claude Code on the user computer with its permission checks.',tools}];
  if(body.tool_choice?.type==='none')request.tool_choice='none';
  if(body.tool_choice?.type==='any')request.tool_choice='required';
  if(body.tool_choice?.type==='tool')request.tool_choice={type:'function',name:body.tool_choice.name,namespace:'claude'};
  const effort=process.env.GPT_CLAUDE_EFFORT || 'medium';
  if(effort!=='default')request.reasoning={effort};
  return request;
}

export async function* sseEvents(body,onChunk=()=>{}) {
  let buffer='';const decoder=new TextDecoder();
  for await(const bytes of body) {
    onChunk(bytes.length);
    buffer+=decoder.decode(bytes,{stream:true});
    buffer=buffer.replaceAll('\r\n','\n');
    let end;
    while((end=buffer.indexOf('\n\n'))>=0) {
      const frame=buffer.slice(0,end);buffer=buffer.slice(end+2);
      const data=frame.split('\n').filter(l=>l.startsWith('data:')).map(l=>l.slice(5).trimStart()).join('\n');
      if(data && data!=='[DONE]')yield JSON.parse(data);
    }
  }
  if(buffer.trim())throw new Error('Truncated OpenAI event stream');
}
export function anthropicResponse(response,model,toolNames=[]) {
  const content=[];
  if(response.output?.length)content.push({type:'redacted_thinking',data:replayPrefix+Buffer.from(JSON.stringify({output:response.output})).toString('base64url')});
  for(const item of response.output||[]) {
    if(item.type==='message')for(const b of item.content||[]) {
      if(b.type==='output_text')content.push({type:'text',text:b.text});
      else if(b.type==='refusal')content.push({type:'text',text:b.refusal});
    }
    else if(item.type==='function_call') {
      const name=toolNames.includes(item.name)?item.name:item.name.replace(/^claude\./,'');
      if(!toolNames.includes(name))throw new Error('OpenAI requested an unknown Claude tool');
      content.push({type:'tool_use',id:item.call_id,name,input:JSON.parse(item.arguments||'{}')});
    }
    else if(item.type!=='reasoning')throw new Error('Unsupported OpenAI output: '+item.type);
  }
  const usage=response.usage||{},cached=usage.input_tokens_details?.cached_tokens||0;
  return {id:response.id||'msg_'+crypto.randomUUID(),type:'message',role:'assistant',model,content,
    stop_reason:content.some(b=>b.type==='tool_use')?'tool_use':'end_turn',stop_sequence:null,
    usage:{input_tokens:Math.max(0,(usage.input_tokens||0)-cached),output_tokens:usage.output_tokens||0,cache_read_input_tokens:cached,cache_creation_input_tokens:0}};
}
function sendEvent(res,type,data){res.write(`event: ${type}\ndata: ${JSON.stringify({type,...data})}\n\n`);}
function sendMessage(res,message,started=false) {
  if(!started)sendEvent(res,'message_start',{message:{...message,content:[],stop_reason:null,usage:{input_tokens:0,output_tokens:0}}});
  message.content.forEach((block,index)=>{
    if(block.type==='text') {
      sendEvent(res,'content_block_start',{index,content_block:{type:'text',text:''}});
      sendEvent(res,'content_block_delta',{index,delta:{type:'text_delta',text:block.text}});
    } else if(block.type==='tool_use') {
      sendEvent(res,'content_block_start',{index,content_block:{...block,input:{}}});
      sendEvent(res,'content_block_delta',{index,delta:{type:'input_json_delta',partial_json:JSON.stringify(block.input)}});
    } else sendEvent(res,'content_block_start',{index,content_block:block});
    sendEvent(res,'content_block_stop',{index});
  });
  sendEvent(res,'message_delta',{delta:{stop_reason:message.stop_reason,stop_sequence:null},usage:message.usage});
  sendEvent(res,'message_stop',{});res.end();
}
export async function modelCatalog() {
  const token=await accessToken();
  const response=await proxyFetch(resource+'/models',{headers:{Authorization:'Bearer '+token},signal:AbortSignal.timeout(30000)});
  if(!response.ok)throw new Error('OpenAI model catalog HTTP '+response.status);
  const data=await response.json();
  if(!Array.isArray(data.models))throw new Error('Expected a ChatGPT subscription model catalog');
  return data.models.filter(m=>m.visibility==='list');
}
function apiError(message,status=502,type='api_error') {
  return Object.assign(new Error(message),{status,type});
}
function safeDetail(value) {
  return String(value??'').replace(/[\r\n\t]+/g,' ')
    .replace(/Bearer\s+\S+/gi,'Bearer [redacted]')
    .replace(/\bsk-[\w-]+/g,'[redacted]')
    .replace(/\beyJ[\w-]+\.[\w-]+\.[\w-]+/g,'[redacted]')
    .slice(0,1500);
}
export function upstreamError(event,httpStatus) {
  const raw=event.response?.error??event.error??event.response?.status_details?.error??event;
  const detail=typeof raw==='string'?{message:raw}:raw||{};
  const code=detail.code||event.code,type=detail.type;
  const overloaded=httpStatus===503||httpStatus===529||['overloaded_error','service_unavailable_error'].includes(type)||['server_is_overloaded','model_overloaded','overloaded'].includes(code);
  const status=overloaded?529:httpStatus||(
    type==='authentication_error'||['invalid_api_key','token_expired'].includes(code)?401:
    type==='permission_error'?403:
    type==='invalid_request_error'||['context_length_exceeded','invalid_encrypted_content','model_not_found','invalid_value'].includes(code)?400:
    type==='rate_limit_error'||['rate_limit_exceeded','insufficient_quota','usage_limit_reached'].includes(code)?429:502);
  const errorType=status===529?'overloaded_error':status===401?'authentication_error':status===403?'permission_error':status===429?'rate_limit_error':status===400?'invalid_request_error':'api_error';
  const parts=[httpStatus?'OpenAI HTTP '+httpStatus:'OpenAI '+(event.type||'error')];
  if(code)parts.push('code='+safeDetail(code));
  if(type)parts.push('type='+safeDetail(type));
  if(detail.param)parts.push('param='+safeDetail(detail.param));
  if(detail.message)parts.push(safeDetail(detail.message));
  if(event.response?.id)parts.push('response='+safeDetail(event.response.id));
  if(parts.length===1)parts.push('no error details provided');
  const error=apiError(parts.join('; '),status,errorType);
  if(code)error.code=String(code);
  if(['insufficient_quota','usage_limit_reached'].includes(code))error.retryable=false;
  return error;
}
export function retryAfterMs(headers,now=Date.now()) {
  const milliseconds=headers.get('retry-after-ms');
  if(milliseconds!==null&&milliseconds.trim()!==''&&Number.isFinite(Number(milliseconds))&&Number(milliseconds)>=0)return Math.ceil(Number(milliseconds));
  const value=headers.get('retry-after');
  if(value===null||value.trim()==='')return undefined;
  const seconds=Number(value);
  if(Number.isFinite(seconds))return seconds>=0?Math.ceil(seconds*1000):undefined;
  const date=Date.parse(value);
  return Number.isFinite(date)?Math.max(0,date-now):undefined;
}
export async function startBridge(model,catalog,options={}) {
  const fetch=options.fetch||proxyFetch,getToken=options.accessToken||accessToken;
  const idleTimeout=options.idleTimeout??Number(process.env.GPT_CLAUDE_IDLE_TIMEOUT_MS||120000);
  if(!Number.isSafeInteger(idleTimeout)||idleTimeout<1)throw new Error('Invalid GPT_CLAUDE_IDLE_TIMEOUT_MS');
  const responseTimeout=options.responseTimeout??Number(process.env.GPT_CLAUDE_RESPONSE_TIMEOUT_MS||900000);
  if(!Number.isSafeInteger(responseTimeout)||responseTimeout<1)throw new Error('Invalid GPT_CLAUDE_RESPONSE_TIMEOUT_MS');
  const attempts=options.attempts??3;
  const retryDelay=options.retryDelay??1000;
  const maxRetryDelay=options.maxRetryDelay??30000;
  const log=options.log||((value)=>console.error('gpt_claude: '+value));
  const diagnose=options.diagnostics||(()=>{});
  const localToken=crypto.randomBytes(32).toString('base64url');
  const allowed=new Set(catalog.map(m=>m.slug));
  const controllers=new Set();
  const server=http.createServer(async(req,res)=>{
    const valid=req.headers.authorization==='Bearer '+localToken || req.headers['x-api-key']===localToken;
    const json=(status,value,headers={})=>{res.writeHead(status,{'Content-Type':'application/json',...headers});res.end(JSON.stringify(value));};
    if(!valid){json(401,{error:{type:'authentication_error',message:'Invalid local bridge token'}});return;}
    const route=req.url.split('?')[0];
    if(req.method==='GET' && route==='/v1/models'){
      json(200,{data:catalog.map(m=>({id:m.slug,type:'model',display_name:m.display_name,created_at:'2026-01-01T00:00:00Z'})),has_more:false});return;
    }
    if(req.method!=='POST' || !['/v1/messages','/v1/messages/count_tokens'].includes(route)){json(404,{error:{type:'not_found_error',message:'Unknown endpoint'}});return;}
    let ping,controller;let streaming=false;
    const started=Date.now(),trace=crypto.randomUUID().slice(0,8);
    try {
      const chunks=[];let size=0;
      for await(const chunk of req){size+=chunk.length;if(size>64*1024*1024)throw new Error('Request exceeds 64 MiB');chunks.push(chunk);}
      const body=JSON.parse(Buffer.concat(chunks));
      const chosen=body.model||model;
      if(!allowed.has(chosen))throw new Error('Model is not available in the signed-in ChatGPT account');
      const request=convertRequest(body,chosen);
      if(route.endsWith('/count_tokens')){
        // Conservative estimate; this is not an exact OpenAI tokenizer count.
        json(200,{input_tokens:Buffer.byteLength(JSON.stringify(request))});return;
      }
      diagnose({event:'request_start',trace,model:chosen,input_bytes:Buffer.byteLength(JSON.stringify(request)),input_items:request.input.length,tool_count:body.tools?.length||0});
      const wantsStream=body.stream===true;
      const messageID='msg_'+crypto.randomUUID();
      const beginStream=()=>{if(streaming||!wantsStream)return;
        streaming=true;
        res.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-cache','Connection':'keep-alive'});
        sendEvent(res,'message_start',{message:{id:messageID,type:'message',role:'assistant',model:chosen,content:[],stop_reason:null,stop_sequence:null,usage:{input_tokens:0,output_tokens:0}}});
        ping=setInterval(()=>sendEvent(res,'ping',{}),10000);
      };
      res.on('close',()=>{
        if(!res.writableEnded)diagnose({event:'request_cancelled',trace,elapsed_ms:Date.now()-started});
        controller?.abort();
      });
      let completed;
      for(let attempt=1;attempt<=attempts;attempt++) {
        controller=new AbortController();controllers.add(controller);
        let timer,totalTimer,requestID='unknown',responseID='unknown';
        let phase='authorization',lastEvent='none',lastChunkAt=Date.now(),lastEventAt=Date.now(),receivedBytes=0;
        let wait=retryDelay*2**(attempt-1)*(1+Math.random()*.25);
        const reset=()=>{clearTimeout(timer);timer=setTimeout(()=>controller.abort(apiError('OpenAI stream idle timeout',504)),idleTimeout);};
        try {
          reset();
          totalTimer=setTimeout(()=>controller.abort(apiError('OpenAI response deadline exceeded',504)),responseTimeout);
          const token=await getToken();
          controller.signal.throwIfAborted();
          phase='headers';
          reset();
          const response=await fetch(resource+'/responses',{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify(request),signal:controller.signal});
          phase='stream';
          reset();
          requestID=response.headers.get('x-request-id')||response.headers.get('request-id')||'unknown';
          const withRetryAfter=error=>{
            const retryAfter=retryAfterMs(response.headers);
            if(retryAfter!==undefined){
              error.retryAt=Date.now()+retryAfter;
              error.message+=`; retry after ${retryAfter}ms`;
            }
            return error;
          };
          if(!response.ok) {
            const e=await response.json().catch(()=>({}));
            const error=upstreamError(e,response.status);
            error.message+='; request '+safeDetail(requestID);
            throw withRetryAfter(error);
          }
          beginStream();
          for await(const event of sseEvents(response.body,bytes=>{
            receivedBytes+=bytes;lastChunkAt=Date.now();reset();
          })) {
            lastEvent=event.type;lastEventAt=Date.now();
            if(event.response?.id)responseID=safeDetail(event.response.id);
            if(event.type==='response.failed' || event.type==='error')throw withRetryAfter(upstreamError(event));
            if(event.type==='response.incomplete')throw apiError('OpenAI response incomplete',502);
            if(event.type==='response.completed'){completed=event.response;break;}
          }
          if(!completed)throw apiError('OpenAI stream ended without completion');
          diagnose({event:'attempt_completed',trace,attempt,elapsed_ms:Date.now()-started,request_id:requestID,response_id:responseID,received_bytes:receivedBytes});
          break;
        } catch(error) {
          const e=controller.signal.aborted?(controller.signal.reason||error):error;
          if(res.destroyed)throw e;
          // No text or tool calls have been delivered yet, so retrying cannot
          // execute a Claude tool twice. Never retry authentication/input errors.
          const retryable=e.retryable!==false&&(!e.status || [408,429,500,502,503,504,529].includes(e.status));
          if(e.retryAt!==undefined)wait=Math.max(wait,e.retryAt-Date.now());
          log(`${trace} attempt ${attempt}/${attempts}, ${Date.now()-started}ms, request ${safeDetail(requestID)}, response ${responseID}: ${e.message}`);
          diagnose({event:'attempt_failed',trace,attempt,elapsed_ms:Date.now()-started,request_id:requestID,response_id:responseID,status:e.status||502,error_type:e.type||'api_error',error_code:e.code||e.cause?.code,phase,last_event:lastEvent,received_bytes:receivedBytes,since_last_chunk_ms:Date.now()-lastChunkAt,since_last_event_ms:Date.now()-lastEventAt,retryable});
          // Long server-requested pauses belong to the client. Never retry
          // earlier than Retry-After just to fit our internal retry budget.
          if(!retryable||attempt===attempts||wait>maxRetryDelay)throw e.status?e:apiError('OpenAI connection failed: '+e.message);
        } finally {clearTimeout(timer);clearTimeout(totalTimer);controller.abort();controllers.delete(controller);}
        if(res.destroyed)return;
        controller=new AbortController();controllers.add(controller);
        try{await delay(wait,undefined,{signal:controller.signal});}
        finally{controllers.delete(controller);}
        if(res.destroyed)return;
      }
      const message=anthropicResponse(completed,chosen,(body.tools||[]).map(t=>t.name));
      diagnose({event:'request_completed',trace,elapsed_ms:Date.now()-started,stop_reason:message.stop_reason});
      if(process.env.GPT_CLAUDE_DEBUG==='1')log(`${trace} completed in ${Date.now()-started}ms, ${message.stop_reason}`);
      if(streaming){message.id=messageID;sendMessage(res,message,true);}else json(200,message);
    } catch(e) {
      if(!res.destroyed){
        diagnose({event:'request_failed',trace,elapsed_ms:Date.now()-started,status:e.status||400,error_type:e.type||'invalid_request_error',error_code:e.code});
        const error={type:e.type||'invalid_request_error',message:e.message};
        const headers=e.retryAt===undefined?{}:{'Retry-After':String(Math.ceil(Math.max(0,e.retryAt-Date.now())/1000))};
        if(streaming){sendEvent(res,'error',{error});res.end();}else json(e.status||400,{type:'error',error},headers);
      }
    } finally {clearInterval(ping);if(controller)controllers.delete(controller);}
  });
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  return {url:`http://127.0.0.1:${server.address().port}`,token:localToken,
    close:()=>{for(const c of controllers)c.abort();server.closeAllConnections();server.close();}};
}
