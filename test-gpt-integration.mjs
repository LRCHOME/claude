import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import {startBridge,sseEvents,retryAfterMs} from './gpt-bridge.mjs';

const catalog=[{slug:'test-model',display_name:'Test'}];
const output=[{type:'message',role:'assistant',content:[{type:'output_text',text:'OK'}]}];
const done=()=>`data: ${JSON.stringify({type:'response.completed',response:{id:'resp_test',output}})}\n\n`;
async function fixture(t,handler,options={}) {
  const upstream=http.createServer(handler);
  await new Promise(resolve=>upstream.listen(0,'127.0.0.1',resolve));
  const bridge=await startBridge('test-model',catalog,{
    accessToken:async()=>'test-token',
    fetch:(_,init)=>fetch(`http://127.0.0.1:${upstream.address().port}`,init),
    retryDelay:1,idleTimeout:500,log:()=>{},...options
  });
  t.after(()=>{bridge.close();upstream.closeAllConnections();upstream.close();});
  const post=(body,token=bridge.token)=>fetch(bridge.url+'/v1/messages',{
    method:'POST',headers:{'x-api-key':token,'Content-Type':'application/json'},
    body:JSON.stringify({model:'test-model',messages:[{role:'user',content:'test'}],...body})
  });
  return {bridge,post};
}

test('HTTP bridge authenticates clients and relays a completed response',async t=>{
  let calls=0;
  const {post}=await fixture(t,(req,res)=>{calls++;res.end(done());});
  assert.equal((await post({},'wrong')).status,401);
  const response=await post({});
  assert.equal(response.status,200);
  assert.equal((await response.json()).content.find(b=>b.type==='text').text,'OK');
  assert.equal(calls,1);
});

test('temporary upstream HTTP errors retry before delivering tool calls',async t=>{
  let calls=0;
  const {post}=await fixture(t,(req,res)=>{
    if(++calls<3){res.writeHead(503);res.end('{"error":{"code":"unavailable"}}');}
    else res.end(done());
  });
  assert.equal((await post({})).status,200);
  assert.equal(calls,3);
});

test('rate limits keep their HTTP status after bounded retries',async t=>{
  let calls=0;
  const {post}=await fixture(t,(req,res)=>{calls++;res.writeHead(429,{'x-request-id':'req_limit'});res.end('{"error":{"code":"rate_limit"}}');});
  const response=await post({});
  assert.equal(response.status,429);
  const error=(await response.json()).error;
  assert.equal(error.type,'rate_limit_error');
  assert.match(error.message,/req_limit/);
  assert.equal(calls,3);
});

test('authentication errors fail once without retry',async t=>{
  let calls=0;
  const {post}=await fixture(t,(req,res)=>{calls++;res.writeHead(401);res.end('{}');});
  assert.equal((await post({})).status,401);
  assert.equal(calls,1);
});

test('silent upstream connections time out and retry instead of hanging',async t=>{
  let calls=0;
  const {post}=await fixture(t,()=>{calls++;},{idleTimeout:40,attempts:2});
  const response=await post({});
  assert.equal(response.status,504);
  assert.match((await response.json()).error.message,/idle timeout/);
  assert.equal(calls,2);
});

test('interrupted SSE retries inside the same Claude message',async t=>{
  let calls=0;
  const {post}=await fixture(t,(req,res)=>{
    res.writeHead(200,{'Content-Type':'text/event-stream'});
    if(++calls===1)res.end('data: {"type":"response.created"}\n\n');
    else res.end(done());
  });
  const response=await post({stream:true});
  const events=[];for await(const event of sseEvents(response.body))events.push(event);
  assert.equal(events.filter(e=>e.type==='message_start').length,1);
  assert.equal(events.filter(e=>e.type==='message_stop').length,1);
  assert.equal(events.filter(e=>e.type==='content_block_delta').length,1);
  assert.equal(calls,2);
});

test('completed event finishes even when upstream leaves its stream open',async t=>{
  const {post}=await fixture(t,(req,res)=>{res.writeHead(200,{'Content-Type':'text/event-stream'});res.write(done());});
  const response=await post({});
  assert.equal(response.status,200);
  assert.equal((await response.json()).stop_reason,'end_turn');
});

test('stream idle timeout is returned as an SSE error after retry exhaustion',async t=>{
  const {post}=await fixture(t,(req,res)=>{res.writeHead(200,{'Content-Type':'text/event-stream'});res.write('data: {"type":"response.created"}\n\n');},{idleTimeout:40,attempts:2});
  const response=await post({stream:true});
  const events=[];for await(const event of sseEvents(response.body))events.push(event);
  assert.equal(events.at(-1).type,'error');
  assert.match(events.at(-1).error.message,/idle timeout/);
});

test('client cancellation aborts upstream without retrying the request',async t=>{
  let calls=0,closed;
  const disconnected=new Promise(resolve=>{closed=resolve;});
  const {post}=await fixture(t,(req,res)=>{
    calls++;
    res.on('close',closed);
    res.writeHead(200,{'Content-Type':'text/event-stream'});
    res.write('data: {"type":"response.created"}\n\n');
  });
  const response=await post({stream:true});
  await response.body.cancel();
  await Promise.race([disconnected,new Promise((_,reject)=>{
    const timer=setTimeout(()=>reject(new Error('Upstream was not cancelled')),1000);
    timer.unref();
  })]);
  assert.equal(calls,1);
});

test('flat streaming errors with null code retain their real message',async t=>{
  let calls=0;const logs=[];
  const {post}=await fixture(t,(req,res)=>{
    calls++;res.writeHead(200,{'Content-Type':'text/event-stream','request-id':'req_flat'});
    res.end('data: {"type":"error","code":null,"message":"Invalid tool schema","param":"tools","error":{"type":"invalid_request_error","message":"Invalid tool schema","param":"tools"}}\n\n');
  },{log:message=>logs.push(message)});
  const response=await post({});
  assert.equal(response.status,400);
  const error=(await response.json()).error;
  assert.equal(error.type,'invalid_request_error');
  assert.match(error.message,/Invalid tool schema/);
  assert.match(error.message,/param=tools/);
  assert.match(logs.join(' '),/req_flat/);
  assert.equal(calls,1);
});

test('standard top-level error message is not replaced with response_failed',async t=>{
  const {post}=await fixture(t,(req,res)=>res.end('data: {"type":"error","code":null,"message":"A specific upstream failure","param":null}\n\n'),{attempts:1});
  assert.match((await (await post({})).json()).error.message,/A specific upstream failure/);
});

test('failed response preserves error message and response ID',async t=>{
  const {post}=await fixture(t,(req,res)=>res.end('data: {"type":"response.failed","response":{"id":"resp_failed","error":{"code":"context_length_exceeded","message":"Context window exceeded"}}}\n\n'));
  const response=await post({});
  assert.equal(response.status,400);
  assert.match((await response.json()).error.message,/Context window exceeded; response=resp_failed/);
});

test('hard quota exhaustion is not retried as a temporary rate limit',async t=>{
  let calls=0;
  const {post}=await fixture(t,(req,res)=>{calls++;res.writeHead(429);res.end('{"error":{"code":"insufficient_quota","message":"Usage exhausted"}}');});
  assert.equal((await post({})).status,429);
  assert.equal(calls,1);
});

test('known credential formats are redacted from upstream error messages',async t=>{
  const {post}=await fixture(t,(req,res)=>{res.writeHead(400);res.end(JSON.stringify({error:{message:'Bad Bearer secret-token; key sk-secret-key; token eyJabc.def.ghi'}}));});
  const message=(await (await post({})).json()).error.message;
  assert.doesNotMatch(message,/secret-token|sk-secret-key|eyJabc/);
});

test('HTTP 503 overload is translated to Anthropic HTTP 529 overloaded_error',async t=>{
  let calls=0;
  const {post}=await fixture(t,(req,res)=>{
    calls++;res.writeHead(503);
    res.end('{"error":{"type":"service_unavailable_error","code":"server_is_overloaded","message":"Model temporarily overloaded"}}');
  });
  const response=await post({});
  assert.equal(response.status,529);
  const error=(await response.json()).error;
  assert.equal(error.type,'overloaded_error');
  assert.match(error.message,/HTTP 503/);
  assert.equal(calls,3);
});

test('HTTP 529 preserves overloaded_error even without a JSON body',async t=>{
  const {post}=await fixture(t,(req,res)=>{res.writeHead(529);res.end('Overloaded');},{attempts:1});
  const response=await post({});
  assert.equal(response.status,529);
  assert.equal((await response.json()).error.type,'overloaded_error');
});

test('SSE response.failed overload emits overloaded_error without a false message_stop',async t=>{
  let calls=0;
  const {post}=await fixture(t,(req,res)=>{
    calls++;res.writeHead(200,{'Content-Type':'text/event-stream'});
    res.end('data: {"type":"response.failed","response":{"error":{"type":"service_unavailable_error","code":"server_is_overloaded","message":"Busy"}}}\n\n');
  });
  const response=await post({stream:true});
  assert.equal(response.status,200);
  const events=[];for await(const event of sseEvents(response.body))events.push(event);
  assert.equal(events.at(-1).type,'error');
  assert.equal(events.at(-1).error.type,'overloaded_error');
  assert.equal(events.filter(e=>e.type==='message_start').length,1);
  assert.equal(events.filter(e=>e.type==='message_stop').length,0);
  assert.equal(calls,3);
});

test('overload inside SSE retains HTTP 529 for a nonstreaming client',async t=>{
  const {post}=await fixture(t,(req,res)=>res.end('data: {"type":"error","code":"server_is_overloaded","message":"Busy"}\n\n'),{attempts:1});
  const response=await post({});
  assert.equal(response.status,529);
  assert.equal((await response.json()).error.type,'overloaded_error');
});

test('Retry-After parses seconds, milliseconds and HTTP dates',()=>{
  const now=Date.UTC(2026,10,17,10);
  assert.equal(retryAfterMs(new Headers({'Retry-After':'2'}),now),2000);
  assert.equal(retryAfterMs(new Headers({'Retry-After':'0.5'}),now),500);
  assert.equal(retryAfterMs(new Headers({'retry-after-ms':'150'}),now),150);
  assert.equal(retryAfterMs(new Headers({'Retry-After':new Date(now+60000).toUTCString()}),now),60000);
  for(const value of ['garbage','','-2'])assert.equal(retryAfterMs(new Headers({'Retry-After':value}),now),undefined);
});

test('retries wait for the server-requested Retry-After interval',async t=>{
  const times=[];
  const {post}=await fixture(t,(req,res)=>{
    times.push(Date.now());
    if(times.length===1){res.writeHead(503,{'Retry-After':'0.04'});res.end('{}');}
    else res.end(done());
  });
  assert.equal((await post({})).status,200);
  assert.equal(times.length,2);
  assert.ok(times[1]-times[0]>=40,'request was retried before Retry-After');
});

test('long Retry-After is forwarded without an early internal retry',async t=>{
  let calls=0;
  const {post}=await fixture(t,(req,res)=>{calls++;res.writeHead(503,{'Retry-After':'120'});res.end('{}');});
  const response=await post({});
  assert.equal(response.status,529);
  assert.equal(response.headers.get('retry-after'),'120');
  assert.equal((await response.json()).error.type,'overloaded_error');
  assert.equal(calls,1);
});

test('SSE keepalive traffic prevents a false network idle timeout',async t=>{
  let calls=0;
  const {post}=await fixture(t,(req,res)=>{
    calls++;res.writeHead(200,{'Content-Type':'text/event-stream'});
    res.write(': keepalive\n\n');
    let ticks=0;
    const timer=setInterval(()=>{
      if(++ticks===8){clearInterval(timer);res.end(done());}
      else res.write(': keepalive\n\n');
    },15);
    res.on('close',()=>clearInterval(timer));
  },{idleTimeout:60,responseTimeout:1000,attempts:1});
  assert.equal((await post({})).status,200);
  assert.equal(calls,1);
});

test('a slowly arriving SSE frame counts as live traffic before JSON parsing',async t=>{
  const {post}=await fixture(t,(req,res)=>{
    const bytes=Buffer.from(done());let offset=0;
    res.writeHead(200,{'Content-Type':'text/event-stream'});
    const timer=setInterval(()=>{
      res.write(bytes.subarray(offset,offset+24));offset+=24;
      if(offset>=bytes.length){clearInterval(timer);res.end();}
    },15);
    res.on('close',()=>clearInterval(timer));
  },{idleTimeout:60,responseTimeout:1000,attempts:1});
  const response=await post({});
  assert.equal(response.status,200);
  assert.equal((await response.json()).stop_reason,'end_turn');
});

test('endless keepalives still hit the overall response deadline',async t=>{
  const events=[];
  const {post}=await fixture(t,(req,res)=>{
    res.writeHead(200,{'Content-Type':'text/event-stream'});
    res.write(': keepalive\n\n');
    const timer=setInterval(()=>res.write(': keepalive\n\n'),15);
    res.on('close',()=>clearInterval(timer));
  },{idleTimeout:60,responseTimeout:130,attempts:1,diagnostics:event=>events.push(event)});
  const response=await post({});
  assert.equal(response.status,504);
  assert.match((await response.json()).error.message,/response deadline/);
  const failure=events.find(event=>event.event==='attempt_failed');
  assert.equal(failure.phase,'stream');
  assert.ok(failure.received_bytes>0);
  assert.equal(failure.last_event,'none');
});

test('a stalled response records phase and last event without storing request content',async t=>{
  const events=[];
  const {post}=await fixture(t,(req,res)=>{
    res.writeHead(200,{'Content-Type':'text/event-stream','x-request-id':'req_stall'});
    res.write('data: {"type":"response.created","response":{"id":"resp_stall"}}\n\n');
  },{idleTimeout:40,attempts:1,diagnostics:event=>events.push(event)});
  assert.equal((await post({messages:[{role:'user',content:'PRIVATE PROMPT'}]})).status,504);
  const failure=events.find(event=>event.event==='attempt_failed');
  assert.equal(failure.request_id,'req_stall');
  assert.equal(failure.response_id,'resp_stall');
  assert.equal(failure.last_event,'response.created');
  assert.equal(failure.phase,'stream');
  assert.ok(failure.since_last_chunk_ms>0);
  assert.doesNotMatch(JSON.stringify(events),/PRIVATE PROMPT|test-token/);
});
