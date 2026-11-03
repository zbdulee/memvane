#!/usr/bin/env python3
"""T2B actual move admission, on the existing disposable published v5 corpus.

The first case covers the held-reader physical-move boundary. Retained tuple,
crash-prefix search and rename/adopt/state completion remain separate gates.
"""
import importlib
import importlib.util
import json
from pathlib import Path
import unittest

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('reader_move_lease_fixture',HERE/'laya-reader-lease-checks.py')
lease_fixture=importlib.util.module_from_spec(spec)
spec.loader.exec_module(lease_fixture)


class ReaderMoveChecks(unittest.TestCase):
    setUp=lease_fixture.ReaderLeaseChecks.setUp
    api=lease_fixture.ReaderLeaseChecks.api

    def test_actual_project_rename_defers_before_any_physical_change_while_reader_is_held(self):
        api=self.api()
        mover=importlib.import_module('laya_project_dir_move')
        self.assertEqual(mover.needed(self.case.project),'rename')
        metadata=self.helper.metadata_snapshot(self.case)
        data=self.helper.data_snapshot(self.case)
        paths=(self.case.db,self.case.catalog,self.case.store,self.case.journal)
        nodes={str(path):(Path(path).stat().st_dev,Path(path).stat().st_ino) for path in paths}
        result,error=None,None
        with self.helper.full_authority(self.case,self.reader,api,mover):
            with api.reader_lifetime(self.install,exclusive=False):
                try:result=mover.move(self.case.project,self.case.state)
                except (ValueError,OSError,RuntimeError) as exc:error=str(exc)
        reason=error or (result.get('reason') if isinstance(result,dict) else None)
        print(json.dumps(dict(case='actual-project-rename-held-reader',result=result,error=error,
            actualHostExecuted=False)),flush=True)
        self.assertEqual(reason,'reader_lifetime_busy')
        self.assertEqual(self.helper.metadata_snapshot(self.case),metadata)
        self.assertEqual(self.helper.data_snapshot(self.case),data)
        self.assertEqual({str(path):(Path(path).stat().st_dev,Path(path).stat().st_ino) for path in paths},nodes)
        self.assertTrue(self.case.auto.is_dir())
        self.assertFalse((self.case.project/'.memvane').exists())

    def test_current_state_noop_does_not_add_a_journal_requirement(self):
        mover=importlib.import_module('laya_state_move')
        self.assertFalse(mover.legacy_root(self.case.state,self.case.home))
        saved=self.case.state/'saved-journal.sqlite'
        self.case.journal.rename(saved)
        try:
            with self.helper.full_authority(self.case,self.reader,mover):
                state,result=mover.move(self.case.project,self.case.state,home=self.case.home)
            self.assertEqual(state,self.case.state)
            self.assertEqual(result,dict(status='state_move_current'))
            self.assertFalse(self.case.journal.exists())
        finally:saved.rename(self.case.journal)


if __name__=='__main__':unittest.main(verbosity=2)
