"""
Shared model-name sanitization transform (single source of truth).

The vLLM publish pipeline derives the Greengrass component name
(``model-vllm-{safe_name}`` — greengrass_publish.py) and the Triton
repository directory (``{safe_name}`` — packaging.py) from the portal
registry model name through this transform, so the device serves the
model under the SANITIZED name. Workflow packaging
(workflow_packaging.py) applies the same transform when rewriting each
``llm_inference`` node's ``modelName`` into the packaged artifacts, so
the packaged name always equals the served name
(vllm-model-name-mismatch Requirements 2.1, 2.2).

Keep the regex here and ONLY here: a drifting copy is exactly the bug
this module fixes (packaged ``Qwen2.5-7B-Instruct-AWQ`` vs served
``qwen2-5-7b-instruct-awq`` -> 409 on every LLM inference).
"""
import re


def safe_model_name(model_name: str) -> str:
    """Sanitized model name: lowercase, every character outside
    ``[a-zA-Z0-9-]`` replaced with ``-``."""
    return re.sub(r'[^a-zA-Z0-9-]', '-', str(model_name).lower())
