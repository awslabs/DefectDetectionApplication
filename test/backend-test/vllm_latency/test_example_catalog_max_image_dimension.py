# Copyright 2026 Amazon Web Services, Inc.
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
"""Example test for the catalog ``max_image_dimension`` descriptor (task 8.3).

# Feature: vllm-workflow-latency-optimization, Example: catalog
# max_image_dimension descriptor presence and shape

Byte-identity of the portal and vendored ``nodes.py`` copies is already
pinned by
``test/backend-test/workflow_engine/test_vendored_catalog_mirror.py`` —
this file deliberately does NOT duplicate that assertion. It pins what the
mirror test cannot: the new ``max_image_dimension`` ParameterDescriptor
(added by task 8.1) is present in BOTH catalog copies with the expected
shape — type ``"int"``, ``required=False``, ``default=None``,
``constraints={"min": 1}``, a non-empty description, and examples that
each satisfy the descriptor's own type and constraints.

**Validates: Requirements 9.3**

Both copies are checked symmetrically by parsing their source (no import
of the portal layer package is needed, avoiding a ``workflow_core``
package-name clash with the vendored copy), and the vendored copy is
additionally checked structurally through the loaded catalog so the
runtime object the LocalServer engine actually serves carries the same
shape.
"""

import ast
from pathlib import Path

import pytest

PORTAL_NODES_RELATIVE = Path(
    "edge-cv-portal/backend/layers/workflow_core/python/workflow_core/"
    "catalog/nodes.py"
)
VENDORED_NODES_RELATIVE = Path(
    "src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py"
)

PARAMETER_NAME = "max_image_dimension"


def _repo_root() -> Path:
    """Walk up from this file until both catalog copies are present."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / PORTAL_NODES_RELATIVE).is_file() and (
            candidate / VENDORED_NODES_RELATIVE
        ).is_file():
            return candidate
    raise AssertionError(
        "Could not locate the repository root containing both "
        f"{PORTAL_NODES_RELATIVE} and {VENDORED_NODES_RELATIVE}"
    )


def _find_descriptor_calls(source: str, parameter_name: str) -> list:
    """Return every ``ParameterDescriptor(...)`` call for the given name."""
    calls = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        func_name = getattr(func, "id", None) or getattr(func, "attr", None)
        if func_name != "ParameterDescriptor":
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and first.value == parameter_name:
            calls.append(node)
    return calls


def _assert_expected_shape(param_type, required, default, constraints,
                           description, examples, source_label):
    assert param_type == "int", source_label
    assert required is False, source_label
    assert default is None, source_label
    assert constraints == {"min": 1}, source_label
    assert isinstance(description, str) and description.strip(), source_label
    assert isinstance(examples, list) and examples, source_label
    for example in examples:
        # Each example must satisfy the descriptor's own type ("int",
        # bool excluded) and constraints ({"min": 1}).
        assert isinstance(example, int) and not isinstance(example, bool), (
            source_label, example)
        assert example >= 1, (source_label, example)


@pytest.mark.parametrize(
    "relative",
    [PORTAL_NODES_RELATIVE, VENDORED_NODES_RELATIVE],
    ids=["portal", "vendored"],
)
def test_max_image_dimension_descriptor_present_with_expected_shape(relative):
    """Both catalog copies declare the descriptor with the expected shape."""
    path = _repo_root() / relative
    calls = _find_descriptor_calls(path.read_text(encoding="utf-8"),
                                   PARAMETER_NAME)
    assert len(calls) == 1, (
        f"Expected exactly one ParameterDescriptor({PARAMETER_NAME!r}, ...) "
        f"in {relative}, found {len(calls)}"
    )
    call = calls[0]

    # Positional layout: ParameterDescriptor(name, param_type, ...).
    assert len(call.args) >= 2, relative
    param_type = ast.literal_eval(call.args[1])
    keywords = {
        kw.arg: ast.literal_eval(kw.value)
        for kw in call.keywords
        if kw.arg is not None
    }

    _assert_expected_shape(
        param_type=param_type,
        required=keywords.get("required"),
        default=keywords.get("default"),
        constraints=keywords.get("constraints"),
        description=keywords.get("description"),
        examples=keywords.get("examples"),
        source_label=str(relative),
    )


def test_vendored_catalog_serves_descriptor_on_llm_inference_node():
    """The loaded vendored catalog exposes the descriptor structurally."""
    from workflow_engine.vendor.workflow_core.catalog import get_node_type

    node_type = get_node_type("llm_inference")
    assert node_type is not None

    matching = [p for p in node_type.parameters
                if p.name == PARAMETER_NAME]
    assert len(matching) == 1, (
        f"Expected exactly one {PARAMETER_NAME!r} parameter on the "
        f"llm_inference node, found {len(matching)}"
    )
    descriptor = matching[0]

    _assert_expected_shape(
        param_type=descriptor.param_type,
        required=descriptor.required,
        default=descriptor.default,
        constraints=descriptor.constraints,
        description=descriptor.description,
        examples=descriptor.examples,
        source_label="loaded vendored catalog",
    )
