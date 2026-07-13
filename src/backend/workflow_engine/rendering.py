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

"""Launch-string rendering of the Compiled Pipeline Document (Requirement 9.2).

Renders the exact dialect ``GstPipelineManager.run_pipeline`` executes
(the same rule the cloud test sandbox harness uses, so cloud tests and
edge runs agree byte-for-byte):

- Elements render as ``factory arg=value arg=value`` and are joined
  with ``" ! "``.
- A segment with ``"from": "t0"`` hangs off the tee named ``t0`` and is
  rendered as ``t0. ! queue ! ...``.
- A segment with ``"linkTo": "f1"`` feeds the funnel named ``f1`` and is
  rendered as ``... ! f1.``.
- Segments are joined with a single space — the ``Gst.parse_launch``
  branch syntax the existing Pipeline_Configuration builder already uses.

Also builds the element-name -> nodeId map used to identify the failing
workflow node from a GStreamer bus error (Requirement 9.7):
``parse_launch`` names each element ``<factory><N>`` with a per-factory
creation counter unless the element carries an explicit ``name=``
argument.

Dependency-free (pure functions over the parsed JSON document) so it is
importable and testable without GStreamer.
"""

import re
from typing import Any, Dict, Optional


def render_value(value: Any) -> str:
    """One argument value in launch-string form (bools lower-cased the
    way GStreamer parses them; everything else via ``str``)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render_element(element: Dict) -> str:
    """``factory arg=value ...`` — the PluginDefinition dialect."""
    parts = [element["factory"]]
    for name, value in element.get("args", {}).items():
        parts.append("{0}={1}".format(name, render_value(value)))
    return " ".join(parts)


def render_segment(segment: Dict) -> str:
    """One segment: elements joined with ``" ! "``, prefixed with the
    tee reference (``t0. ! ...``) and suffixed with the funnel link
    (``... ! f0.``) when present."""
    body = " ! ".join(render_element(element) for element in segment["elements"])
    if segment.get("from"):
        body = "{0}. ! {1}".format(segment["from"], body)
    if segment.get("linkTo"):
        body = "{0} ! {1}.".format(body, segment["linkTo"])
    return body


def render_launch_string(document: Dict) -> str:
    """The complete ``gst-launch``-style string for the document — the
    string the executor hands to ``GstPipelineManager.run_pipeline``."""
    return " ".join(
        render_segment(segment)
        for segment in document.get("segments", [])
        if segment.get("elements")
    )


def element_name_map(document: Dict) -> Dict[str, Optional[str]]:
    """Element name -> originating nodeId for every element in the
    document, in render order.

    ``Gst.parse_launch`` auto-names elements ``<factory><N>`` where N is
    a per-factory counter incremented for every created instance (named
    or not); an explicit ``name=`` argument overrides the auto name.
    Synthetic linking elements (tee/queue/funnel) map to ``None``.
    """
    names: Dict[str, Optional[str]] = {}
    counters: Dict[str, int] = {}
    for segment in document.get("segments", []):
        for element in segment["elements"]:
            factory = element["factory"]
            index = counters.get(factory, 0)
            counters[factory] = index + 1
            explicit = element.get("args", {}).get("name")
            name = str(explicit) if explicit else "{0}{1}".format(factory, index)
            names[name] = element.get("nodeId")
    return names


def node_id_for_element(
    name_map: Dict[str, Optional[str]], element_name: str
) -> Optional[str]:
    """The nodeId behind a bus-error source element name, or None for
    synthetic/unknown elements."""
    return name_map.get(element_name)


#: GStreamer debug strings embed the failing element as an object path:
#: ``/GstPipeline:pipeline0/GstFileSrc:filesrc0:`` — the element name is
#: the last path component before the trailing colon.
_ELEMENT_PATH_RE = re.compile(r"/GstPipeline:[^/\s]+/[^/:\s]+:([^/:\s,]+)")

_TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-]+")


def failing_node_id_from_error(
    name_map: Dict[str, Optional[str]], error_text: Optional[str]
) -> Optional[str]:
    """Map a pipeline error message back to the originating workflow
    node id (Requirement 9.7).

    ``GstPipelineManager`` folds the GStreamer error and debug text into
    the exception message; the debug text names the failing element via
    its pipeline object path. Falls back to scanning the message for any
    known element name. Returns None when no element of the workflow can
    be identified (e.g. the failure came from a synthetic tee/queue or a
    pre-parse syntax error).
    """
    if not error_text:
        return None
    for match in _ELEMENT_PATH_RE.finditer(error_text):
        node_id = name_map.get(match.group(1))
        if node_id:
            return node_id
    for token in _TOKEN_RE.findall(error_text):
        node_id = name_map.get(token)
        if node_id:
            return node_id
    return None
