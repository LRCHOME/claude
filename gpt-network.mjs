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
  let environmentAgent;
  async function request(input,options={}) {
    const url=new URL(typeof input==='string' || input instanceof URL?input:input.url);
    if(platform!=='win32')return fetch(input,options);
    if(['HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy'].some(key=>env[key])) {
      environmentAgent??=new EnvHttpProxyAgent({
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
    if(!agents.has(agentKey))agents.set(agentKey,proxy?new ProxyAgent(proxy):new Agent());
    return fetch(input,{...options,dispatcher:agents.get(agentKey)});
  }
  request.close=async()=>{await Promise.all([...agents.values(),environmentAgent].filter(Boolean).map(agent=>agent.close()));};
  return request;
}

export const proxyFetch=createProxyFetch();
