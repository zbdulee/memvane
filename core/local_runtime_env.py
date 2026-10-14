"""Parse a local trial's environment as data, never as shell code.

Only variables needed to isolate QMD state are exported. PATH and NODE_OPTIONS
are recognized legacy lines but never applied: both can execute arbitrary code
in a hook child. A malformed file is rejected before any value is emitted.
"""
import os
from pathlib import Path
import re
import stat
import sys

PATH_KEYS = frozenset({'QMD_BIN','QMD_NODE_BIN','INDEX_PATH','QMD_CONFIG_DIR',
    'XDG_CACHE_HOME','QMD_DIRTY_QUEUE','QMD_BACKEND_STATE_DIR','QMD_LOCK_BASE',
    'QMD_CACHE_DIR','QMD_BACKEND_LOG','QMD_DAEMON_LOG','QMD_INDEX_WORKER_LOG',
    'QMD_HOOK_LOG','QMD_RECALL_LOG'})
FLAG_KEYS = frozenset({'HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE','QMD_CLEANUP_LEGACY'})
IGNORED_KEYS = frozenset({'PATH','NODE_OPTIONS'})
ALLOWED = PATH_KEYS | FLAG_KEYS | IGNORED_KEYS | {'QMD_DAEMON_PORT','QMD_DAEMON_URL'}
LINE = re.compile(r'(?:export )?([A-Z][A-Z0-9_]*)=(.*)\Z')
MAX_BYTES = 8192


def parse(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or
            info.st_nlink!=1 or info.st_mode & 0o022 or info.st_size>MAX_BYTES):
            raise ValueError('unsafe_runtime_file')
        data=os.read(fd,MAX_BYTES+1)
        if len(data)>MAX_BYTES:raise ValueError('runtime_file_too_large')
        text=data.decode('utf8')
    finally:os.close(fd)
    result={}
    for raw in text.splitlines():
        line=raw.strip()
        if not line or line.startswith('#'):continue
        match=LINE.fullmatch(line)
        if not match:raise ValueError('invalid_runtime_assignment')
        key,value=match.groups()
        if key not in ALLOWED or key in result:raise ValueError('invalid_runtime_key')
        if len(value)>=2 and value[0]==value[-1] and value[0] in ('"',"'"):
            value=value[1:-1]
        if (not value or any(ord(c)<32 or ord(c)==127 for c in value) or
            any(c in value for c in ('`','\\')) or '$(' in value):
            raise ValueError('invalid_runtime_value')
        if key in PATH_KEYS:
            path_value=Path(value)
            if not path_value.is_absolute() or '..' in path_value.parts or '$' in value:
                raise ValueError('invalid_runtime_path')
        elif key in FLAG_KEYS:
            if value not in ('0','1'):raise ValueError('invalid_runtime_flag')
        elif key=='QMD_DAEMON_PORT':
            if not value.isascii() or not value.isdecimal() or not 1024<=int(value)<=65535:
                raise ValueError('invalid_runtime_port')
        elif key=='QMD_DAEMON_URL':
            if not re.fullmatch(r'http://(?:localhost|127\.0\.0\.1|\[::1\]):[0-9]{4,5}',value):
                raise ValueError('invalid_runtime_url')
        elif key=='PATH':
            if not re.fullmatch(r'/[^:$]*:\$PATH',value):
                raise ValueError('invalid_legacy_path')
        elif key=='NODE_OPTIONS':
            if not re.fullmatch(r'--require=/[^\s]+/offline\.cjs',value):
                raise ValueError('invalid_legacy_node_options')
        result[key]=value
    if 'QMD_DAEMON_URL' in result and 'QMD_DAEMON_PORT' in result:
        if not result['QMD_DAEMON_URL'].endswith(':'+result['QMD_DAEMON_PORT']):
            raise ValueError('runtime_port_url_mismatch')
    return {key:value for key,value in result.items() if key not in IGNORED_KEYS}


def main(argv):
    if len(argv)!=1:return 2
    try:values=parse(argv[0])
    except (OSError,UnicodeError,ValueError):return 1
    for key,value in values.items():
        sys.stdout.write(f'{key}={value}\n')
    return 0


if __name__=='__main__':raise SystemExit(main(sys.argv[1:]))
