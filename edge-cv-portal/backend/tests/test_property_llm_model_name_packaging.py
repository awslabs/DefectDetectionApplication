"""Bug condition exploration test — packaged vLLM model name mismatch.

Bugfix spec: vllm-model-name-mismatch (task 1).

**Property 1: Bug Condition — Packaged LLM modelName Equals Served Name**

The vLLM publish pipeline sanitizes the portal registry model name
(``_safe_model_name``: lowercase, every character outside ``[a-zA-Z0-9-]``
becomes ``-``) when deriving the Greengrass component name and the Triton
repository directory, so the device serves the model under the SANITIZED
name. Workflow packaging, however, compiles the ``llm_inference`` node's
``modelName`` parameter VERBATIM from the registry name into both packaged
artifacts (``workflow.json`` and ``compiled_pipeline.json``), and the device
workflow engine passes that verbatim name into the text-generation URL —
guaranteeing a 409 for every registry name that is not sanitization-stable.

This is an EXPLORATION test written against the UNFIXED code: it asserts
the CORRECTED behavior (packaged ``modelName`` equals the served name
``safe_model_name(registry_name)`` in BOTH artifacts), so it is EXPECTED TO
FAIL on the current packaging path for every non-sanitization-stable name.
The failure confirms the bug exists; the same test validates the fix when
it passes afterward.

Live counterexample (verified on JP6 hardware): registry name
``Qwen2.5-7B-Instruct-AWQ`` is served as ``qwen2-5-7b-instruct-awq``; the
published component 6.0.0 artifact carries the verbatim
``"modelName": "Qwen2.5-7B-Instruct-AWQ"`` and every LLM inference gets
409 ``state: 'unknown'``.

**Validates: Requirements 1.1, 1.2, 2.1**

The packaging compile/serialization path is pure over the definition JSON
(parse -> compile -> serialize), so it is exercised directly with no AWS
calls. ``workflow_packaging`` is imported through the shared moto-backed
session fixture only so its module-level boto3 clients are intercepted and
the real ``shared_utils`` layer backs the import, mirroring the other
packaging property tests.
"""

from __future__ import annotations

import json
import re
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.compiler import CompileContext
from workflow_core.compiler import compile as compile_workflow
from workflow_core.serializer import parse as parse_definition

# ---------------------------------------------------------------------------
# Reference expectation, restated from the design (Bug Condition /
# safe_model_name) — deliberately NOT imported from the implementation, so
# the test cannot silently agree with a wrong or missing transform.
# ---------------------------------------------------------------------------


def served_model_name(registry_name: str) -> str:
    """The name the publish pipeline serves the model under on the device:
    the Triton repository directory / component-name transform applied by
    ``greengrass_publish.py::_safe_model_name`` and
    ``packaging.py::_safe_model_name``."""
    return re.sub(r"[^a-zA-Z0-9-]", "-", str(registry_name).lower())


#: A vLLM-capable architecture (llm_inference maps only on these).
VLLM_ARCH = "arm64_jp6"

#: The live counterexample verified on JP6 hardware (bugfix.md).
LIVE_REGISTRY_NAME = "Qwen2.5-7B-Instruct-AWQ"
LIVE_SERVED_NAME = "qwen2-5-7b-instruct-awq"


@pytest.fixture(scope="module")
def packaging(aws_stack):
    """Import workflow_packaging inside the moto mock so its module-level
    boto3 clients (DynamoDB / S3 / KMS) are intercepted."""
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    return workflow_packaging


# ---------------------------------------------------------------------------
# Minimal Workflow_Definition with one llm_inference node
# ---------------------------------------------------------------------------


def minimal_llm_definition_json(registry_name: str) -> str:
    """A minimal valid Workflow_Definition document: a video-frame source
    feeding one ``llm_inference`` node (modelName = the registry name),
    emitting into an mqtt_publish sink. folder_source (not a camera input
    node) keeps the compiled document free of camera bindingPoints, so the
    serialization below is byte-identical to the packaging path's."""
    definition = {
        "schemaVersion": 1,
        "nodes": [
            {
                "id": "src",
                "type": "folder_source",
                "position": {"x": 0, "y": 0},
                "parameters": {"location": "/aws_dda/images"},
            },
            {
                "id": "llm",
                "type": "llm_inference",
                "position": {"x": 200, "y": 0},
                "parameters": {
                    "modelName": registry_name,
                    "prompt_template": "Describe this frame",
                },
            },
            {
                "id": "mq",
                "type": "mqtt_publish",
                "position": {"x": 400, "y": 0},
                "parameters": {"broker_host": "broker.local", "topic": "dda/out"},
            },
        ],
        "connections": [
            {
                "id": "c1",
                "from": {"node": "src", "port": "out"},
                "to": {"node": "llm", "port": "in"},
            },
            {
                "id": "c2",
                "from": {"node": "llm", "port": "out"},
                "to": {"node": "mq", "port": "in"},
            },
        ],
    }
    return json.dumps(definition)


def package_artifacts(packaging, definition_json: str):
    """Run a definition through the packaging compile/serialization path
    exactly as ``package_workflow`` does for a built-in-only workflow, and
    return the two artifact documents as parsed dicts:

    - ``workflow.json``: ``package_workflow`` serializes the stored
      definition through ``packaged_workflow_definition_json`` before
      ``build_arch_zip`` writes it into the zip;
    - ``compiled_pipeline.json``: the per-arch compiler output serialized
      by ``compiled_document_json`` (no camera nodes -> no bindingPoints).
    """
    parse_result = parse_definition(definition_json)
    assert parse_result.ok, f"definition failed to parse: {parse_result.error}"
    graph = parse_result.graph

    context = CompileContext(workflow_id="wf-llm-name", workflow_version="1")
    compiled = compile_workflow(graph, VLLM_ARCH, context, simulation=False)
    assert not isinstance(compiled, list), (
        "expected a compiled document, got errors: "
        f"{[e.to_dict() for e in compiled]}"
    )

    workflow_doc = json.loads(
        packaging.packaged_workflow_definition_json(
            definition_json, json.loads(definition_json),
            {}))  # workflow.json content
    compiled_doc = json.loads(
        packaging.compiled_document_json(compiled, []))  # compiled_pipeline.json
    return workflow_doc, compiled_doc


def packaged_model_names(workflow_doc: dict, compiled_doc: dict):
    """The llm node's ``modelName`` as each packaged artifact carries it."""
    workflow_name = next(
        node["parameters"]["modelName"]
        for node in workflow_doc["nodes"] if node["type"] == "llm_inference")
    compiled_name = next(
        binding["parameters"]["modelName"]
        for binding in compiled_doc["executorBindings"]
        if binding["binding"] == "llm_inference")
    return workflow_name, compiled_name


# ---------------------------------------------------------------------------
# Hypothesis strategies: registry names with characters outside [a-z0-9-]
# (mixed case, dots, underscores) — the non-sanitization-stable domain —
# plus the stable domain that masked the bug.
# ---------------------------------------------------------------------------

_STABLE_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789-"
_UNSAFE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ._"


@st.composite
def unsafe_registry_names(draw):
    """Registry model names guaranteed non-sanitization-stable: at least
    one character outside [a-z0-9-] (capital, dot, or underscore)."""
    chars = draw(st.lists(
        st.sampled_from(_STABLE_ALPHABET + _UNSAFE_ALPHABET),
        min_size=1, max_size=29))
    unsafe_char = draw(st.sampled_from(_UNSAFE_ALPHABET))
    insert_at = draw(st.integers(min_value=0, max_value=len(chars)))
    chars.insert(insert_at, unsafe_char)
    name = "".join(chars)
    assert served_model_name(name) != name
    return name


stable_registry_names = st.text(
    alphabet=_STABLE_ALPHABET, min_size=1, max_size=30)


# ---------------------------------------------------------------------------
# Property 1: Bug Condition — Packaged LLM modelName Equals Served Name
# ---------------------------------------------------------------------------


class TestPackagedLlmModelNameEqualsServedName:
    """**Property 1: Bug Condition — Packaged LLM modelName Equals Served Name**

    **Validates: Requirements 1.1, 1.2, 2.1**
    """

    def test_live_counterexample_qwen(self, packaging):
        """The live JP6 counterexample: registry name
        ``Qwen2.5-7B-Instruct-AWQ`` must be packaged as the served name
        ``qwen2-5-7b-instruct-awq`` in BOTH artifacts.

        EXPECTED OUTCOME on UNFIXED code: FAILS — both artifacts carry the
        verbatim registry name, the exact 409-producing mismatch verified
        live on JP6 hardware.
        """
        assert served_model_name(LIVE_REGISTRY_NAME) == LIVE_SERVED_NAME

        workflow_doc, compiled_doc = package_artifacts(
            packaging, minimal_llm_definition_json(LIVE_REGISTRY_NAME))
        workflow_name, compiled_name = packaged_model_names(
            workflow_doc, compiled_doc)

        assert workflow_name == LIVE_SERVED_NAME, (
            f"workflow.json packages modelName {workflow_name!r}; the device "
            f"serves the model as {LIVE_SERVED_NAME!r} -> 409 on every "
            "LLM inference")
        assert compiled_name == LIVE_SERVED_NAME, (
            f"compiled_pipeline.json packages modelName {compiled_name!r}; "
            f"the device serves the model as {LIVE_SERVED_NAME!r} -> 409 on "
            "every LLM inference")

    @settings(deadline=None)
    @given(registry_name=unsafe_registry_names())
    def test_unsafe_names_packaged_as_served_name(self, packaging, registry_name):
        """For any registry model name containing characters outside
        ``[a-z0-9-]`` (mixed case, dots, underscores), the packaged
        artifacts SHALL carry the llm node's ``modelName`` equal to
        ``safe_model_name(registry_name)`` — the served name — in both
        ``workflow.json`` and ``compiled_pipeline.json``.

        EXPECTED OUTCOME on UNFIXED code: FAILS for every generated name
        (packaging writes the registry name verbatim).
        """
        expected = served_model_name(registry_name)

        workflow_doc, compiled_doc = package_artifacts(
            packaging, minimal_llm_definition_json(registry_name))
        workflow_name, compiled_name = packaged_model_names(
            workflow_doc, compiled_doc)

        assert workflow_name == expected, (
            f"workflow.json modelName {workflow_name!r} != served name "
            f"{expected!r} for registry name {registry_name!r}")
        assert compiled_name == expected, (
            f"compiled_pipeline.json modelName {compiled_name!r} != served "
            f"name {expected!r} for registry name {registry_name!r}")

    @settings(deadline=None)
    @given(registry_name=stable_registry_names)
    def test_stable_names_already_match_served_name(self, packaging, registry_name):
        """Sanitization-stable subcase: names already in ``[a-z0-9-]``
        (e.g. the historic smoke model ``opt125m-smoke``) are packaged
        equal to the served name even on UNFIXED code — the rewrite is a
        no-op for them, which is exactly why the smoke tests never caught
        the bug.

        EXPECTED OUTCOME on UNFIXED code: PASSES (documents the masking).
        """
        assert served_model_name(registry_name) == registry_name

        workflow_doc, compiled_doc = package_artifacts(
            packaging, minimal_llm_definition_json(registry_name))
        workflow_name, compiled_name = packaged_model_names(
            workflow_doc, compiled_doc)

        assert workflow_name == registry_name
        assert compiled_name == registry_name
