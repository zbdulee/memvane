"""Real queued shared/private stores; no provider, trainer, or live data."""
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

from job_telemetry import replay
from context_learning import shared as shared_module
from context_learning.contracts import digest, request
from context_learning.seam import _private_state
from context_learning.selection_gold import _table
from context_learning.shared import _db, _v45_adapter_path, enqueue, run, sync_reviewed
from context_learning.store import canonical, capture, database

candidate = Path(shared_module.__file__).with_name('laya_adapter.py')
candidate_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
production_pin = shared_module.REVIEWED_ADAPTER_SHA256
different_pin = '0' * 64
assert candidate_sha not in (production_pin, different_pin)

with tempfile.TemporaryDirectory(prefix='qmd-historical-sync-', dir='/tmp') as name:
    base = Path(name).resolve()
    shared = base / 'shared'; shared.mkdir(mode=0o700)
    project = base / 'synthetic-story'; project.mkdir(mode=0o700)
    private = base / 'private'; private.mkdir(mode=0o700)
    wiki = project / '.auto-context' / 'wiki'; wiki.mkdir(parents=True)
    (project / '.auto-context' / 'settings.json').write_text(canonical({
        'indexing': True, 'collections': ['wiki'],
        'collectionPaths': {'wiki': '.auto-context/wiki'},
        'collectionRoles': {'wiki': 'wiki'},
        'contextLearning': {'capture': True, 'stateRoot': str(private)},
        'sharedLearning': {'enabled': True, 'root': str(shared)}}))
    consent_dir = shared / 'consents'; consent_dir.mkdir(mode=0o700)
    consent = consent_dir / (digest(str(project)) + '.json')
    consent.write_text(canonical({
        'schema': 'qmd-shared-consent-v1', 'projectRoot': str(project),
        'sharedRoot': str(shared), 'capture': True, 'localTraining': True,
        'externalTeacher': False}))
    consent.chmod(0o600)
    base_artifact = base / 'base.artifact'
    base_artifact.write_text('synthetic base')
    base_artifact.chmod(0o600)
    # Only this disposable test process reviews the candidate's exact bytes.
    with patch.object(Path, 'home', return_value=base), patch.object(
            shared_module, 'REVIEWED_ADAPTER_SHA256', candidate_sha):
        historical_adapter = _v45_adapter_path()
        policy = {'schema': 'qmd-shared-learning-v1', 'enabled': True,
                  'goldModel': 'gpt-6-luna', 'maxGoldCalls': 10,
                  'batchMin': 5, 'batchMax': 20, 'targetExact': .95,
                  'trainerArgv': [sys.executable, str(historical_adapter), str(base)],
                  'baseArtifact': str(base_artifact), 'maxRssMib': 4096,
                  'minAvailableMib': 128, 'maxLoadPerCpu': 4,
                  'safetySeconds': 900, 'experimental': True}
        policy_path = shared / 'shared-policy.json'
        policy_path.write_text(canonical(policy))
        policy_path.chmod(0o600)
        policy_bytes = policy_path.read_bytes()
        for unapproved in (production_pin, different_pin):
            with patch.object(shared_module, 'REVIEWED_ADAPTER_SHA256', unapproved):
                try:
                    shared_module._policy(shared)
                except ValueError as exc:
                    assert str(exc) == 'invalid_shared_trainer', str(exc)
                else:
                    raise AssertionError('unapproved candidate source accepted')
            assert policy_path.read_bytes() == policy_bytes
            assert not (shared / 'shared-learning.sqlite3').exists()
        config = {'sharedLearning': {'enabled': True, 'root': str(shared)},
                  'contextLearning': {'capture': True, 'stateRoot': str(private)},
                  'collectionPaths': {'wiki': '.auto-context/wiki'}}
        state = _private_state(config, project)
        assert state is not None
        for number in (1, 2):
            source = f'---\ntitle: Synthetic {number}\n---\nThe bridge is blue {number}.\n'
            cid = f'wiki/bridge-{number}.md'
            (wiki / f'bridge-{number}.md').write_text(source)
            sampling = {'mode': 'eligible-returned-pool',
                        'scope': 'queried-phases-only',
                        'phase_counts': {'primary': 1}, 'per_phase_limit': 8,
                        'eligible_ids': [cid], 'captured_ids': [cid],
                        'baseline_selected_ids': [cid], 'excluded': [],
                        'snapshot_failures': [], 'retrieval_truncated': False,
                        'assessment_complete': True,
                        'complete_within_returned_bound': True,
                        'index_revision': digest(f'synthetic-index-{number}')}
            sample = request({'prompt': f'Which bridge is blue {number}?'}, [
                {'candidate_id': cid, 'revision_sha256': digest(source),
                 'eligible': True, 'excerpt': source}], host='codex',
                request_id=f'synthetic-{number}', sampling=sampling)
            assert capture(sample, enabled=True, state_dir=state,
                           compact_sources={cid: source}) == 'stored'
            assert enqueue(config, project, sample, {cid: source}) == 'queued'
        with database(state) as db:
            _table(db)
        with _db(shared) as db:
            before = db.execute(
                'SELECT request_id,status,use_state,error FROM cases ORDER BY request_id'
            ).fetchall()
        assert before == [('synthetic-1', 'queued', 'active', None),
                          ('synthetic-2', 'queued', 'active', None)]
        # Consent withdrawn after capture. The real generic sync would retire
        # both queued cases, while a historical runtime must do no import or
        # reconciliation at all.
        consent.unlink()
        db_file = shared / 'shared-learning.sqlite3'
        before_bytes = db_file.read_bytes()
        assert run(shared, project=project, sync_config=config) == {
            'status': 'runtime_approval_required'}
        assert db_file.read_bytes() == before_bytes
        with patch.dict(os.environ, {'QMD_JOB_ID': 'worker:historical',
                                  'QMD_JOB_QUEUED_AT': str(time.time())}):
            assert run(shared, project=project, sync_config=config) == {
                'status': 'runtime_approval_required'}
        assert db_file.read_bytes() == before_bytes
        events = replay((shared / 'job-telemetry.jsonl').read_text().splitlines())
        assert len(events) == 1 and events[0]['status'] == 'deferred', events
        with sqlite3.connect(db_file) as db:
            assert db.execute(
                'SELECT request_id,status,use_state,error FROM cases ORDER BY request_id'
            ).fetchall() == before
        # Positive control: invoke the actual sync against the same files.
        assert sync_reviewed(config, project) == {'status': 'synced', 'imported': 0}
        with sqlite3.connect(db_file) as db:
            after = db.execute(
                'SELECT request_id,status,use_state,error FROM cases ORDER BY request_id'
            ).fetchall()
        assert after == [(f'synthetic-{number}', 'queued', 'historical',
                          'consent_or_source_changed') for number in (1, 2)]

assert shared_module.REVIEWED_ADAPTER_SHA256 == production_pin
print(json.dumps({'queuedCases': len(before), 'guardPreservedBytes': True,
                  'realSyncWouldMutate': len(after), 'telemetryDeferred': True}))
