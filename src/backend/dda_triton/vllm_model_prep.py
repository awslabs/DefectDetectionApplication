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
#
"""vLLM model preparation script (spec: vllm-triton-inference, design 10).

Seeded to /aws_dda by cp_model_conversion_files exactly like
model_convertor.py and invoked by the vLLM_Model_Component recipe:

    Startup:  python3 /aws_dda/vllm_model_prep.py
                  --unarchived_repo_path {artifacts:decompressedPath}/{repo}/
                  [--weights_path {artifacts:decompressedPath}/{weights}/]
                  --model_name {name} --component_name {component}
    Shutdown: python3 /aws_dda/vllm_model_prep.py --cleanup
                  --model_name {name} --component_name {component}

Startup sequence (Requirements 4.4, 4.5, 4.8, 4.9, 2.7):

1. Validate the unarchived Triton_vLLM_Repository: exactly
   ``{model_name}/1/model.json`` + ``{model_name}/config.pbtxt``,
   ``config.pbtxt`` declares ``backend: "vllm"``, ``model.json`` parses as
   a JSON object. Any defect -> exit non-zero naming the defect.
2. S3-sourced only (``--weights_path`` given): rewrite ``model.json``'s
   ``"model"`` reference (the ``./weights`` sentinel written cloud-side by
   packaging.py) to the absolute weights path, verifying the path exists
   and is readable BEFORE staging. If not: report the model FAILED (name +
   unresolved path), stage nothing, exit non-zero, never issue a load
   request.
3. Stage atomically into ``VLLM_MODEL_DIR/{model_name}`` (copy to a temp
   sibling inside VLLM_MODEL_DIR, then rename). No LocalServer restart.
4. Request the model load through the companion vLLM runtime's Triton
   model-control endpoint (POST /v2/repository/models/{m}/load), using the
   ``model_autostart_utils.wait_for_server`` backoff to tolerate
   LocalServer still booting (best-effort, mirroring
   model_convertor.start_model: the runtime carries LOADING/READY/FAILED
   through the device model-status mechanisms).

``--cleanup`` unloads the model via the model-control endpoint and removes
the staged directory, mirroring convert_model_cleanup.py.
"""
import argparse
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time

import requests
from dda_triton.model_autostart_utils import wait_for_server

# --- constants -------------------------------------------------------------

#: Root of every staged Triton_vLLM_Repository on the device. MUST match
#: vllm_runtime.constants.VLLM_MODEL_DIR (the companion runtime scans this
#: directory). Deliberately a sibling of TRITON_MODEL_DIR so the embedded
#: vision Triton never sees a backend "vllm" repository (Requirement 4.3).
VLLM_MODEL_DIR = "/aws_dda/dda_triton/vllm_model_repo"

#: The backend a repository's config.pbtxt must declare (Requirement 4.1).
VLLM_BACKEND_NAME = "vllm"

#: Loopback endpoint of the companion runtime's Triton generate-extension
#: server. Host is fixed by design; the port mirrors
#: vllm_runtime.constants (VLLM_RUNTIME_PORT env var, default 8901).
VLLM_RUNTIME_HOST = "127.0.0.1"
DEFAULT_VLLM_RUNTIME_PORT = 8901

#: The cloud-side model.json model-reference sentinel for S3-sourced
#: records (written by packaging.generate_vllm_repository).
WEIGHTS_SENTINEL = "./weights"

#: Repository-relative layout (Triton vLLM backend contract).
CONFIG_PBTXT_NAME = "config.pbtxt"
VERSION_DIR_NAME = "1"
MODEL_JSON_NAME = "model.json"

#: Matches the ``backend: "..."`` declaration in a config.pbtxt (same
#: grammar shortcut as vllm_runtime.repository).
_BACKEND_RE = re.compile(r'^\s*backend\s*:\s*"([^"]*)"', re.MULTILINE)

#: Prefix of temp staging siblings inside VLLM_MODEL_DIR; --cleanup also
#: sweeps leftovers carrying this prefix for its model.
_STAGING_PREFIX = ".staging-"

#: An engine load can pull/initialize a large model; the recipe's Startup
#: timeout is 1800s, stay inside it.
LOAD_REQUEST_TIMEOUT_SECONDS = 1500
UNLOAD_REQUEST_TIMEOUT_SECONDS = 300

logging.basicConfig(
    format="%(levelname)s:%(message)s",
    level=logging.DEBUG,
    handlers=[logging.StreamHandler(sys.stdout)],
)

parser = argparse.ArgumentParser(
    description="Stages a Triton vLLM model repository and requests its load "
    "through the companion vLLM runtime (or unstages it with --cleanup)."
)
parser.add_argument(
    "--unarchived_repo_path",
    help="Path where the Triton_vLLM_Repository archive is unarchived",
)
parser.add_argument(
    "--weights_path",
    help="Device-local decompressed LLM weights path (S3-sourced records only)",
)
parser.add_argument("--model_name", help="Model name")
parser.add_argument("--component_name", help="Greengrass component name (logging)")
parser.add_argument(
    "--cleanup",
    action="store_true",
    help="Unload the model and remove its staged repository directory",
)


def runtime_port() -> int:
    """Effective runtime port: the VLLM_RUNTIME_PORT environment variable
    when parseable, else the default (mirrors vllm_runtime.constants)."""
    try:
        return int(os.environ.get("VLLM_RUNTIME_PORT", DEFAULT_VLLM_RUNTIME_PORT))
    except ValueError:
        return DEFAULT_VLLM_RUNTIME_PORT


# --- 1. validation ----------------------------------------------------------


def validate_repository(unarchived_repo_path: str, model_name: str):
    """Validate the unarchived Triton_vLLM_Repository.

    Returns ``(defects, engine_args)``: the complete list of defect
    strings (each naming the defect and the offending path) and, when
    ``model.json`` parsed, its JSON object. Valid iff ``defects`` is
    empty. The layout must be exactly ``{model_name}/1/model.json`` +
    ``{model_name}/config.pbtxt`` (Requirement 4.1 / design 10 step 1).
    """
    defects = []
    engine_args = None

    if not unarchived_repo_path or not os.path.isdir(unarchived_repo_path):
        defects.append(
            "unarchived repository path does not exist or is not a directory: "
            "{}".format(unarchived_repo_path)
        )
        return defects, engine_args

    model_dir = os.path.join(unarchived_repo_path, model_name)
    if not os.path.isdir(model_dir):
        defects.append(
            "model repository directory '{}' is missing: {}".format(
                model_name, model_dir
            )
        )
        return defects, engine_args

    # Exactly config.pbtxt + 1/ inside the model directory ...
    entries = sorted(os.listdir(model_dir))
    unexpected = [e for e in entries if e not in (CONFIG_PBTXT_NAME, VERSION_DIR_NAME)]
    if unexpected:
        defects.append(
            "unexpected entries {} in model repository (expected exactly "
            "'{}' and '{}/'): {}".format(
                unexpected, CONFIG_PBTXT_NAME, VERSION_DIR_NAME, model_dir
            )
        )

    # ... config.pbtxt declaring backend: "vllm" ...
    config_path = os.path.join(model_dir, CONFIG_PBTXT_NAME)
    if not os.path.isfile(config_path):
        defects.append("missing config.pbtxt: {}".format(config_path))
    else:
        try:
            with open(config_path, encoding="utf-8") as f:
                match = _BACKEND_RE.search(f.read())
        except OSError as err:
            match = None
            defects.append(
                "unable to read config.pbtxt ({}): {}".format(err, config_path)
            )
        else:
            backend = match.group(1) if match else None
            if backend != VLLM_BACKEND_NAME:
                defects.append(
                    'config.pbtxt must declare backend: "{}" (found {!r}): '
                    "{}".format(VLLM_BACKEND_NAME, backend, config_path)
                )

    # ... and a 1/ version directory holding exactly model.json.
    version_dir = os.path.join(model_dir, VERSION_DIR_NAME)
    model_json_path = os.path.join(version_dir, MODEL_JSON_NAME)
    if not os.path.isdir(version_dir):
        defects.append("missing model version directory: {}".format(version_dir))
    else:
        version_entries = sorted(os.listdir(version_dir))
        unexpected = [e for e in version_entries if e != MODEL_JSON_NAME]
        if unexpected:
            defects.append(
                "unexpected entries {} in model version directory (expected "
                "exactly '{}'): {}".format(unexpected, MODEL_JSON_NAME, version_dir)
            )
        if not os.path.isfile(model_json_path):
            defects.append("missing 1/model.json: {}".format(model_json_path))
        else:
            try:
                with open(model_json_path, encoding="utf-8") as f:
                    parsed = json.load(f)
            except (ValueError, OSError, UnicodeDecodeError) as err:
                defects.append(
                    "model.json does not parse as JSON ({}): {}".format(
                        err, model_json_path
                    )
                )
            else:
                if not isinstance(parsed, dict):
                    defects.append(
                        "model.json must be a JSON object of engine arguments, "
                        "got {}: {}".format(type(parsed).__name__, model_json_path)
                    )
                else:
                    engine_args = parsed

    return defects, engine_args


# --- 2. S3 weights rewrite ---------------------------------------------------


def resolve_weights_path(weights_path: str):
    """The absolute weights path when it exists and is readable, else
    ``None`` (design 10 step 2: verified BEFORE staging; Requirement 4.9)."""
    absolute = os.path.abspath(weights_path)
    if os.path.exists(absolute) and os.access(absolute, os.R_OK):
        return absolute
    return None


def rewrite_model_reference(engine_args: dict, absolute_weights_path: str) -> dict:
    """A copy of ``engine_args`` with ONLY the ``model`` reference (the
    './weights' sentinel serialized cloud-side) rewritten to the absolute
    device-local weights path; every other key is unchanged
    (Requirement 4.5)."""
    rewritten = dict(engine_args)
    rewritten["model"] = absolute_weights_path
    return rewritten


# --- 3. atomic staging --------------------------------------------------------


def stage_repository(
    model_dir_src: str,
    model_name: str,
    rewritten_engine_args=None,
    model_repo_dir: str = VLLM_MODEL_DIR,
) -> str:
    """Stage the validated model directory into
    ``{model_repo_dir}/{model_name}`` atomically: copy into a temp sibling
    inside the repository, then rename (design 10 step 3; Requirement 4.4).
    No LocalServer restart is involved anywhere.

    ``rewritten_engine_args`` (S3-sourced records) replaces the staged
    ``1/model.json`` content; when ``None`` (HF-sourced) the repository is
    staged byte-identical to the source. Returns the staged directory.
    """
    os.makedirs(model_repo_dir, exist_ok=True)
    staging_root = tempfile.mkdtemp(
        prefix="{}{}-".format(_STAGING_PREFIX, model_name), dir=model_repo_dir
    )
    try:
        staged_tmp = os.path.join(staging_root, model_name)
        shutil.copytree(model_dir_src, staged_tmp)
        if rewritten_engine_args is not None:
            model_json_path = os.path.join(staged_tmp, VERSION_DIR_NAME, MODEL_JSON_NAME)
            with open(model_json_path, "w", encoding="utf-8") as f:
                json.dump(rewritten_engine_args, f, indent=2)
        final_dir = os.path.join(model_repo_dir, model_name)
        if os.path.isdir(final_dir):
            # Re-deploy of the same model: replace the previous staging.
            shutil.rmtree(final_dir)
        os.rename(staged_tmp, final_dir)
        return final_dir
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


# --- 4. model-control requests -----------------------------------------------


# Transient-connection retry schedule for the load request: during a
# Greengrass deployment the LocalServer backend container is recreated while
# the model components restart, so the first load attempt regularly lands on
# a backend that is mid-teardown (ConnectionResetError / RemoteDisconnected)
# or just gone (ConnectionError). One attempt used to be made and the failure
# swallowed (exit 0), leaving the component "healthy" with the model staged
# but never loaded until a manual restart. Retrying with backoff, re-checking
# reachability before each attempt, makes deployments self-heal.
LOAD_RETRY_BACKOFF_SECONDS = (3, 6, 12, 24, 48)

#: request_load classifications (spec: edge-deploy-reliability, Defect D).
#: LOAD_OK: HTTP 200 received. LOAD_HTTP_ERROR: an authoritative non-200
#: HTTP response was received (single-attempt semantics — never retried).
#: LOAD_UNREACHABLE: every attempt ended in a wait_for_server failure or a
#: connection-level requests.RequestException with no HTTP response ever
#: received (isBugCondition_D — the runtime was never reachable).
LOAD_OK = "LOAD_OK"
LOAD_HTTP_ERROR = "LOAD_HTTP_ERROR"
LOAD_UNREACHABLE = "LOAD_UNREACHABLE"

#: Markers in an extracted load-failure reason indicating vLLM could not
#: reserve KV-cache blocks (weights already exceed the configured GPU
#: memory fraction) — triggers the actionable remediation hint
#: (spec: vllm-sizing-and-packaging-errors, Requirement 4.2).
KV_CACHE_HINT_MARKERS = (
    "No available memory for the cache blocks",
    "gpu_memory_utilization",
)


def extract_load_failure_reason(body_text: str) -> str:
    """The human-readable reason inside a Triton model-control error body.

    Triton returns ``{"error": "..."}``; the ``error`` text is returned
    when the body is a JSON object carrying a non-empty ``error`` field,
    otherwise the raw body text (stripped) is the reason
    (spec: vllm-sizing-and-packaging-errors, Requirements 4.1, 4.3).
    """
    try:
        parsed = json.loads(body_text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and parsed.get("error"):
        return str(parsed["error"])
    return body_text.strip()


def log_load_failure(model_name: str, status_code, body_text: str, engine_args=None):
    """One prominent ERROR line for an authoritative load failure: model
    name, HTTP status, and the extracted reason; the KV-cache remediation
    is appended when the reason matches a marker, and the staged
    ``gpu_memory_utilization`` / ``max_model_len`` values are included
    when the engine args are available (spec:
    vllm-sizing-and-packaging-errors, Requirements 4.1, 4.2, 4.3, 4.4).
    The raw body stays available at debug level for triage."""
    reason = extract_load_failure_reason(body_text)
    line = "VllmLoadModel: model '{}' FAILED to load (HTTP {}): {}".format(
        model_name, status_code, reason
    )
    if any(marker in reason for marker in KV_CACHE_HINT_MARKERS):
        line += (
            " | Remediation: the model's weights leave no GPU memory for "
            "vLLM KV-cache blocks inside the configured "
            "'gpu_memory_utilization' fraction — RAISE "
            "'gpu_memory_utilization' in the model's engine configuration, "
            "reduce 'max_model_len', or deploy a smaller model, then "
            "re-package and re-publish."
        )
    if isinstance(engine_args, dict):
        line += " | staged engine args: gpu_memory_utilization={}, max_model_len={}".format(
            engine_args.get("gpu_memory_utilization"),
            engine_args.get("max_model_len"),
        )
    logging.error(line)
    logging.debug("VllmLoadModel: raw load-failure response body: {}".format(body_text))


def request_load(model_name: str, engine_args=None) -> str:
    """Request the companion runtime to load the staged model through the
    Triton model-control endpoint (Requirement 4.8). Retries transient
    connection failures with backoff (deployment races recreate the backend
    under us); an HTTP error response (e.g. 409 FAILED) is authoritative and
    is not retried — the runtime reports LOADING/READY/FAILED through the
    device model-status mechanisms — with ONE exception: a KV-cache
    out-of-memory failure triggers a single unload -> reload recovery
    cycle (see below).

    KV-cache OOM recovery (validated on-device, ryan-orin-nano/JP6): the
    first load after a runtime restart can fail with "No available memory
    for the cache blocks" because the failed attempt itself leaves its GPU
    allocations pinned in the runtime process; an unload releases them and
    the immediately following load succeeds. When the authoritative
    failure reason matches the KV-cache markers, that unload -> reload
    cycle runs exactly once; if the retry fails too, the failure is
    authoritative (a genuinely oversized model still fails fast with the
    sizing remediation hint). Every other HTTP error keeps the
    single-attempt semantics.

    Returns a classification: ``LOAD_OK`` (HTTP 200), ``LOAD_HTTP_ERROR``
    (authoritative non-200 HTTP response received), or ``LOAD_UNREACHABLE``
    (no HTTP response ever received — every attempt died at the connection
    level or the server was never reachable)."""
    outcome, reason = _request_load_attempt(model_name, engine_args)
    if (
        outcome == LOAD_HTTP_ERROR
        and reason is not None
        and any(marker in reason for marker in KV_CACHE_HINT_MARKERS)
    ):
        logging.warning(
            "VllmLoadModel: load of '{}' hit a KV-cache out-of-memory "
            "failure; attempting the validated unload -> reload recovery "
            "(a failed load can leave its GPU allocations pinned in the "
            "runtime)".format(model_name)
        )
        request_unload(model_name)
        outcome, _ = _request_load_attempt(model_name, engine_args)
    return outcome


def _request_load_attempt(model_name: str, engine_args=None):
    """One load-request cycle (connection-failure retries included).
    Returns ``(classification, failure_reason)`` where ``failure_reason``
    is the extracted reason of an authoritative HTTP failure, else None."""
    port = runtime_port()
    url = "http://{}:{}/v2/repository/models/{}/load".format(
        VLLM_RUNTIME_HOST, port, model_name
    )
    attempts = len(LOAD_RETRY_BACKOFF_SECONDS) + 1
    got_http_response = False
    for attempt in range(1, attempts + 1):
        if not wait_for_server(VLLM_RUNTIME_HOST, port, "VllmLoadModel"):
            logging.error(
                "VllmLoadModel: vLLM runtime {}:{} is not reachable; model "
                "'{}' stays staged for the next LocalServer start".format(
                    VLLM_RUNTIME_HOST, port, model_name
                )
            )
            return LOAD_UNREACHABLE, None
        logging.info(
            "VllmLoadModel: requesting load of '{}' (attempt {}/{})".format(
                model_name, attempt, attempts
            )
        )
        try:
            response = requests.post(url, timeout=LOAD_REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as err:
            logging.error("VllmLoadModel: load request failed: {}".format(err))
            if attempt <= len(LOAD_RETRY_BACKOFF_SECONDS):
                delay = LOAD_RETRY_BACKOFF_SECONDS[attempt - 1]
                logging.info(
                    "VllmLoadModel: retrying in {} seconds (backend may be "
                    "restarting mid-deployment)...".format(delay)
                )
                time.sleep(delay)
                continue
            return (LOAD_HTTP_ERROR if got_http_response
                    else LOAD_UNREACHABLE), None
        got_http_response = True
        if response.status_code == 200:
            logging.info("Model '{}' loaded successfully!".format(model_name))
            return LOAD_OK, None
        log_load_failure(model_name, response.status_code, response.text, engine_args)
        return LOAD_HTTP_ERROR, extract_load_failure_reason(response.text)
    return (LOAD_HTTP_ERROR if got_http_response else LOAD_UNREACHABLE), None


def request_unload(model_name: str) -> bool:
    """Request the companion runtime to unload the model (idempotent on
    the runtime side). Best-effort, mirroring convert_model_cleanup."""
    port = runtime_port()
    if not wait_for_server(VLLM_RUNTIME_HOST, port, "VllmUnloadModel"):
        logging.info(
            "VllmUnloadModel: vLLM runtime {}:{} is not reachable".format(
                VLLM_RUNTIME_HOST, port
            )
        )
        return False
    url = "http://{}:{}/v2/repository/models/{}/unload".format(
        VLLM_RUNTIME_HOST, port, model_name
    )
    try:
        response = requests.post(url, timeout=UNLOAD_REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as err:
        logging.error("VllmUnloadModel: unload request failed: {}".format(err))
        return False
    if response.status_code == 200:
        logging.info("Model '{}' unloaded successfully!".format(model_name))
        return True
    logging.error(
        "VllmUnloadModel: Request failed with status code: {}".format(
            response.status_code
        )
    )
    logging.error(response.text)
    return False


# --- entry points -------------------------------------------------------------


def prepare(args) -> int:
    """Startup path: validate -> (rewrite) -> stage -> request load.
    Returns the process exit code."""
    model_name = args.model_name
    component = args.component_name or ""
    logging.info(
        "Preparing vLLM model '{}' (component '{}') from '{}'".format(
            model_name, component, args.unarchived_repo_path
        )
    )

    defects, engine_args = validate_repository(args.unarchived_repo_path, model_name)
    if defects:
        for defect in defects:
            logging.error(
                "vLLM repository validation defect for model '{}': {}".format(
                    model_name, defect
                )
            )
        return 1

    rewritten_engine_args = None
    if args.weights_path:
        absolute = resolve_weights_path(args.weights_path)
        if absolute is None:
            # Requirement 4.9: FAILED report naming the model and the
            # unresolved path; nothing staged, no load request ever issued.
            logging.error(
                "Model '{}' FAILED: weights path does not exist or is not "
                "readable: {}".format(model_name, os.path.abspath(args.weights_path))
            )
            return 1
        if engine_args.get("model") != WEIGHTS_SENTINEL:
            logging.warning(
                "model.json 'model' is {!r}, not the expected '{}' sentinel; "
                "rewriting to the weights path anyway".format(
                    engine_args.get("model"), WEIGHTS_SENTINEL
                )
            )
        rewritten_engine_args = rewrite_model_reference(engine_args, absolute)
        logging.info(
            "Rewrote model reference of '{}' to '{}'".format(model_name, absolute)
        )

    model_dir_src = os.path.join(args.unarchived_repo_path, model_name)
    try:
        staged_dir = stage_repository(model_dir_src, model_name, rewritten_engine_args)
    except OSError as err:
        logging.error(
            "Unable to stage vLLM model '{}' into {}: {}".format(
                model_name, VLLM_MODEL_DIR, err
            )
        )
        return 1
    logging.info("Staged vLLM model '{}' at '{}'".format(model_name, staged_dir))

    # Exit non-zero when the load could not be delivered (after retries):
    # Greengrass then fails the Startup script and restarts the component,
    # re-driving the load — matching the vision-model path's behavior. An
    # HTTP-level FAILED from the runtime also exits non-zero so the component
    # state reflects reality instead of "healthy but never loaded". A
    # never-reachable runtime (isBugCondition_D — no HTTP response ever
    # received) gets an actionable diagnostic naming the likely cause: the
    # LocalServer backend container left stopped by a deployment restart
    # (spec: edge-deploy-reliability, Defect D, Requirement 2.10).
    # The staged engine args (rewritten for S3-sourced records, verbatim
    # otherwise) travel into the load path so an authoritative HTTP failure
    # logs the active gpu_memory_utilization / max_model_len (spec:
    # vllm-sizing-and-packaging-errors, Requirement 4.4).
    staged_engine_args = (
        rewritten_engine_args if rewritten_engine_args is not None else engine_args
    )
    outcome = request_load(model_name, staged_engine_args)
    if outcome == LOAD_UNREACHABLE:
        logging.error(
            "Model '{}' staged, but the vLLM runtime at {}:{} was never "
            "reachable across the full retry window (~70s of connection "
            "failures). Likely cause: the LocalServer backend container "
            "(image 'flask-app') is not running — a deployment restart can "
            "leave it stopped. Verify with:\n"
            "  sudo docker ps -a --filter ancestor=flask-app   "
            "(look for Exited)\n"
            "  sudo docker logs <container-id>\n"
            "and check the LocalServer component log "
            "(/greengrass/v2/logs/aws.edgeml.dda.LocalServer.*.log). "
            "Exiting non-zero so the component retries once the backend "
            "is back.".format(model_name, VLLM_RUNTIME_HOST, runtime_port())
        )
        return 1
    if outcome != LOAD_OK:
        logging.error(
            "Model '{}' staged but the load request did not succeed; "
            "exiting non-zero so the component retries".format(model_name)
        )
        return 1
    return 0


def cleanup(args) -> int:
    """Shutdown path (--cleanup): unload, then remove the staged directory
    and any leftover staging temp siblings (mirrors convert_model_cleanup).
    Returns the process exit code."""
    model_name = args.model_name
    try:
        request_unload(model_name)
        staged_dir = os.path.join(VLLM_MODEL_DIR, model_name)
        if os.path.exists(staged_dir) and os.path.isdir(staged_dir):
            shutil.rmtree(staged_dir)
            logging.info("Cleaned directory: {}".format(staged_dir))
        if os.path.isdir(VLLM_MODEL_DIR):
            leftover_prefix = "{}{}-".format(_STAGING_PREFIX, model_name)
            for entry in os.listdir(VLLM_MODEL_DIR):
                if entry.startswith(leftover_prefix):
                    shutil.rmtree(
                        os.path.join(VLLM_MODEL_DIR, entry), ignore_errors=True
                    )
                    logging.info("Cleaned leftover staging directory: {}".format(entry))
        logging.info("Directory cleanup finished")
        return 0
    except Exception as e:
        logging.error("Exception occurred while cleaning vLLM model dir: {}".format(e))
        return 1


def main() -> int:
    try:
        args = parser.parse_args()
        if not args.model_name:
            logging.error("Args not provided: --model_name is required")
            return 1
        if args.cleanup:
            return cleanup(args)
        if not args.unarchived_repo_path:
            logging.error("Args not provided: --unarchived_repo_path is required")
            return 1
        return prepare(args)
    except Exception as e:
        logging.error(
            "Exception occurred while preparing vLLM model: {}".format(e)
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
