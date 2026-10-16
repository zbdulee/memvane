"""Real localhost bind/race tests with a tiny synthetic HTTP child, no QMD."""
import concurrent.futures
import errno
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
import http.server
import threading
import random
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path.cwd() / 'core'))
import qmd_endpoint as E

SERVER = """
import http.server,sys,socket,os
port=int(sys.argv[-1])
host=os.environ.get('QMD_TEST_SERVER_HOST','127.0.0.1')
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.end_headers();self.wfile.write(b'{"status":"ok"}')
 def log_message(self,*args):pass
class Server(http.server.HTTPServer):address_family=socket.AF_INET6 if host=='::1' else socket.AF_INET
try:Server((host,port),Handler).serve_forever()
except OSError as e:
 print('EADDRINUSE' if e.errno==48 or e.errno==98 else str(e),flush=True);sys.exit(1)
"""

class EndpointTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='qmd-endpoint-')
  self.home=Path(self.tmp.name);self.root=self.home/'backend';self.root.mkdir(mode=0o700)
  self.server=self.home/'synthetic-server.py';self.server.write_text(SERVER)
  self.env=patch.dict(os.environ,{'HOME':str(self.home),'QMD_BACKEND_STATE_DIR':str(self.root),
      'QMD_DAEMON_PORT_ATTEMPTS':'4','QMD_DAEMON_READY_SECONDS':'3','QMD_SETUP_GUARD_FIXTURE':''})
  self.env.start()
  sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'helpers'))
  from uid_home import isolate
  self.addCleanup(isolate(self.home))
  os.environ['QMD_LOCK_BASE']=str(self.home/'writer-locks')
  os.environ['TMPDIR']=str(self.home/'tmp');Path(os.environ['TMPDIR']).mkdir(mode=0o700)
  self.spec={'node':sys.executable,'entry':str(self.server),'runtime':'synthetic-runtime','store':'synthetic-store'}
  self.expected=patch.object(E,'expected',return_value=self.spec);self.expected.start()
  self.children=[];self.sockets=[];self.calls=[]
  self.server_host='127.0.0.1'
  self.starter=patch.object(E,'spawn',side_effect=self.spawn);self.starter.start()
  self.identity=patch.object(E,'process_command',side_effect=self.command);self.identity.start()
  self.actual_bound=E.bound
  self.listeners=patch.object(E,'bound',side_effect=self.bound);self.listeners.start()
  # Actual listen ownership is checked via lsof in production; tests map only
  # children they spawned. Real bind and HTTP health still decide readiness.
 def free_port(self):
  # Keep the controlled candidate block outside macOS ephemeral client ports.
  # bind(0) alone says nothing about +1, and health's client may claim +1.
  for _ in range(100):
   port=random.randrange(20000,39000);reserved=[]
   try:
    for n in range(port,port+4):
     s=socket.socket();reserved.append(s);s.bind(('127.0.0.1',n))
    return port
   except OSError:pass
   finally:
    for s in reserved:s.close()
  raise AssertionError('no synthetic consecutive port block')
 def occupy(self,port):
  s=socket.socket();s.bind(('127.0.0.1',port));s.listen();self.sockets.append(s);return s
 def occupy6(self,port):
  s=socket.socket(socket.AF_INET6,socket.SOCK_STREAM)
  try:s.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1);s.bind(('::1',port));s.listen()
  except OSError as e:s.close();self.skipTest(str(e))
  self.sockets.append(s);return s
 def spawn(self,port,log):
  self.calls.append(port)
  child=subprocess.Popen([sys.executable,str(self.server),'mcp','--http','--port',str(port)],
      stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'QMD_TEST_SERVER_HOST':self.server_host});self.children.append((child,port));return child
 def command(self,pid):
  if pid==os.getpid():return 'python synthetic endpoint tests'
  for child,port in self.children:
   if child.pid==pid and child.poll() is None:
    return f'{sys.executable} {self.server} mcp --http --port {port}'
  return ''
 def bound(self,pid,port):
  return self.actual_bound(pid,port) if any(p.pid==pid and n==port and p.poll() is None for p,n in self.children) else set()
 def tearDown(self):
  for child,_ in self.children:
   if child.poll() is None:child.terminate();child.wait(timeout=3)
  for s in self.sockets:s.close()
  for p in (self.listeners,self.identity,self.starter,self.expected,self.env):p.stop()
  self.tmp.cleanup()
 def start(self,base):
  with patch.dict(os.environ,{'QMD_DAEMON_PORT':str(base)}):return E.ensure()
 def test_busy_base_advances_and_publishes_actual_bind(self):
  port=self.free_port();self.occupy(port)
  result=self.start(port);self.assertEqual(result['port'],port+1)
  self.assertTrue(E.healthy(result['port']))
  self.assertEqual(E.read()['port'],port+1)
  self.assertEqual(E.daemon_url(),f'http://127.0.0.1:{port+1}')
 def test_bind_race_advances_without_signalling_foreign(self):
  port=self.free_port();original=self.spawn
  def race(n,log):
   if n==port:self.occupy(n)
   return original(n,log)
  with patch.object(E,'spawn',side_effect=race):result=self.start(port)
  self.assertEqual(result['port'],port+1)
  self.assertEqual(self.sockets[0].getsockname()[1],port)
 def test_ipv6_occupied_base_advances_and_pins_own_ipv6_address(self):
  port=self.free_port();foreign=self.occupy6(port);self.server_host='::1'
  row=self.start(port);self.assertEqual(row['port'],port+1);self.assertEqual(row['url'],f'http://[::1]:{port+1}')
  self.assertTrue(E.healthy(row['port'],row['url']));self.assertEqual(foreign.getsockname()[1],port)
 def test_other_family_bind_race_advances_without_signalling_foreign(self):
  port=self.free_port();original=self.spawn
  def race(n,log):
   if n==port:self.occupy6(n)
   return original(n,log)
  with patch.object(E,'spawn',side_effect=race):row=self.start(port)
  self.assertEqual(row['port'],port+1);self.assertEqual(self.sockets[0].getsockname()[1],port)
 def test_exhaustion_never_publishes_endpoint(self):
  port=self.free_port()
  for n in range(port,port+4):self.occupy(n)
  with self.assertRaisesRegex(ValueError,'qmd_port_range_exhausted'):self.start(port)
  self.assertFalse((self.root/'endpoint.json').exists())
 def test_concurrent_start_reuses_one_owned_healthy_child(self):
  port=self.free_port()
  from context_learning.local_cycle import ResourceDeferred
  def start(_):
   try:return E.ensure()
   except ResourceDeferred as error:
    self.assertEqual(str(error),'qmd_startup_busy');return None
  with patch.dict(os.environ,{'QMD_DAEMON_PORT':str(port)}):
   with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(start,range(2)))
   results=[row for row in results if row is not None]
   self.assertTrue(results);self.assertEqual(E.ensure()['pid'],results[0]['pid'])
  self.assertTrue(all(row['pid']==results[0]['pid'] for row in results));self.assertEqual(len(self.calls),1)
 def test_stale_dead_child_is_replaced_without_changing_data(self):
  port=self.free_port();first=self.start(port)
  child=self.children[0][0];child.terminate();child.wait(timeout=3)
  second=self.start(port)
  self.assertNotEqual(first['pid'],second['pid']);self.assertEqual(second['port'],port)
 def test_runtime_change_rejects_a_live_endpoint(self):
  self.start(self.free_port())
  with patch.object(E,'expected',return_value={**self.spec,'runtime':'changed'}):
   with self.assertRaisesRegex(ValueError,'qmd_endpoint_generation_conflict'):E.ensure()
 def test_foreign_pid_cannot_be_reused_as_ours(self):
  result=self.start(self.free_port())
  with patch.object(E,'process_command',return_value='python slack-monitor-server.py'):
   with self.assertRaisesRegex(ValueError,'qmd_endpoint_identity_invalid'):E.read()
 def test_url_override_must_match_selected_endpoint(self):
  result=self.start(self.free_port())
  with patch.dict(os.environ,{'QMD_DAEMON_URL':'http://localhost:8584'}):
   with self.assertRaisesRegex(ValueError,'qmd_endpoint_url_conflict'):E.daemon_url()
 def test_endpoint_has_no_secrets_and_is_private(self):
  self.start(self.free_port());p=self.root/'endpoint.json'
  self.assertEqual(p.stat().st_mode&0o777,0o600)
  self.assertEqual(set(json.loads(p.read_text())),{'schema','pid','port','url','node','entry','runtime','store'})
 def test_unsafe_or_malformed_endpoint_is_not_overwritten(self):
  p=self.root/'endpoint.json';p.write_text('{}');p.chmod(0o600)
  with self.assertRaisesRegex(ValueError,'qmd_endpoint_invalid'):self.start(self.free_port())
  self.assertEqual(p.read_text(),'{}')
 def test_live_unhealthy_owned_daemon_is_retained(self):
  result=self.start(self.free_port())
  with patch.object(E,'healthy',return_value=False),patch.object(E,'bound',return_value={'127.0.0.1'}):
   with self.assertRaisesRegex(ValueError,'qmd_endpoint_unhealthy'):E.ensure()
  self.assertIsNone(self.children[0][0].poll())
 def test_invalid_scan_bounds_fail_without_spawning(self):
  with patch.dict(os.environ,{'QMD_DAEMON_PORT_ATTEMPTS':'0'}):
   with self.assertRaisesRegex(ValueError,'invalid_qmd_port_scan'):self.start(self.free_port())
  self.assertEqual(self.calls,[])
 def test_real_process_identity_and_loopback_listener(self):
  self.identity.stop();self.listeners.stop()
  # Homebrew Python forwards to its Python.app Mach executable. Pin that
  # actual interpreter identity in this synthetic test, not the shell alias.
  self.spec['node']=E.process_command(os.getpid()).split(' ',1)[0]
  actual=E.owned;seen=[]
  def inspect(row):
   seen.append({'command':E.process_command(row['pid']),'bound':E.bound(row['pid'],row['port']),
                'node':row['node'],'entry':row['entry'],'port':row['port']})
   return actual(row)
  try:
   with patch.object(E,'owned',side_effect=inspect):
    try:
     result=self.start(self.free_port());self.assertEqual(E.read()['pid'],result['pid'])
    except Exception:
     print('process diagnostic:',seen[-1:] ,file=sys.stderr);raise
  finally:self.identity.start();self.listeners.start()
 def test_nonlocal_listener_is_rejected_before_endpoint_reuse(self):
  with patch.object(E.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='n*:19485\n')):
   with self.assertRaisesRegex(ValueError,'qmd_endpoint_nonlocal_bind'):self.actual_bound(os.getpid(),19485)
 def test_wiki_network_client_accepts_only_selected_endpoint(self):
  import wiki_compile,qmd_shared_store,io
  url='http://localhost:19485'
  response=io.BytesIO(b'{"results":[{"file":"synthetic-wiki/card.md","score":1}]}')
  with patch.object(qmd_shared_store,'select',return_value={}),patch.object(E,'daemon_url',return_value=url),patch('urllib.request.urlopen',return_value=response) as request:
   rows=wiki_compile.query_wiki_similar(url,'synthetic-wiki','Synthetic only',3,1)
  self.assertTrue(rows);self.assertEqual(request.call_args.args[0].full_url,url+'/query')
 def test_ps_denial_blocks_before_spawn_and_is_not_daemon_dead(self):
  with patch.object(E,'process_command',side_effect=ValueError('qmd_endpoint_identity_unavailable')):
   with self.assertRaisesRegex(ValueError,'qmd_endpoint_identity_unavailable'):self.start(self.free_port())
  self.assertEqual(self.calls,[])
 def test_kill_zero_eperm_blocks_before_spawn(self):
  with patch.object(E.os,'kill',side_effect=PermissionError('synthetic seatbelt')):
   with self.assertRaisesRegex(ValueError,'qmd_endpoint_identity_unavailable'):self.start(self.free_port())
  self.assertEqual(self.calls,[])
 def test_ps_spawn_permission_denial_is_classified(self):
  self.identity.stop()
  try:
   with patch.object(E.subprocess,'run',side_effect=PermissionError('synthetic ps rc126')):
    with self.assertRaisesRegex(ValueError,'qmd_endpoint_identity_unavailable'):E.process_command(os.getpid())
  finally:self.identity.start()
 def test_other_address_family_is_never_contacted(self):
  port=self.free_port();hits=[]
  class Foreign(http.server.BaseHTTPRequestHandler):
   def do_GET(self):
    hits.append(self.path);self.send_response(200);self.end_headers();self.wfile.write(b'{"status":"ok"}')
   def log_message(self,*args):pass
  class IPv6(http.server.HTTPServer):address_family=socket.AF_INET6
  try:foreign=IPv6(('::1',port),Foreign)
  except OSError as e:self.skipTest(str(e))
  thread=threading.Thread(target=foreign.serve_forever,daemon=True);thread.start()
  try:
   row=self.start(port);self.assertEqual(row['url'],f'http://127.0.0.1:{port+1}')
   self.assertTrue(E.healthy(row['port'],row['url']));self.assertEqual(hits,[])
  finally:foreign.shutdown();foreign.server_close();thread.join(2)
 def test_reload_uses_endpoint_and_same_lock(self):
  self.assertTrue(hasattr(E,'reload'),'missing proved endpoint reload')
  port=self.free_port()
  with patch.dict(os.environ,{'QMD_DAEMON_PORT':str(port)}),patch.object(E,'attest_record',return_value=None):
   first=E.ensure();second=E.reload()
  self.assertNotEqual(first['pid'],second['pid']);self.assertEqual(E.read()['pid'],second['pid'])
 def test_reload_generation_change_uses_attested_previous_endpoint(self):
  self.assertTrue(hasattr(E,'reload'),'missing proved endpoint reload')
  port=self.free_port()
  with patch.dict(os.environ,{'QMD_DAEMON_PORT':str(port)}),patch.object(E,'attest_record',return_value=None):
   first=E.ensure()
   self.spec['runtime']='replacement-runtime';second=E.reload()
  self.assertNotEqual(first['pid'],second['pid']);self.assertEqual(second['runtime'],'replacement-runtime')
 def test_reload_never_signals_foreign_pid(self):
  self.assertTrue(hasattr(E,'reload'),'missing proved endpoint reload')
  self.start(self.free_port())
  with patch.object(E,'attest_record',return_value=None),patch.object(E,'process_command',return_value='python slack-monitor-server.py'):
   with self.assertRaisesRegex(ValueError,'qmd_endpoint_identity_invalid'):E.reload()
  self.assertIsNone(self.children[0][0].poll())
 def test_successful_daemon_uses_one_managed_log(self):
  self.start(self.free_port())
  self.assertTrue((self.root/'daemon.log').exists(),'current log must match logrotate target')
  self.assertEqual(list(self.root.glob('.start-*')),[])
 def test_cpu_flag_is_returned_by_attested_selection_for_child(self):
  import qmd_runtime
  self.assertTrue(hasattr(E,'child_environment'),'missing attested child environment')
  with patch.object(qmd_runtime,'select',return_value={'forceCpu':True}):
   self.assertEqual(E.child_environment(19485)['QMD_FORCE_CPU'],'1')

class PortPolicyTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='qmd-port-policy-');self.root=Path(self.tmp.name)/'backend';self.root.mkdir(mode=0o700)
  self.env=patch.dict(os.environ,{'QMD_BACKEND_STATE_DIR':str(self.root),'QMD_SETUP_GUARD_FIXTURE':''});self.env.start()
  for key in ('QMD_DAEMON_PORT','QMD_DAEMON_PORT_ATTEMPTS','QMD_DAEMON_READY_SECONDS','QMD_DAEMON_URL'):os.environ.pop(key,None)
  self.spec={'node':'synthetic-node','entry':'synthetic-entry','runtime':'synthetic-runtime','store':'synthetic-store'}
  self.current_port=None;self.calls=[]
  self.actual_port_busy=E.port_busy
  self.patches=[patch.object(E,'expected',return_value=self.spec),patch.object(E,'inspection_available'),
   patch.object(E,'alive',side_effect=lambda pid:pid==os.getpid()),
   patch.object(E,'process_command',side_effect=lambda pid:f'synthetic-node synthetic-entry mcp --http --port {self.current_port}'),
   patch.object(E,'bound',side_effect=lambda pid,port:{'127.0.0.1'} if port==self.current_port else set()),
   patch.object(E,'healthy',return_value=True),patch.object(E,'spawn',side_effect=self.spawn),
   patch.object(E,'port_busy',return_value=False,create=True)]
  for p in self.patches:p.start()
 def tearDown(self):
  for p in reversed(self.patches):p.stop()
  self.env.stop();self.tmp.cleanup()
 def spawn(self,port,log):
  self.calls.append(port);self.current_port=port
  return SimpleNamespace(pid=os.getpid(),poll=lambda:None,terminate=lambda:None,kill=lambda:None,wait=lambda **kwargs:0)
 def record(self,port,pid=None):
  self.current_port=port;E.publish(self.root,{'schema':E.SCHEMA,**self.spec,'pid':pid or os.getpid(),'port':port,'url':E.loopback_url('127.0.0.1',port)})
 def test_default_scan_starts_at_8584_with_existing_bounded_budget(self):
  self.assertEqual(E.limits(),(8584,16,10.))
 def test_default_busy_ports_advance_one_at_a_time_and_clients_use_record(self):
  checked=[]
  def busy(port,**kwargs):
   if kwargs.get('owned_hosts'):return False
   checked.append(port);return port in (8584,8585)
  with patch.object(E,'port_busy',side_effect=busy,create=True):row=E.ensure()
  self.assertEqual(checked,[8584,8585,8586]);self.assertEqual(self.calls,[8586]);self.assertEqual(row['port'],8586)
  self.assertEqual(E.daemon_url(),'http://127.0.0.1:8586')
 def test_explicit_port_keeps_existing_scan_start_contract(self):
  with patch.dict(os.environ,{'QMD_DAEMON_PORT':'23010','QMD_DAEMON_PORT_ATTEMPTS':'3'}),patch.object(E,'port_busy',side_effect=lambda port,**kwargs:port==23010,create=True):row=E.ensure()
  self.assertEqual(self.calls,[23011]);self.assertEqual(row['port'],23011);self.assertEqual(E.daemon_url(),'http://127.0.0.1:23011')
 def test_live_owned_old_endpoint_and_record_are_preserved_without_probe(self):
  self.record(19485);before=(self.root/'endpoint.json').read_bytes()
  with patch.object(E,'port_busy',side_effect=AssertionError('healthy endpoint must not scan'),create=True):row=E.ensure()
  self.assertEqual(row['port'],19485);self.assertEqual(self.calls,[]);self.assertEqual((self.root/'endpoint.json').read_bytes(),before)
 def test_stale_endpoint_recovers_using_new_default_without_changing_data(self):
  self.record(19485,pid=12345);marker=self.root/'preserved-marker';marker.write_bytes(b'synthetic existing data')
  row=E.ensure();self.assertEqual(row['port'],8584);self.assertEqual(self.calls,[8584]);self.assertEqual(E.read()['port'],8584)
  self.assertEqual(marker.read_bytes(),b'synthetic existing data')
 def test_range_can_end_at_65535_and_never_wraps(self):
  with patch.dict(os.environ,{'QMD_DAEMON_PORT':'65535','QMD_DAEMON_PORT_ATTEMPTS':'1'}):
   self.assertEqual(E.limits()[0:2],(65535,1));row=E.ensure()
  self.assertEqual(row['port'],65535);self.assertEqual(self.calls,[65535])
 def test_invalid_ranges_fail_before_spawn_or_endpoint_publication(self):
  for port,attempts in [('1023','1'),('65536','1'),('65535','2'),('8584','0'),('8584','129')]:
   with self.subTest(port=port,attempts=attempts),patch.dict(os.environ,{'QMD_DAEMON_PORT':port,'QMD_DAEMON_PORT_ATTEMPTS':attempts}):
    with self.assertRaisesRegex(ValueError,'invalid_qmd_port_scan'):E.ensure()
  self.assertEqual(self.calls,[]);self.assertFalse((self.root/'endpoint.json').exists())
 def test_port_probe_permission_denial_is_not_a_free_port(self):
  with patch.object(E,'port_busy',side_effect=self.actual_port_busy),patch.object(E.socket,'socket',side_effect=PermissionError(errno.EACCES,'synthetic socket denial')):
   with self.assertRaisesRegex(ValueError,'qmd_port_probe_unavailable'):E.ensure()
  self.assertEqual(self.calls,[]);self.assertFalse((self.root/'endpoint.json').exists())

if __name__=='__main__':unittest.main()
