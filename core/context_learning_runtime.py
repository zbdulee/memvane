#!/usr/bin/env python3
"""Review and apply the one-story local runtime authorization."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from context_learning.dataset import read_private_json
from context_learning.runtime_authorization import (activate, effective_argv,
                                                     proposal, proposal_sha256)
from context_learning.shared import POLICY_NAME, REVIEWED_ADAPTER_SHA256, _policy
from context_learning.synthetic_experiment import shared_for


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('plan', 'status', 'activate'))
    parser.add_argument('project_root', type=Path)
    parser.add_argument('--accept-plan-sha256')
    args = parser.parse_args()
    if os.environ.get('QMD_SANDBOX'):
        return 1
    try:
        project = args.project_root.resolve(strict=True)
        root = shared_for(project)
        _policy(root)  # Validate the historical policy and any existing approval.
        raw_policy = read_private_json(root / POLICY_NAME)
        adapter = Path(__file__).parent / 'context_learning/laya_adapter.py'
        expected = proposal(root, project, raw_policy, adapter,
                            reviewed_adapter_sha256=REVIEWED_ADAPTER_SHA256)
        plan_hash = proposal_sha256(expected)
        if args.action == 'plan':
            result = {'status': 'review_required', 'proposalSha256': plan_hash,
                      'proposal': expected}
        elif args.action == 'status':
            effective = effective_argv(root, raw_policy, adapter,
                reviewed_adapter_sha256=REVIEWED_ADAPTER_SHA256)
            result = {'status': ('active' if effective[1] == str(adapter.resolve())
                                 else 'not_authorized'),
                      'proposalSha256': plan_hash}
        else:
            if not args.accept_plan_sha256:
                raise ValueError('runtime_authorization_plan_acceptance_required')
            result = activate(root, project, raw_policy, adapter,
                args.accept_plan_sha256,
                reviewed_adapter_sha256=REVIEWED_ADAPTER_SHA256)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'status': 'rejected', 'reason': str(exc)[:120]}))
        return 1
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
