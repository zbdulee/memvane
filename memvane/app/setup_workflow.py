#!/usr/bin/env python3
"""Plan, prepare, finalize and activate one private MemVane store."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from memvane.install import api as _install
from memvane.install.types import InstallBlocked
from memvane.app.setup_activation import finalize_initial_build

_install_lock = _install.install_lock
activate_origin_selection = _install.activate_origin_selection
recover_origin_selection = _install.recover_origin_selection
plan_fresh_wiki_origin = _install.plan_fresh_wiki_origin
review_local_provider = _install.review_local_provider
prepare_fresh_wiki_origin = _install.prepare_fresh_wiki_origin
initial_build_status = _install.initial_build_status
request_initial_build_rescan = _install.request_initial_build_rescan
restore_origin_selection = _install.restore_origin_selection


def _installed_root():
    return Path(__file__).resolve(strict=True).parents[2]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    fresh_plan = sub.add_parser('plan-fresh-wiki',
                                help='review a new wiki-only generation')
    fresh_plan.add_argument('--project-root', required=True)
    review = sub.add_parser('review-provider',
                            help='inventory exact local Node/model/native artifacts')
    review.add_argument('--state-root', required=True)
    review.add_argument('--model-file')
    review.add_argument('--dimension', type=int)
    fresh_prepare = sub.add_parser('prepare-fresh-wiki',
                                   help='register an inactive new wiki-only build')
    for key in ('project-root', 'state-root', 'proposal-sha256', 'install-id',
                'accepted-provider-sha256'):
        fresh_prepare.add_argument('--' + key, required=True)
    fresh_prepare.add_argument('--model-file')
    fresh_prepare.add_argument('--dimension', type=int)
    status = sub.add_parser('status', help='inspect the inactive or active install journal')
    status.add_argument('--state-root', required=True)
    build_status = sub.add_parser('build-status', help='inspect a registered initial build')
    build_status.add_argument('--state-root', required=True)
    rescan = sub.add_parser('build-rescan',
                            help='request a scan of a registered initial build')
    rescan.add_argument('--state-root', required=True)
    finalize = sub.add_parser('finalize-build',
                              help='bind a sealed, fresh initial build; never activates')
    finalize.add_argument('--project-root', required=True)
    finalize.add_argument('--state-root', required=True)
    for name in ('activate', 'recover'):
        command = sub.add_parser(name)
        command.add_argument('--project-root', required=True)
        command.add_argument('--state-root', required=True)
        command.add_argument('--route-file', required=True)
        if name == 'activate':
            command.add_argument('--approve-activation', action='store_true')
    restore = sub.add_parser('restore',
                             help='withdraw the Laya selection before its first selected write')
    restore.add_argument('--project-root', required=True)
    restore.add_argument('--state-root', required=True)
    restore.add_argument('--approve-restore', action='store_true')
    args = parser.parse_args(argv)
    if args.action not in ('plan-fresh-wiki', 'review-provider', 'status', 'build-status'):
        from memvane.kernel.memvane_execution_guard import require_cached_plugin_for_live_write
        require_cached_plugin_for_live_write()
    root = _installed_root()
    if args.action == 'plan-fresh-wiki':
        result = plan_fresh_wiki_origin(args.project_root)
    elif args.action == 'build-status':
        result = initial_build_status(args.state_root)
    elif args.action == 'build-rescan':
        result = request_initial_build_rescan(args.state_root)
    elif args.action == 'finalize-build':
        result = finalize_initial_build(project_root=args.project_root,
                                        state_root=args.state_root)
    elif args.action == 'review-provider':
        result = review_local_provider(
            plugin_root=root, state_root=args.state_root,
            model_path=args.model_file, dimension=args.dimension)
    elif args.action == 'prepare-fresh-wiki':
        result = prepare_fresh_wiki_origin(
            project_root=args.project_root, plugin_root=root, state_root=args.state_root,
            expected_proposal_sha256=args.proposal_sha256,
            install_id=args.install_id,
            accepted_provider_sha256=args.accepted_provider_sha256,
            model_path=args.model_file, dimension=args.dimension)
    elif args.action == 'status':
        result = {'journal': _install.index_origin_status(args.state_root)['journal']}
    elif args.action == 'restore':
        if not args.approve_restore:
            raise InstallBlocked('restore_approval_required')
        result = restore_origin_selection(project_root=args.project_root,
                                          state_root=args.state_root)
    else:
        if args.action == 'activate' and not args.approve_activation:
            raise InstallBlocked('activation_approval_required')
        result = _install.select_origin(project_root=args.project_root, state_root=args.state_root,
                                        route_file=args.route_file,
                                        recover=args.action == 'recover')
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    if args.action == 'restore' and result.get('status') in ('blocked', 'recovery_required'):
        return 2
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (InstallBlocked, OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc)},
                         sort_keys=True), file=sys.stderr)
        sys.exit(2)
