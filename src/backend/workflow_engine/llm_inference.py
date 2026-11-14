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

# The renderer moved verbatim to the shared workflow_core package
# (anomaly_invocation, standard library only), so the device's
# inspection path and the Portal's replay of an inspection-format sample
# render with one implementation. The names are re-exported here: every
# importer keeps working, and the exception class (with its ``name``
# attribute) is the same object everywhere.
from workflow_engine.vendor.workflow_core.anomaly_invocation import (  # noqa: F401
    PLACEHOLDER_RE,
    UnresolvedPlaceholderError,
    render_prompt,
)
