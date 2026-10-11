#!/usr/bin/env python3
"""Internal registration after two real host edits and exact QMD index ACKs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from context_learning.source_card_provenance import register_card
from context_learning.synthetic_experiment import shared_for


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('project_root', type=Path)
    parser.add_argument('source_relative')
    parser.add_argument('card_relative')
    args = parser.parse_args()
    if os.environ.get('QMD_SANDBOX'):
        return 1
    try:
        result = register_card(shared_for(args.project_root), args.project_root,
                               args.source_relative, args.card_relative)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
        print(json.dumps({'status': 'rejected', 'reason': str(exc)[:100]}))
        return 1
    print(json.dumps({'status': 'registered', **result}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
