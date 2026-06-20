"""Root entry shim — keeps ``python main.py`` working.

The application lives in the ``aset_batt`` package; this file just ensures the
repo root is importable and delegates to ``aset_batt.app.run``.
Equivalent: ``python -m aset_batt``.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aset_batt.app.run import run

if __name__ == "__main__":
    sys.exit(run())
