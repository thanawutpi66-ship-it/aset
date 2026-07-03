"""Enable ``python -m aset_batt`` to launch the integrated GUI."""
import sys
import multiprocessing

from aset_batt.app.run import run

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(run())
