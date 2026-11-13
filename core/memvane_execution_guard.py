"""Keep development checkouts from writing the account's live v1 state."""
import os
from pathlib import Path
import pwd

import memvane_paths


def require_cached_plugin_for_live_write(state=None):
    account = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    live = memvane_paths.state_base(account)
    target = live if state is None else Path(state).resolve(strict=False)
    if target != live and live not in target.parents:
        return
    if os.environ.get('MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT') == '1':
        return
    root = Path(__file__).resolve().parent.parent
    for host in ('.claude', '.codex'):
        cache = account / host / 'plugins' / 'cache'
        try:
            parts = root.relative_to(cache).parts
        except ValueError:
            continue
        if len(parts) == 3 and parts[0] and parts[1] == 'memvane' and parts[2]:
            return
    raise ValueError('live_write_requires_plugin_cache')
