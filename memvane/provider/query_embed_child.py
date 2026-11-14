"""Child process that embeds one query: JSON request on stdin, JSON result on stdout."""
from __future__ import annotations

import json
import sys

from memvane.kernel.bundle_paths import plugin_root

# The child starts with ``-I`` and puts the plugin root (argv[1]) on sys.path itself.
_BOOTSTRAP = '''
import sys
sys.path.insert(0,sys.argv[1])
from memvane.provider.query_embed_child import main
main()
'''


def child_argv():
    return [sys.executable, '-B', '-I', '-c', _BOOTSTRAP, str(plugin_root())]


def main():
    from memvane.provider.adapters.local_embed_provider import ReviewedLocalQmdProvider
    from memvane.kernel.errors import IndexContractError
    request = json.load(sys.stdin)
    # A test runtime root's fixture spec answers instead (``fixture_port.active``).
    from memvane.provider.fixture_port import active
    port = active()
    if port is not None:
        print(json.dumps(port.query_result(request), allow_nan=False, separators=(',', ':')))
        return
    try:
        value = ReviewedLocalQmdProvider(request['manifest'])._embed('query', request['prompt'],
                                                                     timeout=request['timeout'])
        print(json.dumps({'vector': value}, allow_nan=False, separators=(',', ':')))
    except IndexContractError as exc:
        code = str(exc)
        print(json.dumps({'error': code if code in request['allowed'] else 'provider_result_invalid'}))
