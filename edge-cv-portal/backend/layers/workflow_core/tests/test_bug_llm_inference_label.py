"""Bug 4 exploration test — VLM/LLM inference node label.

Bugfix spec: workflow-manager-integration-bugfixes (Bug 4).

The `llm_inference` node is labelled "LLM Inference" in the palette and
on the canvas. As a vision-language node it should read
"VLM/LLM Inference". The palette and canvas render `display_name`
directly from the catalog, so the label is a pure data value in the
node-catalog source of truth.

This is an EXPLORATION test written against the UNFIXED code: it asserts
the CORRECTED behavior (Property 4 — Bug Condition), so it is EXPECTED TO
FAIL on the current catalog (the `display_name` is "LLM Inference"). The
failure confirms the bug exists.

Property 4: Bug Condition — Node label reads "VLM/LLM Inference"
  For any node-type descriptor where isBugCondition4 holds
  (type_id == "llm_inference" labelled "LLM Inference"), the fixed
  catalog SHALL present its display_name as "VLM/LLM Inference" while
  keeping its type_id equal to "llm_inference".

Validates: Requirements 1.4, 2.4
"""

from workflow_core.catalog import get_node_type


class TestBug4LlmInferenceLabel:
    """Property 4: Bug Condition — Node label reads "VLM/LLM Inference"."""

    def test_llm_inference_display_name_is_vlm_llm_inference(self):
        # Fix-checking assertion: the display_name SHALL read
        # "VLM/LLM Inference".
        # EXPECTED OUTCOME on UNFIXED code: FAILS (currently
        # "LLM Inference").
        descriptor = get_node_type("llm_inference")
        assert descriptor is not None
        assert descriptor.display_name == "VLM/LLM Inference"

    def test_llm_inference_type_id_unchanged(self):
        # Preservation: the identifier must stay "llm_inference" (saved
        # workflows and compiler bindings key on it).
        descriptor = get_node_type("llm_inference")
        assert descriptor is not None
        assert descriptor.type_id == "llm_inference"
