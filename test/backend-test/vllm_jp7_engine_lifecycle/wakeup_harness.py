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
"""Subprocess harness for Defect A (spec vllm-jp7-engine-lifecycle).

Shaped like the backend and like ``~/rtsp-verify/wakeup_fd_fork_repro.py``:
the main thread runs an asyncio loop with a handler registered through
``loop.add_signal_handler`` (what uvicorn 0.23.2 does in ``app.py``), and a
NON-main thread forks a ``multiprocessing`` child (what vLLM's engine-core
launch does from the runtime server's thread). Prints one JSON line.

Usage: ``python wakeup_harness.py <hook> <signal-name> <child> <target>``

- hook: ``app`` installs what ``app.py`` installs (``utils.fork_signal_hygiene``,
  when the module exists: on the unfixed tree it does not), ``none``
  installs nothing (the control).
- child: ``handler`` (a Python handler that reports through a pipe and keeps
  running, like vLLM's engine core) or ``default`` (``SIG_DFL``, like the
  digital-input process).
- target: ``child`` signals only the child, ``self`` signals the parent.
"""
import asyncio
import json
import multiprocessing
import os
import signal
import sys
import threading
import time


def _child_main(signum, child_mode, write_fd):
    if child_mode == "default":
        signal.signal(signum, signal.SIG_DFL)
    else:
        def _handler(_signum, _frame):
            os.write(write_fd, b"h")
        signal.signal(signum, _handler)
    os.write(write_fd, b"r")  # ready
    while True:
        time.sleep(0.05)


def main():
    hook_mode, signal_name, child_mode, target = sys.argv[1:5]
    signum = getattr(signal, signal_name)
    hook_installed = False
    if hook_mode == "app":
        try:
            from utils import fork_signal_hygiene
        except ImportError:
            fork_signal_hygiene = None
        if fork_signal_hygiene is not None:
            fork_signal_hygiene.install()
            hook_installed = True

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    fired = []
    loop.add_signal_handler(signum, lambda: fired.append(time.monotonic()))

    read_fd, write_fd = os.pipe()
    holder = {}

    def _fork_from_thread():
        context = multiprocessing.get_context("fork")
        process = context.Process(target=_child_main,
                                  args=(signum, child_mode, write_fd), daemon=True)
        process.start()
        holder["process"] = process

    thread = threading.Thread(target=_fork_from_thread, name="vllm-runtime-http")
    thread.start()
    thread.join()
    process = holder["process"]

    def _read_byte(timeout):
        deadline = time.monotonic() + timeout
        os.set_blocking(read_fd, False)
        while time.monotonic() < deadline:
            try:
                data = os.read(read_fd, 1)
                if data:
                    return data
            except BlockingIOError:
                pass
            loop.run_until_complete(asyncio.sleep(0.02))
        return b""

    ready = _read_byte(5.0) == b"r"
    if target == "self":
        os.kill(os.getpid(), signum)
    else:
        os.kill(process.pid, signum)
    child_handled = False
    if target == "child" and child_mode == "handler":
        child_handled = _read_byte(3.0) == b"h"
    # Give the parent loop the chance to run a (wrong) handler.
    loop.run_until_complete(asyncio.sleep(0.5))
    parent_fired = bool(fired)
    child_alive = process.is_alive()
    process.join(0.5 if child_mode == "default" else 0.0)
    child_exitcode = process.exitcode
    if process.is_alive():
        os.kill(process.pid, signal.SIGKILL)
        process.join(5)
    print(json.dumps({
        "python": sys.version.split()[0],
        "hook_installed": hook_installed,
        "ready": ready,
        "child_handled": child_handled,
        "child_alive_after_signal": child_alive,
        "child_exitcode": child_exitcode,
        "parent_fired": parent_fired,
    }), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
