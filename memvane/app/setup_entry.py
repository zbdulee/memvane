#!/usr/bin/env python3
"""Deterministic, explicit front door for memvane-db index setup and maintenance.

No host installation event calls this file. Status and plan actions are read
only; every action that writes index, runtime or learning state requires its
explicit approval flag. The ``onboarding-*`` actions record only the user's
answer to the SessionStart onboarding notice.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

# Direct skill/CLI setup calls do not pass through the hook shell environment.
sys.dont_write_bytecode = True


def learning_plan(args):
    from memvane.learning.api import learning_profile_digest, propose_learning_profile
    from memvane.learning.types import LEARNING_PROFILE_INSTALL
    if args.learning_choice=='preserve' or not args.learning_namespace:
        raise ValueError('explicit_learning_profile_and_namespace_required')
    root=Path(args.learning_namespace)
    # Exact accepted retry uses its original frozen proposal, never retimes it.
    old=root/LEARNING_PROFILE_INSTALL
    row=None
    if args.accepted_learning_profile and (old.exists() or old.is_symlink()):
        from memvane.learning.api import read_private_json
        previous=read_private_json(old)
        if previous['acceptedSha256']==args.accepted_learning_profile:
            row=previous['proposal']
    if row is None:
        row=propose_learning_profile(Path(args.project),root,choice=args.learning_choice,
            mode=args.learning_migration,expires_at_unix_ms=args.learning_expires_at_unix_ms)
    return {'status':'learning_profile_proposed','proposal':row,'proposalSha256':learning_profile_digest(row),'changesApplied':0}


def select_learning(args):
    from memvane.learning.api import activate_learning_profile
    if not args.accepted_learning_profile:raise ValueError('profile_acceptance_required')
    return activate_learning_profile(Path(args.project),Path(args.learning_namespace),
        accepted_sha256=args.accepted_learning_profile,choice=args.learning_choice,
        mode=args.learning_migration,expires_at_unix_ms=args.learning_expires_at_unix_ms)


def _learning_command(argv):
    """The result a ``learning_main`` setup subcommand prints; its failure raises with the reason."""
    import contextlib
    import io
    from memvane.learning.api import learning_main
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        learning_main(argv)
    result = json.loads(out.getvalue())
    if not isinstance(result, dict):
        raise ValueError('learning_command_failed')
    if result.get('status') == 'failed':
        raise ValueError(result.get('reason') or 'learning_command_failed')
    return result


def _exception_detail(exc):
    """Structured evidence (e.g. unexpected installed files) from the cause chain."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if getattr(exc, 'detail', None) is not None:
            return exc.detail
        exc = exc.__cause__ or exc.__context__
    return None


def _failure(exc):
    """The fixed result of a setup action that raised."""
    import subprocess
    from memvane.kernel.resource_deferred import ResourceDeferred
    if isinstance(exc, ResourceDeferred):
        return {'status': 'deferred', 'reason': str(exc), 'changesApplied': 0}
    if (isinstance(exc, subprocess.TimeoutExpired)
            or (isinstance(exc, RuntimeError) and str(exc) == 'host_inventory_unavailable')):
        # A host plugin listing CLI past its bound: a fixed retryable code,
        # never the command line or a traceback.
        return {'status': 'deferred', 'reason': 'host_inventory_unavailable', 'changesApplied': 0}
    result = {'status': 'rejected', 'reason': str(exc), 'changesApplied': 0}
    detail = _exception_detail(exc)
    if detail is not None:
        result['detail'] = detail
    return result


def _cleanup_preview(everything):
    """``(result, digest, names)`` of what a cleanup would remove now; writes nothing."""
    import hashlib
    from memvane.install import api as product
    result = product.state_cleanup(everything=everything)
    if everything:
        names = {'roots': sorted(row['path'] for row in result['roots'] if row['action'] == 'remove')}
    else:
        try:
            from memvane.provider import api as laya_embed_runtime
            runtime = laya_embed_runtime.collect(approve=False)
            runtime.pop('dependencyClosure', None)
        except Exception as exc:  # No runtime on this machine: nothing of it to collect.
            runtime = {'status': 'unavailable', 'reason': str(exc), 'changesApplied': 0}
        result['indexRuntime'] = runtime
        names = {'installs': sorted(row['install'] for row in result['installs'] if row['action'] == 'remove'),
                 'generations': sorted(row['generation'] for row in runtime.get('generations', ())
                                       if row.get('action') in ('remove', 'remove_stale_staging'))}
    digest = hashlib.sha256(json.dumps(names, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    result['previewSha256'] = digest
    return result, digest, names


def _state_cleanup(argv):
    """``state-cleanup``: list what a cleanup would remove; remove it only when that list is approved.

    Default scope: released install state roots and unused embedding runtime
    generations. ``--all-state`` is the explicit "delete everything" request.
    The preview prints ``previewSha256``; ``--approve-state-cleanup <that
    value>`` removes exactly the previewed list and is refused when the list
    has changed since.
    """
    parser = argparse.ArgumentParser(prog='setup.py state-cleanup')
    parser.add_argument('--approve-state-cleanup', metavar='PREVIEW_SHA256')
    parser.add_argument('--all-state', action='store_true')
    args = parser.parse_args(argv)
    approved = args.approve_state_cleanup
    try:
        result, digest, names = _cleanup_preview(args.all_state)
        if approved is not None and approved != digest:
            # The owner approved another list: nothing is removed, the current one is shown again.
            result.update(status='rejected', reason='state_cleanup_preview_changed')
        elif approved is not None:
            from memvane.kernel.memvane_execution_guard import require_cached_plugin_for_live_write
            require_cached_plugin_for_live_write()
            from memvane.install import api as product
            if args.all_state:
                result = product.state_cleanup(approve=True, everything=True, only=set(names['roots']))
            else:
                result = product.state_cleanup(approve=True, only=set(names['installs']))
                from memvane.provider import api as laya_embed_runtime
                runtime = (laya_embed_runtime.collect(approve=True) if names['generations']
                           else {'status': 'collected', 'changesApplied': 0})
                runtime.pop('dependencyClosure', None)
                result['indexRuntime'] = runtime
                result['changesApplied'] += runtime.get('changesApplied', 0)
                # Either half left something behind: the whole command says so.
                if runtime.get('status') in ('blocked', 'partial') and result['status'] == 'collected':
                    result['status'] = 'partial'
    except Exception as exc:
        result = _failure(exc)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 1 if result.get('status') in ('rejected', 'blocked', 'partial') else 0


def _restore_previous_model(argv):
    """``restore-previous-model --project P [--plan]``: go back to the model the newest application replaced.

    Explicit request only. ``--plan`` shows what would be restored and writes nothing.
    """
    parser = argparse.ArgumentParser(prog='setup.py restore-previous-model')
    parser.add_argument('--project', required=True)
    parser.add_argument('--plan', action='store_true')
    args = parser.parse_args(argv)
    try:
        if not args.plan:
            from memvane.kernel.memvane_execution_guard import require_cached_plugin_for_live_write
            require_cached_plugin_for_live_write()
        result = _learning_command(['restore-previous-model', '--project', args.project,
                                    *(['--plan'] if args.plan else [])])
    except Exception as exc:
        result = _failure(exc)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    if result.get('status') == 'rollback_deferred': return 75
    return 1 if result.get('status') in ('rejected', 'rollback_blocked', 'rollback_interrupted') else 0


_DIRECT = {'state-cleanup': _state_cleanup, 'restore-previous-model': _restore_previous_model}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] and argv[0] in _DIRECT: return _DIRECT[argv[0]](argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan-learning-profile','activate-learning-profile',
                                          'activate-learning-runtime', 'gc-index-runtime',
                                          'plan-fresh-wiki-origin', 'index-runtime-status', 'provision-index-runtime',
                                          'review-index-provider', 'prepare-fresh-wiki-origin',
                                          'index-origin-status', 'index-build-status','index-build-rescan',
                                          'finalize-index-origin',
                                          'activate-index-origin','recover-index-origin',
                                          'restore-index-origin',
                                          'onboarding-skip', 'onboarding-never',
                                          'onboarding-resume', 'onboarding-status',
                                          'restore-teacher-enable', 'teacher-off', 'teacher-on'))
    parser.add_argument('--project')
    parser.add_argument('--learning-choice',choices=('preserve','local-auto-apply','local-staged','off'),default='preserve')
    parser.add_argument('--learning-namespace')
    parser.add_argument('--learning-migration',choices=('new','preserve'),default='preserve')
    parser.add_argument('--learning-expires-at-unix-ms',type=int)
    parser.add_argument('--accepted-learning-profile')
    parser.add_argument('--learning-runtime-executable')
    parser.add_argument('--learning-model-dir')
    parser.add_argument('--approve-learning-runtime-activation', action='store_true')
    parser.add_argument('--state-root')
    parser.add_argument('--install-id')
    parser.add_argument('--accepted-origin-sha256')
    parser.add_argument('--accepted-provider-sha256')
    parser.add_argument('--provider-model-file')
    parser.add_argument('--provider-dimension', type=int)
    parser.add_argument('--approve-index-origin-activation', action='store_true')
    parser.add_argument('--approve-index-origin-restore', action='store_true')
    parser.add_argument('--approve-index-runtime-download', action='store_true')
    parser.add_argument('--approve-index-runtime-gc', action='store_true')
    parser.add_argument('--session')
    parser.add_argument('--plan', action='store_true')
    args = parser.parse_args(argv)
    if args.project is None and args.action not in (
            'index-runtime-status',
            'provision-index-runtime', 'gc-index-runtime', 'activate-learning-runtime'):
        parser.error('--project is required for ' + args.action)
    try:
        read_only = (args.action in ('plan-learning-profile', 'plan-fresh-wiki-origin',
                                    'review-index-provider', 'index-runtime-status',
                                    'index-origin-status', 'index-build-status', 'onboarding-status')
                     or (args.action == 'gc-index-runtime' and not args.approve_index_runtime_gc)
                     or (args.action == 'restore-teacher-enable' and args.plan))
        if not read_only:
            from memvane.kernel.memvane_execution_guard import require_cached_plugin_for_live_write
            require_cached_plugin_for_live_write()
        if args.action.startswith('onboarding-'):
            # The user's answer to the SessionStart onboarding notice. It
            # hides or shows that notice only; it never selects a route.
            from memvane.host import api as choice
            if args.action == 'onboarding-skip':
                result = choice.skip(args.project, args.session)
            elif args.action == 'onboarding-never':
                result = choice.never(args.project)
            elif args.action == 'onboarding-resume':
                result = choice.resume(args.project)
            else:
                result = choice.status(args.project)
        elif args.action in ('index-runtime-status', 'provision-index-runtime',
                             'gc-index-runtime'):
            # The Laya embedding runtime the provider review reads.
            from memvane.provider import api as laya_embed_runtime
            if args.action == 'provision-index-runtime':
                # npm registry and model download: owner approval only.
                result = {'status': 'provisioned', **laya_embed_runtime.provision_new(
                    approve_download=args.approve_index_runtime_download,
                    model_file=args.provider_model_file)}
            elif args.action == 'gc-index-runtime':
                result = laya_embed_runtime.collect(approve=args.approve_index_runtime_gc)
            else:
                result = {'status': 'ready', **laya_embed_runtime.select()}
                from memvane.runtime import api as runtime
                result['processVersions'] = runtime.background_bundle().process_version_counts()
            result.pop('dependencyClosure', None)
        elif args.action in ('plan-fresh-wiki-origin',
                           'review-index-provider',
                           'prepare-fresh-wiki-origin',
                           'index-origin-status',
                           'index-build-status','index-build-rescan',
                           'finalize-index-origin',
                           'activate-index-origin','recover-index-origin',
                           'restore-index-origin'):
            from memvane.install import api as product
            from memvane.app import setup_activation as selection
            installed = Path(__file__).resolve(strict=True).parents[2]
            if args.action != 'plan-fresh-wiki-origin' and not args.state_root:
                raise ValueError('state_root_required')
            if args.action == 'plan-fresh-wiki-origin':
                result = product.plan_fresh_wiki_origin(args.project)
            elif args.action == 'review-index-provider':
                result = product.review_local_provider(
                    plugin_root=installed, state_root=args.state_root,
                    model_path=args.provider_model_file,
                    dimension=args.provider_dimension)
            elif args.action == 'prepare-fresh-wiki-origin':
                result = product.prepare_fresh_wiki_origin(
                    project_root=args.project, plugin_root=installed, state_root=args.state_root,
                    expected_proposal_sha256=args.accepted_origin_sha256,
                    accepted_provider_sha256=args.accepted_provider_sha256,
                    install_id=args.install_id,
                    model_path=args.provider_model_file,
                    dimension=args.provider_dimension)
            elif args.action == 'index-origin-status':
                result = product.index_origin_status(args.state_root)
            elif args.action == 'index-build-status':
                result = product.initial_build_status(args.state_root, project_root=args.project)
            elif args.action == 'index-build-rescan':
                result = product.request_initial_build_rescan(args.state_root,
                                                              project_root=args.project)
            elif args.action == 'finalize-index-origin':
                result = selection.finalize_initial_build(
                    project_root=args.project, state_root=args.state_root)
            elif args.action == 'restore-index-origin':
                # Abandons an unselected install, withdraws a selection before its
                # first selected write, or releases one whose provider changed; deletes no DB.
                if not args.approve_index_origin_restore:
                    raise ValueError('index_origin_restore_approval_required')
                result = product.restore_origin_selection(
                    project_root=args.project, state_root=args.state_root)
            else:
                if (args.action == 'activate-index-origin'
                        and not args.approve_index_origin_activation):
                    raise ValueError('index_origin_activation_approval_required')
                result = product.select_origin(project_root=args.project, state_root=args.state_root,
                                               recover=args.action == 'recover-index-origin')
        elif args.action == 'activate-learning-runtime':
            if not args.approve_learning_runtime_activation:
                raise ValueError('learning_runtime_activation_approval_required')
            if not args.learning_runtime_executable or not args.learning_model_dir:
                raise ValueError('learning_runtime_inputs_required')
            from memvane.learning.api import activate_managed_runtime
            result = activate_managed_runtime(args.learning_runtime_executable, args.learning_model_dir)
        elif args.action in ('restore-teacher-enable', 'teacher-off', 'teacher-on'):
            # Explicit request only. restore-teacher-enable puts back the bytes an
            # unfinished default-teacher enable replaced (--plan shows each target
            # and writes nothing); teacher-off/on write the namespace teacher switch.
            result = _learning_command([args.action, '--project', args.project,
                                        *(['--plan'] if args.plan else [])])
        elif args.action=='plan-learning-profile':result=learning_plan(args)
        else:result=select_learning(args)
    except Exception as exc:
        result = _failure(exc)
    try:
        # The selected project registry is a lookup index: it reflects a
        # finished activation or release and never changes this result.
        from memvane.registry import api as memvane_registry
        memvane_registry.after_setup(args.action, args.project, args.state_root, result)
    except Exception:
        pass
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    if result.get('status')=='deferred':return 75
    return 1 if result.get('status') in ('rejected', 'blocked', 'failed_preparing', 'partial',
        'recovery_required', 'rolled_back_after_activation_failure') else 0


if __name__ == '__main__': raise SystemExit(main())
