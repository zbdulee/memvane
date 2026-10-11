#!/usr/bin/env python3
"""Explicit isolated local train command; ordinary hooks never invoke it."""
import json
import sys

from context_learning.isolated_train import preflight, run
from context_learning.derived_train import preflight as preflight_derived, run as run_derived


def main(argv):
    if len(argv) != 2 or argv[0] not in (
            'preflight', 'run', 'preflight-derived', 'run-derived'):
        return 2
    try:
        if argv[0] in ('preflight', 'preflight-derived'):
            plan = (preflight_derived if argv[0] == 'preflight-derived'
                    else preflight)(argv[1])
            result = {'status': 'ready', 'train_cases': len(plan['train']),
                      ('development_cases' if argv[0] == 'preflight-derived'
                       else 'independent_holdout_cases'): len(plan['holdout']),
                      'evidence_sha256': plan['evidence_sha256'],
                      'output_directory': str(plan['output'])}
        else:
            result = (run_derived if argv[0] == 'run-derived' else run)(argv[1])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result = {'status': 'failed', 'reason': str(exc)[:80]}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 1 if result['status'] == 'failed' else 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
