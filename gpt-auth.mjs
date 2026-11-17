import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import http from 'node:http';
import crypto from 'node:crypto';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {proxyFetch} from './gpt-network.mjs';

export const directory = path.join(os.homedir(), '.config', 'gpt_claude');
const authPath = path.join(directory, 'auth.json');
const issuer = 'https://auth.openai.com';
const tokenEndpoint = issuer + '/api/accounts/oauth/token';
export const resource = 'https://api.openai.com/v1';
const permission = 'chatgpt.tokens.use.direct';
function initialize() { fs.mkdirSync(directory, {recursive:true, mode:0o700}); }
export function savePrivate(name, value) {
  initialize();
  const dest = path.join(directory, name), temp = dest + '.' + crypto.randomUUID();
  fs.writeFileSync(temp, JSON.stringify(value, null, 2) + '\n', {mode:0o600, flag:'wx'});
  fs.renameSync(temp, dest);
}
export function loadAuth() {
  let raw;
  try {raw=fs.readFileSync(authPath,'utf8');}
  catch(e) {
    if(e.code==='ENOENT')throw new Error('Сначала выполните gpt_claude --login.');
    throw new Error('Не удалось прочитать авторизацию gpt_claude: '+e.code);
  }
  const auth = JSON.parse(raw);
  if (!auth.scopes?.includes(permission)) throw new Error('Не разрешено использование подписки. Выполните gpt_claude --login.');
  return auth;
}
async function jsonRequest(url, options={}) {
  const res = await proxyFetch(url, {...options, signal:AbortSignal.timeout(30000)});
  if (!res.ok) throw new Error(`OpenAI HTTP ${res.status}; request ${res.headers.get('x-request-id') || 'unknown'}`);
  return res.json();
}
async function exchange(parameters) {
  return jsonRequest(tokenEndpoint, {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:new URLSearchParams(parameters)});
}
export async function validateID(token, clientID, nonce, expectedSubject) {
  const chunks = token.split('.');
  if (chunks.length !== 3) throw new Error('Invalid ID token');
  const header = JSON.parse(Buffer.from(chunks[0], 'base64url'));
  const claims = JSON.parse(Buffer.from(chunks[1], 'base64url'));
  if (!['RS256','ES256','EdDSA'].includes(header.alg)) throw new Error('Unsupported ID-token signature');
  const jwks = await jsonRequest(issuer + '/.well-known/jwks.json');
  const jwk = jwks.keys.find(k=>k.kid===header.kid && (!k.alg || k.alg===header.alg));
  if (!jwk) throw new Error('Unknown ID-token signing key');
  const key = crypto.createPublicKey({key:jwk, format:'jwk'});
  const valid = crypto.verify(header.alg==='EdDSA'?null:'sha256', Buffer.from(chunks[0]+'.'+chunks[1]),
    header.alg==='ES256'?{key,dsaEncoding:'ieee-p1363'}:key, Buffer.from(chunks[2],'base64url'));
  const audience = Array.isArray(claims.aud)?claims.aud:[claims.aud];
  if (!valid || claims.iss!==issuer || !audience.includes(clientID) || !(claims.exp>Date.now()/1000) ||
      (claims.nbf && claims.nbf>Date.now()/1000+30) || (nonce && claims.nonce!==nonce) ||
      !claims.sub || (expectedSubject && claims.sub!==expectedSubject)) throw new Error('ID-token verification failed');
  return claims;
}
function updatedAuth(old, tokens) {
  const scopes = tokens.scope ? tokens.scope.split(' ') : old.scopes;
  if (!scopes?.includes(permission) || !tokens.access_token) throw new Error('ChatGPT plan permission was not granted');
  return {...old, ...tokens, scopes, expires_at:Date.now()+Number(tokens.expires_in||3600)*1000};
}
export async function accessToken() {
  initialize();
  const lock = path.join(directory, 'refresh.lock');
  let fd;
  for (let i=0;i<400;i++) {
    try { fd=fs.openSync(lock,'wx',0o600); fs.writeFileSync(fd,String(process.pid)); break; }
    catch(e) {
      if(e.code!=='EEXIST') throw e;
      try {
        const pid=Number(fs.readFileSync(lock,'utf8')); if(pid>0) process.kill(pid,0);
      } catch(e) { if(e.code==='ESRCH') { try {fs.unlinkSync(lock);} catch{}; continue; } }
      await new Promise(r=>setTimeout(r,100));
    }
  }
  if(fd===undefined) throw new Error('Another gpt_claude is refreshing login. Retry shortly.');
  try {
    let auth=loadAuth();
    if(auth.expires_at<Date.now()+60000) {
      const tokens=await exchange({grant_type:'refresh_token',client_id:auth.client_id,refresh_token:auth.refresh_token,resource});
      if(tokens.id_token) await validateID(tokens.id_token,auth.client_id,null,auth.subject);
      auth=updatedAuth(auth,tokens); savePrivate('auth.json',auth);
    }
    return auth.access_token;
  } finally {fs.closeSync(fd);fs.unlinkSync(lock);}
}
export async function login() {
  initialize();
  const hostPath=path.join(directory,'host.json');
  if(!fs.existsSync(hostPath)) savePrivate('host.json',{ext_agent_host_id:'urn:uuid:'+crypto.randomUUID()});
  const host=JSON.parse(fs.readFileSync(hostPath,'utf8'));
  const old=fs.existsSync(authPath)?JSON.parse(fs.readFileSync(authPath,'utf8')):null;
  const state=crypto.randomBytes(32).toString('base64url'),nonce=crypto.randomBytes(32).toString('base64url');
  const verifier=crypto.randomBytes(48).toString('base64url');
  let finish;
  const result=new Promise((resolve,reject)=>{finish={resolve,reject};});
  let consumed=false,redirect;
  const server=http.createServer(async(req,res)=>{
    const url=new URL(req.url,'http://127.0.0.1');
    if(url.pathname!=='/auth/callback'){res.writeHead(404);res.end();return;}
    res.setHeader('Content-Type','text/plain; charset=utf-8');res.setHeader('Cache-Control','no-store');
    if(consumed || url.searchParams.get('state')!==state){res.writeHead(400);res.end('Invalid authorization state');return;}
    consumed=true;
    try {
      if(url.searchParams.has('error')) throw new Error('Авторизация отклонена или недоступна.');
      const clientID=url.searchParams.get('client_id')||old?.client_id;
      if(!clientID || clientID==='dynamic_agent_client' || (old && clientID!==old.client_id)) throw new Error('Registration did not return a valid client ID');
      const code=url.searchParams.get('code');if(!code)throw new Error('Missing authorization code');
      const tokens=await exchange({grant_type:'authorization_code',client_id:clientID,code,code_verifier:verifier,redirect_uri:redirect,resource});
      const claims=await validateID(tokens.id_token,clientID,nonce,old?.subject);
      const auth=updatedAuth({client_id:clientID,subject:claims.sub,email:claims.email,issuer,...host},tokens);
      savePrivate('auth.json',auth);
      res.end('gpt_claude: вход выполнен. Можно закрыть эту вкладку.');finish.resolve(auth);
    } catch(e) {res.writeHead(400);res.end('Не удалось завершить вход. См. терминал gpt_claude.');finish.reject(e);}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  redirect=`http://127.0.0.1:${server.address().port}/auth/callback`;
  const params=new URLSearchParams({client_id:old?.client_id||'dynamic_agent_client',ext_agent_host_id:host.ext_agent_host_id,
    response_type:'code',redirect_uri:redirect,scope:'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct',resource,
    state,nonce,code_challenge_method:'S256',code_challenge:crypto.createHash('sha256').update(verifier).digest('base64url')});
  if(!old)params.set('agent_name_hint','gpt_claude');
  const url=issuer+'/api/accounts/authorize?'+params;
  console.log('Continue with ChatGPT — открываю браузер. Разрешите gpt_claude использовать подписку.');
  console.log('Лимиты приложения: https://chatgpt.com/settings/usage');
  console.log('Если браузер не открылся, откройте ссылку вручную:\n'+url);
  const windows=process.platform==='win32';
  const opener=windows?'powershell.exe':process.platform==='darwin'?'/usr/bin/open':'xdg-open';
  // Keep the URL out of shell syntax: OAuth query strings contain ampersands.
  const script="$ErrorActionPreference = 'Stop'; Start-Process -FilePath $env:GPT_CLAUDE_LOGIN_URL";
  const args=windows?['-NoProfile','-NonInteractive','-EncodedCommand',Buffer.from(script,'utf16le').toString('base64')]:[url];
  const browser=spawn(opener,args,{stdio:'ignore',windowsHide:true,
    ...(windows?{env:{...process.env,GPT_CLAUDE_LOGIN_URL:url}}:{})});
  browser.on('error',()=>console.error('Не удалось запустить браузер. Используйте ссылку выше.'));
  browser.on('exit',code=>{if(code!==null && code!==0)console.error('Не удалось открыть браузер. Используйте ссылку выше.');});
  browser.unref();
  const timeout=setTimeout(()=>finish.reject(new Error('Время входа истекло. Повторите gpt_claude --login.')),10*60*1000);
  try {await result;console.log('ChatGPT plan authorization saved securely.');}
  finally {clearTimeout(timeout);server.close();}
}
if(process.argv[1]===fileURLToPath(import.meta.url)) login().catch(e=>{console.error(e.message);process.exitCode=1;});
