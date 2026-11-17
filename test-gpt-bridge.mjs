import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import {convertRequest,anthropicResponse,sseEvents} from './gpt-bridge.mjs';
import {validateID} from './gpt-auth.mjs';

test('converts inline system and tools to stateless subscription request',()=>{
 const body={system:[{type:'text',text:'rules'}],messages:[{role:'user',content:'read file'},{role:'system',content:'environment'}],tools:[{name:'Read',input_schema:{type:'object',properties:{path:{type:'string'}}}}],temperature:1,max_tokens:500,metadata:{secret:'not forwarded'}};
 const r=convertRequest(body,'test-model');
 assert.equal(r.instructions,'rules\n\nenvironment');assert.equal(r.input.length,1);
 assert.equal(r.store,false);assert.equal(r.stream,true);assert.equal(r.tools[0].type,'namespace');
 for(const key of ['temperature','max_output_tokens','max_tokens','metadata','previous_response_id'])assert.equal(r[key],undefined);
});
test('preserves encrypted reasoning, phase, namespace and call id over Claude tool round trip',()=>{
 const output=[{type:'reasoning',id:'r',encrypted_content:'opaque',summary:[]},{type:'message',id:'m',role:'assistant',phase:'commentary',content:[{type:'output_text',text:'Reading'}]},{type:'function_call',id:'fc',call_id:'call1',namespace:'claude',name:'Read',arguments:'{"path":"a"}'}];
 const a=anthropicResponse({id:'resp1',output,usage:{input_tokens:12,output_tokens:7,input_tokens_details:{cached_tokens:5}}},'test-model',['Read']);
 assert.equal(a.stop_reason,'tool_use');assert.equal(a.usage.input_tokens,7);
 const r=convertRequest({messages:[a,{role:'user',content:[{type:'tool_result',tool_use_id:'call1',content:'data'}]}]},'test-model');
 assert.deepEqual(r.input.slice(0,3),output);assert.deepEqual(r.input[3],{type:'function_call_output',call_id:'call1',output:'data'});
});
test('does not execute unexpected model tools',()=>{
 assert.throws(()=>anthropicResponse({output:[{type:'function_call',name:'Other',call_id:'c',arguments:'{}'}]},'test-model',['Read']),/unknown/);
});

function parallelBatch() {
 const output=[{type:'reasoning',id:'r_parallel',encrypted_content:'opaque',summary:[]},
  ...['a','b','c','d'].map(id=>({type:'function_call',id:'fc_'+id,call_id:id,namespace:'claude',name:'Bash',arguments:'{"command":"test"}'}))];
 return {output,message:anthropicResponse({output},'test-model',['Bash'])};
}
const toolResult=id=>({type:'tool_result',tool_use_id:id,content:'actual result '+id});

test('partial classifier history never resurrects pending calls from opaque replay',()=>{
 const {message}=parallelBatch();
 const request=convertRequest({messages:[
  {role:'assistant',content:[message.content[0],...message.content.filter(b=>b.type==='tool_use'&&['a','b'].includes(b.id))]},
  {role:'user',content:[toolResult('a'),toolResult('b'),{type:'text',text:'Review permission for the next action'}]}
 ]},'test-model');
 assert.deepEqual(request.input.filter(i=>i.type==='function_call').map(i=>i.call_id),['a','b']);
 assert.deepEqual(request.input.filter(i=>i.type==='function_call_output').map(i=>i.call_id),['a','b']);
 assert.equal(request.input.some(i=>i.type==='reasoning'),false);
 assert.match(JSON.stringify(request.input),/execution status is unknown/);
});

test('split Claude assistant records replay each completed tool call only once',()=>{
 const {message,output}=parallelBatch();
 const messages=message.content.map(block=>({role:'assistant',content:[block]}));
 messages.push({role:'user',content:['a','b','c','d'].map(toolResult)});
 const input=convertRequest({messages},'test-model').input;
 assert.deepEqual(input.slice(0,output.length),output);
 assert.deepEqual(input.filter(i=>i.type==='function_call').map(i=>i.call_id),['a','b','c','d']);
 assert.equal(input.filter(i=>i.type==='function_call_output').length,4);
});

test('interrupted tool execution is represented as unknown without fabricating results',()=>{
 const input=convertRequest({messages:[
  {role:'assistant',content:[{type:'tool_use',id:'pending',name:'Write',input:{}}]},
  {role:'user',content:'Continue after the interruption'}
 ]},'test-model').input;
 assert.equal(input.some(i=>['function_call','function_call_output'].includes(i.type)),false);
 assert.match(JSON.stringify(input),/Verify state before repeating/);
});

test('partial parallel batch works when results and assistant blocks are interleaved',()=>{
 const {message}=parallelBatch();
 const messages=[{role:'assistant',content:[message.content[0]]}];
 for(const block of message.content.filter(b=>b.type==='tool_use')) {
  messages.push({role:'assistant',content:[block]});
  if(['a','b'].includes(block.id))messages.push({role:'user',content:[toolResult(block.id)]});
 }
 const input=convertRequest({messages},'test-model').input;
 assert.deepEqual(input.filter(i=>i.type==='function_call').map(i=>i.call_id),['a','b']);
 assert.deepEqual(input.filter(i=>i.type==='function_call_output').map(i=>i.call_id),['a','b']);
});
test('SSE parsing handles split UTF8 and CRLF, rejects truncation',async()=>{
 const bytes=Buffer.from('event: x\r\ndata: {"type":"response.output_text.delta","delta":"Привет"}\r\n\r\ndata: [DONE]\r\n\r\n');
 async function* source(){for(let i=0;i<bytes.length;i+=3)yield bytes.subarray(i,i+3);}
 const events=[];for await(const e of sseEvents(source()))events.push(e);
 assert.equal(events[0].delta,'Привет');assert.equal(events.length,1);
 async function* broken(){yield Buffer.from('data: {"type":"response.completed"}');}
 await assert.rejects(async()=>{for await(const e of sseEvents(broken())){}},/Truncated/);
});
test('ID tokens require valid signature, audience and nonce',async()=>{
 const {publicKey,privateKey}=crypto.generateKeyPairSync('rsa',{modulusLength:2048});
 const jwk={...publicKey.export({format:'jwk'}),kid:'test',alg:'RS256'};
 const original=global.fetch;global.fetch=async()=>({ok:true,json:async()=>({keys:[jwk]})});
 try{
  const encode=x=>Buffer.from(JSON.stringify(x)).toString('base64url');
  const h=encode({alg:'RS256',kid:'test'}),p=encode({iss:'https://auth.openai.com',aud:'client',sub:'user',nonce:'nonce',exp:Date.now()/1000+600});
  const signature=crypto.sign('sha256',Buffer.from(h+'.'+p),privateKey).toString('base64url');const token=h+'.'+p+'.'+signature;
  assert.equal((await validateID(token,'client','nonce')).sub,'user');
  await assert.rejects(validateID(token,'wrong','nonce'),/verification/);
  await assert.rejects(validateID(token,'client','wrong'),/verification/);
  await assert.rejects(validateID(h+'.'+p+'.'+Buffer.alloc(256).toString('base64url'),'client','nonce'),/verification/);
 }finally{global.fetch=original;}
});
