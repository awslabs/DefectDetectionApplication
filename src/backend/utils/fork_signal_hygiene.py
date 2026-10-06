# Copyright 2026 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Forked children must not share the backend's signal wakeup fd
(spec vllm-jp7-engine-lifecycle, Defect A; bugfix.md 2.1, 2.2).

``app.py``'s main server (uvicorn 0.23.2) installs its SIGTERM and SIGINT
handlers with ``loop.add_signal_handler``, which points the process's
signal wakeup fd at the event loop's self-pipe. A child forked without
exec inherits that fd, and CPython keeps it across ``fork()``. When a
signal trips a Python-level handler in the child, CPython's C handler
writes the signal number to the inherited fd, the PARENT's loop reads it,
and the parent runs its own handler for a signal it never received.

On JP7 that is every vLLM engine shutdown: vLLM forks its EngineCore, the
EngineCore installs Python SIGTERM/SIGINT handlers, and the engine's
shutdown sends it SIGTERM, so the backend shut down gracefully (exit 0)
and docker restarted it (jetson-thor1, 2026-10-02, deployment cecbee46).
Reproduced in the JP7 image (Python 3.11.16); Python 3.12+ does not show it.

:func:`install` registers one ``os.register_at_fork(after_in_child=...)``
hook that detaches the inherited wakeup fd in every forked child. The
forking thread is the child's main thread, so ``set_wakeup_fd`` is allowed
there. Fork plus exec (``subprocess``, Triton's stubs) needs nothing: exec
resets the signal state. The digital-input process already does the same
reset itself (``DigitalInputProcess.run``); this makes it the rule for every
fork child instead of a per-call-site convention.
"""
import os
import signal

_installed = False


def _detach_signal_wakeup_fd():
    """``after_in_child`` hook: stop the child from writing tripped
    signals into the parent's event-loop self-pipe."""
    try:
        signal.set_wakeup_fd(-1)
    except (ValueError, OSError):
        # Not the child's main thread, or no wakeup fd to detach: nothing
        # was inherited that could reach the parent.
        pass


def install():
    """Register the hook once per process. Idempotent; a no-op where
    ``os.register_at_fork`` does not exist."""
    global _installed
    if _installed or not hasattr(os, "register_at_fork"):
        return
    os.register_at_fork(after_in_child=_detach_signal_wakeup_fd)
    _installed = True


def installed():
    """Whether :func:`install` registered the hook in this process."""
    return _installed
