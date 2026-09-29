"""Generate the shared Scene_Analytics parity fixtures.

rtsp-rtmp-stream-cameras task 12.2, Property 27: for any Detection_List,
analytics parameters, frame size and outcome sequence, the LocalServer
bindings and the sandbox bindings produce identical ``counter``,
``association`` and ``event`` metadata.

Each case holds:

- ``bindings``: the analytics executor bindings of a small workflow,
  compiled by the real workflow_core compiler, so the parameter maps are
  exactly what the device and the sandbox receive (defaults filled in);
- ``frameSize``: the frame the detections were made on, or null;
- ``runs``: a sequence of runs, each with its Detection_List (null for a
  run without one), tag values and clock, and what the bindings must
  produce: the three metadata sections, each counter's and association's
  outcome, and whether each gate passed.

The expected values come from an oracle written here against the shared
``workflow_core.analytics.scene`` functions and the documented metadata
layout (Requirements 13.3, 14.4, 15.4), with its own evaluator for the
fixture's restricted conditions. They do not come from either binding
layer. The sandbox half of the parity test
(``test_property_analytics_parity.py``) replays every case through
``harness.bindings``, and the device half (task 21.3) replays the same
file through the LocalServer bindings, carrying each gate's state from
run to run; agreeing with the oracle makes the two agree with each other.

Usage, from ``edge-cv-portal/test-sandbox``::

    python tests/analytics_parity_fixtures.py --write

``test_analytics_parity_fixtures.py`` fails when the committed file is
stale.
"""

import json
import os
import random
import sys
from typing import Any, Dict, List, Optional, Tuple

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PORTAL_DIR = os.path.dirname(os.path.dirname(TESTS_DIR))
WORKFLOW_CORE_PYTHON = os.path.join(PORTAL_DIR, "backend", "layers",
                                    "workflow_core", "python")
FIXTURE_PATH = os.path.join(TESTS_DIR, "fixtures", "analytics_parity_cases.json")

SEED = 20260927
GENERATED_CASES = 40
START_MS = 1_790_000_000_000

#: The analytics executor bindings a case keeps from its compiled document.
ANALYTICS_BINDINGS = ("detection_counter", "object_association", "event_gate")


def _ensure_paths() -> None:
    # Appended, not prepended: the layer's python/ dir also vendors
    # Lambda-runtime wheels built for CPython 3.11 (see the workflow_core
    # tests conftest).
    if WORKFLOW_CORE_PYTHON not in sys.path:
        sys.path.append(WORKFLOW_CORE_PYTHON)


# ---------------------------------------------------------------------------
# Compiling a case's workflow
# ---------------------------------------------------------------------------

def compile_bindings(analytics_nodes: List[Tuple[str, str, Dict[str, Any]]]
                     ) -> List[Dict[str, Any]]:
    """The analytics executor bindings of folder source -> model inference
    -> the given ``(node id, node type, parameters)`` chain -> MQTT
    publish, compiled for x86_64 (the analytics bindings are identical on
    every architecture and in simulation)."""
    _ensure_paths()
    from workflow_core.compiler import compile as compile_graph
    from workflow_core.serializer import parse

    nodes = [
        {"id": "src", "type": "folder_source", "position": {"x": 0, "y": 0},
         "parameters": {"location": "/data/images"}},
        {"id": "det", "type": "model_inference", "position": {"x": 1, "y": 0},
         "parameters": {"modelName": "ppe-detector"}},
    ]
    for index, (node_id, node_type, parameters) in enumerate(analytics_nodes):
        nodes.append({"id": node_id, "type": node_type,
                      "position": {"x": index + 2, "y": 0},
                      "parameters": parameters})
    nodes.append({"id": "out", "type": "mqtt_publish",
                  "position": {"x": len(nodes), "y": 0},
                  "parameters": {"topic": "dda/analytics", "greengrass": True}})
    chain = [node["id"] for node in nodes]
    connections = [
        {"id": f"c{index}", "from": {"node": source, "port": "out"},
         "to": {"node": target, "port": "in"}}
        for index, (source, target) in enumerate(zip(chain, chain[1:]))
    ]
    definition = {"schemaVersion": 1, "nodes": nodes, "connections": connections}
    parsed = parse(json.dumps(definition))
    if not parsed.ok:
        raise AssertionError(f"fixture graph does not parse: {parsed.error}")
    document = compile_graph(parsed.graph, "x86_64")
    if isinstance(document, list):
        raise AssertionError(f"fixture graph does not compile: {document}")
    return [binding for binding in document.to_dict()["executorBindings"]
            if binding["binding"] in ANALYTICS_BINDINGS]


# ---------------------------------------------------------------------------
# The oracle
# ---------------------------------------------------------------------------

_MISSING = object()


def _resolve(metadata: Dict[str, Any], path: str) -> Any:
    current: Any = metadata
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return _MISSING
        current = current[segment]
    return current


def _atom(metadata: Dict[str, Any], atom: str) -> Optional[bool]:
    """One ``path op literal`` comparison, or None when the path does not
    resolve (the Condition_Language raises for an unknown field)."""
    path, operator, literal = atom.split(" ", 2)
    left = _resolve(metadata, path)
    if left is _MISSING:
        return None
    right: Any = literal[1:-1] if literal[0] == '"' else int(literal)
    return {
        "==": left == right, "!=": left != right,
        ">": left > right, ">=": left >= right,
        "<": left < right, "<=": left <= right,
    }[operator]


def oracle_verdict(condition: str, metadata: Dict[str, Any]) -> Optional[bool]:
    """The fixture conditions are one atom or a conjunction of atoms. An
    unresolved field anywhere makes the whole condition unevaluable,
    because the Condition_Language evaluates both sides of ``&&``."""
    verdicts = [_atom(metadata, atom) for atom in condition.split(" && ")]
    if any(verdict is None for verdict in verdicts):
        return None
    return all(verdicts)


def oracle_run(bindings, tags, detections, frame_size, states, now_ms):
    """One run: counters and associations over the Detection_List, then
    each gate over the metadata so far, in binding order."""
    from workflow_core.analytics import scene

    metadata: Dict[str, Any] = dict(tags)
    if detections is not None:
        metadata["detections"] = detections
    sections: Dict[str, Dict[str, Any]] = {}
    outcomes: Dict[str, str] = {}
    passed: Dict[str, bool] = {}
    next_states = dict(states)
    for binding in bindings:
        node_id, kind = binding["nodeId"], binding["binding"]
        parameters = binding["parameters"]
        if kind == "detection_counter":
            result = scene.count_detections(
                detections, classes=parameters["classes"],
                min_confidence=parameters["min_confidence"],
                zone=parameters["zone"], zone_rule=parameters["zone_rule"],
                frame_size=frame_size)
            section = "counter"
        elif kind == "object_association":
            result = scene.associate(
                detections, subject_class=parameters["subject_class"],
                required_classes=parameters["required_classes"],
                min_overlap=parameters["min_overlap"],
                min_confidence=parameters["min_confidence"],
                zone=parameters["zone"], frame_size=frame_size)
            section = "association"
        else:
            continue
        sections.setdefault(section, {})[node_id] = scene.run_metadata(result)
        outcomes[node_id] = result["outcome"]
    metadata.update(sections)
    for binding in bindings:
        if binding["binding"] != "event_gate":
            continue
        node_id, parameters = binding["nodeId"], binding["parameters"]
        verdict = oracle_verdict(parameters["condition"], metadata)
        state, gate_passed, transition = scene.step_event_gate(
            next_states.get(node_id, scene.EventGateState()), verdict,
            activate_after=parameters["activate_after"],
            clear_after=parameters["clear_after"], emit=parameters["emit"],
            repeat_interval_ms=parameters["repeat_interval_ms"],
            now_ms=now_ms)
        next_states[node_id] = state
        passed[node_id] = gate_passed
        sections.setdefault("event", {})[node_id] = \
            scene.event_gate_metadata(state, transition)
        metadata["event"] = sections["event"]
    return sections, outcomes, passed, next_states


def build_case(name: str, analytics_nodes, frame_size, runs,
               edit_parameters=None) -> Dict[str, Any]:
    """A case with the oracle's expectations for each run.

    ``runs`` is a list of ``(detections, tags, now_ms)``.
    ``edit_parameters`` (node id -> parameter overrides) is applied after
    compiling, for a document that reached the runtime with a value the
    validator would have rejected: the bindings must stay total.
    """
    bindings = compile_bindings(analytics_nodes)
    for binding in bindings:
        binding["parameters"].update((edit_parameters or {}).get(binding["nodeId"], {}))
    states: Dict[str, Any] = {}
    expected_runs = []
    for detections, tags, now_ms in runs:
        sections, outcomes, passed, states = oracle_run(
            bindings, tags, detections, frame_size, states, now_ms)
        expected_runs.append({
            "detections": detections, "tags": tags, "nowMs": now_ms,
            "expected": sections, "outcomes": outcomes, "passed": passed,
        })
    return {"name": name, "bindings": bindings, "frameSize": frame_size,
            "runs": expected_runs}


# ---------------------------------------------------------------------------
# Detections
# ---------------------------------------------------------------------------

TAGS = {"is_anomalous": False, "confidence": 0.9}


def box(identifier, label, confidence, x_min, y_min, x_max, y_max):
    return {"id": identifier, "label": label, "confidence": confidence,
            "x_min": x_min, "y_min": y_min, "x_max": x_max, "y_max": y_max}


def worker(prefix, x, hardhat=True, vest=True, confidence=0.9):
    """A person 200x500 at ``x`` with the requested PPE inside their box."""
    entries = [box(f"{prefix}-p", "person", confidence, x, 100, x + 200, 600)]
    if hardhat:
        entries.append(box(f"{prefix}-h", "hardhat", 0.8, x + 50, 90, x + 150, 160))
    if vest:
        entries.append(box(f"{prefix}-v", "vest", 0.7, x + 20, 250, x + 180, 400))
    return entries


def handwritten_cases() -> List[Dict[str, Any]]:
    frame = {"width": 1920, "height": 1080}
    left_half = "[[0, 0], [0.5, 0], [0.5, 1], [0, 1]]"
    cases = [
        build_case(
            "ppe violation persists, then clears",
            [("ppe", "object_association",
              {"subject_class": "person", "required_classes": "hardhat, vest"}),
             ("alarm", "event_gate",
              {"condition": "association.ppe.violations > 0",
               "activate_after": 2, "clear_after": 2, "emit": "on_change"})],
            frame,
            [(worker("a", 100), TAGS, START_MS),
             (worker("a", 100, vest=False), TAGS, START_MS + 500),
             (worker("a", 100, vest=False) + worker("b", 900), TAGS, START_MS + 1000),
             ([], TAGS, START_MS + 1500),
             ([], TAGS, START_MS + 2000),
             (None, TAGS, START_MS + 2500)]),
        build_case(
            "zone center versus overlap",
            [("by_center", "detection_counter",
              {"classes": "person, forklift", "zone": left_half,
               "zone_rule": "center"}),
             ("by_overlap", "detection_counter",
              {"classes": "person, forklift", "zone": left_half,
               "zone_rule": "overlap"})],
            {"width": 1000, "height": 1000},
            [([box("d1", "person", 0.9, 100, 100, 300, 600),
               # Center at x=550: outside the half, but overlapping it.
               box("d2", "person", 0.9, 450, 100, 650, 600),
               box("d3", "forklift", 0.9, 700, 500, 900, 900)],
              TAGS, START_MS)]),
        build_case(
            "zone with an unknown frame size is an error",
            [("people", "detection_counter",
              {"classes": "person", "zone": left_half}),
             ("gate", "event_gate",
              {"condition": "counter.people.total >= 0", "activate_after": 1})],
            None,
            [(worker("a", 100), TAGS, START_MS)]),
        build_case(
            "a malformed zone in the document is an error",
            [("people", "detection_counter", {"classes": "person"}),
             ("ppe", "object_association",
              {"subject_class": "person", "required_classes": "hardhat"})],
            frame,
            [(worker("a", 100), TAGS, START_MS)],
            edit_parameters={"people": {"zone": "[[0, 0], [2, 0]]"},
                             "ppe": {"zone": "not json"}}),
        build_case(
            "no detection list",
            [("people", "detection_counter", {"classes": "person, vest"}),
             ("ppe", "object_association",
              {"subject_class": "person", "required_classes": "vest"})],
            frame,
            [(None, TAGS, START_MS), ([], TAGS, START_MS + 100)]),
        build_case(
            "while active with a repeat interval",
            [("people", "detection_counter", {"classes": "person"}),
             ("gate", "event_gate",
              {"condition": "counter.people.total >= 1", "activate_after": 1,
               "clear_after": 1, "emit": "while_active",
               "repeat_interval_ms": 1000})],
            frame,
            [(worker("a", 100), TAGS, START_MS + offset)
             for offset in (0, 400, 1000, 1500, 2100)]
            + [([], TAGS, START_MS + 2600), (worker("a", 100), TAGS, START_MS + 2700)]),
        build_case(
            "an unevaluable condition counts as false",
            [("people", "detection_counter", {"classes": "person"}),
             ("gate", "event_gate",
              {"condition": "counter.missing.total > 0", "activate_after": 1})],
            frame,
            [(worker("a", 100), TAGS, START_MS),
             (worker("a", 100), TAGS, START_MS + 100)]),
        build_case(
            "a gate sees the gate before it",
            [("people", "detection_counter", {"classes": "person"}),
             ("first", "event_gate",
              {"condition": "counter.people.total > 0", "activate_after": 1,
               "clear_after": 1, "emit": "on_change"}),
             ("second", "event_gate",
              {"condition": 'event.first.state == "active"',
               "activate_after": 2, "clear_after": 1})],
            frame,
            [(worker("a", 100), TAGS, START_MS),
             (worker("a", 100), TAGS, START_MS + 100),
             ([], TAGS, START_MS + 200)]),
        build_case(
            "label keys and original labels",
            [("hats", "detection_counter",
              {"classes": "Hard Hat, Safety Vest"})],
            frame,
            [([box("d1", "hard-hat", 0.9, 0, 0, 10, 10),
               box("d2", "Hard Hat", 0.9, 20, 0, 30, 10),
               box("d3", "HARD_HAT", 0.9, 40, 0, 50, 10)], TAGS, START_MS)]),
        build_case(
            "confidence thresholds apply to subjects only",
            [("people", "detection_counter",
              {"classes": "person", "min_confidence": 0.5}),
             ("ppe", "object_association",
              {"subject_class": "person", "required_classes": "hardhat",
               "min_confidence": 0.6})],
            frame,
            [([box("p1", "person", 0.95, 100, 100, 300, 600),
               box("h1", "hardhat", 0.1, 150, 90, 250, 160),
               box("p2", "person", 0.55, 800, 100, 1000, 600),
               box("p3", "person", 0.3, 1200, 100, 1400, 600)], TAGS, START_MS)]),
        build_case(
            "matching is one to one",
            [("ppe", "object_association",
              {"subject_class": "person", "required_classes": "hardhat",
               "min_overlap": 0.5})],
            frame,
            [([box("p1", "person", 0.9, 100, 100, 300, 600),
               box("p2", "person", 0.9, 150, 100, 350, 600),
               box("h1", "hardhat", 0.9, 180, 90, 280, 160)], TAGS, START_MS)]),
    ]
    _check_anchors(cases)
    return cases


def _check_anchors(cases: List[Dict[str, Any]]) -> None:
    """Hand-computed values the oracle must reproduce: a check on the
    oracle itself, independent of the shared module's own tests."""
    by_name = {case["name"]: case for case in cases}

    ppe = by_name["ppe violation persists, then clears"]["runs"]
    assert [run["expected"]["association"]["ppe"]["violations"] for run in ppe] == \
        [0, 1, 1, 0, 0, 0]
    assert [run["expected"]["event"]["alarm"]["transition"] for run in ppe] == \
        ["none", "none", "activated", "none", "cleared", "none"]
    assert [run["passed"]["alarm"] for run in ppe] == \
        [False, False, True, False, True, False]
    assert ppe[2]["expected"]["association"]["ppe"]["violating_ids"] == ["a-p"]
    assert ppe[5]["outcomes"]["ppe"] == "warning"

    zone = by_name["zone center versus overlap"]["runs"][0]["expected"]["counter"]
    assert zone["by_center"]["counts"] == {"person": 1, "forklift": 0}
    assert zone["by_overlap"]["counts"] == {"person": 2, "forklift": 0}

    unknown = by_name["zone with an unknown frame size is an error"]["runs"][0]
    assert unknown["outcomes"] == {"people": "error"}
    assert unknown["expected"]["counter"]["people"]["total"] == 0

    malformed = by_name["a malformed zone in the document is an error"]["runs"][0]
    assert malformed["outcomes"] == {"people": "error", "ppe": "error"}

    repeat = by_name["while active with a repeat interval"]["runs"]
    assert [run["passed"]["gate"] for run in repeat] == \
        [True, False, True, False, True, False, True]

    unevaluable = by_name["an unevaluable condition counts as false"]["runs"]
    assert [run["expected"]["event"]["gate"]["consecutive_false"]
            for run in unevaluable] == [1, 2]

    chained = by_name["a gate sees the gate before it"]["runs"]
    assert [run["expected"]["event"]["second"]["consecutive_true"]
            for run in chained] == [1, 2, 0]
    assert chained[1]["passed"]["second"] is True

    hats = by_name["label keys and original labels"]["runs"][0]["expected"]["counter"]["hats"]
    assert hats["counts"] == {"hard_hat": 3, "safety_vest": 0}
    assert hats["labels"] == {"hard_hat": "hard-hat", "safety_vest": "Safety Vest"}

    confidence = by_name["confidence thresholds apply to subjects only"]["runs"][0]["expected"]
    assert confidence["counter"]["people"]["total"] == 2
    assert confidence["association"]["ppe"]["subjects"] == 1
    assert confidence["association"]["ppe"]["compliant"] == 1

    one_to_one = by_name["matching is one to one"]["runs"][0]["expected"]["association"]["ppe"]
    assert (one_to_one["compliant"], one_to_one["violations"]) == (1, 1)


# ---------------------------------------------------------------------------
# Generated cases
# ---------------------------------------------------------------------------

_LABELS = ["person", "person", "person", "hardhat", "hardhat", "vest",
           "forklift", "Hard Hat", "safety-vest"]
_CLASSES = ["", "person", "person, hardhat", "hardhat, vest, forklift",
            "Hard Hat, Safety Vest"]
_REQUIRED = ["hardhat", "vest", "hardhat, vest", "hard hat, safety vest"]
_ZONES = ["", "", "[[0, 0], [0.5, 0], [0.5, 1], [0, 1]]",
          "[[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]",
          "[[0, 0], [1, 0], [0, 1]]"]
_FRAMES = [{"width": 1920, "height": 1080}, {"width": 1280, "height": 720},
           {"width": 640, "height": 640}, None]
_OPERATORS = [">", ">=", "<", "<=", "==", "!="]


def _detections(rng: random.Random, frame) -> Optional[List[Dict[str, Any]]]:
    if rng.random() < 0.1:
        return None
    width = (frame or {"width": 1920})["width"]
    height = (frame or {"height": 1080})["height"]
    entries = []
    for index in range(rng.randint(0, 7)):
        w = rng.randint(10, max(11, width // 3))
        h = rng.randint(10, max(11, height // 2))
        x = rng.randint(0, width - w)
        y = rng.randint(0, height - h)
        label = rng.choice(_LABELS)
        if label in ("hardhat", "vest", "Hard Hat", "safety-vest") and entries \
                and rng.random() < 0.6:
            # Put protective equipment inside an earlier person's box.
            host = rng.choice(entries)
            x = rng.randint(host["x_min"], max(host["x_min"], host["x_max"] - 10))
            y = rng.randint(host["y_min"], max(host["y_min"], host["y_max"] - 10))
            w = rng.randint(5, max(6, host["x_max"] - x))
            h = rng.randint(5, max(6, host["y_max"] - y))
        entries.append(box(f"d{index}", label, round(rng.uniform(0.05, 1.0), 2),
                           x, y, x + w, y + h))
    return entries


def _condition(rng: random.Random, sources: List[Tuple[str, str]],
               gates: List[str]) -> str:
    atoms = []
    for _ in range(rng.choice([1, 1, 2])):
        choice = rng.random()
        if gates and choice < 0.15:
            atoms.append('event.{0}.state == "{1}"'.format(
                rng.choice(gates), rng.choice(["active", "inactive"])))
            continue
        if not sources or choice > 0.92:
            atoms.append("counter.nowhere.total > 0")
            continue
        node_id, kind = rng.choice(sources)
        if kind == "detection_counter":
            path = rng.choice([f"counter.{node_id}.total",
                               f"counter.{node_id}.counts.person",
                               f"counter.{node_id}.counts.hardhat"])
        else:
            path = rng.choice([f"association.{node_id}.violations",
                               f"association.{node_id}.compliant",
                               f"association.{node_id}.subjects"])
        atoms.append(f"{path} {rng.choice(_OPERATORS)} {rng.randint(0, 3)}")
    return " && ".join(atoms)


def generated_cases() -> List[Dict[str, Any]]:
    rng = random.Random(SEED)
    cases = []
    for number in range(GENERATED_CASES):
        nodes = []
        sources: List[Tuple[str, str]] = []
        gates: List[str] = []
        for index in range(rng.randint(1, 4)):
            kind = rng.choice(["detection_counter", "object_association",
                               "event_gate"])
            node_id = f"n{index}"
            if kind == "detection_counter":
                parameters = {"classes": rng.choice(_CLASSES),
                              "min_confidence": rng.choice([0.0, 0.0, 0.5, 0.8]),
                              "zone": rng.choice(_ZONES),
                              "zone_rule": rng.choice(["center", "overlap"])}
                sources.append((node_id, kind))
            elif kind == "object_association":
                parameters = {"subject_class": rng.choice(["person", "Person"]),
                              "required_classes": rng.choice(_REQUIRED),
                              "min_overlap": rng.choice([0.05, 0.5, 0.9]),
                              "min_confidence": rng.choice([0.0, 0.4]),
                              "zone": rng.choice(_ZONES)}
                sources.append((node_id, kind))
            else:
                emit = rng.choice(["on_activate", "on_change", "while_active"])
                parameters = {"condition": _condition(rng, sources, gates),
                              "activate_after": rng.randint(1, 3),
                              "clear_after": rng.randint(1, 3), "emit": emit}
                if emit == "while_active":
                    parameters["repeat_interval_ms"] = rng.choice([0, 0, 250, 1000])
                gates.append(node_id)
            nodes.append((node_id, kind, parameters))
        frame = rng.choice(_FRAMES)
        now = START_MS
        runs = []
        for _ in range(rng.randint(2, 6)):
            now += rng.choice([100, 250, 400, 1000])
            runs.append((_detections(rng, frame), TAGS, now))
        cases.append(build_case(f"generated {number}", nodes, frame, runs))
    return cases


def corpus() -> Dict[str, Any]:
    _ensure_paths()
    return {
        "generatedBy": "edge-cv-portal/test-sandbox/tests/analytics_parity_fixtures.py",
        "property": "rtsp-rtmp-stream-cameras Property 27: analytics parity "
                    "between the device and the sandbox",
        "cases": handwritten_cases() + generated_cases(),
    }


def serialize(data: Dict[str, Any]) -> str:
    return json.dumps(data, indent=1, ensure_ascii=True) + "\n"


def main(argv: List[str]) -> int:
    text = serialize(corpus())
    if "--write" in argv:
        os.makedirs(os.path.dirname(FIXTURE_PATH), exist_ok=True)
        with open(FIXTURE_PATH, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"wrote {FIXTURE_PATH} ({len(text)} bytes)")
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
