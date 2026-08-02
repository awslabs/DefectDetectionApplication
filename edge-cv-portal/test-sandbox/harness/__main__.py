"""Container entrypoint: ``python3 -m harness``.

``HARNESS_MODE`` selects the mode: ``simulate`` runs the single-plugin
Plugin_Simulator harness (:mod:`harness.simulate`, custom-node-designer
Requirements 7.2, 7.3, 7.6); anything else (the default) runs the
workflow test harness (:mod:`harness.harness`).
"""

import os


def _main() -> int:
    if os.environ.get("HARNESS_MODE") == "simulate":
        from .simulate import main
    else:
        from .harness import main
    return main()


if __name__ == "__main__":
    # exit_now (os._exit), not sys.exit: both harness modes execute DDA
    # GStreamer elements that can leave non-daemon threads (in-process
    # Triton, plugin worker threads) blocking interpreter finalization.
    # Results are flushed before main() returns, so a normal shutdown
    # adds nothing — and a hung one holds the Fargate task open until
    # the Step Functions 10-minute timeout displaces the flushed
    # per-node error with a generic timeout message.
    from .harness import exit_now
    exit_now(_main())
