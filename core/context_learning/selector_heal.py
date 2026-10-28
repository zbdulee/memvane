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
import stat
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


def official_runtime(home=None):
    """(interpreter, model dir or None) of the activated official learning runtime.

    Re-verified the way the installer verifies a generation, before anything
    runs: the activation pointer passes ``runtime_setup._activated``; its
    executable is a prepared generation's own venv interpreter
    (``runtime_installer._generation_executable``: generation layout, private
    directories, interpreter resolving inside the generation, ``prepared.json``
    naming exactly that executable); and the runtime identity the installer
    recorded at staging (``prepared.json``), at activation (``active.json``) and
    in the activation attestation (which must be present) are the same.
    """
    from .dataset import read_private_json
    from .runtime_installer import _generation_executable
    from .runtime_setup import _activated, runtime_base
    root = runtime_base(home) / 'runtimes' / 'laya'
    if not _activated(root):
        raise HealRefused('learning_runtime_not_installed')
    try:
        pointer = read_private_json(root / 'active.json')
        executable = _generation_executable(root, pointer['executable'])
        prepared = read_private_json(executable.parent.parent.parent / 'prepared.json')
        identity = pointer['runtime_identity_sha256']
        attestation = pointer['attestation']
        attested = attestation['runtime_identity_sha256']
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # A pointer without its activation attestation (an older layout) is
        # not proof enough to run anything automatically.
        raise HealRefused('learning_runtime_not_installed') from exc
    if not isinstance(attested, str) or not attested:
        raise HealRefused('learning_runtime_not_installed')
    if prepared['runtime_identity_sha256'] != identity or attested != identity:
        raise HealRefused('learning_runtime_changed')
    model = pointer.get('model_dir')
    return str(executable), model if isinstance(model, str) else None


def _approved_interpreter_hash(state, interpreter):
    """The interpreter's content hash in the last prepared evidence, or None."""
    from .dataset import read_private_json
    from .inference_identity import MANIFEST
    try:
        manifest = read_private_json(Path(state) / MANIFEST)
        entry = manifest.get(os.path.realpath(interpreter))
        return entry[1] if isinstance(entry, list) and isinstance(entry[1], str) else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def check_scope(state, policy, *, home=None):
    """Refuse before anything runs unless the policy is inside its approved scope.

    With an accepted learning profile the acceptance must be intact and still
    name exactly this policy: every ``trainerArgv`` element, the policy digest,
    the model identity, and the interpreter (the activated official runtime,
    whose content hash must equal the one the last prepared evidence recorded).
    Without a profile the interpreter must be the activated official learning
    runtime (and the model its model directory, when the pointer names one).
    """
    from .inference_identity import file_sha
    argv = policy['trainerArgv']
    interpreter, model = official_runtime(home)
    if argv[0] != interpreter or (model is not None and argv[2] != model):
        raise HealRefused('learning_runtime_not_official')
    recorded = _approved_interpreter_hash(state, argv[0])
    if recorded is not None and recorded != file_sha(os.path.realpath(argv[0])):
        raise HealRefused('learning_runtime_changed')
    from .learning_profile import NAME, digest, present
    if not present(state):
        return None
    from .dataset import read_private_json
    from .laya_adapter import model_identity
    from .learning_profile import _hash
    try:
        envelope = read_private_json(Path(state) / NAME)
        row = envelope['proposal']
        intact = envelope == {'acceptedSha256': digest(row), 'proposal': row}
        same = (intact and row.get('trainerArgv') == argv and
                row.get('livePolicySha256') == _hash(Path(state) / POLICY) and
                row.get('modelIdentitySha256') == model_identity(argv[2]) and
                recorded is not None)
    except Exception:
        same = False
    if not same:
        raise HealRefused('learning_profile_rebind_required')
    return row


def rebind(state, *, home=None, deadline=None):
    """Check the approved scope, then point the adapter anchor at the running one.

    Nothing runs before ``check_scope`` passes. A policy under an accepted
    learning profile is never rewritten (its acceptance covers the policy).
    """
    from runtime_lease import atomic_json
    policy = _policy(state)
    adapter = running_adapter()
    argv = policy['trainerArgv']
    if argv[1] != str(adapter):
        from .learning_profile import present
        if present(state):
            raise HealRefused('learning_profile_rebind_required')
        try:
            _official(adapter, Path(home) if home is not None else Path.home(), deadline)
        except Exception as exc:
            raise HealRefused('adapter_not_official') from exc
        candidate = {**policy, 'trainerArgv': [argv[0], str(adapter), argv[2]]}
        check_scope(state, candidate, home=home)
        atomic_json(state / POLICY, candidate)
        return candidate, True
    check_scope(state, policy, home=home)
    return policy, False


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


class HealDeferred(RuntimeError):
    pass


PREPARE_LOCK = 'learning-prepare.lock'


def _prepare_lease(home):
    """The per-user lease for background attest/prepare, or a deferral.

    It lives under the MemVane state base, which a learning-state move never
    renames, so every install's worker contends on the same lock.
    """
    import fcntl
    import memvane_paths
    base = memvane_paths.state_base(home)
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(base / PREPARE_LOCK, os.O_RDWR | os.O_CREAT |
                 getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise HealRefused('learning_prepare_lock_unsafe')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise HealDeferred('learning_prepare_busy') from None
    except BaseException:
        os.close(fd)
        raise
    return fd


def pressure(policy):
    """None when memory admits one model load, else the deferral reason."""
    from .resident import _admission
    limits, _report = _admission(policy['maxRssMib'], already_running=False)
    return None if limits is not None else 'learning_prepare_memory_pressure'


def heal(project, *, home=None, deadline=None, state=None):
    """Make a prepared selector admit predictions again; a status row.

    Order: approved-scope check (nothing executes before it), anchor rebind,
    the cheap ``admit`` gate, then under the per-user prepare lease and only
    without memory pressure: attestation and inference preparation. A held
    lease or pressure defers; a later worker run (a recall that misses local
    selection requests one) retries.
    """
    state = Path(state) if state is not None else namespace(project, home)
    if not (state / POLICY).is_file() or not (state / 'active-checkpoint.json').is_file():
        return {'status': 'selector_not_prepared'}
    policy, rebound = rebind(state, home=home, deadline=deadline)
    if admitted(state, policy['trainerArgv']):
        return {'status': 'selector_ready', 'rebound': rebound}
    lease = _prepare_lease(home)
    try:
        reason = pressure(policy)
        if reason is not None:
            raise HealDeferred(reason)
        attest(state, policy)
        prepare(state, policy)
    finally:
        os.close(lease)
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
