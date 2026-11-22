#!/usr/bin/env python3
"""Modules whose result differed between runs of the same commit.

    python3 -B scripts/test_flaky_report.py results.jsonl [more.jsonl ...]

Each input is the JSONL a test run appends when MEMVANE_TEST_RESULTS names it
(tests/support/runner.mjs): {module, status, seconds, commit} per module. A
module is reported when one commit holds both a passing and a failing row for
it. Rows without a commit are counted and left out. Read-only: prints a JSON
report and never skips, retries or records anything.
"""
from __future__ import annotations

import json
import sys


def report(paths):
    seen, uncommitted, rows = {}, 0, 0
    for path in paths:
        with open(path, encoding='utf8') as source:
            for line in source:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if (not isinstance(row, dict) or not isinstance(row.get('module'), str)
                        or row.get('status') not in ('pass', 'fail')):
                    continue
                rows += 1
                commit = row.get('commit')
                if not isinstance(commit, str) or not commit:
                    uncommitted += 1
                    continue
                counts = seen.setdefault((row['module'], commit), {'pass': 0, 'fail': 0})
                counts[row['status']] += 1
    changed = [{'module': module, 'commit': commit, **counts}
               for (module, commit), counts in sorted(seen.items())
               if counts['pass'] and counts['fail']]
    return {'rows': rows, 'rowsWithoutCommit': uncommitted,
            'moduleCommits': len(seen), 'changed': changed}


def main(argv=None):
    paths = list(sys.argv[1:] if argv is None else argv)
    if not paths or paths[0] in ('-h', '--help'):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    print(json.dumps(report(paths), indent=1, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
