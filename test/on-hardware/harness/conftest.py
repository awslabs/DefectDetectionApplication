"""Edge_Test_Harness root conftest: session wiring for the whole suite.

Having a conftest at the package root puts this directory on ``sys.path`` so
``harnesslib`` imports resolve for stages and selftests alike. On top of that
this file wires the session together:

* **Configuration + client fixtures** (Reqs 1.1, 1.2): ``harness_target``
  loads the Harness_Configuration once per session; ``edge_client`` builds the
  :class:`~harnesslib.client.EdgeApiClient` and performs the auth handshake
  when the profile grants ``auth_enabled`` (Req 3.3).
* **Fail-fast reachability probe** (Req 1.3): the first ``edge_client`` use
  probes ``/system-health``; a connection failure calls ``pytest.exit`` with
  returncode 2 and a diagnostic naming the URL and the error, so the run dies
  once instead of failing every test individually.
* **Capability gating** (Reqs 2.1, 2.2, 2.3): ``pytest_collection_modifyitems``
  skips ``@pytest.mark.capability(name)`` items the selected Device_Profile
  does not grant, with a reason naming the capability and the device.
* **Declared-but-absent probes** (Req 2.4): ``vllm_surface`` and
  ``workflows_surface`` verify the granted capability is observable on the
  device, raising :class:`CapabilityMismatchError` — a distinct diagnostic
  contrasting the profile claim with the device observation — otherwise.
* **Run budget** (Req 8.4): a monotonic deadline armed from
  ``timeouts.run_budget_s``; ``pytest_runtest_setup`` fails every remaining
  test with a budget-exceeded message once past it, so a hung device degrades
  to a bounded, explained run.
* **Results bundle wiring** (Req 8.1): the
  :class:`~harnesslib.results.ResultsPlugin` is registered per session with
  the shared :class:`~harnesslib.restoration.StateRegistry`, and the
  ``--harness-output-dir`` option is exposed.

No device configured is not an error (Req 1.5 / unattended selftest runs):
stages still collect, then skip cleanly through ``harness_target``; only the
selftests execute.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
import requests
from harnesslib import results as results_module
from harnesslib.client import DeviceApiError, EdgeApiClient
from harnesslib.config import DeviceTarget, HarnessConfigError, load_config
from harnesslib.restoration import StateRegistry
from harnesslib.results import OUTPUT_DIR_OPTION, ResultsPlugin

#: Registration name of the per-session ResultsPlugin instance.
RESULTS_PLUGIN_NAME = "dda-harness-results"

#: Feature-configurations entry ``type`` of vLLM models (Backend_API:
#: ``utils/feature_configs_utils.VLLM_FEATURE_TYPE``).
VLLM_FEATURE_TYPE = "VllmModel"

# Session state stashed on the pytest config at configure time.
TARGET_KEY = pytest.StashKey[Optional[DeviceTarget]]()
CONFIG_ERROR_KEY = pytest.StashKey[Optional[str]]()
REGISTRY_KEY = pytest.StashKey[Optional[StateRegistry]]()
DEADLINE_KEY = pytest.StashKey[Optional[float]]()


class CapabilityMismatchError(Exception):
    """A Capability_Flag the Device_Profile grants is absent on the device.

    Distinct from an ordinary test failure (Req 2.4): the message contrasts
    the profile's claim with what the device actually reports, so a profile
    misconfiguration (or a device regression) cannot masquerade as a plain
    assertion failure.
    """

    def __init__(self, capability: str, device_name: str, observation: str):
        self.capability = capability
        self.device_name = device_name
        self.observation = observation
        super().__init__(
            f"Capability mismatch on device {device_name!r}: the Device_Profile "
            f"claims capability {capability!r} is available, but the device "
            f"observation contradicts it: {observation}. This is a "
            "declared-but-absent capability (profile misconfiguration or "
            "device regression), not an ordinary test failure."
        )


# ---------------------------------------------------------------------------
# Session configuration: config load, results plugin, budget deadline
# ---------------------------------------------------------------------------


def pytest_addoption(parser) -> None:
    results_module.pytest_addoption(parser)


def pytest_configure(config) -> None:
    """Load the Harness_Configuration once and wire the session around it.

    A missing/invalid configuration is recorded, not raised: selftest-only
    runs must work with no device configured (stages collect but skip
    cleanly, Req 1.5); the stored error becomes the skip reason.
    """
    target: Optional[DeviceTarget] = None
    error: Optional[str] = None
    try:
        target = load_config()
    except HarnessConfigError as err:
        error = str(err)

    config.stash[TARGET_KEY] = target
    config.stash[CONFIG_ERROR_KEY] = error
    config.stash[REGISTRY_KEY] = None
    config.stash[DEADLINE_KEY] = None

    if target is None:
        return

    registry = StateRegistry()
    config.stash[REGISTRY_KEY] = registry

    # Overall run budget (Req 8.4): monotonic deadline armed at configure.
    config.stash[DEADLINE_KEY] = time.monotonic() + target.timeouts.run_budget_s

    # Results_Bundle writer (Req 8.1). getoption with a default tolerates the
    # option being unregistered when this conftest is loaded mid-collection.
    output_dir = config.getoption(OUTPUT_DIR_OPTION, default=None)
    plugin = ResultsPlugin(
        target,
        output_dir=Path(output_dir) if output_dir else None,
        registry=registry,
    )
    config.pluginmanager.register(plugin, name=RESULTS_PLUGIN_NAME)


def _configured_target(config) -> "Tuple[Optional[DeviceTarget], Optional[str]]":
    return config.stash.get(TARGET_KEY, None), config.stash.get(CONFIG_ERROR_KEY, None)


# ---------------------------------------------------------------------------
# Capability gating (Reqs 2.1, 2.2, 2.3)
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(config, items) -> None:
    """Skip capability-marked items the selected Device_Profile does not grant.

    Skip reasons name the missing capability and the device (Req 2.1) and flow
    into JUnit XML and results.json. With no device configured at all, the
    capability-marked stages skip with the configuration error as the reason
    (Req 1.5 — collect, never fail, when no device is configured).
    """
    target, error = _configured_target(config)
    for item in items:
        marker = item.get_closest_marker("capability")
        if marker is None:
            continue
        if not marker.args:
            raise pytest.UsageError(
                f"{item.nodeid}: @pytest.mark.capability requires the "
                "capability name as its argument, e.g. capability('vllm')"
            )
        capability = marker.args[0]
        if target is None:
            item.add_marker(pytest.mark.skip(reason=f"no target device configured: {error}"))
        elif not target.profile.grants(capability):
            item.add_marker(
                pytest.mark.skip(
                    reason=(
                        f"capability {capability!r} not granted by device " f"profile {target.name}"
                    )
                )
            )


# ---------------------------------------------------------------------------
# Run budget (Req 8.4)
# ---------------------------------------------------------------------------


def pytest_runtest_setup(item) -> None:
    """Fail remaining tests once the run budget deadline has passed.

    Armed only when a target device is configured; a hung device therefore
    degrades to a bounded run whose remaining tests carry an explicit
    budget-exceeded diagnostic instead of stalling indefinitely.
    """
    deadline = item.config.stash.get(DEADLINE_KEY, None)
    if deadline is None or time.monotonic() < deadline:
        return
    target, _ = _configured_target(item.config)
    budget = target.timeouts.run_budget_s if target is not None else 0.0
    pytest.fail(
        f"run budget exceeded: the overall budget of {budget:.0f}s "
        f"(timeouts.run_budget_s) elapsed before this test started; "
        "remaining tests are failed so a hung device cannot stall the run",
        pytrace=False,
    )


# ---------------------------------------------------------------------------
# Session fixtures: target, client, registry, identity, results channels
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def harness_target(request) -> DeviceTarget:
    """The selected :class:`DeviceTarget`, or a clean skip when no device is
    configured (Req 1.5: stages collect but never fail without a device)."""
    target, error = _configured_target(request.config)
    if target is None:
        pytest.skip(f"no target device configured: {error}")
    return target


@pytest.fixture(scope="session")
def state_registry(request) -> Iterator[StateRegistry]:
    """The session :class:`StateRegistry`; teardown restores everything the
    harness started, on success and failure alike (Reqs 4.3, 6.4, 8.3)."""
    registry = request.config.stash.get(REGISTRY_KEY, None)
    if registry is None:  # no device configured; selftests never get here
        registry = StateRegistry()
    yield registry
    registry.restore_all()


@pytest.fixture(scope="session")
def edge_client(harness_target: DeviceTarget) -> EdgeApiClient:
    """The session :class:`EdgeApiClient`: reachability-probed (Req 1.3) and
    authenticated when the profile grants ``auth_enabled`` (Req 3.3)."""
    client = EdgeApiClient(harness_target.base_url, timeouts=harness_target.timeouts)
    # Fail-fast reachability probe (Req 1.3): one connection failure aborts
    # the whole run with a single diagnostic naming the URL and the error.
    try:
        client.system_health()
    except DeviceApiError:
        # A non-2xx answer still proves the device is reachable; asserting
        # the health payload itself belongs to the health stage (Req 3.1).
        pass
    except requests.exceptions.RequestException as err:
        pytest.exit(
            f"Target device unreachable at {harness_target.base_url}: {err}",
            returncode=2,
        )
    if harness_target.profile.grants("auth_enabled"):
        _authenticate(client, harness_target)
    return client


def _authenticate(client: EdgeApiClient, target: DeviceTarget) -> None:
    """The ``auth_enabled`` handshake (Req 3.3).

    The credential reference resolves to either ``username:password`` (login
    flow) or a ready-made bearer token. Failures fail fast with a clear
    diagnostic; the secret value never reaches a message (DeviceApiError
    redacts the Authorization header, and only the reference is named here).
    """
    if target.credentials_ref is None:
        pytest.fail(
            f"Device {target.name!r} grants 'auth_enabled' but no credentials "
            "reference is configured (set 'credentials' in devices.yaml or "
            "DDA_HARNESS_CREDENTIALS)",
            pytrace=False,
        )
    try:
        secret = target.credentials_ref.resolve()
    except HarnessConfigError as err:
        pytest.fail(
            f"Device {target.name!r}: cannot resolve credentials reference "
            f"{target.credentials_ref}: {err}",
            pytrace=False,
        )
    username, sep, password = secret.partition(":")
    try:
        if sep:
            client.login(username, password)
        else:
            client.set_bearer_token(secret)
    except DeviceApiError as err:
        pytest.fail(
            f"Authentication to device {target.name!r} failed: {err}",
            pytrace=False,
        )


@pytest.fixture(scope="session")
def device_identity(harness_target: DeviceTarget) -> Dict[str, Any]:
    """Mutable device identity record for the Results_Bundle (Req 3.2).

    The health stage populates ``local_server_version``; later stages depend
    on this fixture, which (with the ``test_NN_*`` module ordering) keeps the
    health stage first (Req 3.1).
    """
    return {
        "device": harness_target.name,
        "architecture": harness_target.profile.architecture,
        "capabilities": sorted(harness_target.profile.capabilities),
        "local_server_version": None,
    }


@pytest.fixture(scope="session")
def results_plugin(request) -> Optional[ResultsPlugin]:
    """The registered :class:`ResultsPlugin`, or ``None`` when no device is
    configured (stages skip before reaching it via ``harness_target``)."""
    return request.config.pluginmanager.get_plugin(RESULTS_PLUGIN_NAME)


@pytest.fixture
def record_metric(request):
    """The ``record_metric(name, value)`` channel (Req 5.4); a no-op when no
    ResultsPlugin is registered so stages always collect (Req 1.5)."""
    plugin = request.config.pluginmanager.get_plugin(RESULTS_PLUGIN_NAME)
    if plugin is None:
        return lambda name, value: None
    return plugin.record_metric


# ---------------------------------------------------------------------------
# Declared-but-absent capability probes (Req 2.4)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def vllm_surface(harness_target: DeviceTarget, edge_client: EdgeApiClient) -> Dict[str, Any]:
    """Probe the granted ``vllm`` capability against the device (Req 2.4).

    The profile claim holds only if ``/text-generation/models`` answers and
    the feature list carries ``VllmModel`` entries; otherwise the stage fails
    with :class:`CapabilityMismatchError`, distinct from an ordinary failure.

    :returns: ``{"textgen_models": [...], "feature_entries": [...]}`` for
        reuse by the vLLM stages.
    """
    try:
        textgen_models = edge_client.textgen_models()
        feature_entries = [
            entry
            for entry in edge_client.feature_configurations()
            if entry.get("type") == VLLM_FEATURE_TYPE
        ]
    except (DeviceApiError, requests.exceptions.RequestException) as err:
        raise CapabilityMismatchError(
            "vllm",
            harness_target.name,
            f"the vLLM surface did not answer: {err}",
        ) from err
    if not feature_entries:
        raise CapabilityMismatchError(
            "vllm",
            harness_target.name,
            "feature-configurations reports no VllmModel entries",
        )
    return {"textgen_models": textgen_models, "feature_entries": feature_entries}


@pytest.fixture(scope="session")
def workflows_surface(
    harness_target: DeviceTarget, edge_client: EdgeApiClient
) -> List[Dict[str, Any]]:
    """Probe the granted ``workflows`` capability against the device (Req 2.4).

    :returns: the device's Deployed_Workflows enumeration for reuse by the
        workflow stage.
    """
    try:
        return edge_client.workflows()
    except (DeviceApiError, requests.exceptions.RequestException) as err:
        raise CapabilityMismatchError(
            "workflows",
            harness_target.name,
            f"GET /workflows did not answer: {err}",
        ) from err
