import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {Agent, EnvHttpProxyAgent, ProxyAgent} from 'undici';

const execute=promisify(execFile);
export async function resolveSystemProxy(url) {
  const script=`$ErrorActionPreference = 'Stop'
$target = [Uri]$env:GPT_CLAUDE_PROXY_TARGET
$proxy = [System.Net.WebRequest]::GetSystemWebProxy()
$resolved = $proxy.GetProxy($target)
if ($resolved -and $resolved.AbsoluteUri -ne $target.AbsoluteUri) {
  [Console]::Write($resolved.AbsoluteUri)
}`;
  const {stdout}=await execute('powershell.exe',[
    '-NoProfile','-NonInteractive','-EncodedCommand',Buffer.from(script,'utf16le').toString('base64')
  ],{windowsHide:true,timeout:20000,env:{...process.env,GPT_CLAUDE_PROXY_TARGET:String(url)}});
  return stdout.trim() || null;
}

// Resolve Windows Internet Options per URL, including PAC/WPAD and bypass rules.
// Explicit proxy environment variables take precedence over system settings.
export function createProxyFetch({platform=process.platform,env=process.env,
  resolve=resolveSystemProxy,fetch=(...args)=>globalThis.fetch(...args)}={}) {
  const routes=new Map(),agents=new Map();
  let environmentAgent,directAgent;
  // Prefer HTTP/2 over TLS, including inside an HTTP CONNECT tunnel. The
  // inference route can cut off long HTTP/1.1 streams while HTTP/2 continues.
  // ALPN still falls back to HTTP/1.1 for servers that do not offer HTTP/2.
  const direct=()=>directAgent??=new Agent({allowH2:true});
  async function request(input,options={}) {
    const url=new URL(typeof input==='string' || input instanceof URL?input:input.url);
    if(platform!=='win32')return fetch(input,{...options,dispatcher:direct()});
    if(['HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy'].some(key=>env[key])) {
      environmentAgent??=new EnvHttpProxyAgent({
        allowH2:true,requestTls:{allowH2:true},
        httpProxy:env.http_proxy || env.HTTP_PROXY,
        httpsProxy:env.https_proxy || env.HTTPS_PROXY,
        noProxy:env.no_proxy || env.NO_PROXY
      });
      return fetch(input,{...options,dispatcher:environmentAgent});
    }
    const key=url.href;
    let route=routes.get(key);
    if(!route || route.expires<Date.now()) {
      route={expires:Date.now()+60000,promise:resolve(key)};
      routes.set(key,route);
    }
    let proxy;
    try {proxy=await route.promise;}
    catch {routes.delete(key);throw new Error('Не удалось определить системный прокси Windows. Проверьте настройки подключения.');}
    const agentKey=proxy || 'DIRECT';
    if(!agents.has(agentKey))agents.set(agentKey,proxy?
      new ProxyAgent({uri:proxy,allowH2:true,requestTls:{allowH2:true}}):direct());
    return fetch(input,{...options,dispatcher:agents.get(agentKey)});
  }
  request.close=async()=>{await Promise.all([...new Set([...agents.values(),environmentAgent,directAgent])].filter(Boolean).map(agent=>agent.close()));};
  return request;
}

export const proxyFetch=createProxyFetch();
