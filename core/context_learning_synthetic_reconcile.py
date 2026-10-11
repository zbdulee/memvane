#!/usr/bin/env python3
"""Owner-bound, local-only SessionStart source inventory reconciliation."""
import json
import os
import sys

if __name__ == '__main__':
    if os.environ.get('QMD_SANDBOX'):
        raise SystemExit(0)
    from context_learning.synthetic_source_reconcile import reconcile
    try:
        result = reconcile(os.environ.get('QMD_RECONCILE_CWD') or os.getcwd())
    except (OSError, ValueError, KeyError, TypeError):
        # Ordinary projects and invalid approvals are inert. Never loosen the
        # global compile gate or read another story after a scope failure.
        print(json.dumps({'status': 'scope_blocked', 'externalCalls': 0}))
        raise SystemExit(0)
    if result.get('status') != 'disabled':
        print(json.dumps(result, sort_keys=True))
