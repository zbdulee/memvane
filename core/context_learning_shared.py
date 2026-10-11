#!/usr/bin/env python3
"""Entry point for the opted-in, local-only shared learning worker."""
import sys

from context_learning.history_lifecycle import worker_lease
from context_learning.shared import main


if __name__ == "__main__":
    with worker_lease():
        raise SystemExit(main(sys.argv[1:]))
