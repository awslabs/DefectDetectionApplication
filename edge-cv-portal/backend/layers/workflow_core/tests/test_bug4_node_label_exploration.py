"""Bug 4 exploration test — VLM/LLM inference node label.

Property 4: Bug Condition — Node label reads "VLM/LLM Inference".

This is an EXPLORATION test written against the UNFIXED code. It is
EXPECTED TO FAIL until Bug 4 is fixed: today the ``llm_inference``
descriptor's ``display_name`` is "LLM Inference", so the palette and the
canvas render "LLM Inference" instead of the expected "VLM/LLM Inference".

isBugCondition4(X) := X.type_id == "llm_inference"
                      AND X.display_name == "LLM Inference"

Fix-checking property (Property 4):
    displayName(catalog'("llm_inference")) == "VLM/LLM Inference"
    typeId(catalog'("llm_inference"))      == "llm_inference"  # unchanged

Counterexample on unfixed code: the palette/canvas label reads
"LLM Inference".

Validates: Requirements 1.4, 2.4
"""

from workflow_core.catalog import get_node_type


class TestBug4NodeLabel:
    def test_llm_inference_display_name_is_vlm_llm_inference(self):
        # Fix-checking property: the fixed catalog SHALL present the
        # llm_inference display name as "VLM/LLM Inference". Expected to
        # FAIL on unfixed code (currently "LLM Inference").
        descriptor = get_node_type("llm_inference")
        assert descriptor is not None
        assert descriptor.display_name == "VLM/LLM Inference"

    def test_llm_inference_type_id_unchanged(self):
        # The identifier must stay "llm_inference": saved workflows and
        # compiler bindings key on it (Requirement 3.5). This holds on
        # both unfixed and fixed code.
        descriptor = get_node_type("llm_inference")
        assert descriptor is not None
        assert descriptor.type_id == "llm_inference"
