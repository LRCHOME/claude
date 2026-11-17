// Explicit live check: consumes ChatGPT plan tokens. Never part of node --test.
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';

const execute=promisify(execFile);
const launcher=fileURLToPath(new URL('./gpt-launch.mjs',import.meta.url));
const directory=await fs.mkdtemp(path.join(os.tmpdir(),'gpt-claude-smoke-'));
try {
  for(let run=1;run<=2;run++) {
    const marker=crypto.randomUUID();
    await fs.writeFile(path.join(directory,'first.txt'),'Read second.txt next.');
    await fs.writeFile(path.join(directory,'second.txt'),marker);
    const started=Date.now();
    const {stdout,stderr}=await execute(process.execPath,[launcher,
      '--bare','--setting-sources','','--strict-mcp-config','--mcp-config','{"mcpServers":{}}',
      '--no-session-persistence','--system-prompt','Follow the user instructions. Use Read to inspect files.',
      '--tools','Read','--allowedTools','Read','--output-format','stream-json','--verbose',
      '-p','Read first.txt, follow its instruction, and return exactly the contents of the second file.'
    ],{cwd:directory,env:{...process.env,GPT_CLAUDE_DEBUG:'1'},timeout:300000,maxBuffer:8*1024*1024});
    const events=stdout.trim().split(/\r?\n/).map(line=>JSON.parse(line));
    const result=events.findLast(e=>e.type==='result');
    const calls=events.filter(e=>e.type==='assistant').flatMap(e=>e.message.content).filter(b=>b.type==='tool_use');
    if(result?.is_error||result?.result?.trim()!==marker||calls.length<2)throw new Error('Live Read/replay round trip failed');
    console.log(JSON.stringify({run,elapsed_ms:Date.now()-started,tool_calls:calls.length,turns:result.num_turns,is_error:result.is_error,result_matches:true}));
    process.stderr.write(stderr);
  }
} finally {
  // Only files created above in this freshly created temporary directory.
  await fs.unlink(path.join(directory,'first.txt')).catch(()=>{});
  await fs.unlink(path.join(directory,'second.txt')).catch(()=>{});
  await fs.rmdir(directory);
}
