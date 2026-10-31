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
    from context_learning import learning_profile as profile
    if args.learning_choice=='preserve' or not args.learning_namespace:
        raise ValueError('explicit_learning_profile_and_namespace_required')
    root=Path(args.learning_namespace)
    # Exact accepted retry uses its original frozen proposal, never retimes it.
    old=root/profile.INSTALL
    row=None
    if args.accepted_learning_profile and (old.exists() or old.is_symlink()):
        from context_learning.dataset import read_private_json
        previous=read_private_json(old)
        if previous['acceptedSha256']==args.accepted_learning_profile:
            row=previous['proposal']
    if row is None:
        row=profile.proposal(Path(args.project),root,choice=args.learning_choice,
            mode=args.learning_migration,expires_at_unix_ms=args.learning_expires_at_unix_ms)
    return {'status':'learning_profile_proposed','proposal':row,'proposalSha256':profile.digest(row),'changesApplied':0}


def select_learning(args):
    from context_learning import learning_profile as profile
    if not args.accepted_learning_profile:raise ValueError('profile_acceptance_required')
    return profile.activate(Path(args.project),Path(args.learning_namespace),
        accepted_sha256=args.accepted_learning_profile,choice=args.learning_choice,
        mode=args.learning_migration,expires_at_unix_ms=args.learning_expires_at_unix_ms)


def _exception_detail(exc):
    """Structured evidence (e.g. unexpected installed files) from the cause chain."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if getattr(exc, 'detail', None) is not None:
            return exc.detail
        exc = exc.__cause__ or exc.__context__
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan-learning-profile','activate-learning-profile',
                                          'plan-legacy-cleanup', 'apply-legacy-cleanup',
                                          'gc-index-runtime',
                                          'state-root-status', 'move-state-roots',
                                          'plan-fresh-wiki-origin',
                                          'index-runtime-status', 'provision-index-runtime',
                                          'review-index-provider',
                                          'prepare-fresh-wiki-origin',
                                          'index-origin-status',
                                          'index-build-status','index-build-rescan',
                                          'upgrade-inactive-index-build',
                                          'restore-inactive-index-upgrade',
                                          'upgrade-active-index-bundle',
                                          'restore-active-index-bundle',
                                          'finalize-index-origin',
                                          'activate-index-origin','recover-index-origin',
                                          'restore-index-origin','retire-index-origin',
                                          'abandon-index-build','abandon-inactive-index-build',
                                          'seal-host-copy',
                                          'onboarding-skip', 'onboarding-never',
                                          'onboarding-resume', 'onboarding-status'))
    parser.add_argument('--project')
    parser.add_argument('--learning-choice',choices=('preserve','local-auto-apply','local-staged','off'),default='preserve')
    parser.add_argument('--learning-namespace')
    parser.add_argument('--learning-migration',choices=('new','preserve'),default='preserve')
    parser.add_argument('--learning-expires-at-unix-ms',type=int)
    parser.add_argument('--accepted-learning-profile')
    parser.add_argument('--source-root')
    parser.add_argument('--commit')
    parser.add_argument('--state-root')
    parser.add_argument('--install-id')
    parser.add_argument('--accepted-origin-sha256')
    parser.add_argument('--accepted-provider-sha256')
    parser.add_argument('--provider-model-file')
    parser.add_argument('--provider-dimension', type=int)
    parser.add_argument('--approve-index-origin-activation', action='store_true')
    parser.add_argument('--approve-index-origin-restore', action='store_true')
    parser.add_argument('--approve-index-origin-retire', action='store_true')
    parser.add_argument('--approve-index-build-abandon', action='store_true')
    parser.add_argument('--approve-inactive-index-build-abandon', action='store_true')
    parser.add_argument('--approve-inactive-index-upgrade', action='store_true')
    parser.add_argument('--approve-active-index-upgrade', action='store_true')
    parser.add_argument('--approve-index-runtime-download', action='store_true')
    parser.add_argument('--approve-index-runtime-gc', action='store_true')
    parser.add_argument('--approve-legacy-cleanup', action='store_true')
    parser.add_argument('--include-user-qmd-data', action='store_true')
    parser.add_argument('--wake-revision', type=int)
    parser.add_argument('--session')
    args = parser.parse_args(argv)
    if args.project is None and args.action not in (
            'plan-legacy-cleanup', 'apply-legacy-cleanup', 'index-runtime-status',
            'provision-index-runtime', 'gc-index-runtime', 'state-root-status',
            'move-state-roots'):
        parser.error('--project is required for ' + args.action)
    try:
        if args.action.startswith('onboarding-'):
            # The user's answer to the SessionStart onboarding notice. It
            # hides or shows that notice only; it never selects a route.
            import onboarding_choice as choice
            if args.action == 'onboarding-skip':
                result = choice.skip(args.project, args.session)
            elif args.action == 'onboarding-never':
                result = choice.never(args.project)
            elif args.action == 'onboarding-resume':
                result = choice.resume(args.project)
            else:
                result = choice.status(args.project)
        elif args.action == 'state-root-status':
            # Read only: installs and shared trees still under ~/.local/state/laya.
            import laya_state_move
            result = laya_state_move.status()
        elif args.action == 'move-state-roots':
            # What each project's SessionStart worker does, for every project now.
            import laya_bundle_upgrade
            result = laya_bundle_upgrade.move_all()
        elif args.action in ('plan-legacy-cleanup', 'apply-legacy-cleanup'):
            # The owner's one-time removal of the retired QMD backend.
            import legacy_cleanup
            if args.action == 'plan-legacy-cleanup':
                result = {'status': 'planned', **legacy_cleanup.plan(
                    include_user_data=args.include_user_qmd_data)}
            else:
                result = legacy_cleanup.apply(approved=args.approve_legacy_cleanup,
                                              include_user_data=args.include_user_qmd_data)
        elif args.action in ('index-runtime-status', 'provision-index-runtime',
                             'gc-index-runtime'):
            # The Laya embedding runtime the provider review reads.
            import laya_embed_runtime
            if args.action == 'provision-index-runtime':
                # npm registry and model download: owner approval only.
                result = {'status': 'provisioned', **laya_embed_runtime.provision_new(
                    approve_download=args.approve_index_runtime_download,
                    model_file=args.provider_model_file)}
            elif args.action == 'gc-index-runtime':
                result = laya_embed_runtime.collect(approve=args.approve_index_runtime_gc)
            else:
                result = {'status': 'ready', **laya_embed_runtime.select()}
            result.pop('dependencyClosure', None)
        elif args.action in ('plan-fresh-wiki-origin',
                           'review-index-provider',
                           'prepare-fresh-wiki-origin',
                           'index-origin-status',
                           'index-build-status','index-build-rescan',
                           'upgrade-inactive-index-build',
                           'restore-inactive-index-upgrade',
                           'upgrade-active-index-bundle',
                           'restore-active-index-bundle',
                           'finalize-index-origin',
                           'activate-index-origin','recover-index-origin',
                           'restore-index-origin','retire-index-origin',
                           'abandon-index-build','abandon-inactive-index-build',
                           'seal-host-copy'):
            import laya_product_install as product
            installed = Path(__file__).resolve(strict=True).parent.parent
            if args.action != 'plan-fresh-wiki-origin' and not args.state_root:
                raise ValueError('state_root_required')
            if args.action == 'plan-fresh-wiki-origin':
                result = product.plan_fresh_wiki_origin(args.project)
                pending = product.retired_build_pointer(Path(args.project).resolve())
                if pending is not None:
                    # Finish the interrupted retirement before preparing.
                    result['pendingRetirement'] = pending
            elif args.action == 'review-index-provider':
                result = product.review_local_provider(
                    source_root=args.source_root, plugin_root=installed,
                    state_root=args.state_root, commit_id=args.commit,
                    model_path=args.provider_model_file,
                    dimension=args.provider_dimension)
            elif args.action == 'prepare-fresh-wiki-origin':
                result = product.prepare_fresh_wiki_origin(
                    project_root=args.project, source_root=args.source_root,
                    plugin_root=installed, state_root=args.state_root,
                    commit_id=args.commit,
                    expected_proposal_sha256=args.accepted_origin_sha256,
                    accepted_provider_sha256=args.accepted_provider_sha256,
                    install_id=args.install_id,
                    model_path=args.provider_model_file,
                    dimension=args.provider_dimension)
            else:
                state = product._private_parent(args.state_root)
                journal = state / 'install-journal.sqlite'
                if args.action == 'index-origin-status':
                    from laya_install_journal import inspect_journal
                    result = {'status': 'inspected', 'journal': inspect_journal(journal),
                              'routeFile': str(state / 'reviewed-route.json')}
                    from laya_initial_build import read as read_build
                    if read_build(state) is not None:
                        result['initialBuild'] = product.initial_build_status(state)
                elif args.action in ('index-build-status','index-build-rescan'):
                    from laya_initial_build import read as read_build
                    build = read_build(state)
                    if build is None or build['identity']['projectRoot'] != str(Path(args.project).resolve()):
                        raise ValueError('initial_build_project_mismatch')
                    result = (product.initial_build_status(state)
                              if args.action == 'index-build-status' else
                              product.request_initial_build_rescan(state))
                elif args.action in ('upgrade-inactive-index-build',
                                     'restore-inactive-index-upgrade'):
                    if not args.approve_inactive_index_upgrade:
                        raise ValueError('inactive_index_upgrade_approval_required')
                    from laya_inactive_upgrade import upgrade
                    result = upgrade(
                        project_root=str(Path(args.project).resolve()),
                        state_root=state, source_root=args.source_root,
                        plugin_root=installed, commit_id=args.commit,
                        accepted_provider_sha256=args.accepted_provider_sha256,
                        model_path=args.provider_model_file,
                        dimension=args.provider_dimension,
                        restore=args.action == 'restore-inactive-index-upgrade')
                elif args.action in ('upgrade-active-index-bundle',
                                     'restore-active-index-bundle'):
                    if not args.approve_active_index_upgrade:
                        raise ValueError('active_index_upgrade_approval_required')
                    from laya_active_upgrade import upgrade
                    result = upgrade(
                        project_root=str(Path(args.project).resolve()),
                        state_root=state, source_root=args.source_root,
                        plugin_root=installed, commit_id=args.commit,
                        accepted_provider_sha256=args.accepted_provider_sha256,
                        model_path=args.provider_model_file,
                        dimension=args.provider_dimension,
                        restore=args.action == 'restore-active-index-bundle')
                elif args.action == 'finalize-index-origin':
                    result = product.finalize_initial_build(
                        project_root=args.project, state_root=state)
                elif args.action == 'restore-index-origin':
                    # Withdraws the route before its first selected write; deletes no DB.
                    if not args.approve_index_origin_restore:
                        raise ValueError('index_origin_restore_approval_required')
                    result = product.restore_origin_selection(
                        project_root=args.project, state_root=state)
                elif args.action == 'retire-index-origin':
                    # A bound, never selected build; deletes no DB or store.
                    if not args.approve_index_origin_retire:
                        raise ValueError('index_origin_retire_approval_required')
                    result = product.retire_drifted_origin(
                        project_root=args.project, state_root=state)
                elif args.action == 'abandon-index-build':
                    # An unused registered build (no source read, nothing written);
                    # the pending laya-sync wake is cancelled by revision. Deletes nothing.
                    if not args.approve_index_build_abandon:
                        raise ValueError('index_build_abandon_approval_required')
                    result = product.abandon_unused_initial_build(
                        project_root=args.project, state_root=state,
                        install_id=args.install_id, commit_id=args.commit,
                        wake_revision=args.wake_revision)
                elif args.action == 'abandon-inactive-index-build':
                    # QA-only recovery of a partly built, still unselected
                    # candidate. All private stores are backed up and kept.
                    if not args.approve_inactive_index_build_abandon:
                        raise ValueError('inactive_index_build_abandon_approval_required')
                    result = product.abandon_inactive_initial_build(
                        project_root=args.project, state_root=state,
                        install_id=args.install_id, commit_id=args.commit,
                        wake_revision=args.wake_revision)
                elif args.action == 'seal-host-copy':
                    # Run from the other host's installed copy: seal it in place
                    # against the journal-bound owner; it never selects a route.
                    from laya_install_journal import inspect_journal
                    from laya_product_bundle import (inspect_full_plugin_candidate,
                                                     seal_companion_host_copy)
                    recorded = inspect_journal(journal)
                    identity = recorded.get('selected') or recorded.get('pending')
                    route = json.loads(product._read_private(state / 'reviewed-route.json', 8192))
                    if not isinstance(identity, dict) or not isinstance(route, dict):
                        raise ValueError('host_copy_owner_unselected')
                    if route.get('projectRoot') != str(Path(args.project).resolve()):
                        raise ValueError('host_copy_project_mismatch')
                    owner = inspect_full_plugin_candidate(identity.get('pluginRoot'))
                    if any(owner[key] != identity.get(key) for key in
                           ('sourceCommit', 'bundleManifestSha256', 'vectorProfile')):
                        raise ValueError('host_copy_owner_changed')
                    proof = seal_companion_host_copy(owner['pluginRoot'], installed)
                    result = {'status': 'host_copy_sealed', 'ownerRoot': owner['pluginRoot'],
                              **{key: proof[key] for key in
                                 ('pluginRoot', 'sourceCommit', 'bundleManifestSha256')}}
                else:
                    route = product._read_private(state / 'reviewed-route.json', 8192)
                    with product._install_lock(state):
                        if args.action == 'activate-index-origin':
                            if not args.approve_index_origin_activation:
                                raise ValueError('index_origin_activation_approval_required')
                            result = product.activate_origin_selection(
                                Path(args.project), journal, route)
                        else:
                            result = product.recover_origin_selection(
                                Path(args.project), journal, route)
        elif args.action=='plan-learning-profile':result=learning_plan(args)
        else:result=select_learning(args)
    except Exception as exc:
        from resource_deferred import ResourceDeferred
        if isinstance(exc, ResourceDeferred):
            result = {'status': 'deferred', 'reason': str(exc),
                      'changesApplied': 0}
        else:
            result = {'status': 'rejected', 'reason': str(exc), 'changesApplied': 0}
            detail = _exception_detail(exc)
            if detail is not None:
                result['detail'] = detail
            if (str(exc) == 'initial_build_already_registered' and
                    args.action == 'prepare-fresh-wiki-origin'):
                import laya_product_install as product
                pending = product.retired_build_pointer(Path(args.project).resolve())
                if pending is not None:
                    # Only the exact retired build's own pointer is named.
                    result['nextAction'] = pending['nextAction']
                    result['retireStateRoot'] = pending['stateRoot']
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    if result.get('status')=='deferred':return 75
    return 1 if result.get('status') in ('rejected', 'blocked', 'failed_preparing', 'partial',
        'recovery_required', 'rolled_back_after_activation_failure') else 0


if __name__ == '__main__': raise SystemExit(main())
