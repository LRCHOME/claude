import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import http2 from 'node:http2';
import https from 'node:https';
import net from 'node:net';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
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

test('TLS requests negotiate HTTP/2 through every route and retain HTTP/1.1 fallback',async t=>{
  const execute=promisify(execFile);
  const candidates=['openssl',...(process.platform==='win32'?['C:/Program Files/Git/usr/bin/openssl.exe']:[])];
  let openssl;
  for(const candidate of candidates){try{await execute(candidate,['version']);openssl=candidate;break;}catch{}}
  if(!openssl){t.skip('OpenSSL is needed to generate an ephemeral trusted test certificate');return;}
  const directory=await fs.mkdtemp(path.join(os.tmpdir(),'gpt-http2-test-'));
  const keyFile=path.join(directory,'key.pem'),certFile=path.join(directory,'cert.pem');
  t.after(async()=>{for(const file of [keyFile,certFile])await fs.unlink(file).catch(()=>{});await fs.rmdir(directory);});
  await execute(openssl,['req','-x509','-newkey','rsa:2048','-nodes','-keyout',keyFile,
    '-out',certFile,'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1']);
  const tls={key:await fs.readFile(keyFile),cert:await fs.readFile(certFile)};
  const h2=http2.createSecureServer({...tls,allowHTTP1:true});
  h2.on('request',(req,res)=>res.end(req.httpVersion));
  const h1=https.createServer(tls,(req,res)=>res.end(req.httpVersion));
  const h2URL=(await listen(h2)).replace('http:','https:');
  const h1URL=(await listen(h1)).replace('http:','https:');
  const proxy=http.createServer();
  proxy.on('connect',(req,client,head)=>{
    const upstream=net.connect({host:'127.0.0.1',port:h2.address().port},()=>{
      client.write('HTTP/1.1 200 Connection Established\r\n\r\n');
      if(head.length)upstream.write(head);
      client.pipe(upstream);upstream.pipe(client);
    });
    upstream.on('error',()=>client.destroy());client.on('error',()=>upstream.destroy());
    client.on('close',()=>upstream.destroy());upstream.on('close',()=>client.destroy());
  });
  const proxyURL=await listen(proxy);
  try {
    // A child loads NODE_EXTRA_CA_CERTS at startup. Production TLS verification
    // stays enabled, and the disposable private key never enters the repo.
    const code=`import {createProxyFetch} from ${JSON.stringify(new URL('./gpt-network.mjs',import.meta.url).href)};
const results=[];
for(const options of [
  {platform:'win32',env:{},resolve:async()=>${JSON.stringify(proxyURL)}},
  {platform:'win32',env:{HTTPS_PROXY:${JSON.stringify(proxyURL)},NO_PROXY:''}},
  {platform:'win32',env:{},resolve:async()=>null},
  {platform:'linux',env:{}}
]){
  const request=createProxyFetch(options);
  try{results.push(await (await request(${JSON.stringify(h2URL)},{signal:AbortSignal.timeout(5000)})).text());}
  finally{await request.close();}
}
const fallback=createProxyFetch({platform:'linux',env:{}});
try{results.push(await (await fallback(${JSON.stringify(h1URL)},{signal:AbortSignal.timeout(5000)})).text());}
finally{await fallback.close();}
console.log(JSON.stringify(results));`;
    const {stdout}=await execute(process.execPath,['--input-type=module','-e',code],
      {env:{...process.env,NODE_EXTRA_CA_CERTS:certFile},timeout:20000});
    assert.deepEqual(JSON.parse(stdout),['2.0','2.0','2.0','2.0','1.1']);
  }finally{await Promise.all([h2,h1,proxy].map(server=>new Promise(resolve=>server.close(resolve))));}
});
