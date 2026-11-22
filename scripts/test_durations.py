#!/usr/bin/env python3
"""Rewrite tests/support/durations.json from module result records.

    python3 -B scripts/test_durations.py results.jsonl [more.jsonl ...] [--out PATH]

Each input is the JSONL a test run appends when MEMVANE_TEST_RESULTS names it
(tests/support/runner.mjs): one {module, status, seconds, commit} row per
module. The output maps every module that passed at least once to the median
seconds of its passing rows. Reads only the files given; a malformed row is
skipped.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

DEFAULT_OUT = Path(__file__).resolve().parents[1] / 'tests' / 'support' / 'durations.json'


def passing_seconds(paths):
    """{module: [seconds of each passing row]} over the given result files."""
    found = {}
    for path in paths:
        with open(path, encoding='utf8') as source:
            for line in source:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get('status') != 'pass':
                    continue
                module, seconds = row.get('module'), row.get('seconds')
                if (isinstance(module, str) and module and type(seconds) in (int, float)
                        and seconds == seconds and 0 <= seconds < 86400):
                    found.setdefault(module, []).append(float(seconds))
    return found


def durations(paths):
    return {module: round(statistics.median(values), 1)
            for module, values in sorted(passing_seconds(paths).items())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('results', nargs='+')
    parser.add_argument('--out', default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    table = durations(args.results)
    if not table:
        print('no passing module rows in the given files', file=sys.stderr)
        return 1
    target = Path(args.out)
    staged = target.with_name(target.name + '.tmp')
    staged.write_text(json.dumps(table, indent=1, sort_keys=True) + '\n', encoding='utf8')
    staged.replace(target)
    print('%d modules -> %s' % (len(table), target))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
