"""Fresh-exec test worker; synthetic bundle/observer authority only."""
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import traceback
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('cold_base', HERE / 'laya-journal-reader-checks.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
J = base.J
from memvane.journal import journal_reader as R
import laya_bridge_readiness as B


def main():
    phase = sys.argv[2]
    row = dict(cold_worker_result=True, phase=phase, pid=os.getpid(), python=sys.version,
               sqlite=sqlite3.sqlite_version, entry='fresh-exec',
               initial_reader_tokens=len(R._issued), initial_bridge_tokens=len(B._ISSUED))
    helper = base.JournalReaderChecks('runTest')
    try:
        assert os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1'
        payload = json.loads(Path(sys.argv[1]).read_text(encoding='utf8'))
        home = Path(os.environ['HOME']).resolve(strict=True)
        case = SimpleNamespace(state=Path(payload['state']), project=Path(payload['project']),
                               plugin=Path(payload['plugin']), proof=payload['proof'])
        for path in (case.state, case.project, case.plugin):
            assert path.resolve(strict=True) == path and home in path.parents
        case.journal = case.state / 'install-journal.sqlite'
        assert row['initial_reader_tokens'] == row['initial_bridge_tokens'] == 0
        if phase == 'hot':
            db = sqlite3.connect(case.journal, isolation_level=None)
            db.execute('PRAGMA cache_size=1')
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE synthetic_cold_hot(x)')
            for _ in range(80):
                db.execute('INSERT INTO synthetic_cold_hot VALUES(randomblob(4000))')
            row.update(status='actual-hot-v4-writer-exit', header_version=int.from_bytes(case.journal.read_bytes()[60:64], 'big'))
            print(json.dumps(row), flush=True)
            os._exit(91)
        with helper.full_authority(case, R, B), \
                mock.patch('subprocess.Popen', side_effect=AssertionError('converter spawned a process')):
            if phase == 'baseline':
                decision = J.select_route(str(case.project), action='recall')
                full = J.verify_selected_route(decision)
                readonly = J.verify_selected_route_read_only(decision)
                assert full['productRouteAllowed'] and readonly['readOnlyAllowed']
                row.update(status='baseline-full-proof', epoch=full['selectionEpoch'])
            else:
                if phase == 'intent':
                    def checkpoint(name):
                        if name == 'initial-intent-durable':
                            row.update(status=name)
                            print(json.dumps(row), flush=True)
                            os._exit(91)
                    hook = mock.patch.object(J, '_conversion_step', checkpoint)
                else:
                    hook = mock.patch.object(J, '_conversion_step', None)
                with hook:
                    installer = helper.installer(case, R, B)
                    _, _, readiness = helper.ready(case, B)
                    selected = R.convert_reader_v5(case.state, case.project,
                                                  installer=installer, readiness=readiness)
                    replay = R.convert_reader_v5(case.state, case.project,
                                                installer=installer, readiness=readiness)
                assert helper.selection(selected) == helper.selection(replay)
                row.update(status='converted', selection=helper.selection(selected), version=J.journal_version(case.journal))
        print(json.dumps(row), flush=True)
        return 0
    except BaseException as exc:
        row.update(status='error', error_type=type(exc).__name__, reason=str(exc))
        print(json.dumps(row), flush=True)
        traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        helper.doCleanups()


if __name__ == '__main__':
    sys.exit(main())
