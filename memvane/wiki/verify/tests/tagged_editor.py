"""Save a real editor revision at the final rename of an archived verifier."""
import json
import os
from pathlib import Path
import secrets
import sys
from unittest.mock import patch

from memvane.wiki.engines import api as engines
from memvane.wiki.verify import wiki_verify_worker as verifier

bundle, root, target = map(Path, sys.argv[1:4])
config, mode = json.loads(sys.argv[4]), sys.argv[5]
assert root.resolve().is_relative_to(Path.home().resolve()) and bundle.resolve() == Path.cwd()
memo = ('\n## Manual notes\n' + secrets.token_hex(24) + '\n').encode()
replace = os.replace
opened = []


def edit_then_replace(source, destination, *args, **kwargs):
    if Path(destination) == target:
        assert not opened
        old = target.read_bytes()
        if mode == 'atomic':
            edit = target.with_name('.fixture-editor-save')
            with edit.open('xb') as stream:
                stream.write(old + memo)
                stream.flush()
                os.fsync(stream.fileno())
            replace(edit, target)
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        else:
            with target.open('ab') as stream:
                stream.write(memo)
                stream.flush()
                os.fsync(stream.fileno())
        assert target.read_bytes() == old + memo
        opened.append(target.open('rb'))
    return replace(source, destination, *args, **kwargs)


answer = {'verdict': 'pass', 'claims': [{'claim': 'Decision', 'supported': True,
          'quote': 'Older source', 'sourcePath': 'docs/source.md'}], 'reasons': []}
with patch.object(os, 'replace', side_effect=edit_then_replace), \
     patch.object(engines, 'run_extractor', return_value=(answer, None, 0)) as model:
    result = verifier.run(root, config, config['compile'])
reader, = opened
assert memo in reader.read()
links = os.fstat(reader.fileno()).st_nlink
reader.close()
paths = [str(path.relative_to(root)) for path in root.rglob('*')
         if path.is_file() and not path.is_symlink() and memo in path.read_bytes()]
for name, module in sys.modules.items():
    if name == 'memvane' or name.startswith('memvane.'):
        assert Path(module.__file__).resolve().is_relative_to(bundle)
print(json.dumps({'links': links, 'paths': paths, 'calls': model.call_count, 'remaining': result['remaining']}))
