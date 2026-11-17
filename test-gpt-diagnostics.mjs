import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createDiagnostics} from './gpt-diagnostics.mjs';

function temporary(t) {
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'gpt-diagnostics-'));
  t.after(()=>{
    for(const file of fs.readdirSync(directory))fs.unlinkSync(path.join(directory,file));
    fs.rmdirSync(directory);
  });
  return directory;
}

test('persistent diagnostics records only whitelisted protocol metadata',t=>{
  const record=createDiagnostics({directory:temporary(t)});
  record({event:'attempt_failed',trace:'trace1',phase:'stream',status:504,response_id:'resp_test',
    message:'PRIVATE ERROR',headers:{Authorization:'Bearer PRIVATE TOKEN'},input:'PRIVATE PROMPT',tool_arguments:'PRIVATE ARGUMENTS'});
  const raw=fs.readFileSync(record.path,'utf8');
  const row=JSON.parse(raw);
  assert.equal(row.event,'attempt_failed');
  assert.equal(row.status,504);
  assert.equal(row.response_id,'resp_test');
  assert.doesNotMatch(raw,/PRIVATE|Authorization/);
});

test('disabled logging creates no files',t=>{
  const directory=temporary(t),record=createDiagnostics({directory,enabled:false});
  record({event:'request_start'});
  assert.deepEqual(fs.readdirSync(directory),[]);
  assert.equal(record.path,undefined);
});

test('an unwritable diagnostics path does not stop inference',t=>{
  const directory=temporary(t),file=path.join(directory,'not-a-directory');
  fs.writeFileSync(file,'fixture');
  const warnings=[];
  const record=createDiagnostics({directory:file,warn:message=>warnings.push(message)});
  assert.doesNotThrow(()=>{record({event:'request_start'});record({event:'request_completed'});});
  assert.equal(warnings.length,1);
});

test('diagnostic logging stops at its per-process size cap without deleting prior logs',t=>{
  const directory=temporary(t),warnings=[];
  const record=createDiagnostics({directory,maxBytes:400,warn:message=>warnings.push(message)});
  for(let index=0;index<20;index++)record({event:'request_completed',trace:'trace'+index});
  assert.ok(fs.statSync(record.path).size<=400);
  assert.equal(warnings.length,1);
});
