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
   through the device model-status mechanisms). A refusal from the runtime's
   own device-memory preflight (reason prefixed ``preflight-refused:``) is
   classified as ``LOAD_PREFLIGHT_REFUSED``: it skips the KV-cache
   unload -> reload recovery and exits 0, because the verdict is produced
   before any allocation and a component retry cannot change it (spec:
   jp6-vllm-kv-cache-oom-regression, design Decision 4).

``--cleanup`` unloads the model via the model-control endpoint and removes
the staged directory, mirroring convert_model_cleanup.py.

EXIT-CODE CONTRACT (this is the COMPONENT's exit code, not the model's
state — the model's state always travels through the model-status
surfaces):

===========================  ====  ==========================================
classification               exit  why
===========================  ====  ==========================================
``LOAD_OK``                     0  loaded.
``LOAD_PREFLIGHT_REFUSED``      0  deterministic, pre-allocation refusal; a
                                   component retry cannot change it.
``LOAD_HTTP_ERROR``             0  the runtime answered authoritatively, so
                                   the model is FAILED-with-reason on the
                                   status surfaces and the in-backend
                                   reconciler owns the retries. Failing the
                                   COMPONENT here takes unrelated,
                                   co-deployed components and every workflow
                                   that HARD-depends on this one down with
                                   it (see below).
``LOAD_UNREACHABLE``            1  the runtime was never reachable: the
                                   component genuinely started before the
                                   backend was ready, and a component-level
                                   retry IS the correct recovery.
repository/weights defects      1  nothing was staged; the artifact or the
                                   arguments are wrong.
===========================  ====  ==========================================

``LOAD_HTTP_ERROR`` -> 0 is a DELIBERATE change of the mapping recorded in
bugfix.md 3.8 (spec: jp6-vllm-kv-cache-oom-regression, task 11 OUTCOME
block 18). Evidence: three consecutive load attempts failed on TRANSIENT DNS
(``Failed to resolve 'huggingface.co' ([Errno -3] Temporary failure in name
resolution)``) at 12:00:47Z / 12:02:09Z / 12:03:22Z, each logging ``Startup
script exited. {exitCode=1}``; after the third the component reached
``currentState=BROKEN``. Two deployed workflows
(``dda.workflow.0c7fe31a-...`` 7.0.0 and ``dda.workflow.1f0b4c0c-...``
9.0.0) HARD-depend on that component, so they were left stuck at
``INSTALLED`` and the whole core device went ``UNHEALTHY``: a transient
network fault inside ONE model's load took down unrelated workflows. A model
that cannot load is a MODEL failure and is reported as one; it is not a
COMPONENT failure. Do NOT "restore" exit 1 here.

STAGED-REPOSITORY OWNERSHIP (spec: jp6-vllm-kv-cache-oom-regression,
task 14 H11/H12; task 11 OUTCOME blocks 13 and 17-18). Every component that
passes the same ``--model_name`` stages the SAME device path
``{VLLM_MODEL_DIR}/{model_name}/``, and multiple portal model records
legitimately do. Two consequences were observed in production: a concurrent
double-stage 2 ms apart (so ordering alone can never make it safe), and a
NON-OWNING component's ``--cleanup`` unloading and deleting another
component's freshly-loaded model 0.6 s after its load succeeded. Therefore
the stage-or-cleanup critical section is serialised on an OS-level advisory
file lock keyed by the model name (:func:`stage_lock`), and a successful
stage records an owner marker inside the staged repository
(:data:`OWNER_MARKER_NAME`): a foreign stage is permitted but WARNED (the
newest deployment legitimately takes over), a foreign ``--cleanup`` is
REFUSED (both the unload POST and the directory removal are skipped) and
still exits 0. Both guards fail OPEN — a stuck lock or an unreadable marker
must never brick a device.
"""
import argparse
import contextlib
import datetime
import errno
import fcntl
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

#: Name of the owner marker written INSIDE a successfully staged repository
#: (spec: jp6-vllm-kv-cache-oom-regression, task 14 H11).
#:
#: Filename choice, documented because it must not collide with the Triton
#: vLLM backend's repository contract. That layout is exactly
#: ``config.pbtxt`` + ``1/model.json`` (see :func:`validate_repository` and
#: ``vllm_runtime.repository.parse_repository``), so a dot-prefixed
#: ``.dda_stage_owner.json`` can never shadow a Triton file, a version
#: directory (those are numeric) or a backend/config name. The runtime
#: IGNORES it by construction: ``parse_repository`` reads only the two
#: contract paths and never enumerates the repository, and
#: ``VllmRuntimeManager._repository_staged`` keys on
#: ``{model_name}/config.pbtxt`` alone. It is also written only into the
#: STAGED tree, never into the unarchived artifact, so
#: :func:`validate_repository`'s "unexpected entries" rule is untouched.
OWNER_MARKER_NAME = ".dda_stage_owner.json"

#: Directory holding the per-model advisory lock files. Deliberately a
#: SIBLING of VLLM_MODEL_DIR, not a child: the lock must survive
#: ``--cleanup`` deleting the staged tree (and must never appear inside the
#: repository root the runtime scans). See :func:`stage_lock_path`.
STAGE_LOCK_DIR_SUFFIX = ".locks"

#: Bounded blocking acquire for :func:`stage_lock`. The measured collision
#: window was 2 ms (two components staging the same path), so this is five
#: orders of magnitude more than the stage path needs; it is sized for the
#: ``--cleanup`` path, whose unload POST can take up to
#: UNLOAD_REQUEST_TIMEOUT_SECONDS. On timeout the lock is NOT taken and the
#: work proceeds anyway (fail-open): a stuck lock must not brick a device or
#: fail a Greengrass deployment.
STAGE_LOCK_TIMEOUT_SECONDS = 120

#: Poll interval of the bounded acquire (non-blocking flock + sleep, so the
#: timeout needs no signals and stays portable).
STAGE_LOCK_POLL_SECONDS = 0.05

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
parser.add_argument(
    "--component_name",
    help="Greengrass component name: logged, and recorded as the owner of "
    "the staged repository so a non-owning component's --cleanup is refused "
    "(optional; when absent or empty, ownership is not enforced at all and "
    "behaviour is exactly as before the marker existed)",
)
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


# --- 3b. staged-repository ownership (H11) and mutual exclusion (H12) --------
#
# Why this exists at all, so nobody removes it as ceremony (spec:
# jp6-vllm-kv-cache-oom-regression, task 14 H11/H12; task 11 OUTCOME blocks
# 13 and 17-18):
#
#   * ``--component_name`` used to be logging-only, and there was no guard on
#     ``{VLLM_MODEL_DIR}/{model_name}/`` at all: staging was last-write-wins
#     and ``--cleanup`` deleted the tree regardless of who staged it. Three
#     portal model records publish components that all stage
#     ``--model_name qwen2-5-vl-7b-instruct-awq``.
#   * Block 13: two components logged ``INFO:Preparing vLLM model ...`` at
#     08:53:59.239Z and ``INFO:Staged vLLM model ...`` at 08:53:59.241Z — 2 ms
#     apart, same directory. Ordering alone therefore cannot make the stage
#     safe; it needs MUTUAL EXCLUSION.
#   * Blocks 17-18: one component's load succeeded at 11:00:23.768Z; 0.6 s
#     later the OTHER component's Shutdown ran ``POST /unload`` 200 ->
#     ``unloaded successfully`` -> ``Cleaned directory: .../
#     qwen2-5-vl-7b-instruct-awq``. So the guard must cover ``--cleanup``'s
#     UNLOAD step as well as its directory removal.
#
# Both guards fail OPEN. A stuck lock, an unwritable lock directory, or a
# malformed marker degrades to today's behaviour with a WARNING; it never
# fails a deployment.


def stage_lock_path(model_name: str, model_repo_dir: str = None) -> str:
    """Absolute path of the advisory lock file for ``model_name``.

    Lives in ``{model_repo_dir}{STAGE_LOCK_DIR_SUFFIX}``, a SIBLING of the
    repository root: the lock file must survive ``--cleanup`` deleting the
    staged tree, and must not appear inside the directory the runtime scans
    for staged repositories.
    """
    root = VLLM_MODEL_DIR if model_repo_dir is None else model_repo_dir
    lock_dir = "{}{}".format(root.rstrip("/"), STAGE_LOCK_DIR_SUFFIX)
    return os.path.join(lock_dir, "{}.lock".format(model_name))


@contextlib.contextmanager
def stage_lock(model_name: str, model_repo_dir: str = None,
               timeout_seconds: float = STAGE_LOCK_TIMEOUT_SECONDS):
    """Serialise the stage-or-cleanup critical section for ``model_name``
    across PROCESSES.

    An OS-level advisory lock (``fcntl.flock``) on a lock file outside the
    staged tree is the only mechanism that works here: Greengrass spawns a
    separate ``python3`` process per lifecycle script, so a threading lock
    would be useless, and the lock has to outlive the tree that ``--cleanup``
    deletes.

    The acquire is BOUNDED (``timeout_seconds``, a named constant by default)
    and implemented as a non-blocking ``flock`` polled until the deadline, so
    it needs no signals. On timeout — or if the lock file cannot be created
    at all — a WARNING is logged and the body runs UNLOCKED (fail-open): a
    stuck lock must not brick a device or fail a Greengrass deployment.

    Yields ``True`` when the lock is held, ``False`` when it was not taken.
    """
    path = stage_lock_path(model_name, model_repo_dir)
    handle = None
    acquired = False
    try:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            handle = open(path, "a+")
        except OSError as err:
            logging.warning(
                "Stage lock for model '{}' is unavailable ({}: {}); "
                "proceeding WITHOUT mutual exclusion — a concurrent stage or "
                "cleanup by another component sharing this --model_name could "
                "interleave".format(model_name, path, err)
            )
            handle = None
        if handle is not None:
            deadline = time.monotonic() + max(float(timeout_seconds), 0.0)
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError as err:
                    if err.errno not in (errno.EACCES, errno.EAGAIN):
                        logging.warning(
                            "Stage lock for model '{}' could not be acquired "
                            "({}); proceeding WITHOUT mutual "
                            "exclusion".format(model_name, err)
                        )
                        break
                    if time.monotonic() >= deadline:
                        logging.warning(
                            "Stage lock for model '{}' was still held by "
                            "another process after the {:g}s "
                            "STAGE_LOCK_TIMEOUT_SECONDS budget; proceeding "
                            "WITHOUT mutual exclusion rather than failing the "
                            "deployment (fail-open)".format(
                                model_name, timeout_seconds
                            )
                        )
                        break
                    time.sleep(STAGE_LOCK_POLL_SECONDS)
        yield acquired
    finally:
        if handle is not None:
            if acquired:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                handle.close()
            except OSError:
                pass


def owner_marker_path(model_name: str, model_repo_dir: str = None) -> str:
    """Absolute path of the owner marker inside ``model_name``'s staged
    repository."""
    root = VLLM_MODEL_DIR if model_repo_dir is None else model_repo_dir
    return os.path.join(root, model_name, OWNER_MARKER_NAME)


def read_owner_marker(model_name: str, model_repo_dir: str = None):
    """The parsed owner marker of ``model_name``'s staged repository, or
    ``None`` when there is none.

    FAILS OPEN: an absent, unreadable, malformed or non-object marker yields
    ``None`` (with a WARNING for the malformed/unreadable cases) so a
    corrupt marker degrades to the pre-marker behaviour instead of crashing
    a deployment.
    """
    path = owner_marker_path(model_name, model_repo_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            marker = json.load(handle)
    except (ValueError, OSError, UnicodeDecodeError) as err:
        logging.warning(
            "Owner marker of staged vLLM model '{}' is unreadable or "
            "malformed ({}: {}); treating the staged repository as UNOWNED so "
            "this deployment is not blocked by a corrupt "
            "marker".format(model_name, path, err)
        )
        return None
    if not isinstance(marker, dict):
        logging.warning(
            "Owner marker of staged vLLM model '{}' is not a JSON object "
            "(got {}): {}; treating the staged repository as "
            "UNOWNED".format(model_name, type(marker).__name__, path)
        )
        return None
    return marker


def marker_owner(marker) -> str:
    """The component name recorded in ``marker``, or ``""`` when the marker
    is absent or records no usable owner (fail-open)."""
    if not isinstance(marker, dict):
        return ""
    owner = marker.get("component_name")
    return owner if isinstance(owner, str) else ""


def write_owner_marker(staged_dir: str, model_name: str, component: str,
                       source_path: str = None) -> bool:
    """Record ``component`` as the owner of the repository staged at
    ``staged_dir``.

    Written ATOMICALLY (temp sibling inside the staged directory +
    ``os.replace``) so a marker is never observed half-written. Returns
    whether it was written; a failure is a WARNING, never a failed
    deployment — the marker is a guard, not a prerequisite for serving.
    """
    path = os.path.join(staged_dir, OWNER_MARKER_NAME)
    marker = {
        "component_name": component,
        "model_name": model_name,
        "source_unarchived_path": source_path or "",
        "staged_at": datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "marker_version": 1,
    }
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=OWNER_MARKER_NAME + ".", dir=staged_dir
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(marker, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    except OSError as err:
        logging.warning(
            "Unable to write the owner marker for staged vLLM model '{}' "
            "({}: {}); the staged repository stays UNOWNED and another "
            "component sharing this --model_name could clean it "
            "up".format(model_name, path, err)
        )
        return False
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    logging.info(
        "Recorded component '{}' as owner of staged vLLM model '{}' "
        "({})".format(component, model_name, path)
    )
    return True


def warn_foreign_stage(model_name: str, owner: str, component: str) -> None:
    """The prominent WARNING for a stage by a component that is NOT the
    recorded owner. The stage itself is PERMITTED — the newest deployment
    legitimately takes the path over — but both component names and the
    shared ``--model_name`` are named so the collision is visible in the
    component log instead of being inferred from a config that silently
    changed (task 11 OUTCOME blocks 11 and 13)."""
    logging.warning(
        "STAGING COLLISION: component '{}' is staging vLLM model '{}', which "
        "is currently owned by a DIFFERENT component '{}'. Both components "
        "pass --model_name '{}' and therefore share ONE staged path '{}': "
        "whichever stages last wins, so this component's engine "
        "configuration now replaces the other's. Publish the two records "
        "under distinct model names, or remove the component that should no "
        "longer own this model.".format(
            component, model_name, owner, model_name,
            os.path.join(VLLM_MODEL_DIR, model_name),
        )
    )


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
#:
#: EXIT CODES: LOAD_OK -> 0, LOAD_HTTP_ERROR -> **0**, LOAD_UNREACHABLE -> 1.
#: LOAD_HTTP_ERROR moved from 1 to 0 deliberately (spec:
#: jp6-vllm-kv-cache-oom-regression, task 11 OUTCOME block 18) — a MODEL that
#: cannot load must not be able to mark the COMPONENT broken. See the
#: module docstring's exit-code contract for the evidence (three transient-DNS
#: load failures -> currentState=BROKEN -> two HARD-dependent workflows stuck
#: at INSTALLED -> core device UNHEALTHY). LOAD_UNREACHABLE keeps exit 1: the
#: runtime was never reachable, so the component genuinely started before the
#: backend was ready and a component-level retry IS the recovery.
LOAD_OK = "LOAD_OK"
LOAD_HTTP_ERROR = "LOAD_HTTP_ERROR"
LOAD_UNREACHABLE = "LOAD_UNREACHABLE"

#: LOAD_PREFLIGHT_REFUSED: the runtime's device-side memory preflight refused
#: the load BEFORE constructing an engine — no GPU memory was allocated, no
#: ~4 min profiling ran, and the refusal reason carries the measured available
#: memory, the computed requirement with every term, and the setting to change
#: (spec: jp6-vllm-kv-cache-oom-regression, design Decision 4).
#: This classification exits **0**: the verdict is deterministic and produced
#: before any allocation, so a Greengrass component retry cannot change it.
#: Exit 1 here is the mechanism of defect 1.9 — one mis-sized model takes the
#: whole deployment BROKEN → FAILED_ROLLBACK_COMPLETE and blocks every
#: unrelated change for that device. Nothing is hidden: the prominent ERROR
#: line below carries the full diagnostic and the runtime reports the model
#: FAILED with the same reason through the unchanged status surfaces.
#: ``LOAD_UNREACHABLE`` is the ONLY classification that still exits 1, because
#: it is the only one where a COMPONENT-level retry is the right recovery (the
#: component started before the backend was ready). ``LOAD_HTTP_ERROR`` now
#: exits 0 for the same reason this classification does — the model's failure
#: is reported as the MODEL's, and the in-backend reconciler owns its retries.
LOAD_PREFLIGHT_REFUSED = "LOAD_PREFLIGHT_REFUSED"

#: Stable prefix of a device-side memory-preflight refusal reason.
#: OWNER: ``src/backend/vllm_runtime/memory_budget.py``
#: (``memory_budget.PREFLIGHT_REFUSED_MARKER``). DUPLICATED here on purpose —
#: this script is seeded standalone to /aws_dda by cp_model_conversion_files
#: and runs outside the backend package, so it cannot import ``vllm_runtime``
#: in every context. A host test pins the two literals EQUAL
#: (test/backend-test/jp6_vllm_kv_cache_oom/): keep them in lockstep.
PREFLIGHT_REFUSED_MARKER = "preflight-refused:"

#: Markers in an extracted load-failure reason indicating vLLM could not
#: reserve KV-cache blocks (weights already exceed the configured GPU
#: memory fraction) — triggers the actionable remediation hint
#: (spec: vllm-sizing-and-packaging-errors, Requirement 4.2). UNCHANGED by
#: jp6-vllm-kv-cache-oom-regression: only the remediation TEXT changed, and
#: PREFLIGHT_REFUSED_MARKER is tested BEFORE these markers.
KV_CACHE_HINT_MARKERS = (
    "No available memory for the cache blocks",
    "gpu_memory_utilization",
)

#: Ceiling the remediation menu quotes for 'gpu_memory_utilization'.
#: MIRRORED from ``memory_budget.fraction_cap()`` /
#: ``edge-cv-portal/backend/functions/vllm_fit_check.py`` (the single source of
#: truth): (30 GiB total − 6 GiB measured as held by the co-resident ONNX GPU
#: models) / 30 GiB = 0.80 on ``arm64_jp6``, the incident device class. Quoted
#: as that architecture's figure, NOT as a measurement of the device this
#: script runs on — the prep reads no memory itself. The measured, quantified
#: cap comes from the runtime's own preflight, which carries it inside the
#: refusal reason (spec: jp6-vllm-kv-cache-oom-regression, Decision 3).
CO_TENANCY_FRACTION_CAP_JP6 = 0.80

#: Extracted reason of the most recent authoritative load failure in this
#: process. ``prepare`` restates a preflight refusal's full diagnostic in its
#: terminal ERROR line from here, rather than widening ``request_load``'s
#: return value — the string classification it returns is pinned by the device
#: preservation suites. Reset at the start of every ``request_load``.
_last_load_failure_reason = None


def last_load_failure_reason():
    """The extracted reason of the most recent authoritative load failure in
    this process, or ``None`` when no HTTP failure was seen (e.g. the load
    succeeded, or ``request_load`` was substituted in a test)."""
    return _last_load_failure_reason


def kv_cache_remediation_menu(engine_args=None) -> str:
    """Decision 3's ordered remediation menu for a KV-cache budget failure
    (spec: jp6-vllm-kv-cache-oom-regression, defect 1.3).

    The ORDER is the whole point, and it is the same wording contract the
    portal's ``vllm_fit_check`` messages use:

    1. the co-tenancy hazard first — this device shares ONE pool of unified
       memory with the co-resident ONNX GPU models and
       ``gpu_memory_utilization`` is a fraction of TOTAL memory, so a larger
       fraction takes memory those models are already using (success condition
       2.10 makes starving them a failure, so "raise the fraction" cannot lead);
    2. the remediations that reduce THIS model's own demand;
    3. raising the fraction last, bounded by the co-tenancy ceiling, and
       replaced by "unsafe here" once the staged fraction already meets it.

    Nothing here ever advises *lowering* ``gpu_memory_utilization`` as a cure
    for insufficient KV cache — that invariant (sibling spec Requirement 3.9)
    is kept in full for every failure mode.
    """
    utilization = None
    if isinstance(engine_args, dict):
        try:
            utilization = float(engine_args.get("gpu_memory_utilization"))
        except (TypeError, ValueError):
            utilization = None

    parts = [
        "Remediation (in this order): this device shares ONE pool of unified "
        "memory with the co-resident ONNX GPU models, and "
        "'gpu_memory_utilization' is a fraction of TOTAL device memory — so a "
        "larger fraction is taken from memory those models are already using.",
        "Reduce this model's own demand FIRST: (1) bound "
        "'limit_mm_per_prompt.image' in the model's engine configuration (the "
        "biggest single lever for a vision-language model — every extra image "
        "per prompt enlarges vLLM's activation/profiling peak); (2) reduce "
        "'max_model_len'; (3) choose a smaller or more quantized model; "
        "(4) free device memory by stopping unused model components.",
    ]
    if utilization is not None and utilization >= CO_TENANCY_FRACTION_CAP_JP6:
        parts.append(
            "Raising 'gpu_memory_utilization' is unsafe here: the staged {:g} "
            "already meets or exceeds the {:.2f} co-tenancy ceiling for this "
            "device class (30 GiB total minus ~6 GiB held by the co-resident "
            "models), so a larger fraction would come out of "
            "theirs.".format(utilization, CO_TENANCY_FRACTION_CAP_JP6)
        )
    else:
        parts.append(
            "ONLY THEN, and only while the fraction stays below the {:.2f} "
            "co-tenancy ceiling for this device class (30 GiB total minus "
            "~6 GiB held by the co-resident models), RAISE "
            "'gpu_memory_utilization' — in small steps, verifying after each "
            "one that the co-resident ONNX models still load on "
            "GPU.".format(CO_TENANCY_FRACTION_CAP_JP6)
        )
    parts.append(
        "Then re-package and re-publish the model; the portal's fit check "
        "sizes weights, the activation allowance (an ESTIMATE) and the "
        "KV-cache floor against the target architecture's budget."
    )
    return " ".join(parts)


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
    The raw body stays available at debug level for triage.

    The KV remediation is Decision 3's ordered menu (spec:
    jp6-vllm-kv-cache-oom-regression, defect 1.3) — the old "RAISE
    'gpu_memory_utilization'" advice led, which on this shared unified-memory
    device grows the model's claim on memory the co-resident ONNX GPU models
    hold. A refusal from the runtime's own memory preflight already carries
    that menu, composed from the device's MEASURED numbers, so no second and
    less informed copy is appended to it.
    """
    reason = extract_load_failure_reason(body_text)
    line = "VllmLoadModel: model '{}' FAILED to load (HTTP {}): {}".format(
        model_name, status_code, reason
    )
    if PREFLIGHT_REFUSED_MARKER in reason:
        pass
    elif any(marker in reason for marker in KV_CACHE_HINT_MARKERS):
        line += " | " + kv_cache_remediation_menu(engine_args)
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
    (authoritative non-200 HTTP response received), ``LOAD_UNREACHABLE``
    (no HTTP response ever received — every attempt died at the connection
    level or the server was never reachable), or ``LOAD_PREFLIGHT_REFUSED``
    (the runtime's device-side memory preflight refused the load before any
    allocation)."""
    global _last_load_failure_reason
    _last_load_failure_reason = None
    outcome, reason = _request_load_attempt(model_name, engine_args)
    _last_load_failure_reason = reason
    if outcome == LOAD_HTTP_ERROR and reason is not None \
            and PREFLIGHT_REFUSED_MARKER in reason:
        # Tested BEFORE KV_CACHE_HINT_MARKERS, deliberately: the preflight
        # diagnostic legitimately contains the string 'gpu_memory_utilization'
        # (it spells the device budget out as util x MemTotal), which would
        # otherwise trigger the unload -> reload recovery below for a load that
        # never allocated anything — an unload of a model that was never
        # constructed, followed by an identical, equally doomed second refusal.
        # The refusal is deterministic and pre-allocation: there is nothing to
        # release and nothing a retry could change (spec:
        # jp6-vllm-kv-cache-oom-regression, design Decision 4).
        logging.warning(
            "VllmLoadModel: load of '{}' was refused by the runtime's device "
            "memory preflight before any GPU allocation; skipping the KV-cache "
            "unload -> reload recovery (nothing was allocated, so there is "
            "nothing to reclaim and a retry would be refused "
            "identically)".format(model_name)
        )
        return LOAD_PREFLIGHT_REFUSED
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
        outcome, recovery_reason = _request_load_attempt(model_name, engine_args)
        _last_load_failure_reason = recovery_reason
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
    # The stage is serialised against every other component that shares this
    # --model_name, and the owner marker is written INSIDE the lock, so a
    # concurrent stage cannot interleave between the copy and the marker
    # (task 14 H12: two components were caught staging this exact path 2 ms
    # apart). The lock is held for the stage only — NOT across the load
    # request, which can take ~4 min of engine construction and must never
    # block another component's teardown.
    try:
        with stage_lock(model_name):
            existing_owner = marker_owner(read_owner_marker(model_name))
            if component and existing_owner and existing_owner != component:
                warn_foreign_stage(model_name, existing_owner, component)
            staged_dir = stage_repository(
                model_dir_src, model_name, rewritten_engine_args
            )
            if component:
                # Only an identified component can own the path; with
                # --component_name absent or empty nothing is recorded and
                # behaviour is exactly as before the marker existed.
                write_owner_marker(
                    staged_dir, model_name, component,
                    args.unarchived_repo_path,
                )
    except OSError as err:
        logging.error(
            "Unable to stage vLLM model '{}' into {}: {}".format(
                model_name, VLLM_MODEL_DIR, err
            )
        )
        return 1
    logging.info("Staged vLLM model '{}' at '{}'".format(model_name, staged_dir))

    # Exit non-zero ONLY when a COMPONENT-level retry is the right recovery.
    # That is exactly the never-reachable case (isBugCondition_D — no HTTP
    # response ever received): the component started before the backend was
    # ready, so Greengrass restarting it re-drives the load. It gets an
    # actionable diagnostic naming the likely cause, the LocalServer backend
    # container left stopped by a deployment restart (spec:
    # edge-deploy-reliability, Defect D, Requirement 2.10).
    #
    # Every AUTHORITATIVE answer from the runtime — a device-memory preflight
    # refusal AND any other HTTP failure — exits 0: the model's failure is the
    # MODEL's, reported with its reason through the model-status surfaces, and
    # failing the component instead takes co-deployed components and every
    # workflow that HARD-depends on this one down with it (see the module
    # docstring's exit-code contract for the block-18 evidence).
    # The staged engine args (rewritten for S3-sourced records, verbatim
    # otherwise) travel into the load path so an authoritative HTTP failure
    # logs the active gpu_memory_utilization / max_model_len (spec:
    # vllm-sizing-and-packaging-errors, Requirement 4.4).
    staged_engine_args = (
        rewritten_engine_args if rewritten_engine_args is not None else engine_args
    )
    outcome = request_load(model_name, staged_engine_args)
    if outcome == LOAD_PREFLIGHT_REFUSED:
        # Deterministic, pre-allocation refusal: exit 0 so this one model's
        # memory budget does not take the whole Greengrass deployment BROKEN ->
        # rolled back (defect 1.9: revision 73 -> FAILED_ROLLBACK_COMPLETE,
        # blocking every unrelated change for the device and leaving the latest
        # cloud deployment a FAILED revision that future revisions preload
        # from). Nothing is silent: this prominent ERROR carries the full
        # diagnostic, the runtime reports the model FAILED with the same reason
        # through the unchanged status surfaces, and the portal's fit check
        # refuses the same configuration at publish time.
        diagnostic = last_load_failure_reason() or (
            "the runtime's device memory preflight refused the load; see the "
            "LocalServer log for the measured available memory and the "
            "computed requirement"
        )
        logging.error(
            "VllmLoadModel: model '{}' was REFUSED by the device memory "
            "preflight before any GPU memory was allocated: {} | The staged "
            "configuration cannot load on this device as it stands, so a "
            "component retry cannot change the outcome and the deployment is "
            "NOT failed for this reason; the model is reported FAILED with "
            "its reason through the model-status surfaces. Re-package and "
            "re-publish with the remediation above, or free device memory, "
            "then redeploy.".format(model_name, diagnostic)
        )
        return 0
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
    if outcome == LOAD_HTTP_ERROR:
        # DELIBERATE (spec: jp6-vllm-kv-cache-oom-regression, task 11 OUTCOME
        # block 18): exit 0, not 1. The prominent ERROR line was already
        # emitted by log_load_failure() with the model name, the HTTP status,
        # the runtime's verbatim reason and the staged engine args (bugfix.md
        # 3.8's pinned elements, all preserved); this terminal line states what
        # happens next instead of failing the component.
        #
        # Why the old exit 1 was harmful, from production: three consecutive
        # load attempts failed on TRANSIENT DNS ("Failed to resolve
        # 'huggingface.co' ([Errno -3] Temporary failure in name resolution)")
        # at 12:00:47Z / 12:02:09Z / 12:03:22Z, each ending "Startup script
        # exited. {exitCode=1}"; after the third the component went
        # currentState=BROKEN, and because dda.workflow.0c7fe31a-... 7.0.0 and
        # dda.workflow.1f0b4c0c-... 9.0.0 HARD-depend on it they were left
        # stuck at INSTALLED and the core device went UNHEALTHY. A transient
        # network fault in ONE model's load must not take unrelated workflows
        # down. Do NOT "restore" exit 1 here.
        logging.error(
            "Model '{}' is staged but the vLLM runtime answered the load "
            "request with an authoritative failure (the ERROR above carries "
            "the HTTP status and the runtime's verbatim reason). The model is "
            "reported FAILED with that reason through the model-status "
            "surfaces, the in-backend vLLM reconciler owns the retries (it "
            "re-drives staged models with backoff and a 4-attempt budget), "
            "and this component is deliberately NOT failed — exiting 0 so "
            "co-deployed components and the workflows that depend on this one "
            "stay available.".format(model_name)
        )
        return 0
    if outcome != LOAD_OK:
        # Defensive: an unknown classification keeps the historical
        # fail-the-component behaviour, message verbatim.
        logging.error(
            "Model '{}' staged but the load request did not succeed; "
            "exiting non-zero so the component retries".format(model_name)
        )
        return 1
    return 0


def cleanup(args) -> int:
    """Shutdown path (--cleanup): unload, then remove the staged directory
    and any leftover staging temp siblings (mirrors convert_model_cleanup).
    Returns the process exit code.

    Ownership guard (spec: jp6-vllm-kv-cache-oom-regression, task 14
    H11/H12). The whole critical section — the owner check, the unload POST
    and the directory removal — runs under the per-model stage lock, and a
    teardown by a component that is NOT the recorded owner is REFUSED: both
    the unload and the removal are skipped, a prominent WARNING names the
    owner and the requester, and the exit code is still 0 so the teardown is
    not failed. The unload MUST be inside the guard: in production a
    non-owning component's Shutdown unloaded and deleted another component's
    model 0.6 s after that model's load succeeded (task 11 OUTCOME blocks
    17-18). A cleanup by the recorded owner, or when no marker exists, or
    when ``--component_name`` is absent, behaves exactly as before.
    """
    model_name = args.model_name
    component = args.component_name or ""
    try:
        with stage_lock(model_name):
            owner = marker_owner(read_owner_marker(model_name))
            if component and owner and owner != component:
                logging.warning(
                    "REFUSING --cleanup of staged vLLM model '{}': it is "
                    "owned by component '{}', not by the requesting component "
                    "'{}'. Skipping BOTH the unload request and the removal "
                    "of '{}' — a non-owning teardown has already destroyed a "
                    "freshly-loaded model in production (unload 200 then "
                    "'Cleaned directory' 0.6 s after the owner's load "
                    "succeeded). Exiting 0 so this teardown is not "
                    "failed.".format(
                        model_name, owner, component,
                        os.path.join(VLLM_MODEL_DIR, model_name),
                    )
                )
                return 0
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
                            os.path.join(VLLM_MODEL_DIR, entry),
                            ignore_errors=True,
                        )
                        logging.info(
                            "Cleaned leftover staging directory: {}".format(entry)
                        )
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
