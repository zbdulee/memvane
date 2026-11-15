"""Recall capture seam: shared enqueue diagnostics, offline contracts and source snapshots."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from memvane.learning.context_learning.contracts import digest, request, validate_label
from memvane.learning.context_learning.local_cycle import ResourceDeferred
from memvane.learning.context_learning.seam import _snapshot, observe
from memvane.learning.context_learning.store import capture


def _shared_enqueue_failure(base):
 project=base/'project';private=base/'private'
 (project/'docs').mkdir(parents=True);private.mkdir(mode=0o700)
 (project/'docs/guide.md').write_text('synthetic evidence')
 config={'collectionPaths':{'sample':'docs'},'contextLearning':{'capture':True,'stateRoot':str(private)},'sharedLearning':{'enabled':True}}
 state=private/digest(str(project))
 class Unprintable(ValueError):
  def __str__(self):raise RuntimeError('diagnostic formatting failed')
 for error,reason in [(ResourceDeferred('preparation_budget'),'preparation_budget'),(ValueError('synthetic body /private/path TOKEN=secret'),'enqueue_unavailable'),(Unprintable('private message'),'enqueue_unavailable'),(ValueError('project_scope_installation_changed'),'project_scope_installation_changed')]:
  with patch('memvane.learning.context_learning.corpus.snapshot',return_value=None),patch('memvane.learning.context_learning.shared.enqueue',side_effect=error),patch('memvane.learning.context_learning.shared.launch') as launch:
   assert observe({'prompt':'synthetic question'},config,project,[{'file':'sample/guide.md'}])=='stored'
   launch.assert_not_called()
  status=json.loads((state/'shared-last-status.json').read_text())
  assert status['status']=='unavailable'
  assert status.get('reason')==reason,status
  assert status['stage']=='enqueue' and status['errorType']==type(error).__name__
  assert 'TOKEN' not in json.dumps(status) and '/private/path' not in json.dumps(status)
 for error,reason in [(ResourceDeferred('learning_compute_busy'),'learning_compute_busy'),(ValueError('private message'),'launch_unavailable')]:
  with patch('memvane.learning.context_learning.corpus.snapshot',return_value=None),patch('memvane.learning.context_learning.shared.enqueue',return_value='queued'),patch('memvane.learning.context_learning.shared.launch',side_effect=error) as launch:
   assert observe({'prompt':'synthetic question'},config,project,[{'file':'sample/guide.md'}])=='stored'
   launch.assert_called_once()
  status=json.loads((state/'shared-last-status.json').read_text())
  assert status['status']=='unavailable' and status['stage']=='launch'
  assert status['reason']==reason and status['errorType']==type(error).__name__
 assert not (state/'shared-learning.sqlite3').exists()
 with sqlite3.connect(state/'learning.sqlite3') as db:assert db.execute('SELECT COUNT(*) FROM samples').fetchone()[0]==6


def _offline_contracts(root):
 c=[dict(candidate_id='c1',revision_sha256=digest('source'),excerpt='evidence'*100,eligible=True)]
 for host in ('claude','codex','hermes'):
  s=request({'prompt':'question'*400},c,host=host,request_id=host)
  assert s['prompt']['truncated'] and s['candidates'][0]['excerpt']['truncated']
  assert len(s['candidates'][0]['excerpt']['text'])==600
  before=list(root.iterdir())
  assert capture(s)=='disabled' and list(root.iterdir())==before
  assert capture(s,enabled='true',state_dir=root)=='disabled'
  assert capture(s,enabled=True,state_dir=root)=='stored'
  assert capture(s,enabled=True,state_dir=root)=='stored'
  label=dict(schema_version=1,request_id=host,candidate_id='c1',revision_sha256=digest('source'),abstain=False,relevance='necessary',evidence='evidence')
  assert validate_label(label,s)['relevance']=='necessary'
  for change in ({'schema_version':2},{'candidate_id':'unknown'},{'revision_sha256':digest('changed')},{'evidence':'absent'}, {'abstain':True}):
   try: validate_label(dict(label,**change),s)
   except ValueError: pass
   else: raise AssertionError(change)
  assert validate_label(dict(label,abstain=True,relevance=None),s)['abstain']
 for p in ({}, {'prompt':None}, {'prompt':''}):
  try: request(p,c,host='claude',request_id='x')
  except ValueError: pass
  else: raise AssertionError(p)
 with sqlite3.connect(root/'learning.sqlite3') as db:
  assert db.execute('select count(*) from samples').fetchone()[0]==3
  db.execute('BEGIN EXCLUSIVE')
  assert capture(s,enabled=True,state_dir=root)=='storage_unavailable'
 with patch('memvane.learning.context_learning.store.sqlite3.connect',side_effect=sqlite3.OperationalError('disk full')):
  assert capture(s,enabled=True,state_dir=root)=='storage_unavailable'
 assert capture(s,enabled=True)=='missing_state_dir'
 assert capture(dict(s,schema_version=99),enabled=True,state_dir=root)=='invalid_schema'
 assert capture(dict(s,prompt=dict(s['prompt'],sha256='bad')),enabled=True,state_dir=root)=='storage_unavailable'
 for candidates in (c*9, c+c, [dict(c[0],eligible=False)], [dict(c[0],revision_sha256='bad')]):
  try: request({'prompt':'question'},candidates,host='codex',request_id='x')
  except ValueError: pass
  else: raise AssertionError('invalid candidates accepted')
 (root/'link').symlink_to(root, target_is_directory=True)
 assert capture(s,enabled=True,state_dir=root/'link')=='invalid_state_dir'


def _snapshot_kinds(root):
 fifo=root/'fifo';os.mkfifo(fifo)
 assert _snapshot(fifo) is None
 regular=root/'regular';regular.write_text('snapshot')
 assert _snapshot(regular)[1]==b'snapshot'
 regular.write_bytes(b'x'*65537)
 assert _snapshot(regular) is None


class CaptureSeam(unittest.TestCase):
    def run_in(self, check, prefix):
        with tempfile.TemporaryDirectory(prefix=prefix) as name:
            check(Path(name).resolve())

    def test_shared_enqueue_failure_retains_safe_diagnostics_and_the_private_capture(self):
        self.run_in(_shared_enqueue_failure, 'qmd-shared-failure-')

    def test_offline_contracts_and_storage_are_isolated_explicit_and_fail_open(self):
        self.run_in(_offline_contracts, 'qmd-learning-')

    def test_source_snapshot_rejects_non_regular_files_without_blocking(self):
        self.run_in(_snapshot_kinds, 'qmd-source-kind-')


if __name__ == '__main__':
    unittest.main()
