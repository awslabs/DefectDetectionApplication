"""Freshness and coverage of the shared Scene_Analytics parity fixtures
(rtsp-rtmp-stream-cameras task 12.2).

``fixtures/analytics_parity_cases.json`` is committed so the device half
of the parity test (task 21.3) replays exactly the cases the sandbox half
replays. This file fails when the committed copy is stale, and when the
corpus stops exercising a rule the parity property covers.
"""
import json
import os
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import analytics_parity_fixtures as fixtures  # noqa: E402


@pytest.fixture(scope="module")
def committed():
    with open(fixtures.FIXTURE_PATH, encoding="utf-8") as handle:
        return handle.read()


def test_the_committed_fixture_is_fresh(committed):
    """Regenerate with ``python tests/analytics_parity_fixtures.py
    --write`` after changing the generator, the shared analytics module,
    the catalog defaults, or the compiler."""
    assert committed == fixtures.serialize(fixtures.corpus())


def test_the_corpus_covers_every_rule(committed):
    cases = json.loads(committed)["cases"]
    bindings = [binding for case in cases for binding in case["bindings"]]
    runs = [run for case in cases for run in case["runs"]]

    kinds = {binding["binding"] for binding in bindings}
    assert kinds == {"detection_counter", "object_association", "event_gate"}

    gates = [binding for binding in bindings if binding["binding"] == "event_gate"]
    assert {gate["parameters"]["emit"] for gate in gates} == \
        {"on_activate", "on_change", "while_active"}
    assert any(gate["parameters"]["repeat_interval_ms"] > 0 for gate in gates)

    outcomes = {outcome for run in runs for outcome in run["outcomes"].values()}
    assert outcomes == {"ok", "warning", "error"}

    transitions = {section["transition"] for run in runs
                   for section in run["expected"].get("event", {}).values()}
    assert transitions == {"activated", "cleared", "none"}
    passed = [value for run in runs for value in run["passed"].values()]
    assert True in passed and False in passed

    assert any(run["detections"] is None for run in runs)
    assert any(case["frameSize"] is None for case in cases)
    assert any(" && " in gate["parameters"]["condition"] for gate in gates)
    # Unevaluable conditions: a reference to a node that does not exist.
    assert any("nowhere" in gate["parameters"]["condition"]
               or "missing" in gate["parameters"]["condition"] for gate in gates)
