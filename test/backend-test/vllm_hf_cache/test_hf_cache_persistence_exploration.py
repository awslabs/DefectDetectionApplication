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
"""Bug-condition exploration test (Task 1) for vllm-hf-cache-persistence.

Property 1: Bug Condition — HF cache persists across container recreation
(`isBugCondition` from the design: `hfCacheEphemeral` holds when a backend
service sets no `HF_HOME`, or sets it outside a bind-mounted persistent
path, so vLLM/huggingface_hub caches weights at /root/.cache/huggingface on
the container's ephemeral writable layer).

**These tests assert the FIXED (post-fix) compose configuration, so they are
EXPECTED TO FAIL on the UNFIXED tree.** Each failure is the counterexample
confirming the defect: none of the three backend services declares any
`HF_HOME`, so every container recreation wipes the HF cache, forces a full
weights re-download (~6 GB per 7B AWQ model), and — when huggingface.co is
unreachable in the post-recreation load window (device boot / dockerd
restart) — escalates the model component to BROKEN (observed 2026-08-12 on
ryanorinagxdevkithomelabjp622: recreation → cache wiped → HTTP 409
"Invalid repository ID or local directory specified" → 3 Startup failures →
BROKEN).

The SAME tests are re-run in task 3.4 against the fixed compose file, where
they must PASS (`HF_HOME=/aws_dda/hf_cache` in each backend service's
environment, with `/aws_dda` bind-mounted read-write in that service).

Config test as the testable seam (per the design and the established
`deploy_reliability/test_compose_restart_race_exploration.py` pattern): the
defect is deterministic configuration, so the test parses
`src/docker-compose.yaml` and asserts the HF-cache-persistence properties
directly for each backend service.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4**
"""
import os

import pytest
import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
COMPOSE_PATH = os.path.join(REPO_ROOT, "src", "docker-compose.yaml")

#: The three backend services (tegra / generic / generic_nvidia profiles)
#: that run the flask-app image hosting the vLLM runtime.
BACKEND_SERVICES = (
    "backend_tegra_gpu_enabled",
    "backend_generic",
    "backend_generic_nvidia",
)

#: The persistent host directory already bind-mounted rw into every backend
#: service; the fix parks the HF hub cache under it.
PERSISTENT_MOUNT_SOURCE = "/aws_dda"


def _environment_map(service):
    """Parse a compose service's `environment` into a {KEY: value} dict.

    Handles the list form (["KEY=value", "KEY"] — bare keys are
    pass-through entries with value None) and the mapping form
    ({KEY: value}).
    """
    environment = service.get("environment") or {}
    if isinstance(environment, dict):
        return dict(environment)
    env_map = {}
    for entry in environment:
        key, sep, value = str(entry).partition("=")
        env_map[key] = value if sep else None
    return env_map


def _bind_mounts(service):
    """Parse a compose service's `volumes` into (source, target, rw) tuples.

    Handles the short string form ("src:dst", "src:dst:mode") and the long
    mapping form ({type, source, target, read_only}). A mount is read-write
    unless its mode contains "ro" / read_only is true.
    """
    mounts = []
    for volume in service.get("volumes") or []:
        if isinstance(volume, dict):
            source = volume.get("source")
            target = volume.get("target")
            rw = not volume.get("read_only", False)
        else:
            parts = str(volume).split(":")
            assert len(parts) >= 2, (
                "unparseable compose volume entry: {!r}".format(volume))
            source, target = parts[0], parts[1]
            mode = parts[2] if len(parts) > 2 else "rw"
            rw = "ro" not in mode.split(",")
        mounts.append((source, target, rw))
    return mounts


def _is_under(path, mount_target):
    """True when `path` equals `mount_target` or lies beneath it."""
    path = os.path.normpath(path)
    mount_target = os.path.normpath(mount_target)
    return path == mount_target or path.startswith(mount_target + "/")


def _rw_mount_containing(service, path):
    """The (source, target, rw) rw bind mount whose target contains `path`,
    preferring the deepest (longest-target) match, or None."""
    candidates = [m for m in _bind_mounts(service)
                  if m[2] and _is_under(path, m[1])]
    if not candidates:
        return None
    return max(candidates, key=lambda m: len(os.path.normpath(m[1])))


@pytest.fixture(scope="module")
def compose_services():
    with open(COMPOSE_PATH, encoding="utf-8") as f:
        compose = yaml.safe_load(f)
    services = compose.get("services") or {}
    for name in BACKEND_SERVICES:
        assert name in services, (
            "backend service '{}' missing from {}".format(name, COMPOSE_PATH))
    return services


# Tests 1-3 (tegra / generic / generic-nvidia backend cache seams)
@pytest.mark.parametrize("service_name", BACKEND_SERVICES)
def test_backend_service_hf_home_under_persistent_rw_mount(
        compose_services, service_name):
    """isBugCondition / hfCacheEphemeral: without an `HF_HOME` under a
    bind-mounted read-write path, vLLM/huggingface_hub (root in the
    container, HOME=/root) caches weights at /root/.cache/huggingface on the
    ephemeral container layer — wiped on every recreation, forcing a full
    re-download and, when huggingface.co is unreachable in that window, the
    409 → BROKEN escalation. The fixed compose declares `HF_HOME` under a
    rw bind mount in every backend service.

    Validates: Requirements 2.1, 2.2, 2.3, 2.4
    """
    service = compose_services[service_name]
    env = _environment_map(service)
    assert "HF_HOME" in env, (
        "COUNTEREXAMPLE (hfCacheEphemeral): service '{}' declares no HF_HOME "
        "in its environment in src/docker-compose.yaml — the effective HF "
        "cache root is /root/.cache/huggingface on the ephemeral container "
        "layer, wiped on every container recreation".format(service_name))
    hf_home = env["HF_HOME"]
    assert hf_home, (
        "COUNTEREXAMPLE (hfCacheEphemeral): service '{}' declares HF_HOME "
        "with no value (bare pass-through entry) — the effective cache "
        "location is host-environment-dependent, not a pinned persistent "
        "path".format(service_name))
    mount = _rw_mount_containing(service, hf_home)
    assert mount is not None, (
        "COUNTEREXAMPLE (hfCacheEphemeral): service '{}' sets HF_HOME={!r}, "
        "which is not under any read-write bind mount ({}) — the cache "
        "still lives on the ephemeral container layer".format(
            service_name, hf_home, _bind_mounts(service)))


# Test 4 (persistence-root edge case)
@pytest.mark.parametrize("service_name", BACKEND_SERVICES)
def test_backend_service_hf_home_mount_source_is_aws_dda(
        compose_services, service_name):
    """Persistence-root edge case: the mount backing HF_HOME must be the
    persistent /aws_dda specifically — NOT /tmp, which is also bind-mounted
    rw into every backend service but is cleared on host reboot, so a cache
    under /tmp would re-create the bug on exactly the incident's
    boot-window path.

    Validates: Requirements 2.1, 2.4
    """
    service = compose_services[service_name]
    env = _environment_map(service)
    hf_home = env.get("HF_HOME")
    assert hf_home, (
        "COUNTEREXAMPLE (hfCacheEphemeral): service '{}' declares no HF_HOME "
        "— there is no cache mount source to pin to {}".format(
            service_name, PERSISTENT_MOUNT_SOURCE))
    mount = _rw_mount_containing(service, hf_home)
    assert mount is not None, (
        "COUNTEREXAMPLE (hfCacheEphemeral): service '{}' HF_HOME={!r} is not "
        "under any read-write bind mount".format(service_name, hf_home))
    source, target, _ = mount
    assert source == PERSISTENT_MOUNT_SOURCE, (
        "COUNTEREXAMPLE (persistence root): service '{}' HF_HOME={!r} is "
        "backed by the bind mount {}:{} — its source must be the persistent "
        "{} (a /tmp-backed cache is cleared on host reboot, re-creating the "
        "boot-window incident)".format(
            service_name, hf_home, source, target, PERSISTENT_MOUNT_SOURCE))
