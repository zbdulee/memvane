"""Offline QMD 2.5.3 typed-query contract against disposable synthetic data."""
import json
import os

if os.environ.get('QMD_REAL_MODEL_TESTS') != '1':
    print(json.dumps({'skipped': 'real model tests require explicit QMD_REAL_MODEL_TESTS=1'}))
    raise SystemExit(0)
from pathlib import Path
import shutil
import subprocess
import tempfile

snap=Path('/Users/dulee/work/laya-search-experiments/snapshot-20261003-165529')
qmd=Path('/Users/dulee/work/.qmd-tools/bin/qmd')
if not qmd.is_file() or not (snap/'models').is_dir() or not (snap/'offline.cjs').is_file():
    print(json.dumps({'skipped':'offline local QMD snapshot unavailable'}))
    raise SystemExit(0)
with tempfile.TemporaryDirectory(prefix='qmd-real-query-',dir=Path.home()) as name:
    root=Path(name);docs=root/'docs';docs.mkdir()
    (docs/'note.md').write_text('# Synthetic compass\nThe amber compass points north.\n')
    config=root/'config';config.mkdir()
    (config/'index.yml').write_text('collections: {}\nmodels:\n'
        '  embed: hf:ggml-org/embeddinggemma-300M-GGUF/embeddinggemma-300M-Q8_0.gguf\n'
        '  generate: hf:tobil/qmd-query-expansion-1.7B-gguf/qmd-query-expansion-1.7B-q4_k_m.gguf\n'
        '  rerank: hf:ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF/qwen3-reranker-0.6b-q8_0.gguf\n')
    cache=root/'cache/qmd';cache.mkdir(parents=True)
    (cache/'models').symlink_to(snap/'models',target_is_directory=True)
    index=root/'selected.sqlite'
    env={**os.environ,'INDEX_PATH':str(index),'QMD_CONFIG_DIR':str(config),
        'XDG_CACHE_HOME':str(cache.parent),'NODE_OPTIONS':'--require='+str(snap/'offline.cjs'),
        'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1'}
    def run(*args,timeout=120):
        return subprocess.run([str(qmd),*args],cwd=root,env=env,text=True,
            capture_output=True,timeout=timeout,check=True)
    assert run('--version').stdout.strip()=='qmd 2.5.3'
    run('collection','add',str(docs),'--name','synthetic')
    run('update')
    run('embed','--max-docs-per-batch','1','--max-batch-mb','1',timeout=180)
    response=run('query','lex: amber compass\nvec: amber compass','-c','synthetic',
        '-n','3','--format','json','--no-rerank',timeout=120)
    rows=json.loads(response.stdout)
    assert isinstance(rows,list) and rows,rows
    assert any('note.md' in row.get('file','') for row in rows),rows
    assert index.is_file() and index.stat().st_size>0
    print(json.dumps({'realQmdVersion':'2.5.3','typedJsonList':True,
        'isolatedIndexBytes':index.stat().st_size,'results':len(rows),'externalCalls':0}))
