#!/usr/bin/env python3
"""Strict disposable QMD stand-in for scoped story authoring tests."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys


def main(args):
    project = Path(os.environ['QMD_TOPICAL_STORY_TEST_ROOT']).resolve(strict=True)
    index_path = Path(os.environ['INDEX_PATH']).resolve()
    config_dir = Path(os.environ['QMD_CONFIG_DIR']).resolve()
    story = project / 'planned-story'
    if (not index_path.is_relative_to(project) or not config_dir.is_relative_to(project)
            or not (story / '.qmd-topical-v2-project').is_file()):
        raise ValueError('fake_qmd_scope_invalid')
    mapping_path = config_dir / 'fake-collections.json'
    mapping = json.loads(mapping_path.read_text()) if mapping_path.exists() else {}
    index_path.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(index_path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS documents '
                   '(collection TEXT,path TEXT,hash TEXT,active INTEGER, '
                   'PRIMARY KEY(collection,path))')
        db.execute('CREATE TABLE IF NOT EXISTS content_vectors(hash TEXT PRIMARY KEY)')
        if (len(args) == 7 and args[:2] == ['collection','add']
                and args[3] == '--name' and args[5:] == ['--mask','**/*.md']):
            folder = Path(args[2]).resolve(strict=True)
            if folder != story / '.auto-context/wiki' or args[4] != 'fantasy-20261008-wiki':
                raise ValueError('fake_qmd_collection_scope_invalid')
            mapping[args[4]] = str(folder)
            mapping_path.write_text(json.dumps(mapping))
            with (config_dir/'index.yml').open('a') as stream:
                stream.write('\n# '+args[4]+'\n')
        elif args != ['update'] and args != ['cleanup'] and not (
                len(args) == 7 and args[0] == 'embed' and args[1] == '-c'
                and args[3] == '--max-docs-per-batch' and args[5] == '--max-batch-mb'
                and args[2] == 'fantasy-20261008-wiki'):
            raise ValueError('fake_qmd_arguments_invalid')
        if args == ['update'] or args[:2] == ['collection','add']:
            for collection, folder in mapping.items():
                base = Path(folder)
                present = set()
                for page in sorted(base.rglob('*.md')):
                    if page.is_symlink() or not page.is_file():
                        raise ValueError('fake_qmd_unsafe_page')
                    relative = page.relative_to(base).as_posix()
                    present.add(relative)
                    db.execute('INSERT OR REPLACE INTO documents VALUES (?,?,?,1)',
                               (collection,relative,hashlib.sha256(page.read_bytes()).hexdigest()))
                for (relative,) in db.execute('SELECT path FROM documents WHERE collection=?',
                                              (collection,)).fetchall():
                    if relative not in present:
                        db.execute('UPDATE documents SET active=0 WHERE collection=? AND path=?',
                                   (collection,relative))
        elif args[0] == 'embed':
            for (revision,) in db.execute('SELECT hash FROM documents WHERE collection=? AND active=1',
                                          (args[2],)):
                db.execute('INSERT OR IGNORE INTO content_vectors VALUES (?)',(revision,))
        elif args == ['cleanup']:
            db.execute('DELETE FROM documents WHERE active=0')


if __name__ == '__main__':
    try: main(sys.argv[1:])
    except (OSError,ValueError,KeyError,sqlite3.Error) as exc:
        print(str(exc),file=sys.stderr)
        raise SystemExit(2)
