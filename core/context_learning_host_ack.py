#!/usr/bin/env python3
"""Silent SessionStart acknowledgement for an owner-requested history cutover."""
from pathlib import Path
import sys

from context_learning.history_lifecycle import acknowledge_host


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(2)
    acknowledge_host(sys.argv[1], Path(__file__).resolve().parent.parent)
