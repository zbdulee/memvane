"""Installed 0644 manifests and recovery of intents emitted by shipped 1.0.16."""
from contextlib import contextmanager, nullcontext, redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import runpy
import stat
import sys
import types
from unittest import mock

from memvane.learning import default_renewal as renewal, project_learning_recovery as recovery
from memvane.learning.context_learning import dataset, learning_profile, project_scope, shared
from memvane.learning.tests.renewal_fixture import RenewalFixture


class ManifestCases:
    def manifest(self):
        return self.running/('.'+self.host+'-plugin/plugin.json')

    def shipped_intent(self):
        source=Path(__file__).resolve().parents[1]/'fixtures/default_renewal_1_0_16.py.txt'
        old=types.ModuleType('shipped_116')
        old.__package__='memvane.learning'
        old.__file__=str(source)
        exec(compile(source.read_bytes(),str(source),'exec'),old.__dict__)
        self.select(self.initial_host,'1.0.16','B')
        from memvane.learning.context_learning import selector_heal
        lease=selector_heal._prepare_lease(None)
        before=self.snapshot_targets()
        try:
            with self.assertRaisesRegex(ValueError,'^unsafe_input_file$'):
                old.renew(self.project,self.install,'fixture',lease_fd=lease)
        finally:os.close(lease)
        self.assertEqual(before,self.snapshot_targets())
        intent=self.read(learning_profile.RENEWAL)
        self.assertEqual(intent['schema'],'memvane-default-renewal-v1')
        self.assertEqual(intent['hostVersion'],'1.0.16')
        self.assertEqual(intent['host'],self.initial_host)
        return intent

    def run_project_entry(self, expected_code=0):
        entry=Path(__file__).resolve().parents[4]/'entry/run.py'
        out,err=io.StringIO(),io.StringIO()
        with mock.patch.object(shared,'resource_report',return_value={'ready':True}), \
                mock.patch.object(shared,'run_training_pending',return_value={'status':'no_synthetic_cases'}), \
                mock.patch.object(sys,'argv',[str(entry),'learning-shared','run-project',str(self.project)]), \
                redirect_stdout(out),redirect_stderr(err):
            with self.assertRaises(SystemExit) as ended:runpy.run_path(str(entry),run_name='__main__')
        self.assertEqual(ended.exception.code,expected_code,err.getvalue())
        return json.loads(out.getvalue())

    def assert_admitted(self):
        self.assertFalse((self.root/learning_profile.RENEWAL).exists())
        self.assertEqual(stat.S_IMODE(self.manifest().stat().st_mode),0o644)
        self.assertTrue(learning_profile.require(self.root,shared._policy(self.root),'local_train'))
        result=self.run_project_entry()
        self.assertEqual(result['teacher'],{'status':'disabled_local_only'})
        self.assertEqual(result['cycle']['status'],'no_synthetic_cases')
        self.assertEqual(self.settings_before,(self.settings.read_bytes(),self.settings.stat().st_ino))

    def test_first_update_0644_maintenance_and_run_project(self):
        self.select(self.initial_host,'1.0.17','C')
        self.assertEqual(stat.S_IMODE(self.manifest().stat().st_mode),0o644)
        self.maintenance()
        self.assert_admitted()

    def test_actual_116_fence_resumes_through_117_entries(self):
        self.shipped_intent()
        self.assertEqual(self.run_project_entry(1)['reason'],'project_scope_renewal_pending')
        self.select(self.initial_host,'1.0.17','C')
        self.maintenance()
        self.assert_admitted()
        self.assertEqual(self.read(project_scope.NAME)['proposal']['binding']['installations'][0]['version'],'1.0.17')

    def test_same_target_116_intent_resumes_with_0644_manifest(self):
        self.shipped_intent()
        self.assertEqual(self.renew()['status'],'renewed')
        self.assert_admitted()

    def test_all_publication_boundaries_recover_with_0644_manifest(self):
        restore=renewal._restore_bytes
        for boundary in range(1,6):
            with self.subTest(boundary=boundary):
                for name,raw in self.original.items():(self.root/name).write_bytes(raw)
                self.select(self.initial_host,'1.0.17','C')
                count=[0]
                def interrupted(*args,**kwargs):
                    restore(*args,**kwargs);count[0]+=1
                    if count[0]==boundary:raise InterruptedError('synthetic publication interruption')
                with mock.patch.object(renewal,'_restore_bytes',side_effect=interrupted):
                    with self.assertRaises(InterruptedError):self.renew()
                self.assertTrue((self.root/learning_profile.RENEWAL).is_file())
                self.maintenance()
                self.assert_admitted()

    def test_unsafe_manifest_is_rejected_without_changing_intent_or_targets(self):
        intent=self.shipped_intent()
        path=self.manifest();raw=path.read_bytes()
        before=self.snapshot_targets()
        fence=(self.root/learning_profile.RENEWAL).read_bytes()
        for kind in ('group_writable','symlink','hardlink','owner'):
            with self.subTest(kind=kind):
                alias=path.with_name('alias.json')
                if kind=='group_writable':path.chmod(0o664)
                elif kind=='symlink':
                    path.rename(alias);path.symlink_to(alias)
                elif kind=='hardlink':os.link(path,alias)
                actual_fstat=os.fstat
                inode=path.stat().st_ino
                def different_owner(fd):
                    info=actual_fstat(fd)
                    if info.st_ino==inode:
                        fields=list(info);fields[4]=os.getuid()+1
                        return os.stat_result(fields)
                    return info
                patch=(mock.patch.object(recovery.os,'fstat',side_effect=different_owner)
                       if kind=='owner' else nullcontext())
                try:
                    with patch:
                        with self.assertRaisesRegex(ValueError,'^setup_unsafe_path$'):
                            renewal._chain(self.project,self.root,'fixture',intent)
                    self.assertEqual(before,self.snapshot_targets())
                    self.assertEqual(fence,(self.root/learning_profile.RENEWAL).read_bytes())
                finally:
                    if kind=='symlink':path.unlink();alias.rename(path)
                    elif kind=='hardlink':alias.unlink()
                    path.chmod(0o644)
                self.assertEqual(raw,path.read_bytes())

    def test_manifest_content_or_path_change_during_read_is_rejected(self):
        intent=self.shipped_intent()
        path=self.manifest();raw=path.read_bytes()
        before=self.snapshot_targets()
        original_fdopen=os.fdopen
        for change in ('content','path'):
            with self.subTest(change=change):
                inode=path.stat().st_ino
                @contextmanager
                def opened(fd,*args,**kwargs):
                    with original_fdopen(fd,*args,**kwargs) as stream:
                        if os.fstat(fd).st_ino==inode:
                            data=stream.read()
                            if change=='content':path.write_bytes(data+b' ')
                            else:
                                replacement=path.with_name('replacement.json')
                                replacement.write_bytes(data);replacement.chmod(0o644)
                                os.replace(replacement,path)
                            yield io.BytesIO(data)
                        else:yield stream
                with mock.patch.object(recovery.os,'fdopen',side_effect=opened):
                    with self.assertRaisesRegex(ValueError,'configuration_changed'):
                        renewal._chain(self.project,self.root,'fixture',intent)
                self.assertEqual(before,self.snapshot_targets())
                self.assertTrue((self.root/learning_profile.RENEWAL).exists())
                path.write_bytes(raw);path.chmod(0o644)

    def test_private_state_reader_still_refuses_public_manifest(self):
        with self.assertRaisesRegex(ValueError,'^unsafe_input_file$'):
            dataset.read_private_json(self.manifest())


class CodexManifest(ManifestCases,RenewalFixture):
    initial_host='codex'


class ClaudeManifest(ManifestCases,RenewalFixture):
    initial_host='claude'
