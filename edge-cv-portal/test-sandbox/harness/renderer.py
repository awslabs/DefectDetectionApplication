"""Launch-string rendering of the Compiled Pipeline Document.

Renders exactly the dialect LocalServer executes (design section 5;
src/backend/model/PipelineConfiguration.py builds the same strings):

- Elements render as ``factory arg=value arg=value`` (PluginArg's
  ``{}={}`` pattern) and are joined with ``" ! "``.
- A segment with ``"from": "t0"`` hangs off the tee named ``t0`` and is
  rendered as ``t0. ! queue ! ...``.
- A segment with ``"linkTo": "f1"`` feeds the funnel named ``f1`` and is
  rendered as ``... ! f1.``.
- Segments are joined with a single space — the ``Gst.parse_launch``
  branch syntax the existing builder already uses for tees.

Also builds the element-name -> nodeId map used to identify the failing
node from a GStreamer bus error (Requirement 12.10): ``parse_launch``
names each element ``<factory><N>`` with a per-factory creation counter
unless the element carries an explicit ``name=`` argument.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

#: Characters in a rendered argument value that require launch quoting:
#: without it ``Gst.parse_launch`` misreads the token (e.g. a bare
#: ``meta=`` from an empty value makes the parser treat ``meta`` as an
#: element name and fail with ``no element "meta"``). Kept identical to
#: src/backend/workflow_engine/rendering.py so the sandbox renders the
#: same launch string LocalServer does.
_UNSAFE_VALUE_PATTERN = re.compile(r"[\s!\"'();\\]")


def render_value(value: Any) -> str:
    """One argument value in launch-string form (bools lower-cased the
    way GStreamer parses them; everything else via ``str``). Empty
    strings and values containing launch-syntax characters are
    double-quoted with backslash escaping — the quoting
    ``Gst.parse_launch`` understands — so e.g. an empty property renders
    as ``meta=""`` rather than the bare ``meta=`` the parser misreads as
    an element named ``meta``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if isinstance(value, str) and (not text or _UNSAFE_VALUE_PATTERN.search(text)):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return '"{0}"'.format(escaped)
    return text


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
    """The complete ``gst-launch``-style string for the document —
    the same string LocalServer hands to ``GstPipelineManager``."""
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


def node_id_for_element(name_map: Dict[str, Optional[str]],
                        element_name: str) -> Optional[str]:
    """The nodeId behind a bus-error source element name, or None for
    synthetic/unknown elements (Requirement 12.10)."""
    return name_map.get(element_name)


def gst_node_ids(document: Dict) -> List[str]:
    """Node ids realized by pipeline elements, in document order —
    contiguous runs of the same nodeId count once (Requirement 6.6)."""
    ordered: List[str] = []
    for segment in document.get("segments", []):
        previous: Any = object()
        for element in segment["elements"]:
            node_id = element.get("nodeId")
            if node_id is not None and node_id != previous:
                if node_id not in ordered:
                    ordered.append(node_id)
            previous = node_id
    return ordered


def executor_node_ids(document: Dict) -> List[str]:
    """Node ids realized as executor bindings, in document order."""
    return [binding["nodeId"] for binding in document.get("executorBindings", [])]


def all_node_ids(document: Dict) -> List[str]:
    """Every node the document references, pipeline nodes first."""
    ordered = gst_node_ids(document)
    for node_id in executor_node_ids(document):
        if node_id not in ordered:
            ordered.append(node_id)
    return ordered


def nodes_with_factory(document: Dict, factory: str) -> List[str]:
    """Node ids whose element chain contains ``factory`` (e.g. the
    ``emltriton`` inference nodes whose tag output the harness reports)."""
    matches: List[str] = []
    for segment in document.get("segments", []):
        for element in segment["elements"]:
            if element["factory"] == factory and element.get("nodeId"):
                if element["nodeId"] not in matches:
                    matches.append(element["nodeId"])
    return matches


def resolve_placeholder(document: Dict, placeholder: str, value: str) -> int:
    """Resolve ``{placeholder}`` occurrences left in element argument
    values (the simulation compiler leaves ``{dataset_location}`` for
    the harness — Requirement 12.5). Returns the substitution count."""
    token = "{" + placeholder + "}"
    count = 0
    for segment in document.get("segments", []):
        for element in segment["elements"]:
            for name, arg_value in list(element.get("args", {}).items()):
                if isinstance(arg_value, str) and token in arg_value:
                    element["args"][name] = arg_value.replace(token, value)
                    count += 1
    return count


def sim_inference_node_ids(document: Dict) -> List[str]:
    """Node ids of simulation-stubbed model inference nodes.

    In simulation the catalog maps model_inference to a pass-through
    chain whose identity element is named ``sim_inference_<nodeId>``
    (the model is not executed in the sandbox); the harness injects the
    configured simulated inference outcome for these nodes
    (Requirement 12.6)."""
    matches: List[str] = []
    for segment in document.get("segments", []):
        for element in segment["elements"]:
            name = element.get("args", {}).get("name")
            if (element["factory"] == "identity" and isinstance(name, str)
                    and name.startswith("sim_inference_")
                    and element.get("nodeId")):
                if element["nodeId"] not in matches:
                    matches.append(element["nodeId"])
    return matches


def custom_stub_node_ids(document: Dict) -> List[str]:
    """Node ids of stubbed Custom_Node_Types (custom-node-designer 12.2).

    The test-runner compile step substitutes a pass-through recording
    stub — an identity element named ``custom_stub_<nodeId>`` — for every
    Custom_Node_Type without a successful x86_64 Plugin_Artifact; the
    harness records the substitution as stub activity so the test run
    report identifies the node as stubbed."""
    matches: List[str] = []
    for segment in document.get("segments", []):
        for element in segment["elements"]:
            name = element.get("args", {}).get("name")
            if (element["factory"] == "identity" and isinstance(name, str)
                    and name.startswith("custom_stub_")
                    and element.get("nodeId")):
                if element["nodeId"] not in matches:
                    matches.append(element["nodeId"])
    return matches


def sim_appsrc_names(document: Dict) -> List[Tuple[str, str]]:
    """(element name, nodeId) of simulation ``appsrc`` stub sources
    (``sim_source_<nodeId>``, emitted for hardware event inputs in
    simulation mode) the harness must feed/close instead of hardware."""
    sources: List[Tuple[str, str]] = []
    for segment in document.get("segments", []):
        for element in segment["elements"]:
            name = element.get("args", {}).get("name")
            if (element["factory"] == "appsrc" and isinstance(name, str)
                    and name.startswith("sim_source_") and element.get("nodeId")):
                sources.append((name, element["nodeId"]))
    return sources
