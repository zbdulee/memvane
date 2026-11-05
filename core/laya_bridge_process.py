"""Bounded OS process identities for the owned bridge collector; no environment dump."""
import ctypes
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys


def _refuse():
    raise ValueError('reader_hosts_pending')


def peer_identity(peer):
    if sys.platform.startswith('linux'):
        pid, uid, _gid = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    elif sys.platform == 'darwin':
        user, group = ctypes.c_uint(), ctypes.c_uint()
        if ctypes.CDLL(None, use_errno=True).getpeereid(peer.fileno(), ctypes.byref(user), ctypes.byref(group)) != 0:
            _refuse()
        uid = user.value
        pid = struct.unpack('i', peer.getsockopt(0, 2, 4))[0]  # SOL_LOCAL / LOCAL_PEERPID
    else:
        _refuse()
    if uid != os.getuid() or pid <= 0:
        _refuse()
    return uid, pid


def process_identity(pid):
    if type(pid) is not int or pid <= 0:
        _refuse()
    try:
        if sys.platform.startswith('linux'):
            root = Path('/proc') / str(pid)
            first = (root / 'stat').read_text()
            fields = first[first.rfind(')') + 2:].split()
            if fields[0] in ('Z', 'X'):
                _refuse()
            uid = root.stat().st_uid
            parent, started = int(fields[1]), int(fields[19])
            exe = Path(os.readlink(root / 'exe')).resolve(strict=True)
            raw = (root / 'cmdline').read_bytes()
            if len(raw) > 131072:
                _refuse()
            argv = tuple(item.decode('utf8', 'strict') for item in raw.rstrip(b'\0').split(b'\0'))
            again = (root / 'stat').read_text()
            if again[again.rfind(')') + 2:].split()[19] != str(started):
                _refuse()
            start = started / os.sysconf('SC_CLK_TCK')
            key = (started,)
        elif sys.platform == 'darwin':
            class Bsd(ctypes.Structure):
                _fields_ = [(name, ctypes.c_uint32) for name in
                    ('flags','status','xstatus','pid','ppid','uid','gid','ruid','rgid','svuid','svgid','rfu')]
                _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
                _fields_ += [(name, ctypes.c_uint32) for name in ('nfiles','pgid','jobc','tdev','tpgid','nice')]
                _fields_ += [('seconds', ctypes.c_uint64), ('micros', ctypes.c_uint64)]
            lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
            value = Bsd()
            if lib.proc_pidinfo(pid, 3, 0, ctypes.byref(value), ctypes.sizeof(value)) != ctypes.sizeof(value):
                _refuse()
            if value.pid != pid or value.status == 5:
                _refuse()
            path = ctypes.create_string_buffer(4096)
            if lib.proc_pidpath(pid, path, len(path)) <= 0:
                _refuse()
            exe = Path(path.value.decode()).resolve(strict=True)
            libc = ctypes.CDLL(None, use_errno=True)
            mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN / KERN_PROCARGS2
            data = ctypes.create_string_buffer(131072)
            size = ctypes.c_size_t(len(data))
            if libc.sysctl(mib, 3, data, ctypes.byref(size), None, 0) != 0:
                _refuse()
            raw = data.raw[:size.value]
            argc = struct.unpack('i', raw[:4])[0]
            if not 1 <= argc <= 4096:
                _refuse()
            offset = raw.index(b'\0', 4) + 1
            while offset < len(raw) and raw[offset] == 0:
                offset += 1
            args = []
            for _ in range(argc):
                end = raw.index(b'\0', offset)
                args.append(raw[offset:end].decode('utf8', 'strict'))
                offset = end + 1
            argv = tuple(args)  # Do not parse or expose the trailing environment.
            again = Bsd()
            if (lib.proc_pidinfo(pid, 3, 0, ctypes.byref(again), ctypes.sizeof(again)) != ctypes.sizeof(again)
                    or (again.seconds, again.micros) != (value.seconds, value.micros)):
                _refuse()
            uid, parent = value.uid, value.ppid
            key = (value.seconds, value.micros)
            start = value.seconds + value.micros / 1000000
        else:
            _refuse()
        if uid != os.getuid() or not exe.is_file() or not argv or start <= 0:
            _refuse()
        return dict(pid=pid, uid=uid, parent=parent, start=start, start_key=key,
                    executable=str(exe), argv=argv)
    except (OSError, ValueError, IndexError, UnicodeError):
        _refuse()


def same_process(saved):
    try:
        now = process_identity(saved['pid'])
        return all(now[key] == saved[key] for key in ('pid', 'uid', 'start_key', 'executable'))
    except ValueError:
        return False


def descendant_chain(peer_pid, root):
    found, current = [], peer_pid
    for _ in range(32):
        identity = process_identity(current)
        found.append(identity)
        if current == root['pid']:
            if not same_process(root):
                _refuse()
            return found
        current = identity['parent']
        if current <= 1:
            break
    _refuse()


def descendants(root):
    if not same_process(root):
        return []
    parents = {}
    if sys.platform.startswith('linux'):
        for name in os.listdir('/proc'):
            if not name.isdigit():
                continue
            try:
                row = (Path('/proc') / name / 'stat').read_text()
                parents[int(name)] = int(row[row.rfind(')') + 2:].split()[1])
            except (OSError, ValueError, IndexError):
                continue
    else:
        result = subprocess.run(['/bin/ps', '-ax', '-o', 'pid=,ppid='], capture_output=True,
                                encoding='utf8', timeout=1)
        if result.returncode != 0 or len(result.stdout) > 1024 * 1024:
            _refuse()
        parents = {int(line.split()[0]): int(line.split()[1])
                   for line in result.stdout.splitlines() if len(line.split()) == 2}
    selected = {root['pid']}
    for _ in range(32):
        more = {pid for pid, parent in parents.items() if parent in selected}
        if more <= selected:
            break
        selected.update(more)
        if len(selected) > 64:
            _refuse()
    found = []
    for pid in selected - {root['pid']}:
        try:
            chain = descendant_chain(pid, root)
        except ValueError:
            continue
        found.extend(chain[:-1])
        if len(found) > 64:
            _refuse()
    return list({row['pid']: row for row in found}.values())
