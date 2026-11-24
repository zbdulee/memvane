"""Run the writer from the checked-out release in the fixture project."""
import json
from pathlib import Path
import sys

from memvane.wiki.compile import wiki_compile as writer
from memvane.wiki.workspace import api as workspace


root = Path(sys.argv[1])
config, candidate = json.load(sys.stdin)
assert Path(writer.__file__).resolve().is_relative_to(Path.cwd())
config['compile']['verify'] = {'enabled': False}
with workspace.card_write_lock(root), workspace.mutation_lock(root):
    raise SystemExit(writer._compile_locked(root, config, config['compile'], root / config['wikiPath'],
                                           candidate, False))
