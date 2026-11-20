"""Configuration-only recovery of an interrupted project learning setup."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat

from memvane.learning.project_learning_setup import (
    BACKUPS, NAME, SCHEMA, configuration_paths,
    _locked, _namespace, _path, _project_settings, _write,
)
from memvane.learning.context_learning import learning_profile
from memvane.learning.context_learning.dataset import read_private_json
from memvane.learning.context_learning.inference_identity import file_sha
from memvane.learning.context_learning.training_cursor import _sync


def _recovery_read(path,*,private=True):
    """Read a stable recovery input without following a substituted leaf."""
    path=_path(path,private=private)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        before=os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid()
                or before.st_nlink!=1 or before.st_mode&(0o077 if private else 0o022)):
            raise ValueError('setup_unsafe_path')
        with os.fdopen(fd,'rb',closefd=False) as stream:raw=stream.read()
        after=os.fstat(fd)
        named=_path(path,private=private).stat()
        def identity(info):
            return (info.st_dev,info.st_ino,info.st_uid,info.st_mode,info.st_nlink,
                    info.st_size,info.st_mtime_ns,info.st_ctime_ns)
        if identity(before)!=identity(after) or identity(after)!=identity(named):
            raise ValueError('setup_recovery_configuration_changed')
        return raw
    finally:os.close(fd)


def _recovery_target(path,expected,*,private=True):
    actual=hashlib.sha256(_recovery_read(path,private=private)).hexdigest() if os.path.lexists(path) else None
    if actual!=expected:raise ValueError('setup_recovery_configuration_changed')


_UNCHECKED_RESTORE=object()


def _restore_bytes(path,raw,*,private_parent=True,private=True,
           expected_sha256=_UNCHECKED_RESTORE,preserve_mode=False):
    # Backup restoration preserves the original byte representation.
    import tempfile
    _path(path,private=private,missing=True);_path(path.parent,directory=True,private=private_parent)
    mode=stat.S_IMODE(path.stat().st_mode) if preserve_mode and path.exists() else 0o600
    fd,name=tempfile.mkstemp(prefix='.restore-',dir=path.parent)
    try:
        os.fchmod(fd,mode)
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        if expected_sha256 is not _UNCHECKED_RESTORE:
            _recovery_target(path,expected_sha256,private=private)
        _path(path,private=private,missing=True);_path(path.parent,directory=True,private=private_parent)
        os.replace(name,path);_sync(path.parent)
    finally:Path(name).unlink(missing_ok=True)


def recover(project,root):
    root=Path(root)
    previous=read_private_json(_path(root/NAME,private=True))
    project,root=_namespace(project,root,mode=previous.get('plan',{}).get('mode','default'))
    from memvane.settings.api import settings_lock
    # Match activation's lock order: namespace first, then the shared project
    # writer lock. Keep both through verification and every restoration write.
    with _locked(root), settings_lock(project):
        journal=read_private_json(_path(root/NAME,private=True))
        if journal.get('schema')!=SCHEMA or journal.get('plan',{}).get('projectRoot')!=str(project) or journal['plan'].get('namespaceRoot')!=str(root):
            raise ValueError('setup_recovery_invalid')
        if journal['phase']=='recovered':return {'status':'already_recovered'}
        # A default-teacher enable intent owns these files until it completes
        # or restore-teacher-enable puts the old bytes back.
        if os.path.lexists(root/'project-teacher-enable.json'):
            raise ValueError('setup_teacher_enable_pending')
        backup=_path(journal['backupRoot'],directory=True,private=True)
        if backup.parent!=root/BACKUPS:raise ValueError('setup_recovery_invalid')
        manifest=read_private_json(_path(backup/'manifest.json',private=True))
        if manifest.get('acceptedSha256')!=journal['acceptedSha256']:raise ValueError('setup_backup_changed')
        if manifest['files'].get('settings.json')!=journal['sourceSettingsSha256']:
            raise ValueError('setup_backup_changed')
        for relative,expected in manifest['files'].items():
            if '..' in Path(relative).parts or Path(relative).is_absolute():raise ValueError('setup_backup_changed')
            if file_sha(_path(backup/relative,private=True))!=expected:raise ValueError('setup_backup_changed')
        settings=_path(_project_settings(project))
        settings_raw=_recovery_read(settings,private=False)
        settings_sha=hashlib.sha256(settings_raw).hexdigest()
        original_settings=_recovery_read(backup/'settings.json')
        if hashlib.sha256(original_settings).hexdigest()!=journal['sourceSettingsSha256']:
            raise ValueError('setup_backup_changed')
        if settings_sha not in {journal['sourceSettingsSha256'],journal.get('targetSettingsSha256')}:
            raise ValueError('setup_recovery_settings_changed')
        # Verify all configuration before changing any of it.
        # default-teacher (like an explicit setup) includes the teacher consent file.
        allowed=set(configuration_paths(project,journal['plan'].get('mode','default')))
        if set(journal['configurationPaths'])!=allowed or not set(journal['written'])<=allowed:
            raise ValueError('setup_recovery_invalid')
        affected=set(journal['written'])
        if journal.get('profileProposal') is not None:affected.add(learning_profile.INSTALL)
        recovering=journal['phase']=='recovering'
        for relative,expected in journal['written'].items():
            path=root/relative
            permitted={expected}
            original_sha=manifest['files'].get('namespace/'+relative)
            if recovering and original_sha is not None:permitted.add(original_sha)
            if os.path.lexists(path):
                if hashlib.sha256(_recovery_read(path)).hexdigest() not in permitted:
                    raise ValueError('setup_recovery_configuration_changed')
        install=root/learning_profile.INSTALL
        restored_install=(recovering and os.path.lexists(install)
            and hashlib.sha256(_recovery_read(install)).hexdigest()==manifest['files'].get('namespace/'+learning_profile.INSTALL))
        if os.path.lexists(install) and learning_profile.INSTALL not in journal['written'] and not restored_install:
            row=read_private_json(_path(install,private=True));profile=journal.get('profileProposal')
            if row.get('proposal')!=profile or row.get('acceptedSha256')!=learning_profile.digest(profile) or row.get('phase') not in ('intent','settings_written','activated'):
                raise ValueError('setup_recovery_configuration_changed')
        restorations=[]
        for relative in sorted(affected):
            target=_path(root/relative,private=True,missing=True)
            original=backup/'namespace'/relative
            raw=None
            if os.path.lexists(original):
                raw=_recovery_read(original)
                if hashlib.sha256(raw).hexdigest()!=manifest['files'].get('namespace/'+relative):
                    raise ValueError('setup_backup_changed')
            current=hashlib.sha256(_recovery_read(target)).hexdigest() if os.path.lexists(target) else None
            if raw is not None or current is not None:
                _path(target.parent,directory=True,private=True)
            restorations.append((target,raw,current))
        # Reject deterministic path/CAS failures before recording any recovery work.
        _recovery_target(settings,settings_sha,private=False)
        for target,raw,current in restorations:_recovery_target(target,current)
        if settings_raw!=original_settings:
            _path(settings.parent,directory=True,private=False)
        journal['phase']='recovering';_write(root/NAME,journal)
        if settings_raw!=original_settings:
            _restore_bytes(settings,original_settings,private_parent=False,private=False,
                   expected_sha256=settings_sha,preserve_mode=True)
        for target,raw,current in restorations:
            if raw is not None:
                if hashlib.sha256(raw).hexdigest()!=current:
                    _restore_bytes(target,raw,expected_sha256=current)
            elif current is not None:
                _recovery_target(target,current)
                _path(target,private=True).unlink();_sync(target.parent)
        journal['phase']='recovered';_write(root/NAME,journal)
        return {'status':'recovered','backupRoot':str(backup),'dataPreserved':True,'checkpointChanged':False}
