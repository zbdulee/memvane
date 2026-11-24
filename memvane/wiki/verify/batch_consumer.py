"""Classify an immutable generation before issuing bounded private deliveries in physical order."""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import os
import time

from memvane.wiki.workspace.handoff import api as raw_queue
from memvane.wiki.verify import batch_handoff


def _range(fd, start, size, continued, row_limit, lane, settled):
    """Return an LF-delimited frontier and whether it ends inside an oversized occurrence."""
    amount = min(raw_queue.SLAB, size - start)
    raw_queue.charge(lane, amount)
    raw = os.pread(fd, amount, start)
    if not raw:
        return start, [], continued, False, 0
    records = []
    inspected = 0
    pos = 0
    if continued:
        lf = raw.find(b'\n')
        if lf < 0:
            return start + len(raw), [], True, True, 1
        pos = lf + 1
        continued = False
    opaque = False
    while pos < len(raw) and row_limit > 0:
        lf = raw.find(b'\n', pos)
        if lf < 0:
            if pos:
                break  # Retry the next occurrence from its true boundary.
            if len(raw) < raw_queue.SLAB:
                return start, [], False, True, 1
            return start + len(raw), [], True, True, 1
        line = raw[pos:lf + 1]
        job, _safe = raw_queue.occurrence(line)
        if job is None:
            opaque = True
        else:
            if not settled(start + pos, line):
                records.append(line)
        pos = lf + 1
        inspected += 1
        row_limit -= 1
    return start + pos, records, continued, opaque, inspected


def _ordinary(window, size, proof):
    """Keep pointers into the original inode for independently checkable ordinary rows."""
    known = list(proof.get('handoff', {}).get('ordinary', []))
    data = window.data[:size]
    pos = 0
    if window.offset and not proof.get('handoff', {}).get('boundary'):
        pos = data.find(b'\n') + 1
        if not pos:
            return known
    while len(known) < 16:
        end = data.find(b'\n', pos)
        if end < 0:
            break
        job, _ = raw_queue.occurrence(data[pos:end+1])
        if job and job.get('sourceRevisions') and 'pendingPath' not in job and '_pendingCleanup' not in job:
            known.append([window.offset + pos, end + 1 - pos])
        pos = end + 1
    return known


def parked(batch):
    state = batch.work.state
    if not state.get('proof', {}).get('parked'):
        return False
    with batch._input() as (_, node):
        return node == state['sourceNode']


def advance(batch, *, gate=nullcontext, clock=time.monotonic, seconds=.05, steps=16):
    """Read and classify outside the mutation lock, then recheck the generation before committing."""
    ledger = raw_queue.budget.current()
    deadline = clock() + seconds
    if ledger is not None:
        if ledger.deadline is None:
            ledger.deadline = deadline
        deadline = min(deadline, ledger.deadline)
    work = batch.work
    count = 0
    lane = batch.lane + '_rows'
    source_rows = raw_queue.MAX_ROWS if ledger is None else ledger.available(lane)
    while count < steps and source_rows and clock() < deadline:
        raw_queue.charge('commits', 1)
        count += 1
        state = work.state
        proof = state['proof']
        prior = proof.get('lastDelivery')
        if prior and raw_queue.delivery_pending(work.queue, prior):
            return False
        window = batch.read_window(gate=gate)
        if window is None:
            continue
        if window.data:
            # This generation must reach EOF before any hidden candidate can be issued.
            size = len(window.data)
            lines = window.data.count(b'\n')
            if lines > source_rows:
                end = 0
                for _ in range(source_rows):
                    end = window.data.index(b'\n', end) + 1
                size, lines = end, source_rows
            source_rows -= lines
            raw_queue.charge(lane, lines)
            ordinary = _ordinary(window, size, proof) if batch.lane == 'verify' else []
            handoff = {'scanned': True, 'ordinary': ordinary, 'boundary': window.data[size - 1:size] == b'\n'}
            with gate():
                batch.commit_window(window, handoff, end=window.offset + size)
            continue
        sent = proof.get('sent', 0)
        if 'offering' in proof:
            offer = proof['offering']
            updated = dict(proof, sent=offer['end'], continued=offer['continued'], lastDelivery=offer['name'])
            del updated['offering']
            with gate():
                with batch._input() as (_, node):
                    if node != window.snapshot:
                        return False
                raw_queue.deliver(work.queue, offer['name'])
                work.commit('held', proof=updated)
            continue
        if sent == state['cursor']:
            with gate():
                work.commit('held', proof=dict(proof, parked=True))
            return True  # An old file descriptor can append even after our first EOF.
        with batch._input() as (fd, node):
            if node != window.snapshot:
                return False
            offsets = {entry[0] for entry in proof.get('handoff', {}).get('ordinary', [])}
            from memvane.wiki.verify.ordinary_slot import owns_occurrence
            def settled(offset, raw):
                return offset in offsets and owns_occurrence(work.queue, node['ino'], offset, raw)
            end, rows, continued, opaque, inspected = _range(fd, sent, node['size'], proof.get('continued', False),
                                                            source_rows, batch.lane, settled)
            if batch_handoff._node(os.fstat(fd)) != node:
                return False
            if end <= sent:
                with gate():
                    work.commit('held', proof=dict(proof, parked=True, opaque=True))
                return True  # A future append can finish the suffix starting at sent.
        updated = dict(proof, parked=False, opaque=bool(opaque or proof.get('opaque')))
        used = min(source_rows, max(1, inspected))
        source_rows -= used
        raw_queue.charge(lane, used)
        with gate(), batch._input() as (_, checked):
            if checked != node:
                return False
            if rows:
                raw = b''.join(rows)
                key = f"{state['id']}-{sent}-{end}-{hashlib.sha256(raw).hexdigest()}"
                name = raw_queue.prepare_delivery(work.queue, key, raw)
                updated['offering'] = dict(name=name, end=end, continued=continued)
            else:
                updated.update(sent=end, continued=continued)
            work.commit('held', proof=updated)
    return False
