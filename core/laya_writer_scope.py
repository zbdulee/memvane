"""Inactive normal-writer admission, separate from reader and mover authority.

Admission proves one warm content image under EX. A later component opener
must revalidate its actual header under EX; this token is no immutable image
or all-writer cohort proof. Product RW openers do not accept it yet.
"""
from contextlib import ExitStack
from dataclasses import dataclass
import os
from pathlib import Path
import threading
import time

import laya_install_journal as J
import laya_journal_reader as R
import laya_reader_windows as W
import laya_writer_open_plan as P
from laya_index_sync import validate_sync_owner

# Keep abandoned admissions strongly owned. Only explicit creator exit can
# release their lifetime and queue pin; no generator/finalizer does cleanup.
_issued = {}


@dataclass(frozen=True, eq=False, init=False)
class NormalWriter:
    install_id: str
    queue_path: Path
    scope_id: str
    incarnation: str

    def __init__(self):
        raise ValueError('normal_writer_not_issued')

    def __reduce__(self):
        raise TypeError('normal_writer_not_serializable')


def _record(writer):
    R._need(type(writer) is NormalWriter and writer in _issued, 'normal_writer_not_issued')
    record = _issued[writer]
    context, snapshot = record['context'], record['snapshot']
    context._creator()
    R._need(not context.finished and context.entered, 'normal_writer_expired')
    decision = snapshot['decision']
    R._need(writer.install_id == decision['installId']
            and writer.queue_path == Path(snapshot['components']['queue']['path'])
            and writer.scope_id == decision['scopeId']
            and writer.incarnation == decision['dbIncarnation'], 'normal_writer_changed')
    return record


def validate_normal_writer(writer):
    """Validate current admission metadata, not a continuing content snapshot."""
    record = _record(writer)
    context = record['context']
    R._need(time.monotonic() < context.deadline, 'normal_writer_deadline')
    validate_sync_owner(context.queue_owner, context.plan.queue_path)
    P.validate_writer_plan(context.plan)
    context.lifetime.check()


def _approved(proof, snapshot):
    decision, selected = snapshot['decision'], snapshot['bundle']['selected']
    expected = dict(status='installer_active_full_plugin_verified',
        executingPluginRoot=snapshot['executing_root'], pluginRoot=snapshot['executing_root'],
        journalPath=decision['journalPath'], selectionEpoch=snapshot['bundle']['epoch'],
        installId=decision['installId'], scopeId=decision['scopeId'],
        dbIncarnation=decision['dbIncarnation'], projectRoot=snapshot['project'],
        generation=selected['generation'], sourceCommit=selected['sourceCommit'],
        bundleManifestSha256=selected['bundleManifestSha256'],
        ownershipManifestSha256=selected['ownershipManifestSha256'],
        vectorProfile=selected['vectorProfile'])
    R._need(type(proof) is dict and proof.get('productRouteAllowed') is True
            and type(proof.get('selectionEpoch')) is int
            and type(proof.get('providerManifest')) is dict
            and all(proof.get(key) == value for key, value in expected.items()),
            'normal_writer_full_proof_required')


class _NormalWriterContext:
    def __init__(self, plan, queue_owner, deadline):
        self.plan, self.queue_owner, self.deadline = plan, queue_owner, deadline
        self.pid, self.thread = os.getpid(), threading.current_thread()
        self.started = self.entered = self.finished = False
        self.stack = self.lifetime = self.writer = None

    def _creator(self):
        R._need(self.pid == os.getpid() and self.thread is threading.current_thread(),
                'normal_writer_owner_changed')

    def __enter__(self):
        self._creator()
        R._need(not self.started and not self.finished, 'normal_writer_reentered')
        self.started = True
        W._deadline(self.deadline)
        R._need(time.monotonic() < self.deadline, 'normal_writer_deadline')
        P.validate_writer_plan(self.plan)
        validate_sync_owner(self.queue_owner, self.plan.queue_path)
        self.stack = ExitStack()
        try:
            pin = self.queue_owner.pin()
            self.stack.callback(pin.close)
            self.lifetime = W._Lifetime(self.plan.install_id)
            self.stack.callback(self.lifetime.close)
            with P.writer_proof_windows(self.plan, queue_owner=self.queue_owner,
                                        deadline=self.deadline) as window:
                with J.writer_identity_reads(window):
                    snapshot = P._plan_record(self.plan)[1]
                    proof = J.verify_selected_route_for_write(snapshot['decision'])
                # Both native RO readers are closed while every physical EX
                # is still held. Complete approval before releasing that EX.
                P.validate_writer_proof_window(window)
                _approved(proof, snapshot)
                writer = object.__new__(NormalWriter)
                object.__setattr__(writer, 'install_id', self.plan.install_id)
                object.__setattr__(writer, 'queue_path', self.plan.queue_path)
                object.__setattr__(writer, 'scope_id', snapshot['decision']['scopeId'])
                object.__setattr__(writer, 'incarnation', snapshot['decision']['dbIncarnation'])
                self.writer = writer
                self.entered = True
                _issued[writer] = dict(context=self, snapshot=snapshot, proof=proof)
            validate_normal_writer(writer)
            return writer
        except BaseException:
            if self.writer is not None:
                _issued.pop(self.writer, None)
            self.finished = True
            self.stack.close()
            raise

    def __exit__(self, exc_type, exc, traceback):
        self._creator()
        if self.finished or not self.entered:
            return False
        # Revocation precedes lock release and needs no current write proof.
        self.finished = True
        _issued.pop(self.writer, None)
        self.stack.close()
        return False


def normal_writer_scope(plan, *, queue_owner, deadline):
    """Issue inactive admission only after an owned, approved full write proof."""
    return _NormalWriterContext(plan, queue_owner, deadline)
