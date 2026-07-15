"""Container entrypoint: ``python3 -m harness``.

``HARNESS_MODE`` selects the mode: ``simulate`` runs the single-plugin
Plugin_Simulator harness (:mod:`harness.simulate`, custom-node-designer
Requirements 7.2, 7.3, 7.6); anything else (the default) runs the
workflow test harness (:mod:`harness.harness`).
"""

import os
import sys


def _main() -> int:
    if os.environ.get("HARNESS_MODE") == "simulate":
        from .simulate import main
    else:
        from .harness import main
    return main()


if __name__ == "__main__":
    sys.exit(_main())
