"""Synthetic capture -> review -> split -> local train -> score -> promote -> rollback."""
import json
import fcntl
import os
from pathlib import Path
import sys
import tempfile

from memvane.learning.context_learning.contracts import request, digest
from memvane.learning.context_learning.offline import build_manifest, review_digest, save_label
from memvane.learning.context_learning.selection_gold import save_selection
from memvane.learning.context_learning.store import capture, canonical
from memvane.learning.context_learning.local_cycle import LocalTrainer, run_cycle, rollback
from memvane.learning.context_learning.schedule import run_due_cycle
from memvane.learning.tests.support import MOCK_TRAINER


def reject(fn):
    try: fn()
    except (ValueError, BlockingIOError) as exc:
        if os.environ.get('QMD_TEST_DEBUG'): print('expected rejection:', repr(exc), file=sys.stderr)
        return
    raise AssertionError('expected rejection')


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp); state = root/'state'; state.mkdir(mode=0o700)
    assignments = {}; revisions = {}
    for i in range(200):
        split = 'train' if i < 100 else 'validation' if i < 150 else 'evaluation'
        wanted = i % 5 != 0
        rid = 'q'+str(i); cid = 'card'+str(i); revision = digest(cid)
        sample = request({'prompt': ('wanted' if wanted else 'none')+' synthetic '+str(i)},
            [{'candidate_id': cid, 'revision_sha256': revision, 'eligible': True,
              'excerpt': 'synthetic evidence '+str(i)}], host='codex', request_id=rid)
        assert capture(sample, enabled=True, state_dir=state) == 'stored'
        label = {'schema_version': 1, 'request_id': rid, 'candidate_id': cid,
            'revision_sha256': revision, 'abstain': False,
            'relevance': 'necessary' if wanted else 'irrelevant', 'evidence': 'synthetic'}
        assert save_label(state, label, review={'reviewer_id': 'synthetic-reviewer',
            'reference': 'synthetic-case-'+str(i), 'evidence_sha256': review_digest(label)}) == 'reviewed'
        save_selection(state, rid, [cid] if wanted else [], input_sha256=digest(canonical(sample)),
            reviewer_id='synthetic-reviewer', reference='synthetic-case-'+str(i))
        assignments[rid] = {'split': split, 'task_family': 'family'+str(i),
                            'document_families': {cid: 'source'+str(i)}}
        revisions[cid] = revision
    manifest = build_manifest(state, 'synthetic-v1', assignments, revisions)
    assert len(manifest['cases']) == 200
    base = state/'base.artifact'
    base.write_text(json.dumps({'model': 'always-none'})); base.chmod(0o600)
    trainer = LocalTrainer([sys.executable, str(MOCK_TRAINER)],
                           max_seconds=30, max_rss_mib=1024)
    (state/'cycles').mkdir(mode=0o700)
    state_dir = state/'cycles'/'resume'
    state_dir.mkdir(mode=0o700)
    lock_fd=os.open(state/'cycles'/'cycle.lock',os.O_RDWR|os.O_CREAT,0o600)
    fcntl.flock(lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    reject(lambda:run_cycle(state,'concurrent','synthetic-v1',lambda:revisions,trainer,
                            incumbent_artifact=base))
    os.close(lock_fd)
    (state_dir/'fail-once').write_text('synthetic interrupt')
    reject(lambda: run_cycle(state, 'resume', 'synthetic-v1', lambda: revisions, trainer, incumbent_artifact=base))
    assert json.loads((state_dir/'state.json').read_text())['phase'] == 'queued'
    result = run_cycle(state, 'resume', 'synthetic-v1', lambda: revisions, trainer, incumbent_artifact=base)
    assert result['phase'] == 'promoted'
    assert result['decision']['reason'] == 'held_out_improvement'
    assert result['challenger_score']['exact_rate'] == 1
    assert result['incumbent_score']['exact_rate'] == .2
    assert result['independent_evaluation']['challenger']['exact_rate'] == 1
    assert run_cycle(state, 'resume', 'synthetic-v1', lambda: revisions, trainer, incumbent_artifact=base) == result
    stale = dict(revisions, card199=digest('changed'))
    reject(lambda: run_cycle(state, 'stale', 'synthetic-v1', lambda: stale, trainer, incumbent_artifact=base))
    assert rollback(state)['status'] == 'rolled_back'
    assert json.loads((state/'active-checkpoint.json').read_text())['artifact_sha256'] == digest(base.read_text())
    scheduled=run_due_cycle(state,'scheduled','synthetic-v1',lambda:revisions,trainer,
                            incumbent_artifact=base,now=100000)
    assert scheduled['status']=='completed' and scheduled['cadence']['target']==.95
    assert scheduled['cadence']['reason']=='below_target'
    assert run_due_cycle(state,'too-early','synthetic-v1',lambda:revisions,trainer,
                         incumbent_artifact=base,now=100001)['status']=='not_due'
    print(json.dumps({'synthetic_questions': 200, 'train': 100, 'validation': 50,
                      'evaluation': 50, 'resume': 'passed', 'promotion': 'passed',
                      'rollback': 'passed', 'stale_guard': 'passed', 'concurrency': 'passed',
                      'cadence': 'passed', 'train_resources': result['train_resources']}))
