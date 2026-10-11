"""Explicit offline selection cycle with a trusted local trainer protocol.

No hook starts this worker. The trainer receives reviewed train cases only;
predict requests contain no labels. All artifacts stay in an owner-only state
directory. A promotion changes only that directory's active pointer.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import time

from .contracts import digest
from .cycle_policy import promotion_decision, selection_score
from .dataset import read_private_json
from .selection_queue import build_queue
from .store import canonical, database

MAX_QUESTIONS = 2000
MAX_FILE = 16 * 1024 * 1024


def _safe_file(path, *, max_size=2 * 1024 * 1024 * 1024):
    p = Path(path)
    if p.is_symlink() or not p.is_file(): raise ValueError('unsafe_artifact')
    s = p.stat()
    if s.st_uid != os.getuid() or s.st_nlink != 1 or s.st_mode & 0o077 or s.st_size > max_size:
        raise ValueError('unsafe_artifact')
    return p


def _hash(path):
    h = hashlib.sha256()
    with _safe_file(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _atomic_json(path, value):
    path = Path(path)
    data = (canonical(value)+'\n').encode()
    if len(data) > MAX_FILE: raise ValueError('output_budget_exceeded')
    fd, name = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(data); out.flush(); os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def _load_state(path):
    return read_private_json(path) if Path(path).exists() else None


class LocalTrainer:
    """Trusted executable implementing train/predict JSON file protocol.

    argv is a fixed local command prefix, never generated from captured text.
    Its subprocess has a minimal offline environment and bounded wall/RSS/output.
    Offline variables are advisory; the executable itself must be audited local code.
    """
    def __init__(self, argv, *, max_seconds=900, max_rss_mib=8192,
                 pressure_probe=None, require_monitor=False, scratch_dir=None):
        if not isinstance(argv, (tuple, list)) or not argv or len(argv) > 4 or any(not isinstance(x, str) or not x for x in argv):
            raise ValueError('invalid_trainer_command')
        executable = Path(argv[0]).resolve()
        if not Path(argv[0]).is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError('invalid_trainer_executable')
        if type(max_seconds) is not int or not 1 <= max_seconds <= 3600 or type(max_rss_mib) is not int or not 128 <= max_rss_mib <= 65536:
            raise ValueError('invalid_resource_budget')
        self.argv = tuple(argv); self.max_seconds = max_seconds; self.max_rss_mib = max_rss_mib
        if pressure_probe is not None and not callable(pressure_probe):
            raise ValueError('invalid_pressure_probe')
        self.pressure_probe = pressure_probe
        self.require_monitor = require_monitor
        self.scratch_dir = Path(scratch_dir) if scratch_dir is not None else None
        if self.scratch_dir is not None:
            folder = self.scratch_dir
            if folder.is_symlink() or not folder.is_dir():
                raise ValueError('unsafe_trainer_scratch')
            info = folder.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('unsafe_trainer_scratch')
        self.identity = digest(canonical([self.argv, max_seconds, max_rss_mib]))
        self.last_resources = None
        self.last_exit_code = None
        self.last_error_code = None

    def call(self, mode, request, result_path, *, cwd):
        self.last_resources = None
        self.last_exit_code = None
        self.last_error_code = None
        if mode not in ('train', 'predict', 'smoke', 'segment'): raise ValueError('invalid_trainer_mode')
        request_path = Path(cwd) / (mode+'-request.json')
        _atomic_json(request_path, request)
        result_path = Path(result_path)
        if result_path.exists() or result_path.is_symlink(): raise ValueError('result_exists')
        pending = result_path.with_name(result_path.name+'.pending')
        pending.unlink(missing_ok=True)  # Only this cycle's interrupted output.
        env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TMPDIR') if k in os.environ}
        if self.scratch_dir is not None:
            env['TMPDIR'] = str(self.scratch_dir)
        env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_DATASETS_OFFLINE='1',
                   QMD_LOCAL_TRAINER='1', PYTHONDONTWRITEBYTECODE='1')
        # Avoid inheriting API keys, HOME, proxy settings and project-wide hooks.
        proc = subprocess.Popen([*self.argv, mode, str(request_path), str(pending)],
            cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        started = time.monotonic(); peak_kib = 0
        rss_sum = cpu_sum = peak_cpu = samples = 0
        next_pressure_check = started + 2
        def observed():
            return {'elapsed_seconds': round(time.monotonic()-started, 3),
                    'observed_peak_rss_kib': peak_kib if samples else None,
                    'observed_avg_rss_kib': round(rss_sum / samples, 1) if samples else None,
                    'observed_peak_cpu_percent': round(peak_cpu, 2) if samples else None,
                    'observed_avg_cpu_percent': round(cpu_sum / samples, 2) if samples else None,
                    'resource_samples': samples if samples else None,
                    'rss_monitor': 'sampled-ps-process-group' if samples else 'unavailable'}
        try:
            while proc.poll() is None:
                if time.monotonic()-started > self.max_seconds:
                    raise ValueError('trainer_timeout')
                try:
                    stat_out = subprocess.run(['/bin/ps', '-axo', 'pgid=,%cpu=,rss='],
                        capture_output=True, text=True, timeout=1, check=False)
                    if stat_out.returncode != 0:
                        raise OSError('process_stats_unavailable')
                    group = [parts for line in stat_out.stdout.splitlines()
                             if len(parts := line.split()) == 3 and parts[0] == str(proc.pid)]
                    if group:
                        rss = sum(int(parts[2]) for parts in group)
                        cpu = sum(float(parts[1]) for parts in group)
                        peak_kib = max(peak_kib, rss)
                        peak_cpu = max(peak_cpu, cpu)
                        rss_sum += rss; cpu_sum += cpu; samples += 1
                        if rss > self.max_rss_mib*1024:
                            raise ValueError('trainer_memory_budget')
                except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
                    if isinstance(exc, ValueError) and str(exc) == 'trainer_memory_budget': raise
                    if self.require_monitor:
                        raise ValueError('trainer_resource_monitor_unavailable') from exc
                if self.pressure_probe is not None and time.monotonic() >= next_pressure_check:
                    if not self.pressure_probe()['ready']:
                        raise ValueError('trainer_system_pressure')
                    next_pressure_check = time.monotonic() + 2
                time.sleep(.05)
            if proc.returncode != 0:
                code = 'trainer_process_failed'
                try:
                    failure = read_private_json(_safe_file(pending, max_size=512))
                    if (isinstance(failure, dict) and set(failure) ==
                            {'schema_version', 'error_code'} and failure['schema_version'] == 1
                            and isinstance(failure['error_code'], str) and
                            failure['error_code'] in {
                                'adapter_memory_error', 'adapter_permission_error',
                                'adapter_file_missing', 'adapter_runtime_error',
                                'adapter_unclassified_error',
                                'model_token_budget_exceeded',
                                'tokenizer_model_limit_exceeded',
                                'model_input_view_unsupported',
                                'invalid_complete_input', 'invalid_request_file',
                                'invalid_predict_request',
                                'head_artifact_provenance_mismatch',
                                'missing_artifact', 'invalid_model_max_len',
                                'unexpected_laya_logit_shape',
                                'checkpoint_reload_logits_changed',
                                'invalid_candidate_pool', 'invalid_case_count',
                                'invalid_case', 'invalid_model_input_view',
                                'invalid_edit_segmentation',
                                'edit_tokenizer_unsupported',
                                'invalid_edit_offsets', 'invalid_edit_window',
                                'invalid_edit_overlap', 'edit_segment_limit',
                                'edit_segment_coverage_failed'}):
                        # Tokenizer-only segmentation shares the same bounded
                        # local process protocol as prediction.
                        code = failure['error_code']
                except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                    pass
                self.last_error_code = code
                # Keep the historical generic error for child exits without
                # a validated sidecar; only audited codes gain specificity.
                raise ValueError('trainer_failed' if code == 'trainer_process_failed'
                                 else 'trainer_failed:' + code)
            response = read_private_json(_safe_file(pending, max_size=MAX_FILE))
            os.replace(pending, result_path)
            return response, observed()
        except BaseException:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try: proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL); proc.wait()
                except OSError:
                    pass  # Cleanup must not replace the original trainer error.
            raise
        finally:
            self.last_exit_code = proc.returncode
            self.last_resources = observed()
            try: os.killpg(proc.pid, signal.SIGKILL)
            except OSError: pass
            try: request_path.unlink(missing_ok=True)
            except OSError: pass


class VerifiedLayaTrainer(LocalTrainer):
    """Existing local Laya runtime, checked with real synthetic smoke on first use."""
    def __init__(self, argv, *, proof_cache_dir=None, deadline=None,
                 isolated_temp_dir=None, **kwargs):
        super().__init__(argv, scratch_dir=isolated_temp_dir, **kwargs)
        self.deadline = deadline
        self.isolated_temp_dir = self.scratch_dir
        expected=Path(__file__).with_name('laya_adapter.py').resolve()
        if len(self.argv)!=3 or Path(self.argv[1]).resolve()!=expected or not Path(self.argv[2]).is_absolute():
            raise ValueError('invalid_verified_laya_command')
        from .laya_adapter import model_identity
        self.identity=digest(canonical([self.identity,_hash_bytes(expected),
            model_identity(self.argv[2])]))
        self.proof=None
        self.proof_cache_dir=Path(proof_cache_dir) if proof_cache_dir is not None else None
        if self.proof_cache_dir is not None:
            folder=self.proof_cache_dir
            if folder.is_symlink() or not folder.is_dir():
                raise ValueError('unsafe_laya_proof_cache')
            info=folder.stat()
            if info.st_uid!=os.getuid() or info.st_mode & 0o077:
                raise ValueError('unsafe_laya_proof_cache')

    def call(self, mode, request, result_path, *, cwd):
        if mode not in ('train','predict'):raise ValueError('invalid_verified_laya_mode')
        from .laya_adapter import model_identity
        from .runtime_setup import probe_runtime, verify_compatibility
        if self.deadline is not None:
            left = self.deadline - time.monotonic()
            if left < 1: raise ValueError('trainer_timeout')
            self.max_seconds = min(self.max_seconds, max(1, int(left)))
        adapter_sha=_hash_bytes(self.argv[1])
        model_sha=model_identity(self.argv[2])
        runtime=(probe_runtime(self.argv[0], temp_dir=self.isolated_temp_dir)
                 if self.isolated_temp_dir is not None else probe_runtime(self.argv[0]))
        if runtime.get('status')!='metadata_compatible':
            raise ValueError('laya_runtime_or_source_changed')
        if self.proof is None:
            cached=None
            path=self.proof_cache_dir/'laya-synthetic-attestation.json' if self.proof_cache_dir else None
            if path is not None and path.exists():
                try:
                    cached=read_private_json(path)
                except (OSError,ValueError,json.JSONDecodeError):
                    cached=None
            if (isinstance(cached,dict) and cached.get('schema')=='qmd-laya-live-proof-v1'
                    and type(cached.get('verifiedAt')) in (int,float)
                    and 0 <= time.time()-cached['verifiedAt'] <= 86400
                    and isinstance(cached.get('attestation'),dict)
                    and cached['attestation'].get('adapter_sha256')==adapter_sha
                    and verify_compatibility(runtime,cached['attestation'],model_sha)):
                self.proof={'attestation':cached['attestation']}
            else:
                from .runtime_setup import attest_runtime
                options={'max_seconds':min(120,self.max_seconds),
                         'max_rss_mib':self.max_rss_mib}
                if self.isolated_temp_dir is not None:
                    options['temp_dir']=self.isolated_temp_dir
                proof=attest_runtime(self.argv[0],self.argv[1],self.argv[2],**options)
                if (proof['probe']['runtime_identity_sha256']!=runtime['runtime_identity_sha256']
                        or proof['attestation']['adapter_sha256']!=adapter_sha
                        or not verify_compatibility(runtime,proof['attestation'],model_sha)):
                    raise ValueError('laya_runtime_not_verified')
                self.proof=proof
                if path is not None:
                    _atomic_json(path,{'schema':'qmd-laya-live-proof-v1',
                        'verifiedAt':time.time(),'attestation':proof['attestation']})
        attestation=self.proof['attestation']
        if (adapter_sha!=attestation['adapter_sha256'] or
            not verify_compatibility(runtime,attestation,model_sha)):
            raise ValueError('laya_runtime_or_source_changed')
        if self.deadline is not None:
            left = self.deadline - time.monotonic()
            if left < 1: raise ValueError('trainer_timeout')
            self.max_seconds = min(self.max_seconds, max(1, int(left)))
        return super().call(mode,request,result_path,cwd=cwd)


def _hash_bytes(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):
            h.update(block)
    return h.hexdigest()


def _inputs(cases):
    return [{'request_id': c['request_id'], 'input_sha256': c['input_sha256'],
             'prompt': c['prompt'], 'candidates': [{k: row[k] for k in ('id', 'input_text', 'input_kind', 'revision_sha256')}
                for row in c['candidates']]} for c in cases]


def _score(cases, predictions):
    if not isinstance(predictions, dict) or set(predictions) != {'schema_version', 'predictions'} or predictions['schema_version'] != 1:
        raise ValueError('invalid_predictions')
    records = predictions['predictions']
    if not isinstance(records, list) or len(records) != len(cases): raise ValueError('prediction_membership_mismatch')
    by_id = {}
    for row in records:
        if not isinstance(row, dict) or set(row) != {'request_id', 'input_sha256', 'selected_ids'} or row['request_id'] in by_id:
            raise ValueError('invalid_prediction')
        by_id[row['request_id']] = row
    compared = []; needed = retrieved = irrelevant = 0
    for case in cases:
        row = by_id.pop(case['request_id'], None)
        if row is None or row['input_sha256'] != case['input_sha256']:
            raise ValueError('prediction_input_mismatch')
        selected = row['selected_ids']; universe = {c['id'] for c in case['candidates']}
        if not isinstance(selected, list) or len(selected) > 3 or len(set(selected)) != len(selected) or not set(selected) <= universe:
            raise ValueError('invalid_predicted_selection')
        necessary = {c['id'] for c in case['candidates'] if c['relevance'] == 'necessary'}
        irrelevant_ids = {c['id'] for c in case['candidates'] if c['relevance'] == 'irrelevant'}
        needed += len(necessary); retrieved += len(necessary & set(selected)); irrelevant += len(irrelevant_ids & set(selected))
        compared.append({'gold': case['selected_ids'], 'selected': selected,
                         'family': case['family_hashes'][0] if case['family_hashes'] else case['request_id']})
    if by_id: raise ValueError('unexpected_prediction')
    score = selection_score(compared)
    score.update(necessary_recall=retrieved/needed if needed else 1,
                 irrelevant_injection_rate=irrelevant/len(cases) if cases else 0)
    return score


def _predict(trainer, directory, artifact, cases, label):
    path = directory / (label+'-predictions.json')
    if path.exists():
        prediction = read_private_json(path)
        resources = {'resumed': True}
    else:
        prediction, resources = trainer.call('predict', {'schema_version': 1,
            'artifact_path': str(artifact), 'cases': _inputs(cases)}, path, cwd=directory)
    return _score(cases, prediction), resources


def _stable_queue(state_dir, version_id, revision_reader, expected):
    queue = build_queue(state_dir, version_id, revision_reader())
    if queue['queue_sha256'] != expected['queue_sha256'] or queue['manifest_sha256'] != expected['manifest_sha256']:
        raise ValueError('stale_or_changed_queue')


def run_cycle(state_dir, cycle_id, version_id, revision_reader, trainer, *, incumbent_artifact):
    """One manual cycle; rerunning the same ID resumes or returns prior verdict."""
    if not isinstance(cycle_id, str) or not 1 <= len(cycle_id) <= 80 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in cycle_id):
        raise ValueError('invalid_cycle_id')
    if not callable(revision_reader) or not isinstance(trainer, LocalTrainer): raise ValueError('invalid_cycle_input')
    with database(state_dir): pass  # Validate owner-only root before writing files.
    root = Path(state_dir); cycles = root/'cycles'; cycles.mkdir(mode=0o700, exist_ok=True)
    if cycles.is_symlink() or cycles.stat().st_uid != os.getuid() or cycles.stat().st_mode & 0o077: raise ValueError('unsafe_cycle_directory')
    lock = cycles/'cycle.lock'
    fd = os.open(lock, os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        queue = build_queue(state_dir, version_id, revision_reader())
        splits = queue['splits']; train = splits['train']; validation = splits['validation']; evaluation = splits['evaluation']
        if sum(map(len, splits.values())) > MAX_QUESTIONS: raise ValueError('question_budget_exceeded')
        if len(train) < 100 or len(validation) < 50 or len(evaluation) < 50: raise ValueError('insufficient_separated_data')
        if any(sum(not c['selected_ids'] for c in cases) < 10 for cases in (validation, evaluation)):
            raise ValueError('insufficient_none_cases')
        if any(len({c['family_hashes'][0] for c in cases}) < 5 for cases in (validation, evaluation)):
            raise ValueError('insufficient_families')
        folder = cycles/cycle_id
        folder.mkdir(mode=0o700, exist_ok=True)
        if folder.is_symlink() or folder.stat().st_uid != os.getuid() or folder.stat().st_mode & 0o077: raise ValueError('unsafe_cycle_directory')
        state_path = folder/'state.json'
        state = _load_state(state_path)
        pin = {'queue_sha256': queue['queue_sha256'], 'manifest_sha256': queue['manifest_sha256'],
               'trainer_sha256': trainer.identity}
        if state is None:
            state = {'schema_version': 1, 'cycle_id': cycle_id, 'pin': pin, 'phase': 'queued'}
            _atomic_json(state_path, state)
        elif state.get('pin') != pin: raise ValueError('cycle_pin_conflict')
        elif state.get('phase') in ('promoted', 'rejected'):
            return state
        active_path = root/'active-checkpoint.json'
        active = _load_state(active_path)
        incumbent = _safe_file(active['artifact_path']) if active else _safe_file(incumbent_artifact)
        incumbent_sha = _hash(incumbent)
        if active and incumbent_sha != active['artifact_sha256']: raise ValueError('incumbent_changed')
        if 'incumbent_sha256' in state and state['incumbent_sha256'] != incumbent_sha:
            # Crash after atomic promotion but before final state write.
            if not (active and active.get('cycle_id') == cycle_id and active.get('artifact_sha256') == state.get('artifact_sha256')):
                raise ValueError('incumbent_changed')
            state.update(phase='promoted', decision={'promote': True, 'reason': 'recovered_promoted_pointer'})
            _atomic_json(state_path, state)
            return state
        state['incumbent_sha256'] = incumbent_sha
        _stable_queue(state_dir, version_id, revision_reader, queue)
        artifact = folder/'candidate.artifact'
        if state['phase'] == 'queued':
            # Delete an incomplete artifact left by a killed trainer, then retry locally.
            artifact.unlink(missing_ok=True)
            (folder/'train-result.json').unlink(missing_ok=True)
            response, resources = trainer.call('train', {'schema_version': 1,
                'train': train, 'artifact_path': str(artifact), 'manifest_sha256': queue['manifest_sha256']},
                folder/'train-result.json', cwd=folder)
            if response != {'schema_version': 1, 'status': 'trained'}:
                raise ValueError('invalid_train_result')
            state.update(phase='trained', artifact_sha256=_hash(artifact), train_resources=resources)
            _atomic_json(state_path, state)
        if state['phase'] != 'trained' or _hash(artifact) != state['artifact_sha256']:
            raise ValueError('candidate_changed')
        _stable_queue(state_dir, version_id, revision_reader, queue)
        # Both models see the same unlabeled validation set for promotion.
        challenger_score, challenger_resource = _predict(trainer, folder, artifact, validation, 'challenger-validation')
        incumbent_score, incumbent_resource = _predict(trainer, folder, incumbent, validation, 'incumbent-validation')
        val = challenger_score
        decision = promotion_decision(
            {k: incumbent_score[k] for k in ('exact_rate', 'necessary_recall', 'irrelevant_injection_rate', 'none_exact_rate')},
            {k: challenger_score[k] for k in ('exact_rate', 'necessary_recall', 'irrelevant_injection_rate', 'none_exact_rate')},
            train_questions=len(train), validation_score=val)
        # Final evaluation is reported once, never used for checkpoint choice.
        challenger_eval, challenger_eval_resource = _predict(trainer, folder, artifact, evaluation, 'challenger-evaluation')
        incumbent_eval, incumbent_eval_resource = _predict(trainer, folder, incumbent, evaluation, 'incumbent-evaluation')
        _stable_queue(state_dir, version_id, revision_reader, queue)
        if _load_state(active_path) != active: raise ValueError('active_pointer_changed')
        state.update(phase='promoted' if decision['promote'] else 'rejected', decision=decision,
                     incumbent_score=incumbent_score, challenger_score=challenger_score,
                     independent_evaluation={'challenger': challenger_eval, 'incumbent': incumbent_eval},
                     predict_resources={'challenger_validation': challenger_resource,
                        'incumbent_validation': incumbent_resource,
                        'challenger_evaluation': challenger_eval_resource,
                        'incumbent_evaluation': incumbent_eval_resource})
        if decision['promote']:
            pointer = {'schema_version': 1, 'artifact_path': str(artifact),
                'artifact_sha256': state['artifact_sha256'], 'cycle_id': cycle_id,
                'previous': active if active else {'artifact_path': str(incumbent), 'artifact_sha256': incumbent_sha}}
            _atomic_json(active_path, pointer)
        _atomic_json(state_path, state)
        return state
    finally:
        os.close(fd)


def rollback(state_dir):
    """Restore the prior local pointer; never alter or delete model weights."""
    with database(state_dir): pass
    root = Path(state_dir); active_path = root/'active-checkpoint.json'
    fd = os.open(root/'cycles'/'cycle.lock', os.O_RDWR|os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        active = read_private_json(active_path)
        previous = active.get('previous')
        if not isinstance(previous, dict) or not isinstance(previous.get('artifact_path'), str) or _hash(previous['artifact_path']) != previous.get('artifact_sha256'):
            raise ValueError('unavailable_previous_checkpoint')
        _atomic_json(active_path, {'schema_version': 1, 'artifact_path': previous['artifact_path'],
            'artifact_sha256': previous['artifact_sha256'], 'cycle_id': 'rollback', 'previous': None})
        return {'status': 'rolled_back', 'artifact_sha256': previous['artifact_sha256']}
    finally:
        os.close(fd)
