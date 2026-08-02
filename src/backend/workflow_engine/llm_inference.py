#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""LLM inference prompt rendering (Requirements 7.3, 7.5).

Strict Prompt_Template substitution for the ``llm_inference`` executor
binding (vllm-triton-inference feature). ``render_prompt`` replaces every
``{placeholder}`` in a template with the corresponding value from the
upstream Inference_Metadata:

- placeholder names match ``[A-Za-z_][A-Za-z0-9_.]*``; dotted names
  resolve nested dictionary keys (``{x.y}`` reads ``metadata['x']['y']``);
- resolved values are substituted as ``str(value)``;
- literal text is preserved unchanged; ``{{`` and ``}}`` escape a literal
  ``{`` / ``}``;
- the first placeholder that cannot be resolved raises
  :class:`UnresolvedPlaceholderError` naming it, so the caller (the
  ``LlmInferenceProcessor``, task 11.2) can record the node execution as
  failed without invoking the Text_Generation_API (Requirement 7.5).

Stdlib-only on purpose: this module is imported by the workflow engine's
post-run processing and must stay importable everywhere.
"""

import re
from typing import Any, Dict

#: A placeholder: '{' + identifier (optionally dotted) + '}'.
PLACEHOLDER_RE = re.compile(r'\{([A-Za-z_][A-Za-z0-9_.]*)\}')


class UnresolvedPlaceholderError(Exception):
    """A Prompt_Template placeholder has no value in the upstream
    Inference_Metadata (Requirement 7.5)."""

    def __init__(self, name: str):
        super().__init__("unresolved placeholder {0}".format(name))
        #: The full (possibly dotted) placeholder name that failed.
        self.name = name


def _resolve(name: str, metadata: Dict[str, Any]) -> Any:
    """Resolve a possibly dotted placeholder name against nested dicts."""
    value: Any = metadata
    for part in name.split("."):
        if not isinstance(value, dict) or part not in value:
            raise UnresolvedPlaceholderError(name)
        value = value[part]
    return value


def render_prompt(template: str, metadata: Dict[str, Any]) -> str:
    """Render a Prompt_Template against upstream Inference_Metadata.

    Strict substitution: every ``{placeholder}`` is replaced by
    ``str(metadata[name])`` (dotted names resolve nested keys); literal
    text is preserved; ``'{{'``/``'}}'`` escape a literal brace. Raises
    :class:`UnresolvedPlaceholderError` on the first missing key
    (Requirements 7.3, 7.5).
    """
    rendered = []
    position = 0
    length = len(template)
    while position < length:
        char = template[position]
        if char == "{":
            if template.startswith("{{", position):
                rendered.append("{")
                position += 2
                continue
            match = PLACEHOLDER_RE.match(template, position)
            if match:
                rendered.append(str(_resolve(match.group(1), metadata)))
                position = match.end()
                continue
            rendered.append(char)
            position += 1
        elif char == "}":
            if template.startswith("}}", position):
                rendered.append("}")
                position += 2
                continue
            rendered.append(char)
            position += 1
        else:
            rendered.append(char)
            position += 1
    return "".join(rendered)
