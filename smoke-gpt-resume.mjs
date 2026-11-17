// Explicit live check: consumes plan tokens and creates a saved test session.
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';

const execute=promisify(execFile);
const launcher=fileURLToPath(new URL('./gpt-launch.mjs',import.meta.url));
const directory=await fs.mkdtemp(path.join(os.tmpdir(),'gpt-claude-resume-'));
const session=crypto.randomUUID(),marker=crypto.randomUUID();
await fs.writeFile(path.join(directory,'marker.txt'),marker);
try {
  for(const stage of ['create','resume']) {
    const started=Date.now();
    const {stdout,stderr}=await execute(process.execPath,[launcher,
      '--bare','--setting-sources','','--strict-mcp-config','--mcp-config','{"mcpServers":{}}',
      '--system-prompt','Follow the user instructions. Use Read for files. Keep the conversation history.',
      '--tools','Read','--allowedTools','Read','--output-format','stream-json','--verbose',
      ...(stage==='create'?['--session-id',session]:['--resume',session]),
      '-p',stage==='create'?'Read marker.txt and reply with exactly its contents.':'Reply with exactly the marker from our previous conversation. Do not read files or call tools.'
    ],{cwd:directory,env:{...process.env,GPT_CLAUDE_DEBUG:'1'},timeout:300000,maxBuffer:8*1024*1024});
    process.stderr.write(stderr);
    const events=stdout.trim().split(/\r?\n/).map(line=>JSON.parse(line));
    const result=events.findLast(event=>event.type==='result');
    const calls=events.filter(event=>event.type==='assistant').flatMap(event=>event.message.content).filter(block=>block.type==='tool_use');
    if(result?.is_error||result?.result?.trim()!==marker||(stage==='create'&&!calls.length)||(stage==='resume'&&calls.length))throw new Error(`Live ${stage} failed: ${result?.result||'no result'}`);
    console.log(JSON.stringify({stage,elapsed_ms:Date.now()-started,tool_calls:calls.length,is_error:result.is_error,result_matches:true}));
  }
} finally {
  await fs.unlink(path.join(directory,'marker.txt'));
  await fs.rmdir(directory).catch(()=>{});
}
