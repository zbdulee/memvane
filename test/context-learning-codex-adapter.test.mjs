import {test} from 'node:test';
import {execFileSync} from 'node:child_process';
import assert from 'node:assert/strict';
test('Codex lean descriptor preserves policies and rejects actual actions',()=>{
const out=execFileSync('python3',['-c',`
import sys,json
sys.path.insert(0,'.')
from memvane.learning.context_learning.codex_adapter import argv, child_environment, inspect_event
args=argv();assert args[args.index('--model')+1]=='gpt-6-luna'
assert '--ignore-user-config' in args and '--ignore-rules' in args and '--ephemeral' in args
assert args[args.index('--sandbox')+1]=='read-only'
assert 'skills.include_instructions=false' in args and 'model_reasoning_effort="low"' in args
assert not any('hooks=false' in a or 'instructions_file' in a or 'base_instructions' in a or 'include_permissions' in a or 'bypass' in a for a in args)
env=child_environment({'HOME':'synthetic-home','CODEX_HOME':'existing-auth-location','PATH':'synthetic-path','ORCA_AGENT_HOOK_TOKEN':'secret','OPENAI_API_KEY':'secret','CODEX_SESSION_SUGGESTIONS_DEBUG_LOG':'private'})
assert env=={'HOME':'synthetic-home','CODEX_HOME':'existing-auth-location','PATH':'synthetic-path','QMD_SANDBOX':'1'}
assert inspect_event({'type':'item.completed','item':{'type':'agent_message','text':'synthetic'}})['response']=='synthetic'
assert inspect_event({'type':'turn.completed','usage':{'input_tokens':12,'cached_input_tokens':0,'output_tokens':3,'private':'secret','invalid':-1}})['usage']=={'input_tokens':12,'cached_input_tokens':0,'output_tokens':3}
for event in [{'type':'item.started','item':{'type':t}} for t in ('command_execution','mcp_tool_call','web_search','file_change','collab_tool_call','error')]+[{'type':'hook.started'},{'type':'error'},{'type':'turn.failed'},None,{'type':'item.completed','item':{'type':'agent_message','text':'x'*32769}}]:
 try:inspect_event(event)
 except ValueError:pass
 else:raise AssertionError('action/error accepted')
print('ok')
`],{encoding:'utf8',timeout:10000,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
assert.equal(out.trim(),'ok');
});
