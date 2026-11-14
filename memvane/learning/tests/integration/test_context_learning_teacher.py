"""Offline teacher contract rejects unsafe transport and malformed evidence."""
import json
import unittest

from memvane.learning.context_learning.contracts import request, digest
from memvane.learning.context_learning.teacher import prompt, validate_response, require_tool_free_transport
from memvane.learning.context_learning.capabilities import preflight, CHECKS
from memvane.learning.context_learning.claude_adapter import argv, inspect_event, startup_metadata, StartupRejected


def _check():
 configured=argv(disabled_plugin_ids=['synthetic@builtin']);settings=json.loads(configured[configured.index('--settings')+1]);assert settings=={'disableAllHooks':True,'enabledPlugins':{'synthetic@builtin':False}}
 for value in ['*',['*'],['synthetic'],['synthetic@builtin;echo'],[None]]:
  try:argv(disabled_plugin_ids=value)
  except ValueError:pass
  else:raise AssertionError('guessed/invalid plugin exclusion accepted')
 a=argv();assert a[a.index("--model")+1]=="sonnet" and a[a.index("--effort")+1]=="low" and a[a.index("--tools")+1]==""
 assert inspect_event({"type":"system","subtype":"init","model":"claude-sonnet-synthetic","tools":[],"mcp_servers":[],"plugins":[],"skills":[]})["catalogs"]["tools"]["state"]=="empty"
 policy={'type':'system','subtype':'init','model':'claude-sonnet-synthetic','tools':[],'mcp_servers':[],'skills':[],'plugins':[{'name':'cc-plugin-sec-default','source':'cc-plugin-sec-default@builtin','path':'builtin'}]}
 assert inspect_event(policy,allow_security_policy=True)['retained_security_policy_only'] is True
 for change in [{'plugins':policy['plugins']+[{'name':'other','source':'other@builtin'}]}, {'plugins':[{'name':'cc-plugin-sec-default','source':'cc-plugin-sec-default@untrusted'}]}, {'plugins':[{'name':'cc-plugin-sec-default'}]}, {'tools':['Read']}, {'mcp_servers':['server']}, {'skills':['skill']}]:
  try:inspect_event(dict(policy,**change),allow_security_policy=True)
  except StartupRejected:pass
  else:raise AssertionError('policy allowance broadened isolation')
 try:inspect_event(policy)
 except StartupRejected:pass
 else:raise AssertionError('policy allowance enabled implicitly')
 metadata=startup_metadata({'model':'claude-sonnet-synthetic','effort':'low','tools':[],'mcp_servers':[],'skills':[],'plugins':[{'name':'synthetic-private-name','path':'/synthetic/private/location'}],'apiKeySource':'synthetic-secret'})
 identified=startup_metadata({'plugins':[{'name':'synthetic-known'},{'name':'synthetic-new@builtin'},{'name':'unmatched','path':'/private/secret'}]},inventory_ids=['synthetic-known@verified-market'])
 assert identified['plugin_identities'][0]['candidate_ids']==['synthetic-known@verified-market']
 assert identified['plugin_identities'][1]=={'status':'reported-full-id','id':'synthetic-new@builtin'}
 assert identified['plugin_identities'][2]['status']=='unresolved-name' and identified['plugin_identities'][2]['name']=='unmatched' and '/private/secret' not in json.dumps(identified)
 source_meta=startup_metadata({'plugins':[{'name':'synthetic-builtin','source':'synthetic-builtin@builtin','path':'/private/path','auth':'secret'}]})
 assert source_meta['plugin_identities'][0]['status']=='reported-source-id' and source_meta['plugin_identities'][0]['source']=='synthetic-builtin@builtin' and '/private/path' not in json.dumps(source_meta) and 'secret' not in json.dumps(source_meta)
 unsafe_meta=startup_metadata({'plugins':[{'name':'/private/path','source':'https://secret.example/key'}]})
 assert '/private/path' not in json.dumps(unsafe_meta) and 'secret.example' not in json.dumps(unsafe_meta)
 assert metadata['catalogs']['plugins']=={'state':'nonempty','count':1} and metadata['reported_effort']=='low'
 assert metadata['plugin_identities'][0]['name']=='synthetic-private-name' and 'private/location' not in json.dumps(metadata) and 'synthetic-secret' not in json.dumps(metadata)
 try:inspect_event({'type':'system','subtype':'init','model':'claude-sonnet-synthetic','tools':[],'mcp_servers':[],'skills':[],'plugins':[{'name':'synthetic-private-name'}]})
 except StartupRejected as exc:assert exc.metadata['resolved_model']=='claude-sonnet-synthetic' and exc.metadata['catalogs']['plugins']['count']==1
 else:raise AssertionError('loaded plugin accepted')
 for value in [None,False,'',{},0]:
  try:inspect_event({'type':'system','subtype':'init','model':'claude-sonnet-synthetic','tools':value,'mcp_servers':[],'plugins':[],'skills':[]})
  except StartupRejected:pass
  else:raise AssertionError('missing/invalid catalog accepted')
 for event in [{"type":"system","subtype":"init","model":"claude-sonnet-synthetic","plugins":[{"name":"synthetic-builtin"}]},{"type":"system","subtype":"init","model":"claude-sonnet-synthetic","skills":["synthetic-skill"]},{"type":"system","subtype":"init","model":"claude-sonnet-synthetic","mcp_servers":[{"name":"synthetic-mcp"}]},{"type":"system","subtype":"hook_started"},{"type":"tool_progress"},{"type":"assistant","message":{"content":[{"type":"tool_use"}]}},{"type":"system","subtype":"init","model":"claude-opus-synthetic"},{"type":"system","subtype":"init","model":"claude-sonnet-synthetic","tools":["Read"]}]:
  try:inspect_event(event)
  except ValueError:pass
  else:raise AssertionError("unexpected tool/hook/model accepted")
 for host in ('claude','codex'):
  report=preflight(host);assert report['cli']==host and report['fallback'] is None and len(report['missing'])==5 and not report['provider_enabled']
  verified={k:{'state':'verified','basis':'synthetic-mock-only'} for k in CHECKS}
  report=preflight(host,checks=verified);assert report['capabilities_complete'] and not report['provider_enabled'] and report['transport_implemented'] and report['transport_default']=='off-explicit-call-required'
  verified['tool_catalog_empty']={'state':'unverified','basis':'execution-denied-is-not-catalog-empty'}
  assert 'tool_catalog_empty' in preflight(host,checks=verified)['missing']
 for host in ('agy','hermes','unknown'):
  try:preflight(host)
  except ValueError:pass
  else:raise AssertionError('silent host fallback')
 try:preflight('codex',checks={'readonly':{'state':'verified','basis':'mock'}})
 except ValueError:pass
 else:raise AssertionError('unknown guarantee accepted')
 sample=request({'prompt':'Synthetic approval question'},[{'candidate_id':'synthetic-a','revision_sha256':digest('source'),'eligible':True,'excerpt':'Approval is required.'}],host='codex',request_id='synthetic-request')
 assert json.loads(prompt(sample))['sample']==sample
 label={'schema_version':2,'request_id':'synthetic-request','candidate_id':'synthetic-a','revision_sha256':digest('source'),'abstain':False,'relevance':'necessary','evidence':'Approval','rationale':{'kind':'support','text':'The excerpt supplies the required approval.','span':{'start':0,'end':8},'scope':'provided-excerpt'}}
 raw=json.dumps({'labels':[label]}).encode();assert validate_response(raw,sample)==[label]
 for payload in [b'{}',b'{"labels":[],"labels":[]}',b'x'*32769,json.dumps({'labels':[dict(label,evidence='invented')]}).encode(),json.dumps({'labels':[label,label]}).encode()]:
  try:validate_response(payload,sample)
  except ValueError:pass
  else:raise AssertionError('invalid response accepted')
 for catalog,isolated in [(None,True),(['apply_patch'],True),([],False)]:
  try:require_tool_free_transport(verified_tool_catalog=catalog,context_isolated=isolated)
  except ValueError:pass
  else:raise AssertionError('unverified transport accepted')
 require_tool_free_transport(verified_tool_catalog=[],context_isolated=True)


class TeacherContract(unittest.TestCase):
    def test_offline_teacher_contract_rejects_unsafe_transport_and_malformed_evidence(self):
        _check()


if __name__ == '__main__':
    unittest.main()
