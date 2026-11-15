"""CLI for the shared provider QA call budget."""
import os
from pathlib import Path
import sys

from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.runtime.api import (qa_budget_initialize as initialize,
                                  qa_budget_status as status,
                                  reserve_qa_budget as reserve_from_env)


def main(argv=None):
    """Reserve a QA host turn before exec, sharing its ledger with workers."""
    import argparse
    parser = argparse.ArgumentParser(description='Private QA model-call ledger')
    actions = parser.add_subparsers(dest='action', required=True)
    for name in ('init', 'status', 'reserve', 'run'):
        action = actions.add_parser(name)
        action.add_argument('path', type=Path)
        action.add_argument('limit', type=int)
        if name == 'init':
            action.add_argument('--used', type=int, required=True)
        elif name == 'run':
            action.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.action == 'init':
        initialize(args.path, limit=args.limit, used=args.used)
    else:
        if args.action in ('reserve', 'run'):
            os.environ['QMD_QA_CALL_LEDGER'] = str(args.path)
            os.environ['QMD_QA_CALL_LIMIT'] = str(args.limit)
            if args.action == 'run':
                command = args.command[1:] if args.command[:1] == ['--'] else args.command
                if not command:
                    parser.error('run requires a command after --')
            reserve_from_env()
            if args.action == 'run':
                os.execvpe(command[0], command, os.environ.copy())
        print(status(args.path, args.limit))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except ResourceDeferred as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(75)
