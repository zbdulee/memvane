"""Keep a project's prepared local selector usable across plugin releases.

A release changes the code digest that prepared inference evidence binds, a
host removes the superseded cache that the policy's adapter anchor names, and
the synthetic attestation counts for 24 hours. Each of those turns local
selection off ("local selection unavailable; serving hybrid rank"). The
background worker heals a prepared selector with the same verified steps an
owner runs by hand, in the order their evidence depends on each other:

1. rebind ``live-selector.json`` ``trainerArgv[1]`` to the running bundle's
   adapter, only when that adapter is an installed official copy (or the
   registered local source the host executes); the interpreter and model
   directory stay as the owner wrote them. A namespace with an accepted
   learning profile is not rebound: the profile's acceptance digest covers
   the policy, so that change needs the owner (``learning_profile_rebind_required``);
2. refresh the synthetic attestation through ``VerifiedLayaTrainer`` (its
   real synthetic checks, at most 120 seconds) when it is missing, expired or
   names another adapter;
3. prepare inference evidence with ``core/prepare_laya_inference.py`` in the
   policy interpreter (at most 120 seconds) when ``admit`` no longer accepts
   the existing evidence.

Nothing is re-signed: every check that admits a prediction is unchanged.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

POLICY = 'live-selector.json'
ATTESTATION = 'laya-synthetic-attestation.json'
PREPARE_SECONDS = 120
# Reasons a recall reports that this heal can clear.
HEALABLE = frozenset(('adapter_anchor_missing', 'unsupported_adapter',
                      'inference_attestation_required'))


class HealRefused(RuntimeError):
    pass


def running_adapter():
    return Path(__file__).with_name('laya_adapter.py').resolve()


def namespace(project, home=None):
    """The project's live-selection state directory (``seam`` naming)."""
    from .contracts import digest
    from .runtime_setup import learning_state_base
    return learning_state_base(home) / 'live-selection' / digest(str(project))


def _official(adapter, home, deadline):
    from .adapter_binding import (_installed_current, _local_binding,
                                  _official_cache)
    try:
        target = _official_cache(adapter, home)
        _installed_current(*target[:3], home, deadline)
        return
    except ValueError:
        pass
    _local_binding(adapter, home, deadline, execution=True)


def _policy(state):
    from .dataset import read_private_json
    policy = read_private_json(state / POLICY)
    if (not isinstance(policy, dict) or set(policy) != {
            'schema', 'trainerArgv', 'timeoutSeconds', 'maxRssMib'}
            or policy['schema'] != 'qmd-live-selector-v1'
            or not isinstance(policy['trainerArgv'], list) or len(policy['trainerArgv']) != 3
            or any(not isinstance(x, str) or not Path(x).is_absolute()
                   for x in policy['trainerArgv'])):
        raise HealRefused('selector_policy_invalid')
    return policy


def rebind(state, *, home=None, deadline=None):
    """Point the policy's adapter anchor at the running official adapter."""
    from runtime_lease import atomic_json
    from .learning_profile import present
    policy = _policy(state)
    adapter = running_adapter()
    argv = policy['trainerArgv']
    if argv[1] == str(adapter):
        return policy, False
    if present(state):
        raise HealRefused('learning_profile_rebind_required')
    try:
        _official(adapter, Path(home) if home is not None else Path.home(), deadline)
    except Exception as exc:
        raise HealRefused('adapter_not_official') from exc
    rebound = {**policy, 'trainerArgv': [argv[0], str(adapter), argv[2]]}
    atomic_json(state / POLICY, rebound)
    return rebound, True


def _view(state):
    from .dataset import read_private_json
    from .laya_adapter import LEGACY_INPUT_VIEW
    active = read_private_json(state / 'active-checkpoint.json')
    return active.get('model_input_view', LEGACY_INPUT_VIEW)


def admitted(state, argv):
    from .inference_identity import admit
    try:
        return admit(state, argv, _view(state)) is not None
    except (OSError, ValueError, KeyError, TypeError):
        return False


def attest(state, policy):
    """The real synthetic attestation, written only when missing or stale."""
    from .local_cycle import VerifiedLayaTrainer
    trainer = VerifiedLayaTrainer(policy['trainerArgv'], proof_cache_dir=state,
                                  max_seconds=PREPARE_SECONDS,
                                  max_rss_mib=policy['maxRssMib'],
                                  deadline=time.monotonic() + PREPARE_SECONDS + 30)
    trainer._prepare('train', cwd=state)


def prepare(state, policy):
    """``prepare_laya_inference.py`` in the policy interpreter (it must be ``sys.executable``)."""
    script = Path(__file__).resolve().parent.parent / 'prepare_laya_inference.py'
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'TMPDIR', 'USER')
           if key in os.environ}
    result = subprocess.run([policy['trainerArgv'][0], '-I', '-B', str(script), str(state),
                             '--max-seconds', str(PREPARE_SECONDS)],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            timeout=PREPARE_SECONDS + 60, env=env, cwd=str(state),
                            check=False)
    if result.returncode != 0:
        tail = (result.stderr or '').strip().splitlines()[-1:] or ['']
        code = tail[0].rsplit(':', 1)[-1].strip()
        raise HealRefused('inference_prepare_failed' if not code.replace('_', '').isalnum()
                          else 'inference_prepare_' + code[:60])
    try:
        if json.loads(result.stdout.strip().splitlines()[-1]).get('status') != 'prepared':
            raise ValueError
    except (ValueError, IndexError, AttributeError) as exc:
        raise HealRefused('inference_prepare_failed') from exc


def heal(project, *, home=None, deadline=None, state=None):
    """Make a prepared selector admit predictions again; a status row."""
    state = Path(state) if state is not None else namespace(project, home)
    if not (state / POLICY).is_file() or not (state / 'active-checkpoint.json').is_file():
        return {'status': 'selector_not_prepared'}
    policy, rebound = rebind(state, home=home, deadline=deadline)
    if admitted(state, policy['trainerArgv']):
        return {'status': 'selector_ready', 'rebound': rebound}
    attest(state, policy)
    prepare(state, policy)
    if not admitted(state, policy['trainerArgv']):
        raise HealRefused('inference_prepare_unverified')
    return {'status': 'selector_prepared', 'rebound': rebound}


def selector_projects(home=None):
    """Projects of every install (either state base) that have a prepared selector."""
    import memvane_paths
    import laya_initial_build as build
    found = []
    for base in (memvane_paths.state_base(home), memvane_paths.legacy_state_base(home)):
        try:
            names = sorted(os.listdir(base))
        except FileNotFoundError:
            continue
        for name in names:
            try:
                record = build.read(base / name)
            except Exception:
                continue
            project = record['identity']['projectRoot'] if record else None
            if (isinstance(project, str) and project not in found and
                    (namespace(project, home) / POLICY).is_file()):
                found.append(project)
    return found
