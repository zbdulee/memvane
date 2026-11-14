"""Learning test helpers: the repository root and the synthetic trainer (``mock_trainer.py``)."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
MOCK_TRAINER = Path(__file__).resolve().with_name('mock_trainer.py')
