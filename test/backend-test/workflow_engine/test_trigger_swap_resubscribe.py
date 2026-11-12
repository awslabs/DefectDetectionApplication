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
"""Reproduction: a workflow swap must not kill the Greengrass MQTT trigger.

Feature: keep the Greengrass IPC trigger subscription alive across a
workflow swap. When a deployment replaces the version of a trigger-driven
workflow, the watcher marks the old registration ``removed`` and registers
the new one, and ``TriggerSubscriptionManager.on_registrations_changed``
reconciles both in one call. Before the fix the manager stopped every
departing group before starting any replacement. The Greengrass nucleus
sends the cloud UNSUBSCRIBE when a component's last ``SubscribeToIoTCore``
stream of a topic filter closes but drops the filter from its map only on
the UNSUBACK, so the replacement's subscribe a few milliseconds later was
acknowledged as a piggyback on the dying filter and never received a
message until the process restarted — with no error anywhere.

The tests drive the real manager and the real ``GreengrassIpcSubscriber``
against ``greengrass_nucleus_double.FakeNucleus`` (the nucleus's semantics,
injected through the worker's ``ipc_connect`` seam). They import only names
that exist in the pre-fix revision of ``workflow_engine.trigger_runtime``,
so the module runs unchanged against that revision and FAILS there:

- ``test_double_swap_keeps_the_shared_filter_alive`` and
  ``test_same_id_fingerprint_change_overlaps`` at ``unsubscribes == []``
  (the pre-fix order lets the filter's open-stream count reach zero);
- ``test_split_scan_lingers_the_last_stream`` at scan 1's
  ``open_streams == 1`` (the pre-fix stop closes the last stream at once);
- ``test_message_in_the_overlap_runs_once`` at the ``fired`` assertion (no
  overlap ever has four open streams before the fix);
- ``test_log_order_shows_subscribe_before_stop`` at the count of
  ``quiesced`` records (the pre-fix reconcile writes none).

With the fix the departing groups are quiesced, the replacements subscribe
while the old streams are still open, and only then do the old streams
close; a removal and a re-registration seen in separate reconciles are
bridged by the transport keeping the filter's last stream open for a grace
period (manual timers here: no test waits on real time).
"""
import json
import logging
import os

import pytest

import workflow_engine.trigger_runtime as trigger_runtime
from workflow_engine.trigger_runtime import GreengrassIpcSubscriber

from workflow_engine_test_utils import make_session_factory, write_artifact_set

from greengrass_nucleus_double import FakeNucleus, install_awsiot_stubs
from test_property_concurrency_policies import ManualTimerFactory
from test_trigger_lifecycle import (
    add_registration,
    make_manager,
    set_registration_status,
    trigger_compiled,
    wait_for,
)

LOGGER = "workflow_engine.trigger_runtime"
TOPIC = "factory/line1/inspection/evaluate"
NODE_ID = "trigger_t1"
PAYLOAD = '{"order_id":"order-1"}'


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def awsiot_stubs(monkeypatch):
    return install_awsiot_stubs(monkeypatch)


@pytest.fixture
def manual_registry(monkeypatch):
    """With the fix: the Greengrass transport's stream registry with manual
    timers (nothing waits on the real grace), closed at teardown. On the
    pre-fix revision the registry does not exist and this yields None."""
    registry_class = getattr(trigger_runtime, "GreengrassStreamRegistry", None)
    if registry_class is None:
        yield None
        return
    registry = registry_class(timer_factory=ManualTimerFactory())
    monkeypatch.setattr(trigger_runtime, "DEFAULT_STREAM_REGISTRY", registry)
    yield registry
    registry.close_all()


@pytest.fixture
def manager_stopper():
    """Stop every manager built in a test so no dispatcher/worker thread
    outlives it."""
    managers = []
    yield managers.append
    for manager in managers:
        manager.stop()


def swap_compiled(qos=1):
    return trigger_compiled(node_id=NODE_ID, parameters={"topic": TOPIC, "qos": qos})


def register(session_factory, tmp_path, workflow_id, version, qos=1):
    """One registered artifact set ``<workflow_id>:<version>`` whose single
    trigger subscribes TOPIC through the Greengrass transport."""
    version_dir = write_artifact_set(
        tmp_path, workflow_id=workflow_id, version=version, compiled=swap_compiled(qos)
    )
    add_registration(
        session_factory,
        registration_id=f"{workflow_id}:{version}",
        artifact_path=version_dir,
        workflow_id=workflow_id,
    )
    return version_dir


def mark_removed(session_factory, registration_id):
    """What the watcher writes when a registration's artifact directory
    disappears (a deployment replacing the workflow version)."""
    set_registration_status(session_factory, registration_id, "removed")


def build(session_factory, nucleus, fired):
    """The real manager over real Greengrass workers on the double; a run
    starter that records ``(registration_id, payload)``. The worker is
    constructed exactly as the pre-fix revision allows (no registry
    keyword), so the module runs against it."""

    def mqtt_factory(kind, parameters, on_delivery, on_connection_lost, health):
        return GreengrassIpcSubscriber(
            parameters, on_delivery, on_connection_lost, health, ipc_connect=nucleus.connect
        )

    def run_starter_factory(registration_id):
        return lambda activation: fired.append(
            (registration_id, activation.context["payload"])
        )

    return make_manager(
        session_factory, mqtt_factory=mqtt_factory, run_starter_factory=run_starter_factory
    )


def start_double(session_factory, tmp_path, nucleus, fired, manager_stopper):
    """Registrations wfA:8 and wfB:5, both on TOPIC, started."""
    register(session_factory, tmp_path, "wfA", "8")
    register(session_factory, tmp_path, "wfB", "5")
    manager = build(session_factory, nucleus, fired)
    manager_stopper(manager)
    manager.on_registrations_changed()
    assert nucleus.subscribes == [(TOPIC, "real"), (TOPIC, "piggyback")]
    assert nucleus.open_streams(TOPIC) == 2
    return manager


def swap_both(session_factory, tmp_path):
    """The deployment: both old registrations removed, both new versions
    registered — seen by one reconcile."""
    mark_removed(session_factory, "wfA:8")
    mark_removed(session_factory, "wfB:5")
    register(session_factory, tmp_path, "wfA", "9")
    register(session_factory, tmp_path, "wfB", "6")


# ---------------------------------------------------------------------------
# The reproduction
# ---------------------------------------------------------------------------


def test_double_swap_keeps_the_shared_filter_alive(
    tmp_path, awsiot_stubs, manual_registry, manager_stopper
):
    """AC1: wfA:8 and wfB:5 are replaced by wfA:9 and wfB:6 in one
    reconcile. The filter's open-stream count never reaches zero (no
    UNSUBSCRIBE), the old streams are closed, two streams are open, and a
    message after the UNSUBACKs runs exactly once per new registration."""
    session_factory = make_session_factory()
    nucleus = FakeNucleus()
    fired = []
    manager = start_double(session_factory, tmp_path, nucleus, fired, manager_stopper)
    old_operations = list(nucleus.streams[TOPIC])

    swap_both(session_factory, tmp_path)
    manager.on_registrations_changed()

    assert nucleus.unsubscribes == [], (
        "UNSUBSCRIBE sent: the filter's open-stream count reached zero"
    )
    assert nucleus.open_streams(TOPIC) == 2
    assert all(operation.closed for operation in old_operations)
    assert manager.group("wfA:8") is None and manager.group("wfB:5") is None
    assert manager.group("wfA:9") is not None and manager.group("wfB:6") is not None

    nucleus.complete_unsubacks()
    assert nucleus.deliver(TOPIC, PAYLOAD.encode()) == 2
    assert wait_for(lambda: len(fired) == 2), f"runs after the swap: {fired}"
    assert sorted(fired) == [("wfA:9", PAYLOAD), ("wfB:6", PAYLOAD)]


def test_same_id_fingerprint_change_overlaps(
    tmp_path, awsiot_stubs, manual_registry, manager_stopper
):
    """AC2: the same registration id with a changed trigger surface is
    replaced the same way: the new group subscribes while the old one still
    holds the filter, so no UNSUBSCRIBE is sent."""
    session_factory = make_session_factory()
    nucleus = FakeNucleus()
    fired = []
    version_dir = register(session_factory, tmp_path, "wfA", "8", qos=1)
    manager = build(session_factory, nucleus, fired)
    manager_stopper(manager)
    manager.on_registrations_changed()
    old_group = manager.group("wfA:8")
    assert old_group is not None

    with open(os.path.join(version_dir, "compiled_pipeline.json"), "w") as handle:
        json.dump(swap_compiled(qos=0), handle)
    manager.on_registrations_changed()

    assert manager.group("wfA:8") is not old_group
    assert nucleus.unsubscribes == [], (
        "UNSUBSCRIBE sent: the filter's open-stream count reached zero"
    )
    assert nucleus.open_streams(TOPIC) == 1

    nucleus.complete_unsubacks()
    assert nucleus.deliver(TOPIC, PAYLOAD.encode()) == 1
    assert wait_for(lambda: len(fired) == 1), f"runs after the change: {fired}"
    assert fired == [("wfA:8", PAYLOAD)]


def test_split_scan_lingers_the_last_stream(
    tmp_path, awsiot_stubs, manual_registry, manager_stopper
):
    """AC3: the removal is seen in one reconcile and the new version in the
    next. The last stream of the filter stays open after scan 1 (nothing
    else in the process holds the filter), the new version's subscribe in
    scan 2 lands on the live filter, and only then does the old stream
    close — no UNSUBSCRIBE at any point."""
    session_factory = make_session_factory()
    nucleus = FakeNucleus()
    fired = []
    register(session_factory, tmp_path, "wfA", "8")
    manager = build(session_factory, nucleus, fired)
    manager_stopper(manager)
    manager.on_registrations_changed()
    old_operation = nucleus.streams[TOPIC][0]

    mark_removed(session_factory, "wfA:8")
    manager.on_registrations_changed()  # scan 1

    assert manager.group("wfA:8") is None
    assert nucleus.open_streams(TOPIC) == 1, (
        "stream closed in scan 1: nothing holds the filter"
    )
    assert nucleus.unsubscribes == []

    register(session_factory, tmp_path, "wfA", "9")
    manager.on_registrations_changed()  # scan 2

    assert nucleus.open_streams(TOPIC) == 1
    assert nucleus.streams[TOPIC][0] is not old_operation
    assert old_operation.closed
    assert nucleus.unsubscribes == []

    nucleus.complete_unsubacks()
    assert nucleus.deliver(TOPIC, PAYLOAD.encode()) == 1
    assert wait_for(lambda: len(fired) == 1), f"runs after scan 2: {fired}"
    assert fired == [("wfA:9", PAYLOAD)]


def test_message_in_the_overlap_runs_once(
    tmp_path, awsiot_stubs, manual_registry, manager_stopper, caplog
):
    """AC6: a message delivered the instant both replacements are
    subscribed and both old streams are still open (four open streams)
    starts exactly one run per new registration; the departing groups
    discard their copies at INFO and queue nothing."""
    session_factory = make_session_factory()
    nucleus = FakeNucleus()
    fired = []
    manager = start_double(session_factory, tmp_path, nucleus, fired, manager_stopper)
    old_groups = [manager.group("wfA:8"), manager.group("wfB:5")]

    def deliver_mid_swap(topic, operation):
        if nucleus.open_streams(TOPIC) == 4:
            nucleus.deliver(TOPIC, b"mid-swap")

    nucleus.on_subscribed = deliver_mid_swap
    swap_both(session_factory, tmp_path)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        manager.on_registrations_changed()

    assert all(len(group.core.queue) == 0 for group in old_groups)
    assert wait_for(lambda: len(fired) == 2, timeout=2.0), (
        f"no activation for the replacements: {fired}"
    )
    assert sorted(fired) == [("wfA:9", "mid-swap"), ("wfB:6", "mid-swap")]

    discarded = [
        record.getMessage()
        for record in caplog.records
        if "firing discarded" in record.getMessage()
        and "(gate closed)" in record.getMessage()
    ]
    assert len(discarded) == 2
    assert all(NODE_ID in message for message in discarded)
    assert sorted("wfA:8" in message for message in discarded) == [False, True]
    assert sorted("wfB:5" in message for message in discarded) == [False, True]


def test_log_order_shows_subscribe_before_stop(
    tmp_path, awsiot_stubs, manual_registry, manager_stopper, caplog
):
    """AC8: the INFO lines of the double swap show the new order — every
    departing group quiesced before any replacement subscribed, each
    replacement subscribed right before it is reported started, and every
    departing group stopped after every replacement started."""
    session_factory = make_session_factory()
    nucleus = FakeNucleus()
    fired = []
    manager = start_double(session_factory, tmp_path, nucleus, fired, manager_stopper)

    swap_both(session_factory, tmp_path)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        manager.on_registrations_changed()

    markers = {
        "quiesced": "quiesced (",
        "subscribed": "subscribed to IoT Core topic filter",
        "started": "Trigger subscriptions started for registration",
        "stopped": "Trigger subscriptions stopped for registration",
    }
    sequence = []
    for record in caplog.records:
        message = record.getMessage()
        for kind, marker in markers.items():
            if marker in message:
                sequence.append(kind)
                break
    positions = {
        kind: [index for index, item in enumerate(sequence) if item == kind]
        for kind in markers
    }

    assert len(positions["quiesced"]) == 2, f"quiesced records: {sequence}"
    assert len(positions["subscribed"]) == 2, f"subscribed records: {sequence}"
    assert len(positions["started"]) == 2, f"started records: {sequence}"
    assert len(positions["stopped"]) == 2, f"stopped records: {sequence}"
    assert max(positions["quiesced"]) < min(positions["subscribed"]), sequence
    for index in positions["started"]:
        assert sequence[index - 1] == "subscribed", sequence
    assert min(positions["stopped"]) > max(positions["started"]), sequence
