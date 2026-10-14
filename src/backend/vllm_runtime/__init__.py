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
"""Companion vLLM runtime for the vllm-triton-inference feature.

The :class:`~vllm_runtime.manager.VllmRuntimeManager` owns every vLLM
model on the device: it parses staged Triton_vLLM_Repositories under
:data:`~vllm_runtime.constants.VLLM_MODEL_DIR`, creates one
``AsyncLLMEngine`` per model, tracks the per-model state machine
``STAGED -> LOADING -> READY | FAILED(reason)`` (``UNKNOWN`` for
never-staged names), and serves ``generate`` / ``generate_stream``
(Requirements 4.1, 4.6, 4.7, 8.8, 8.9).

:mod:`vllm_runtime.server` wraps the manager in a loopback-only HTTP
server exposing the Triton generate-extension and model-control contract
(Requirements 4.8, 5.2); it is imported lazily here so this package's
core stays importable even without FastAPI/uvicorn.

This package imports cleanly without the ``vllm`` wheel: vLLM is only
imported lazily inside the default engine/sampling-params factories, both
of which are injectable for tests and non-GPU environments.
"""
from vllm_runtime.constants import (
    DEFAULT_VLLM_RUNTIME_PORT,
    VLLM_BACKEND_NAME,
    VLLM_MODEL_DIR,
    VLLM_RUNTIME_HOST,
    VLLM_RUNTIME_PORT,
)
from vllm_runtime.manager import (
    UNKNOWN_STATUS,
    GenerationError,
    ModelState,
    ModelStatus,
    ModelUnavailableError,
    VllmRuntimeError,
    VllmRuntimeManager,
)
from vllm_runtime.repository import (
    CONFIG_PBTXT_RELATIVE_PATH,
    MODEL_JSON_RELATIVE_PATH,
    RepositoryValidationError,
    parse_backend,
    parse_repository,
)

__all__ = [
    "DEFAULT_VLLM_RUNTIME_PORT",
    "VLLM_BACKEND_NAME",
    "VLLM_MODEL_DIR",
    "VLLM_RUNTIME_HOST",
    "VLLM_RUNTIME_PORT",
    "UNKNOWN_STATUS",
    "GenerationError",
    "ModelState",
    "ModelStatus",
    "ModelUnavailableError",
    "VllmRuntimeError",
    "VllmRuntimeManager",
    "CONFIG_PBTXT_RELATIVE_PATH",
    "MODEL_JSON_RELATIVE_PATH",
    "RepositoryValidationError",
    "parse_backend",
    "parse_repository",
]
