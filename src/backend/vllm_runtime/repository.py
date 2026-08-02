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
"""Pure parsing/validation of a staged Triton_vLLM_Repository.

A repository is one model's directory in the Triton vLLM backend layout
(Requirement 4.1):

.. code-block:: text

    {model_name}/
    ├── 1/
    │   └── model.json      # JSON object of vLLM AsyncEngineArgs
    └── config.pbtxt        # must declare backend: "vllm"

Everything here is filesystem-in/dict-out with no vLLM dependency, so the
validation rules are directly unit- and property-testable and shared by
the runtime manager and (later) the model preparation script.
"""
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Union

from vllm_runtime.constants import VLLM_BACKEND_NAME

#: Repository-relative path of the engine-args file.
MODEL_JSON_RELATIVE_PATH = "1/model.json"

#: Repository-relative path of the Triton model configuration.
CONFIG_PBTXT_RELATIVE_PATH = "config.pbtxt"

#: Matches the ``backend: "..."`` declaration in a config.pbtxt. The
#: pbtxt grammar for this scalar field is simple enough that a full
#: protobuf-text parser is not warranted here.
_BACKEND_RE = re.compile(r'^\s*backend\s*:\s*"([^"]*)"', re.MULTILINE)


class RepositoryValidationError(Exception):
    """A staged repository does not satisfy the Triton vLLM backend
    contract. The message names the defect and the offending path."""


def parse_backend(config_pbtxt_text: str) -> Optional[str]:
    """The ``backend`` declared by a config.pbtxt, or ``None`` when the
    file declares no backend."""
    match = _BACKEND_RE.search(config_pbtxt_text)
    return match.group(1) if match else None


def parse_repository(repository_dir: Union[str, Path]) -> Dict[str, Any]:
    """Validate a staged Triton_vLLM_Repository and return its engine
    arguments (the parsed ``1/model.json`` object).

    Raises :class:`RepositoryValidationError` naming the defect when the
    directory is missing, ``config.pbtxt`` is missing or does not declare
    ``backend: "vllm"``, or ``1/model.json`` is missing or is not a JSON
    object (Requirement 4.1).
    """
    repo = Path(repository_dir)
    if not repo.is_dir():
        raise RepositoryValidationError(
            "model repository directory does not exist: {}".format(repo)
        )

    config_path = repo / CONFIG_PBTXT_RELATIVE_PATH
    if not config_path.is_file():
        raise RepositoryValidationError(
            "missing config.pbtxt: {}".format(config_path)
        )
    backend = parse_backend(config_path.read_text())
    if backend != VLLM_BACKEND_NAME:
        raise RepositoryValidationError(
            'config.pbtxt must declare backend: "{}" (found {!r}): {}'.format(
                VLLM_BACKEND_NAME, backend, config_path
            )
        )

    model_json_path = repo / MODEL_JSON_RELATIVE_PATH
    if not model_json_path.is_file():
        raise RepositoryValidationError(
            "missing 1/model.json: {}".format(model_json_path)
        )
    try:
        engine_args = json.loads(model_json_path.read_text())
    except (ValueError, UnicodeDecodeError) as err:
        raise RepositoryValidationError(
            "model.json does not parse as JSON ({}): {}".format(
                err, model_json_path
            )
        ) from err
    if not isinstance(engine_args, dict):
        raise RepositoryValidationError(
            "model.json must be a JSON object of engine arguments, "
            "got {}: {}".format(type(engine_args).__name__, model_json_path)
        )
    return engine_args
