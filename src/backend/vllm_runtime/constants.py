# Copyright 2025 Amazon Web Services, Inc.
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
"""Constants for the companion vLLM runtime (Requirement 4.1).

``VLLM_MODEL_DIR`` is deliberately a *sibling* of the embedded vision
Triton's ``TRITON_MODEL_DIR`` (``/aws_dda/dda_triton/triton_model_repo``,
see ``dda_triton.constants``), never the same directory: the embedded
Triton scans its own repository and must never see a ``backend: "vllm"``
model, and the vLLM runtime must never touch a vision model. Keeping the
two runtimes on disjoint directories is the strongest backward
compatibility guarantee in the design (Requirements 4.3, 8.8).

``VLLM_RUNTIME_PORT`` is the loopback TCP port of the runtime's Triton
generate-extension HTTP server (design section 9; Requirement 5.2). The
default avoids every port LocalServer already listens on (5000 plaintext,
5443 TLS — see ``app.py``) and the conventional Triton trio (8000-8002)
so a real Triton could later coexist. It is overridable through the
``VLLM_RUNTIME_PORT`` environment variable.
"""
import os

#: Root of every staged Triton_vLLM_Repository on the device. Each model
#: lives at ``{VLLM_MODEL_DIR}/{model_name}/`` with ``config.pbtxt`` and
#: ``1/model.json`` (the Triton vLLM backend repository layout).
VLLM_MODEL_DIR = "/aws_dda/dda_triton/vllm_model_repo"

#: The backend name a staged repository's config.pbtxt must declare.
VLLM_BACKEND_NAME = "vllm"

#: Loopback host the runtime HTTP server binds. Never anything but
#: 127.0.0.1: the generate interface is a device-internal contract for
#: the Text_Generation_API and vllm_model_prep.py, not a LAN service.
VLLM_RUNTIME_HOST = "127.0.0.1"

#: Default TCP port of the runtime HTTP server (see the module
#: docstring for the choice rationale).
DEFAULT_VLLM_RUNTIME_PORT = 8901

#: Effective port: the ``VLLM_RUNTIME_PORT`` environment variable when
#: set (and parseable as an integer), else the default.
try:
    VLLM_RUNTIME_PORT = int(
        os.environ.get("VLLM_RUNTIME_PORT", DEFAULT_VLLM_RUNTIME_PORT)
    )
except ValueError:
    VLLM_RUNTIME_PORT = DEFAULT_VLLM_RUNTIME_PORT

#: Unload_Tombstone marker filename (spec
#: vllm-model-reload-after-backend-restart, Requirements 2.4, 3.5).
#: An explicit unload writes ``{VLLM_MODEL_DIR}/{model_name}/{marker}``
#: so the post-restart reconciler never re-drives a load the operator
#: deliberately stopped. The marker clears on re-stage for free: the
#: component Startup's atomic directory replace in vllm_model_prep.py
#: (``shutil.rmtree`` + ``os.rename`` swaps the whole model directory)
#: removes the marker together with the old directory — zero
#: ``vllm_model_prep.py`` changes needed. An explicit load also clears
#: it (re-arming reconciliation). One shared constant for the writer
#: (the manager's ``unload()``) and any reader (the reconciler).
UNLOAD_TOMBSTONE_NAME = ".dda_explicit_unload"

# --- Construction_Watchdog (spec vllm-jp7-engine-lifecycle, Defect B) -----
#
# An engine construction (``AsyncLLMEngine.from_engine_args``) runs on the
# runtime server's event loop and blocks it. Without a bound, a construction
# that never returns leaves the model LOADING and the runtime answering
# nothing until the model component goes BROKEN and the deployment rolls
# back (jetson-thor1, 2026-10-01, deployment e8c4694a: 1 h 45 min).
#
# Measured legitimate constructions (2026-10-01/02): qwen3-vl-8b-instruct on
# jetson-thor1 (JP7, vLLM 0.11 V1) 144 s with a cold compile cache (the cache
# lives in the container layer, so every deployment is cold) and 72 s warm;
# qwen2.5-vl-7b-instruct-awq on the Orin (JP6, vLLM 0.9.3 V0) 159-164 s.
#
# Two triggers, whichever comes first (the owner asked for faster than the
# 900 s first proposed):
# - the STALL check: no CPU time and no I/O by the constructing thread and
#   the engine-core process tree over the stall window. A legitimate
#   construction keeps burning CPU (weight loading, torch.compile, CUDA
#   graph capture, profiling); a deadlocked engine core (H1), a lost
#   handshake (H3) or a backend blocked before the fork (H2) does not.
# - the hard Construction_Bound, for a construction that keeps busy without
#   finishing. 600 s is about 3.7x the slowest measured construction.

#: Hard bound on one engine construction, seconds. 0 disables it.
DEFAULT_ENGINE_CONSTRUCTION_TIMEOUT_S = 600.0
ENGINE_CONSTRUCTION_TIMEOUT_ENV = "VLLM_ENGINE_CONSTRUCTION_TIMEOUT_S"

#: Stall window, seconds: the construction fails when it made no progress
#: (see the two thresholds below) over this long. 0 disables the check.
DEFAULT_ENGINE_STALL_WINDOW_S = 120.0
ENGINE_STALL_WINDOW_ENV = "VLLM_ENGINE_STALL_WINDOW_S"

#: How long the construction may take to return after the watchdog fired
#: and stopped its engine core (vLLM notices a dead engine core at once:
#: its startup wait polls the process sentinels). After that the runtime
#: is treated as unrecoverable in this backend life (Decision 3).
DEFAULT_ENGINE_UNBLOCK_GRACE_S = 30.0
ENGINE_UNBLOCK_GRACE_ENV = "VLLM_ENGINE_UNBLOCK_GRACE_S"

#: Progress below BOTH of these over the stall window is a stall.
ENGINE_STALL_CPU_SECONDS = 1.0
ENGINE_STALL_IO_BYTES = 1024 * 1024

#: Watchdog sampling period, seconds.
ENGINE_WATCHDOG_SAMPLE_PERIOD_S = 5.0

#: Construction diagnostics files kept on the device (oldest deleted).
ENGINE_DIAGNOSTICS_KEEP = 5

#: Hang_Marker filename (Decision 3, option (a)): written into a staged
#: repository when a construction could not be unblocked, right before the
#: backend restarts itself. The repository then reports FAILED with the
#: recorded reason, so the post-restart reconciler does not re-drive it (no
#: restart loop). An explicit load clears it, like the Unload_Tombstone, and
#: the component's atomic re-stage removes it with the old directory.
CONSTRUCTION_HANG_MARKER_NAME = ".dda_construction_hang"


def _env_seconds(name, default):
    """A non-negative float from the environment, else ``default``."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value != value or value < 0:  # NaN or negative
        return default
    return value


def engine_construction_timeout_s():
    """Effective Construction_Bound (environment override, else default)."""
    return _env_seconds(ENGINE_CONSTRUCTION_TIMEOUT_ENV,
                        DEFAULT_ENGINE_CONSTRUCTION_TIMEOUT_S)


def engine_stall_window_s():
    """Effective stall window (environment override, else default)."""
    return _env_seconds(ENGINE_STALL_WINDOW_ENV, DEFAULT_ENGINE_STALL_WINDOW_S)


def engine_unblock_grace_s():
    """Effective Unblock_Grace (environment override, else default)."""
    return _env_seconds(ENGINE_UNBLOCK_GRACE_ENV, DEFAULT_ENGINE_UNBLOCK_GRACE_S)
