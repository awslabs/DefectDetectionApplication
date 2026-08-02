#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""Process-wide shared Greengrass IPC client (DD-19576).

Why this exists
---------------
The backend talks to the Greengrass Nucleus over the local IPC socket
(``awsiot.greengrasscoreipc``). Several call sites historically opened a
*fresh* connection per call:

* ``utils.gg_utils`` connected and ``close()``d on every list/stop/restart
  operation (connect/close churn), and
* ``utils.feature_configs_utils.get_default_configs_lfv`` connected but never
  closed, leaving each per-model client to be finalized by the Python garbage
  collector at an arbitrary later time — on the GC thread, while the native
  event loop may still be tearing down continuations.

Both patterns drive a reference-counting bug in the bundled
``aws-c-event-stream`` native layer that aborts the **entire** process with:

    Fatal error condition occurred in .../event_stream_rpc_client.c:961:
    ref_count != 0 && "Continuation ref count has gone negative"

That abort (exit 255) took the LocalServer backend container down with no
recovery. ``local_auth.config`` already worked around it locally by caching a
single reader; this module makes that a first-class, process-wide primitive so
*every* IPC caller reuses one long-lived connection instead of churning
connections.

Usage
-----
    from utils.ipc_client import get_ipc_client
    client = get_ipc_client()          # lazily connects once, then reused
    op = client.new_list_components()
    ...

Callers MUST NOT call ``close()`` on the returned client — it is shared for
the lifetime of the process. If a connection is detected as broken, call
``reset_ipc_client()`` so the next ``get_ipc_client()`` establishes a fresh
one; ``call_with_ipc_retry`` wraps that recover-once behavior.
"""
import logging
import threading

import awsiot.greengrasscoreipc

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_client = None


def get_ipc_client():
    """Return the process-wide shared Greengrass IPC client, connecting it
    lazily on first use.

    Thread-safe: concurrent first callers coordinate on a lock so exactly one
    connection is created. The awsiot IPC client supports many concurrent
    operations (each ``new_*`` creates its own continuation), so a single
    shared client serves the whole process — which is the point: it removes
    the connect/close churn and GC-timed finalization that abort the process
    (see module docstring, DD-19576).
    """
    global _client
    client = _client
    if client is not None:
        return client
    with _lock:
        if _client is None:
            _client = awsiot.greengrasscoreipc.connect()
            logger.info("Created shared Greengrass IPC client")
        return _client


def reset_ipc_client():
    """Drop the cached client so the next ``get_ipc_client()`` reconnects.

    Recovers from a broken connection without reintroducing per-call
    connect/close churn. The previous client is intentionally not ``close()``d
    here: an orderly close of an already-broken connection is what trips the
    native abort this module exists to avoid, so we let it be reclaimed and
    simply establish a fresh connection on next use.
    """
    global _client
    with _lock:
        _client = None


def call_with_ipc_retry(operation):
    """Run ``operation(client)`` with the shared client, reconnecting once if
    the first attempt raises.

    A single transient IPC failure (e.g. a dropped socket after a Nucleus
    restart) resets the shared client and retries exactly once, so one bad
    connection neither wedges the caller nor requires per-call churn. The
    second failure propagates to the caller.
    """
    try:
        return operation(get_ipc_client())
    except Exception as first_error:  # noqa: BLE001 — reconnect on any IPC error
        logger.warning(
            "Shared IPC operation failed (%s); reconnecting and retrying once",
            first_error,
        )
        reset_ipc_client()
        return operation(get_ipc_client())
