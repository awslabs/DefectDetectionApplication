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
"""Unit tests for LLM prompt template rendering (Requirements 7.3, 7.5)."""

import pytest

from workflow_engine.llm_inference import (
    PLACEHOLDER_RE,
    UnresolvedPlaceholderError,
    render_prompt,
)


class TestRenderPrompt:
    def test_literal_text_preserved(self):
        assert render_prompt("no placeholders here", {}) == "no placeholders here"

    def test_simple_substitution(self):
        assert render_prompt("value: {x}", {"x": 42}) == "value: 42"

    def test_dotted_name_resolves_nested_keys(self):
        metadata = {"inference": {"label": "scratch", "confidence": 0.93}}
        assert (
            render_prompt(
                "Defect {inference.label} at {inference.confidence}", metadata
            )
            == "Defect scratch at 0.93"
        )

    def test_double_braces_escape_literals(self):
        assert render_prompt("a {x.y} {{b}}", {"x": {"y": 1}}) == "a 1 {b}"

    def test_escaped_braces_only(self):
        assert render_prompt("{{}}", {}) == "{}"

    def test_missing_key_raises_naming_placeholder(self):
        with pytest.raises(UnresolvedPlaceholderError) as excinfo:
            render_prompt("hello {name}", {})
        assert excinfo.value.name == "name"
        assert "name" in str(excinfo.value)

    def test_missing_nested_key_raises_full_dotted_name(self):
        with pytest.raises(UnresolvedPlaceholderError) as excinfo:
            render_prompt("{a.b.c}", {"a": {"b": {}}})
        assert excinfo.value.name == "a.b.c"

    def test_non_dict_intermediate_raises(self):
        with pytest.raises(UnresolvedPlaceholderError) as excinfo:
            render_prompt("{a.b}", {"a": "flat"})
        assert excinfo.value.name == "a.b"

    def test_first_missing_key_wins(self):
        with pytest.raises(UnresolvedPlaceholderError) as excinfo:
            render_prompt("{present} {gone} {also_gone}", {"present": 1})
        assert excinfo.value.name == "gone"

    def test_value_stringified(self):
        assert render_prompt("{v}", {"v": [1, 2]}) == "[1, 2]"

    def test_brace_without_valid_placeholder_is_literal(self):
        # '{ }' and '{1x}' do not match PLACEHOLDER_RE: kept as literal text.
        assert render_prompt("{ } {1x}", {}) == "{ } {1x}"

    def test_placeholder_re_shape(self):
        assert PLACEHOLDER_RE.findall("{a} {b.c} {{d}} {_e1}") == ["a", "b.c", "d", "_e1"]
