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
"""Bug-condition exploration test (Task 1, case 1) for edge-deploy-reliability.

Property 1: Bug Condition — Backend survives or recovers from deployment
restarts (Defect A, `isBugCondition_A`), and structurally Property 2
(`isBugCondition_B`: no compose healthchecks exist for anything to observe).

**These tests assert the FIXED (post-fix) compose configuration, so they are
EXPECTED TO FAIL on the UNFIXED tree.** Each failure is the counterexample
confirming the defect:

  * neither backend service declares `stop_grace_period` — Docker SIGKILLs at
    the 10s default (the incident's exit 137, OOMKilled=false),
  * both backend services use `restart: unless-stopped`, which never
    re-launches a docker-stopped container while the daemon runs,
  * neither backend service declares a `healthcheck`, so neither Docker nor
    the Greengrass lifecycle can observe backend health.

The SAME tests are re-run in task 3.5 against the fixed compose file, where
they must PASS (stop_grace_period >= 120s, restart: always, healthcheck
present on both backend services).

Config test as the testable seam (per the design): the compose defect is a
configuration defect, so the test parses `src/docker-compose.yaml` and asserts
the reliability-critical properties directly.

Validates: Requirements 1.1, 1.2, 1.6
"""
import os
import re

import pytest
import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
COMPOSE_PATH = os.path.join(REPO_ROOT, "src", "docker-compose.yaml")

#: The two backend services (tegra / generic profiles) hosting the FastAPI
#: app and, on vLLM-capable images, the vLLM runtime on 127.0.0.1:8901.
BACKEND_SERVICES = ("backend_tegra_gpu_enabled", "backend_generic")

#: Requirement 2.1 / Property 1: sized well above the bounded 20s cleanup.
MIN_STOP_GRACE_SECONDS = 120

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(us|ms|s|m|h)?\s*$")
_UNIT_SECONDS = {"us": 1e-6, "ms": 1e-3, "s": 1, "m": 60, "h": 3600, None: 1}


def _duration_seconds(value):
    """Parse a compose duration ('120s', '2m', bare int) into seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    match = _DURATION_RE.match(str(value))
    assert match, "unparseable compose duration: {!r}".format(value)
    return float(match.group(1)) * _UNIT_SECONDS[match.group(2)]


@pytest.fixture(scope="module")
def compose_services():
    with open(COMPOSE_PATH, encoding="utf-8") as f:
        compose = yaml.safe_load(f)
    services = compose.get("services") or {}
    for name in BACKEND_SERVICES:
        assert name in services, (
            "backend service '{}' missing from {}".format(name, COMPOSE_PATH))
    return services


@pytest.mark.parametrize("service_name", BACKEND_SERVICES)
def test_backend_service_declares_stop_grace_period_at_least_120s(
        compose_services, service_name):
    """isBugCondition_A: without `stop_grace_period` Docker SIGKILLs the
    backend 10s after SIGTERM (incident: exit 137 at t+13s, OOMKilled=false),
    mid- shutdown cleanup. The fixed compose declares >= 120s on both backend
    services.

    Validates: Requirements 1.1 (expected behavior 2.1)
    """
    service = compose_services[service_name]
    assert "stop_grace_period" in service, (
        "COUNTEREXAMPLE (Defect A): service '{}' declares no "
        "stop_grace_period in src/docker-compose.yaml — Docker's 10s default "
        "grace window applies and shutdown cleanup exceeding it is SIGKILLed "
        "(exit 137)".format(service_name))
    grace = _duration_seconds(service["stop_grace_period"])
    assert grace >= MIN_STOP_GRACE_SECONDS, (
        "COUNTEREXAMPLE (Defect A): service '{}' stop_grace_period is {}s, "
        "below the required {}s (must exceed the bounded 20s cleanup with "
        "margin)".format(service_name, grace, MIN_STOP_GRACE_SECONDS))


@pytest.mark.parametrize("service_name", BACKEND_SERVICES)
def test_backend_service_restart_policy_is_always(compose_services,
                                                  service_name):
    """isBugCondition_A: `restart: unless-stopped` never re-launches a
    docker-stopped container while the daemon runs — the incident's backend
    stayed Exited(137) permanently. The fixed compose uses `restart: always`
    on both backend services (a strict superset for crash recovery).

    Validates: Requirements 1.2 (expected behavior 2.2)
    """
    service = compose_services[service_name]
    assert service.get("restart") == "always", (
        "COUNTEREXAMPLE (Defect A): service '{}' has restart={!r}; "
        "'unless-stopped' does not apply to docker-stopped containers, so a "
        "stop-timeout-killed backend is never recovered"
        .format(service_name, service.get("restart")))


@pytest.mark.parametrize("service_name", BACKEND_SERVICES)
def test_backend_service_declares_healthcheck(compose_services, service_name):
    """isBugCondition_B (structural leg): with no compose healthcheck there is
    nothing for Docker — or a health-gated Greengrass lifecycle
    (`compose up -d --wait`) — to observe, so a dead backend is invisible.
    The fixed compose declares a healthcheck on both backend services.

    Validates: Requirements 1.6 (expected behavior 2.5)
    """
    service = compose_services[service_name]
    assert "healthcheck" in service, (
        "COUNTEREXAMPLE (Defect B): service '{}' declares no healthcheck in "
        "src/docker-compose.yaml — neither Docker nor the Greengrass "
        "lifecycle can observe backend health".format(service_name))
    healthcheck = service["healthcheck"]
    assert healthcheck.get("test"), (
        "COUNTEREXAMPLE (Defect B): service '{}' healthcheck declares no "
        "test command".format(service_name))
