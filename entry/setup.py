#!/usr/bin/env python3
"""Setup alias for the plugin process entry point."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

sys.argv.insert(1, 'setup')
runpy.run_path(str(Path(__file__).with_name('run.py')), run_name='__main__')
