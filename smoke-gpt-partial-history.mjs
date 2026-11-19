// Live protocol check. Uses synthetic Read results; executes no real tools.
import {modelCatalog,startBridge} from './gpt-bridge.mjs';
const catalog=await modelCatalog();
const model=process.env.GPT_CLAUDE_MODEL||'gpt-6.2-sol';
const bridge=await startBridge(model,catalog);
async function send(body) {
  const response=await fetch(bridge.url+'/v1/messages',{
    method:'POST',headers:{'x-api-key':bridge.token,'Content-Type':'application/json'},
    body:JSON.stringify({model,...body}),signal:AbortSignal.timeout(120000)
  });
  const message=await response.json();
  if(!response.ok)throw new Error(JSON.stringify(message.error));
  return message;
}
try {
  const tools=[{name:'Read',description:'Read a synthetic test fixture.',input_schema:{type:'object',properties:{file_path:{type:'string'}},required:['file_path']}}];
  const initial={role:'user',content:'Call Read four times in parallel for a.txt, b.txt, c.txt and d.txt in one response. Do not output prose.'};
  const first=await send({messages:[initial],tools,tool_choice:{type:'any'}});
  const calls=first.content.filter(block=>block.type==='tool_use');
  if(calls.length<2)throw new Error('Model did not produce a parallel batch; check is inconclusive');
  const second=await send({
    messages:[initial,...first.content.map(block=>({role:'assistant',content:[block]})),
      {role:'user',content:[{type:'tool_result',tool_use_id:calls[0].id,content:'Synthetic fixture value: alpha'},
        {type:'text',text:'This is a permission-review snapshot. The other calls have no results yet. Reply only OK and do not request tools.'}]}],
    tools,tool_choice:{type:'none'}
  });
  if(second.stop_reason!=='end_turn'||!second.content.some(block=>block.type==='text'))throw new Error('Partial-history response did not complete');
  console.log(JSON.stringify({parallel_calls:calls.length,supplied_results:1,partial_history_accepted:true,real_tools_executed:0}));
} finally {bridge.close();}
