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
