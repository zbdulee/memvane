"""Offline Laya 0.3.20 head-only selection adapter for LocalTrainer.

Executable protocol: python laya_adapter.py MODEL_DIR train|predict|smoke REQUEST RESULT.
MODEL_DIR is an already present local Laya checkpoint. Only the explicit
artifact and response paths may be written; the base checkpoint is read-only.
"""
import hashlib
from contextlib import nullcontext
import json
import os
from pathlib import Path
import random
import stat
import sys
import tempfile
import time

# LocalTrainer executes this file directly, before any request guard runs.
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

ADAPTER_VERSION = 'qmd-laya-selection-v1'
EXPERIMENT_ADAPTER_VERSION = 'qmd-laya-selection-v2'
EXPERIMENT_INPUT_VIEW = 'full-candidate-1024-v1'
EXPERIMENT_INPUT_TOKENS = 1024
EDIT_INPUT_VIEW = 'tool-edit-8192-choice-v1'
EDIT_INPUT_TOKENS = 8192
LEGACY_INPUT_VIEW = 'checkpoint-config-v1'
POLICY = 'binary-needed-p-ge-0.5-top3-v1'
# Prediction keeps every candidate: an over-budget card body is cut to its
# longest fitting leading prefix. Training pairs are never truncated.
PREDICTION_PAIRING = 'leading-body-prefix-v1'
MAX_INPUT_BYTES = 16 * 1024
MAX_REQUEST_BYTES = 16 * 1024 * 1024
QUESTION = {'t': 'noul', 'ins': 'Is this wiki card needed to answer the user request?',
            'crit': {'false': 'not needed', 'true': 'needed'}}
EDIT_QUESTION = {'t': 'choice',
                 'ins': 'Is this wiki card needed to answer the user request?',
                 'crit': {'A': 'not needed', 'B': 'needed'}}
MODEL_FILES = ('model.safetensors', 'rl_agent_config.json', 'encoder/config.json',
               'tokenizer/tokenizer.json', 'tokenizer/tokenizer_config.json')


def _torch():
    from runtime_cpu import environment,configure_torch
    os.environ.update(environment({}))
    import torch
    configure_torch(torch)
    return torch


def _digest(path):
    runtime=sys.modules.get('runtime_admission')
    if runtime is not None:return runtime.hash_file(path)
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def model_identity(model_dir):
    root = Path(model_dir)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError('missing_local_model_dir')
    files = {}
    for name in MODEL_FILES:
        path = root/name
        if not path.is_file(): raise ValueError('missing_local_model_file:'+name)
        files[name] = _digest(path)
    return hashlib.sha256(json.dumps(files, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()


def prepare_base_artifact(model_dir, destination):
    """Create a private base reference without copying or changing base weights."""
    path=Path(destination)
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise ValueError('unsafe_base_artifact_path')
    parent=path.parent
    info=parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077:
        raise ValueError('unsafe_base_artifact_directory')
    identity=model_identity(model_dir)
    _write_json(path,{'schema_version':1,'kind':'laya_base_head',
        'base_model_sha256':identity,'adapter_version':ADAPTER_VERSION})
    return {'status':'prepared','artifact_path':str(path),'base_model_sha256':identity}


def _read_json(path):
    p = Path(path)
    if not p.is_file() or p.stat().st_size > MAX_REQUEST_BYTES:
        raise ValueError('invalid_request_file')
    return json.loads(p.read_text(encoding='utf8'))


def _write_json(path, data):
    payload = (json.dumps(data, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'))+'\n').encode()
    fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(payload); output.flush(); os.fsync(output.fileno())


def _device(torch):
    return torch.device('mps' if torch.backends.mps.is_available() else 'cpu')


def _request_view(request):
    view = request.get('model_input_view', LEGACY_INPUT_VIEW)
    if view not in (LEGACY_INPUT_VIEW, EXPERIMENT_INPUT_VIEW, EDIT_INPUT_VIEW):
        raise ValueError('invalid_model_input_view')
    return view


def _load(model_dir, *, head_artifact=None, input_view=LEGACY_INPUT_VIEW):
    torch=_torch()
    from safetensors.torch import load_file
    from transformers import AutoTokenizer
    from transformers.initialization import no_init_weights
    from laya.common import build_model

    root = Path(model_dir)
    cfg = _read_json(root/'rl_agent_config.json')
    if not isinstance(cfg.get('max_len'), int) or cfg['max_len'] < 128 or cfg['max_len'] > 8192:
        raise ValueError('invalid_model_max_len')
    # Do not call laya.Agent: its tokenizer-repair path may replace a file in
    # the shared Hugging Face snapshot. This checkpoint is already compatible.
    tok = AutoTokenizer.from_pretrained(str(root/'tokenizer'), local_files_only=True)
    with no_init_weights():
        model = build_model(cfg, encoder_dir=str(root/'encoder'), pretrained=False)
    if input_view in (EXPERIMENT_INPUT_VIEW, EDIT_INPUT_VIEW):
        # Keep the older English 1024-view experiment bound to its original
        # shape. Real edit training requires the official multilingual head;
        # accepting the English base here would mislabel a model migration.
        required = (EXPERIMENT_INPUT_TOKENS if input_view == EXPERIMENT_INPUT_VIEW
                    else EDIT_INPUT_TOKENS)
        original_shape = (cfg['max_len'], cfg.get('head_max_len'))
        valid_shapes = ({(512, 192)} if input_view == EXPERIMENT_INPUT_VIEW else
                        {(1024, 256)})
        if (original_shape not in valid_shapes
                or (input_view == EDIT_INPUT_VIEW and
                    cfg.get('encoder') != 'jhu-clsp/mmBERT-base')
                or getattr(model.encoder.config, 'max_position_embeddings', 0)
                < required or tok.model_max_length < required):
            raise ValueError('model_input_view_unsupported')
        cfg = {**cfg, 'max_len': required,
               'model_input_view': input_view}
    elif input_view != LEGACY_INPUT_VIEW:
        raise ValueError('invalid_model_input_view')
    model.load_state_dict(load_file(str(root/'model.safetensors')), strict=True)
    if head_artifact is not None:
        head = load_file(str(head_artifact))
        expected = {k for k in model.state_dict() if not k.startswith('encoder.')}
        if set(head) != expected:
            raise ValueError('head_artifact_keys_mismatch')
        model.load_state_dict(head, strict=False)
    model.to(_device(torch))
    for p in model.encoder.parameters(): p.requires_grad_(False)
    model.eval()
    return model, tok, cfg


def _sequence(prompt, body, tok, cfg, question):
    """The untruncated model input for one pair; raises when it does not fit."""
    from laya.common import build_sequence
    state = 'User request:\n'+prompt+'\nWiki card body:\n'+body
    state_ids = tok(state, add_special_tokens=False)['input_ids']
    ids, markers = build_sequence(tok, state, question,
        max_len=cfg['max_len'], head_max_len=cfg.get('head_max_len', 192),
        state_ids=state_ids)
    instruction_ids=tok(question['t']+' question: '+question['ins'],
        add_special_tokens=False)['input_ids']
    if (ids[1:1+len(instruction_ids)]!=instruction_ids or
        len(markers) != 2 or any(ids[position]!=tok.mask_token_id for position in markers) or
        len(ids) < len(state_ids)+1 or
        ids[-len(state_ids)-1:-1] != state_ids or
        len(ids) > cfg['max_len']):
        raise ValueError('model_token_budget_exceeded')
    if tok.model_max_length < len(ids):
        raise ValueError('tokenizer_model_limit_exceeded')
    return ids, markers


_BUDGET_ERRORS = ('model_token_budget_exceeded', 'tokenizer_model_limit_exceeded')


def _fitting(prompt, body, tok, cfg, question):
    """(ids, markers, kept codepoints) for the longest body prefix that fits.

    Bisection over codepoints. Every probe re-tokenizes the whole state, so the
    boundary is exact for this tokenizer, and a str slice never splits a UTF-8
    sequence. The same input always keeps the same prefix. A request that does
    not fit beside one body character raises its budget error unchanged.
    """
    try:
        return (*_sequence(prompt, body, tok, cfg, question), len(body))
    except ValueError as exc:
        if str(exc) not in _BUDGET_ERRORS: raise
    ids, markers = _sequence(prompt, body[:1], tok, cfg, question)
    lo, hi = 1, len(body)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        try:
            ids, markers = _sequence(prompt, body[:mid], tok, cfg, question)
        except ValueError as exc:
            if str(exc) not in _BUDGET_ERRORS: raise
            hi = mid
        else:
            lo = mid
    return ids, markers, lo


def _pair(prompt, candidate, tok, cfg, *, fit_body=False):
    """One tokenized pair. Training input is never truncated (``fit_body`` off).

    Prediction (``fit_body``) follows ``PREDICTION_PAIRING``: the whole user
    request and the longest leading prefix of the card body (its title first)
    that fits the model budget.
    """
    from laya.common import render_options, QTYPES

    body = candidate.get('input_text')
    if (not isinstance(prompt, str) or not prompt.strip() or
        not isinstance(body, str) or not body.strip() or
        len(body.encode('utf8')) > MAX_INPUT_BYTES or
        candidate.get('input_kind') not in ('full_compact_wiki_body', 'legacy_complete_excerpt')):
        raise ValueError('invalid_complete_input')
    state = 'User request:\n'+prompt+'\nWiki card body:\n'+body
    mask = tok.mask_token
    if mask in state: raise ValueError('mask_token_in_source')
    question = (EDIT_QUESTION if cfg.get('model_input_view') == EDIT_INPUT_VIEW
                else QUESTION)
    for option in render_options(question):
        if len(tok(' '+option, add_special_tokens=False)['input_ids']) > 48:
            raise ValueError('option_token_truncation')
    if not fit_body:
        ids, markers = _sequence(prompt, body, tok, cfg, question)
        return {'ids':ids, 'markers':markers, 'qtype':QTYPES[question['t']]}
    ids, markers, kept = _fitting(prompt, body, tok, cfg, question)
    return {'ids':ids, 'markers':markers, 'qtype':QTYPES[question['t']],
            'truncated': kept < len(body)}


def _pairs(cases, tok, cfg, *, with_gold, cancelled=None):
    if not isinstance(cases, list) or not cases or len(cases) > 2000:
        raise ValueError('invalid_case_count')
    result = []
    for case in cases:
        if cancelled is not None and cancelled():
            raise ValueError('prediction_cancelled')
        if not isinstance(case, dict) or not isinstance(case.get('candidates'), list):
            raise ValueError('invalid_case')
        candidates = case['candidates']
        if not 1 <= len(candidates) <= 30 or len({c.get('id') for c in candidates}) != len(candidates):
            raise ValueError('invalid_candidate_pool')
        if with_gold:
            selected = case.get('selected_ids')
            if not isinstance(selected, list) or not set(selected) <= {c['id'] for c in candidates}:
                raise ValueError('invalid_train_selection')
        elif 'selected_ids' in case:
            raise ValueError('heldout_gold_exposed')
        for candidate in candidates:
            if cancelled is not None and cancelled():
                raise ValueError('prediction_cancelled')
            item = _pair(case.get('prompt'), candidate, tok, cfg, fit_body=not with_gold)
            item.update(request_id=case['request_id'], candidate_id=candidate['id'])
            if with_gold: item['gold_label'] = int(candidate['id'] in selected)
            result.append(item)
    return result


def _batch(items, tok, device):
    from laya.common import collate_items
    batch = collate_items([[item] for item in items], tok.pad_token_id)
    return {name: value.to(device) for name, value in batch.items()
            if name in ('input_ids','attention_mask','marker_pos','marker_mask','qtype')}


def select_ids(scores):
    """Stable 0-3 mapping; score ties preserve candidate pool order."""
    if any(not isinstance(p, (float, int)) or not 0 <= p <= 1 for _, p in scores):
        raise ValueError('invalid_probability')
    ranked = sorted(enumerate(scores), key=lambda row:(-row[1][1], row[0]))
    return [candidate_id for _, (candidate_id, p) in ranked if p >= .5][:3]


def _save_head(path, model, base_sha, input_view):
    from safetensors.torch import save_file
    tensors = {k:v.detach().cpu().contiguous() for k,v in model.state_dict().items()
               if not k.startswith('encoder.')}
    metadata = {'adapter_version':ADAPTER_VERSION,
        'selection_policy':POLICY, 'base_model_sha256':base_sha,
        'kind':'head_only'}
    if input_view in (EXPERIMENT_INPUT_VIEW, EDIT_INPUT_VIEW):
        metadata.update(adapter_version=EXPERIMENT_ADAPTER_VERSION,
                        model_input_view=EXPERIMENT_INPUT_VIEW)
        metadata['model_input_view'] = input_view
    save_file(tensors, str(path), metadata=metadata)
    os.chmod(path, 0o600)


def _head_path(artifact, base_sha, input_view):
    from safetensors import safe_open
    path = Path(artifact)
    if not path.is_file() or path.is_symlink(): raise ValueError('missing_artifact')
    with path.open('rb') as source:
        prefix = source.read(1)
    if prefix == b'{':
        manifest = _read_json(path)
        if manifest != {'schema_version':1,'kind':'laya_base_head',
                        'base_model_sha256':base_sha,'adapter_version':ADAPTER_VERSION}:
            raise ValueError('invalid_base_manifest')
        return None
    with safe_open(str(path), framework='pt', device='cpu') as source:
        meta = source.metadata()
    expected = {'adapter_version':ADAPTER_VERSION,'selection_policy':POLICY,
                'base_model_sha256':base_sha,'kind':'head_only'}
    if input_view in (EXPERIMENT_INPUT_VIEW, EDIT_INPUT_VIEW):
        expected.update(adapter_version=EXPERIMENT_ADAPTER_VERSION,
                        model_input_view=input_view)
    if meta != expected:
        raise ValueError('head_artifact_provenance_mismatch')
    return path


def _training_limits(request, model_sha, device):
    value = request.get('training_limits')
    if value is None:
        return None
    keys = {'microbatchSize', 'effectiveBatchSize', 'device',
            'modelIdentitySha256', 'reserveFraction'}
    if (not isinstance(value, dict) or set(value) != keys
            or type(value['microbatchSize']) is not int
            or value['microbatchSize'] not in (1, 2)
            or type(value['effectiveBatchSize']) is not int
            or value['effectiveBatchSize'] != 2
            or type(value['reserveFraction']) not in (int, float)
            or not .10 <= value['reserveFraction'] <= .50):
        raise ValueError('invalid_training_limits')
    if value['modelIdentitySha256'] != model_sha or value['device'] != str(device):
        raise ValueError('training_memory_profile_changed')
    return value


def _check_mps_budget(torch, limits):
    if limits is None or limits['device'] != 'mps':
        return
    from resource_controls import MpsMemoryControl
    MpsMemoryControl.check(torch,limits)


def _train_group(torch, F, model, tok, params, optimizer, current, microbatch,
                 *, limits,checkpoint=None):
    """One effective group/update; all microbatch means weighted by pair count."""
    optimizer.zero_grad(set_to_none=True)
    total_loss = 0.0
    for start in range(0, len(current), microbatch):
        if checkpoint is not None:checkpoint()
        group = current[start:start+microbatch]
        _check_mps_budget(torch, limits)
        batch = _batch(group, tok, params[0].device)
        targets = torch.tensor([x['gold_label'] for x in group],
            device=params[0].device, dtype=torch.long)
        logits, _ = model(**batch)
        if logits.shape != (len(group), 2):
            raise ValueError('unexpected_laya_logit_shape')
        loss = F.cross_entropy(logits.float(), targets)
        if not torch.isfinite(loss):
            raise ValueError('nonfinite_training_loss')
        weight = len(group) / len(current)
        (loss * weight).backward()
        total_loss += float(loss.detach().cpu().item()) * weight
        if not all(p.grad is None or torch.isfinite(p.grad).all().item() for p in params):
            raise ValueError('nonfinite_head_gradient')
        _check_mps_budget(torch, limits)
    if checkpoint is not None:checkpoint()
    optimizer.step()
    _check_mps_budget(torch, limits)
    return total_loss


def _allocation(torch):
    from context_learning.inference_service import _rss_mib
    rss=_rss_mib()
    driver=(torch.mps.driver_allocated_memory()/(1024*1024)
            if str(_device(torch))=='mps' else 0)
    return rss,driver


def _training_guard(request):
    control=request.get('learning_control')
    if control is None:return None
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from context_learning.resource_coordination import TrainingGuard
    return TrainingGuard(control)


def train(model_dir, request, *, max_updates=None, verification=False):
    from context_learning.bounded_calibration import validate_request
    validate_request(request)
    guard = _training_guard(request)
    torch=_torch()
    import torch.nn.functional as F

    if request.get('schema_version') != 1 or not isinstance(request.get('artifact_path'), str):
        raise ValueError('invalid_train_request')
    record_step_losses = request.get('record_step_losses', False)
    if type(record_step_losses) is not bool:
        raise ValueError('invalid_train_request')
    artifact = Path(request['artifact_path'])
    if not artifact.is_absolute() or artifact.exists(): raise ValueError('unsafe_artifact_path')
    input_view = _request_view(request)
    base_sha = model_identity(model_dir)
    limits = _training_limits(request, base_sha, _device(torch))
    before=_allocation(torch) if guard is not None else None
    with guard.step() if guard is not None else nullcontext():
        model, tok, cfg = _load(model_dir, input_view=input_view)
    if guard is not None:guard.mark_loaded(before,_allocation(torch))
    _check_mps_budget(torch, limits)
    items = _pairs(request.get('train'), tok, cfg, with_gold=True)
    torch.manual_seed(17)
    random.Random(17).shuffle(items)
    params = [p for p in model.parameters() if p.requires_grad]
    if not params: raise ValueError('no_trainable_head')
    witness = model.scorer[-1].weight.detach().clone()
    optimizer = torch.optim.SGD(params, lr=0.01)
    model.train(); model.encoder.eval()
    updates = 0
    step_losses = [] if record_step_losses else None
    train_groups = (prediction_batches(items, max_padded_tokens=cfg['max_len'],
                                       max_size=2)
                    if input_view == EDIT_INPUT_VIEW else
                    [items[start:start+2] for start in range(0, len(items), 2)])
    if guard is not None:
        from context_learning.training_cursor import UpdateCursor, run_updates
        cursor=UpdateCursor(request,train_groups)
        if cursor.saved is not None:
            from safetensors.torch import load_file
            head=_head_path(cursor.head(),base_sha,input_view)
            with guard.step():model.load_state_dict(load_file(str(head)),strict=False)
            saved=cursor.saved['rng']
            torch.set_rng_state(torch.tensor(saved['cpu'],dtype=torch.uint8))
            if str(_device(torch))=='mps':
                if saved['mps'] is None:raise ValueError('learning_cursor_changed')
                torch.mps.set_rng_state(torch.tensor(saved['mps'],dtype=torch.uint8))
        if optimizer.state:raise ValueError('learning_optimizer_unsupported')
        def rng():
            return {'cpu':torch.get_rng_state().tolist(),
                    'mps':torch.mps.get_rng_state().tolist() if str(_device(torch))=='mps' else None}
        def update(group,size):
            return _train_group(torch,F,model,tok,params,optimizer,group,
                min(size,limits['microbatchSize']) if limits is not None else size,limits=limits,checkpoint=guard.poll)
        run_updates(cursor,train_groups,guard,update,
            lambda path:_save_head(path,model,base_sha,input_view),rng)
        updates=cursor.step
    else:
        for current in train_groups:
            step_started = time.perf_counter() if record_step_losses else None
            loss_value = _train_group(torch, F, model, tok, params, optimizer,
                current, limits['microbatchSize'] if limits is not None else 2,
                limits=limits)
            updates += 1
            if step_losses is not None:
                step_losses.append({'step': updates, 'pairs': len(current),
                                    'cross_entropy': loss_value,
                                    'wall_seconds': round(time.perf_counter() - step_started, 6)})
            if max_updates is not None and updates >= max_updates: break
    if not updates: raise ValueError('empty_train')
    if torch.equal(witness, model.scorer[-1].weight.detach()):
        raise ValueError('head_weights_unchanged')
    model.eval()
    probe = None
    if verification:
        item = items[0]
        with torch.no_grad():
            batch = _batch([item], tok, next(model.parameters()).device)
            probe_logits, _ = model(**batch)
        probe = {'candidate_id':item['candidate_id'],
                 'logits':probe_logits[0].detach().cpu().tolist()}
    if guard is not None:
        from context_learning.training_cursor import recover_completed
        with guard.step():
            if not recover_completed(request):raise ValueError('learning_cursor_changed')
    else:_save_head(artifact, model, base_sha, input_view)
    response = {'schema_version':1,'status':'trained'}
    if step_losses is not None:
        response['step_losses'] = step_losses
    return (response, probe) if verification else response


def prediction_batches(items, *, max_padded_tokens=8192, max_size=4):
    if (not isinstance(items, list) or not 1 <= max_size <= 4
            or not 128 <= max_padded_tokens <= 8192):
        raise ValueError('invalid_predict_request')
    # Bound the padded tokens in flight, rather than batching an arbitrary
    # number of long edit windows. A four-card microbatch is safe for the
    # measured local 8192-position encoder; a near-limit pair runs alone.
    batches = []
    current = []
    longest = 0
    for item in items:
        length = len(item['ids'])
        if not 1 <= length <= max_padded_tokens:
            raise ValueError('model_token_budget_exceeded')
        proposed_longest = max(longest, length)
        if current and (len(current) >= max_size or
                        proposed_longest * (len(current) + 1) > max_padded_tokens):
            batches.append(current)
            current = []
            longest = 0
        current.append(item)
        longest = max(longest, length)
    if current:
        batches.append(current)
    return batches


def predict(model_dir, request):
    guard=_training_guard(request)
    if request.get('schema_version') != 1 or not isinstance(request.get('artifact_path'), str):
        raise ValueError('invalid_predict_request')
    input_view = _request_view(request)
    base_sha = model_identity(model_dir)
    head = _head_path(request['artifact_path'], base_sha, input_view)
    limits = None
    if request.get('training_limits') is not None:
        torch=_torch()
        limits = _training_limits(request, base_sha, _device(torch))
    if guard is not None:
        torch=_torch()
        before=_allocation(torch)
    with guard.step() if guard is not None else nullcontext():
        model, tok, cfg = _load(model_dir, head_artifact=head, input_view=input_view)
    if guard is not None:guard.mark_loaded(before,_allocation(torch))
    return predict_loaded(model, tok, cfg, request, memory_limits=limits,compute_guard=guard)


def predict_loaded(model, tok, cfg, request, *, max_batch_size=4,
                   max_padded_tokens=None, timing=None, cancelled=None,
                   memory_limits=None,compute_guard=None):
    """Use an already loaded, pinned head for a bounded selection batch."""
    if cancelled is not None and cancelled():
        raise ValueError('prediction_cancelled')
    torch=_torch()
    if memory_limits is not None:
        max_batch_size = min(max_batch_size, memory_limits['microbatchSize'])
        _check_mps_budget(torch, memory_limits)
    if (request.get('schema_version') != 1
            or _request_view(request) != cfg.get('model_input_view', LEGACY_INPUT_VIEW)):
        raise ValueError('invalid_predict_request')
    if cancelled is not None and cancelled():
        raise ValueError('prediction_cancelled')
    began = time.perf_counter()
    if timing is not None:
        timing.update(tokenize_seconds=None, batch_prepare_seconds=None,
                      forward_seconds=None,
                      total_seconds=None, batches=0, pairs=0)
    try:
        items = _pairs(request.get('cases'), tok, cfg, with_gold=False,
                       cancelled=cancelled)
    finally:
        if timing is not None:
            timing['tokenize_seconds'] = round(time.perf_counter() - began, 6)
    if timing is not None:
        timing['truncated_pairs'] = sum(1 for item in items if item.get('truncated'))
    if cancelled is not None and cancelled():
        raise ValueError('prediction_cancelled')
    by_request = {}
    batches = prediction_batches(items,
        max_padded_tokens=min(cfg['max_len'], max_padded_tokens or cfg['max_len']),
        max_size=max_batch_size)
    forward_seconds = 0.0
    batch_prepare_seconds = 0.0
    if timing is not None:
        timing.update(planned_pairs=len(items), planned_batches=len(batches),
                      pairs=0, batches=0, batch_prepare_seconds=0.0,
                      forward_seconds=0.0)
    with torch.no_grad():
        for group in batches:
            with compute_guard.step() if compute_guard is not None else nullcontext():
                _check_mps_budget(torch, memory_limits)
                if cancelled is not None and cancelled():
                    raise ValueError('prediction_cancelled')
                preparing = time.perf_counter()
                try:
                    batch = _batch(group, tok, next(model.parameters()).device)
                finally:
                    batch_prepare_seconds += time.perf_counter() - preparing
                    if timing is not None:
                        timing['batch_prepare_seconds'] = round(batch_prepare_seconds, 6)
                started = time.perf_counter()
                try:
                    logits, _ = model(**batch)
                    if logits.shape != (len(group), 2):
                        raise ValueError('unexpected_laya_logit_shape')
                    # .tolist() synchronizes MPS results. Timing only model(...)
                    # understated the actual scoring cost of every microbatch.
                    probabilities = torch.softmax(logits.float(), -1)[:, 1].tolist()
                    _check_mps_budget(torch, memory_limits)
                finally:
                    forward_seconds += time.perf_counter() - started
                    if timing is not None:
                        timing['forward_seconds'] = round(forward_seconds, 6)
                if timing is not None:
                    timing['pairs'] += len(group)
                    timing['batches'] += 1
                for item, probability in zip(group, probabilities):
                    by_request.setdefault(item['request_id'], []).append(
                        (item['candidate_id'], probability))
                if cancelled is not None and cancelled():
                    raise ValueError('prediction_cancelled')
    predictions = []
    for case in request['cases']:
        predictions.append({'request_id':case['request_id'],
            'input_sha256':case['input_sha256'],
            'selected_ids':select_ids(by_request[case['request_id']])})
    if timing is not None:
        timing.update(total_seconds=round(time.perf_counter() - began, 6),
                      max_batch_size=max_batch_size,
                      max_padded_tokens=max_padded_tokens or cfg['max_len'],
                      device=str(next(model.parameters()).device))
    return {'schema_version':1,'predictions':predictions}


def smoke(model_dir, directory):
    """Actual local synthetic tokenizer, forward, one update, save/reload proof."""
    _torch()
    from safetensors.torch import load_file
    from transformers import AutoTokenizer
    root = Path(model_dir)
    tokenizer = AutoTokenizer.from_pretrained(str(root/'tokenizer'),
                                              local_files_only=True)
    checkpoint = _read_json(root/'rl_agent_config.json')
    encoder = _read_json(root/'encoder'/'config.json')
    long_view = (checkpoint.get('max_len') == 512 and
                 checkpoint.get('head_max_len', 192) == 192 and
                 encoder.get('max_position_embeddings', 0) >= EXPERIMENT_INPUT_TOKENS and
                 tokenizer.model_max_length >= EXPERIMENT_INPUT_TOKENS)
    input_view = EXPERIMENT_INPUT_VIEW if long_view else LEGACY_INPUT_VIEW
    def body_with_tokens(sentence, count):
        seed = tokenizer(sentence * 85, add_special_tokens=False)['input_ids']
        text = tokenizer.decode(seed[:count], skip_special_tokens=True,
                                clean_up_tokenization_spaces=False)
        if len(tokenizer(text, add_special_tokens=False)['input_ids']) != count:
            raise ValueError('synthetic_long_body_roundtrip_changed')
        return text
    sample = {'request_id':'synthetic-1','input_sha256':'0'*64,
        'prompt':'Which synthetic town has the blue bridge?',
        'candidates':[{'id':'card-blue','input_kind':'full_compact_wiki_body',
                       'input_text':body_with_tokens(
                           'Synthetic town Arin has a blue bridge. ', 527 if long_view else 25)},
                      {'id':'card-red','input_kind':'full_compact_wiki_body',
                       'input_text':body_with_tokens(
                           'Synthetic town Bori has a red bridge. ', 735 if long_view else 25)}],
        'selected_ids':['card-blue']}
    base_sha = model_identity(model_dir)
    with tempfile.TemporaryDirectory(prefix='qmd-laya-smoke-',dir=directory) as temp:
        artifact = Path(temp)/'head.artifact'
        _, probe = train(model_dir, {'schema_version':1,
            'model_input_view':input_view,
            'artifact_path':str(artifact),'train':[sample]},
            max_updates=1, verification=True)
        head = load_file(str(artifact))
        if not head: raise ValueError('empty_head_artifact')
        model, tok, cfg = _load(model_dir, head_artifact=artifact,
                                input_view=input_view)
        matched = next(c for c in sample['candidates'] if c['id']==probe['candidate_id'])
        item = _pair(sample['prompt'], matched, tok, cfg)
        if long_view and not 512 < len(item['ids']) <= EXPERIMENT_INPUT_TOKENS:
            raise ValueError('synthetic_long_view_not_exercised')
        torch=_torch()
        too_long = dict(matched, input_text='synthetic overflow ' * 700)
        try:
            _pair(sample['prompt'], too_long, tok, cfg)
        except ValueError as exc:
            if str(exc) != 'model_token_budget_exceeded': raise
        else:
            raise ValueError('silent_model_token_truncation')
        with torch.no_grad():
            batch = _batch([item], tok, next(model.parameters()).device)
            reloaded_logits, _ = model(**batch)
        if not torch.allclose(reloaded_logits[0].cpu(),
                              torch.tensor(probe['logits']), rtol=1e-4, atol=1e-5):
            raise ValueError('checkpoint_reload_logits_changed')
        del model
        unlabeled = {k:v for k,v in sample.items() if k != 'selected_ids'}
        base = Path(temp)/'base.artifact'
        _write_json(base, {'schema_version':1,'kind':'laya_base_head',
            'base_model_sha256':base_sha,'adapter_version':ADAPTER_VERSION})
        base_prediction = predict(model_dir, {'schema_version':1,
            'model_input_view':input_view,
            'artifact_path':str(base),'cases':[unlabeled]})
        trained_prediction = predict(model_dir, {'schema_version':1,
            'model_input_view':input_view,
            'artifact_path':str(artifact),'cases':[unlabeled]})
        assert select_ids([('a',.9),('b',.8),('c',.7),('d',.6)]) == ['a','b','c']
        return {'schema_version':1,'base_model_sha256':base_sha,
            'checks':{'tokenization_no_truncation':True,'synthetic_inference':True,
                      'synthetic_one_step_train':True,'checkpoint_reload':True,
                      'fixed_selection_mapping':True},
            'base_selected_ids':base_prediction['predictions'][0]['selected_ids'],
            'trained_selected_ids':trained_prediction['predictions'][0]['selected_ids'],
            'reloaded_logits':reloaded_logits[0].cpu().tolist(),
            'synthetic_body_bytes':len(matched['input_text'].encode()),
            'model_input_tokens':len(item['ids']),
            'model_input_view':input_view,
            'head_bytes':artifact.stat().st_size,'device':str(_device(__import__('torch')))}


def segment_edit(model_dir, request):
    from runtime_cpu import environment
    os.environ.update(environment({}))
    from transformers import AutoTokenizer
    from edit_segments import split_with_offsets
    if (not isinstance(request, dict) or set(request) !=
            {'schema_version', 'text', 'target_tokens', 'overlap_tokens'}
            or request['schema_version'] != 1 or not isinstance(request['text'], str)
            or len(request['text'].encode()) > 65536):
        raise ValueError('invalid_edit_segmentation')
    tokenizer = AutoTokenizer.from_pretrained(str(Path(model_dir)/'tokenizer'),
                                              local_files_only=True, use_fast=True)
    if not tokenizer.is_fast or tokenizer.model_max_length < 8192:
        raise ValueError('edit_tokenizer_unsupported')
    encoded = tokenizer(request['text'], add_special_tokens=False,
                        return_offsets_mapping=True)
    segments = split_with_offsets(request['text'],
        encoded['offset_mapping'], target_tokens=request['target_tokens'],
        overlap_tokens=request['overlap_tokens'])
    return {'schema_version': 1, 'segments': segments,
            'source_sha256': hashlib.sha256(request['text'].encode()).hexdigest()}


def main(argv):
    if len(argv) != 4: raise ValueError('usage: MODEL_DIR MODE REQUEST RESULT')
    from runtime_cpu import environment
    os.environ.update(environment({}))
    os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',
        HF_DATASETS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',
        USE_TF='0',USE_TORCH='1')
    model_dir, mode, request_path, response_path = argv
    if mode == 'train': result = train(model_dir, _read_json(request_path))
    elif mode == 'predict': result = predict(model_dir, _read_json(request_path))
    elif mode == 'smoke': result = smoke(model_dir, Path(request_path).parent)
    elif mode == 'segment': result = segment_edit(model_dir, _read_json(request_path))
    else: raise ValueError('invalid_adapter_mode')
    _write_json(response_path, result)


_SAFE_ERRORS = {
    'cpu_thread_configuration_invalid','cpu_thread_configuration_unavailable',
    'learning_yield','learning_control_invalid','learning_cursor_changed',
    'learning_update_budget_exceeded','learning_lease_missing','learning_lease_changed',
    'model_token_budget_exceeded', 'tokenizer_model_limit_exceeded',
    'model_input_view_unsupported', 'invalid_complete_input',
    'invalid_request_file', 'invalid_predict_request',
    'head_artifact_provenance_mismatch', 'missing_artifact',
    'invalid_model_max_len', 'unexpected_laya_logit_shape',
    'checkpoint_reload_logits_changed', 'invalid_candidate_pool',
    'invalid_case_count', 'invalid_case', 'invalid_model_input_view',
    'invalid_edit_segmentation', 'edit_tokenizer_unsupported',
    'invalid_edit_offsets', 'invalid_edit_window', 'invalid_edit_overlap',
    'edit_segment_limit', 'edit_segment_coverage_failed',
    'invalid_training_limits', 'training_memory_profile_changed',
    'training_memory_unavailable', 'training_memory_budget',
}


def _safe_error(exc):
    """Never copy source text, a traceback, or a local path into diagnostics."""
    if isinstance(exc, MemoryError):
        return 'adapter_memory_error'
    if isinstance(exc, PermissionError):
        return 'adapter_permission_error'
    if isinstance(exc, FileNotFoundError):
        return 'adapter_file_missing'
    if isinstance(exc, RuntimeError):
        return 'adapter_runtime_error'
    if isinstance(exc, ValueError) and str(exc) in _SAFE_ERRORS:
        return str(exc)
    return 'adapter_unclassified_error'


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except BaseException as exc:
        if len(sys.argv) == 5:
            try:
                _write_json(sys.argv[4], {'schema_version': 1,
                          'error_code': _safe_error(exc)})
            except (OSError, ValueError):
                pass
        raise SystemExit(1)
