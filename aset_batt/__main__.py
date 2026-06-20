"""Enable ``python -m aset_batt`` to launch the integrated GUI."""
import sys

from aset_batt.app.run import run

if __name__ == "__main__":
    sys.exit(run())
