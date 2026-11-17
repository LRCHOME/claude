import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import {createProxyFetch} from './gpt-network.mjs';

async function listen(server) {
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  return `http://127.0.0.1:${server.address().port}`;
}
test('system proxy carries the request without resolving the destination locally',async()=>{
  let destination;
  const proxy=http.createServer((req,res)=>{destination=req.url;res.end('via proxy');});
  proxy.on('connect',(req,socket)=>{
    destination=req.url;
    socket.write('HTTP/1.1 200 Connection Established\r\n\r\n');
    socket.once('data',()=>socket.end('HTTP/1.1 200 OK\r\nContent-Length: 9\r\nConnection: close\r\n\r\nvia proxy'));
  });
  const address=await listen(proxy);
  const request=createProxyFetch({platform:'win32',env:{},resolve:async()=>address});
  try {
    const response=await request('http://unresolvable.invalid/test',{signal:AbortSignal.timeout(3000)});
    assert.equal(await response.text(),'via proxy');
    assert.match(destination,/unresolvable\.invalid/);
  }finally{await request.close();await new Promise(resolve=>proxy.close(resolve));}
});
test('system bypass makes a direct local connection',async()=>{
  const server=http.createServer((req,res)=>res.end('direct'));
  const address=await listen(server);
  const request=createProxyFetch({platform:'win32',env:{},resolve:async()=>null});
  try {assert.equal(await (await request(address)).text(),'direct');}
  finally{await request.close();await new Promise(resolve=>server.close(resolve));}
});
test('proxy resolution errors do not fall back to direct traffic',async()=>{
  let called=false;
  const request=createProxyFetch({platform:'win32',env:{},resolve:async()=>{throw Error('PAC failed');},fetch:async()=>{called=true;}});
  await assert.rejects(request('https://example.com'),/системный прокси/);
  assert.equal(called,false);
});
test('explicit proxy environment overrides system resolution and respects NO_PROXY',async()=>{
  const server=http.createServer((req,res)=>res.end('bypassed'));
  const address=await listen(server);
  const request=createProxyFetch({platform:'win32',env:{HTTPS_PROXY:'http://127.0.0.1:1',HTTP_PROXY:'http://127.0.0.1:1',NO_PROXY:'127.0.0.1'},
    resolve:async()=>{throw Error('system resolver must not run');}});
  try {assert.equal(await (await request(address)).text(),'bypassed');}
  finally{await request.close();await new Promise(resolve=>server.close(resolve));}
});
