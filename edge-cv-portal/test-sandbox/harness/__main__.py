"""Container entrypoint: ``python3 -m harness``."""

import sys

from .harness import main

if __name__ == "__main__":
    sys.exit(main())
