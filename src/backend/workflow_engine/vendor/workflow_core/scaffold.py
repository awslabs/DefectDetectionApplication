"""Plugin_Scaffold rendering and validation (custom-node-designer).

Pure template rendering, usable in tests without AWS (design, "Plugin_Scaffold
and Node_Generator"): given a validated Custom_Node_Type declaration (name,
category, ports, parameters, architectures), :func:`render_scaffold` renders a
GStreamer plugin project as a file map ``{path: content}``:

- a C skeleton element (``plugin/gst<element>.c``) wrapping an embedded Python
  Frame_Processing_Hook via an internal appsink/appsrc bridge — the same
  bridge approach as the existing ``emlpython`` custom-python element
  (Requirement 1.3),
- the Frame_Processing_Hook file (``plugin/frame_processing_hook.py``)
  exposing ``process_frame(frame, params) -> frame`` where the user writes
  the per-frame logic (Requirements 1.2, 1.3),
- one ``meson.build`` build configuration per selected Target_Architecture
  (``builds/<arch>/meson.build``, Requirement 1.2),
- a ``README.md`` describing the project layout, the hook contract, and the
  per-architecture build instructions.

Declared parameters surface as GObject properties on the C element and are
plumbed into the ``params`` dict handed to the hook, keyed by their declared
names (Requirement 1.4).

:func:`validate_scaffold` rejects non-buildable scaffold source — a missing
Frame_Processing_Hook file, missing build configurations, or empty required
files — with a description of the failure, and accepts every scaffold
produced by :func:`render_scaffold` unchanged (Requirements 1.7, 2.6).

The declaration is the same node-catalog wire shape accepted by
:func:`workflow_core.catalog.custom.descriptor_from_declaration`, plus an
``architectures`` list naming the selected Target_Architectures (falling back
to the architectures of the declaration's ``mappings`` when absent).
"""

from __future__ import annotations

import re
from string import Template
from typing import Any, Mapping, Sequence

from .catalog.custom import DeclarationError, descriptor_from_declaration
from .catalog.models import (
    DEVICE_ARCHITECTURES,
    PARAM_TYPE_BOOL,
    PARAM_TYPE_FLOAT,
    PARAM_TYPE_INT,
    NodeTypeDescriptor,
    ParameterDescriptor,
)

# --------------------------------------------------------------------------
# Scaffold layout
# --------------------------------------------------------------------------

#: Path of the Frame_Processing_Hook file inside every Plugin_Scaffold.
HOOK_FILE = "plugin/frame_processing_hook.py"

#: Path of the scaffold README.
README_FILE = "README.md"


def c_source_path(declaration: Any) -> str:
    """Path of the C skeleton element source for ``declaration``."""
    return "plugin/gst{0}.c".format(element_name_for(declaration))


def build_config_path(arch: str) -> str:
    """Path of the ``meson.build`` build configuration for ``arch``."""
    return "builds/{0}/meson.build".format(arch)


class ScaffoldError(ValueError):
    """A Plugin_Scaffold could not be generated or is not buildable.

    ``field`` identifies the failing declaration input when the failure is
    an invalid declaration (Requirement 1.7); it is None for scaffold
    source validation failures. ``defects`` lists every described failure
    found by :func:`validate_scaffold` (Requirement 2.6).
    """

    def __init__(self, message: str, field: str | None = None,
                 defects: Sequence[str] | None = None):
        self.field = field
        self.defects = list(defects) if defects is not None else []
        if field is not None:
            message = "{0}: {1}".format(field, message)
        super().__init__(message)


# --------------------------------------------------------------------------
# Declaration handling
# --------------------------------------------------------------------------

def element_name_for(declaration: Any) -> str:
    """The GStreamer element (factory) name derived from the declaration.

    Lower-cases the declaration's ``typeId`` and strips everything outside
    ``[a-z0-9]``; a leading non-letter is prefixed so the name is a valid
    C identifier and element factory name.
    """
    if not isinstance(declaration, dict):
        raise ScaffoldError("must be an object, got {0}".format(
            type(declaration).__name__), field="declaration")
    type_id = declaration.get("typeId")
    if not isinstance(type_id, str) or not type_id.strip():
        raise ScaffoldError(
            "must be a non-empty string, got {0!r}".format(type_id),
            field="typeId")
    name = "".join(ch for ch in type_id.lower() if ch.isascii() and ch.isalnum())
    if not name:
        raise ScaffoldError(
            "{0!r} contains no usable characters for a GStreamer element "
            "name".format(type_id), field="typeId")
    if not name[0].isalpha():
        name = "x" + name
    return name


def plugin_ident_for(declaration: Any) -> str:
    """The GST_PLUGIN_DEFINE plugin name token for ``declaration``.

    GStreamer only loads a plugin library when it exports the symbol
    ``gst_plugin_<ident>_get_desc`` where ``<ident>`` is derived from the
    library FILENAME (strip a leading ``libgst``/``lib``, cut the
    extension at the first dot, then map every non-alphanumeric to
    ``_``). The Plugin_Build_Service promotes the built artifact as
    ``{plugin}.so`` where ``{plugin}`` is
    ``plugin_builds.sanitize_plugin_name`` of the Plugin_Record name -
    which the designer wizard sets from the declaration's
    ``displayName``. This helper mirrors that exact chain so the
    generated GST_PLUGIN_DEFINE matches the promoted filename; a
    mismatch makes GStreamer silently reject the library and the run
    fails with "the run's workflow plugin directories register no
    elements".

    Falls back to :func:`element_name_for` when the displayName
    sanitizes to nothing usable.
    """
    element = element_name_for(declaration)
    display = declaration.get("displayName")
    if not isinstance(display, str):
        return element
    # plugin_builds.sanitize_plugin_name: the promoted artifact base name.
    sanitized = re.sub(r'[^A-Za-z0-9_.-]+', '-', display).strip('-.')
    if not sanitized:
        return element
    # GStreamer extract_symname on "{sanitized}.so": '-' -> '_' first,
    # then strip a libgst/lib/gst prefix, then cut at the first dot.
    sanitized = sanitized.replace("-", "_")
    for prefix in ("libgst", "lib", "gst"):
        if sanitized.startswith(prefix):
            sanitized = sanitized[len(prefix):]
            break
    ident = sanitized.split(".", 1)[0]
    if not ident or not all(ch == "_" or (ch.isascii() and ch.isalnum())
                            for ch in ident):
        return element
    return ident


def _validated(declaration: Any) -> tuple:
    """Validate the declaration, returning ``(descriptor, architectures)``.

    Raises :class:`ScaffoldError` identifying the failing input
    (Requirement 1.7).
    """
    try:
        descriptor = descriptor_from_declaration(declaration)
    except DeclarationError as exc:
        raise ScaffoldError(str(exc).split(": ", 1)[-1], field=exc.field)

    architectures = declaration.get("architectures")
    if architectures is None:
        architectures = [m.arch for m in descriptor.mappings
                         if m.arch in DEVICE_ARCHITECTURES]
    if not isinstance(architectures, list):
        raise ScaffoldError(
            "must be a list, got {0}".format(type(architectures).__name__),
            field="architectures")
    if not architectures:
        raise ScaffoldError(
            "at least one Target_Architecture must be selected",
            field="architectures")
    seen = set()
    for index, arch in enumerate(architectures):
        field = "architectures[{0}]".format(index)
        if arch not in DEVICE_ARCHITECTURES:
            raise ScaffoldError(
                "unknown Target_Architecture {0!r}; must be one of {1}".format(
                    arch, list(DEVICE_ARCHITECTURES)), field=field)
        if arch in seen:
            raise ScaffoldError(
                "duplicate Target_Architecture {0!r}".format(arch), field=field)
        seen.add(arch)

    # element name derivability is part of declaration validity
    element_name_for(declaration)

    return descriptor, list(architectures)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def render_scaffold(declaration: Any) -> dict:
    """Render the Plugin_Scaffold for ``declaration`` as ``{path: content}``.

    Raises :class:`ScaffoldError` identifying the failing input when the
    declaration is invalid (Requirement 1.7).
    """
    descriptor, architectures = _validated(declaration)
    element = element_name_for(declaration)
    plugin_ident = plugin_ident_for(declaration)
    description = declaration.get("description") or descriptor.display_name

    files = {
        HOOK_FILE: _render_hook(descriptor),
        c_source_path(declaration): _render_c_source(
            descriptor, element, description, plugin_ident),
        README_FILE: _render_readme(
            descriptor, element, description, architectures),
    }
    for arch in architectures:
        files[build_config_path(arch)] = _render_meson(
            element, arch, plugin_ident)
    return files


def scaffold_defects(files: Any, declaration: Any) -> list:
    """Every buildability defect in ``files``, each as a description.

    Checks: presence of the Frame_Processing_Hook file, presence of the
    build configuration for every selected Target_Architecture, and
    non-emptiness of every required file (hook, C skeleton, build
    configurations). An empty list means the scaffold is buildable.
    """
    descriptor, architectures = _validated(declaration)
    del descriptor
    if not isinstance(files, Mapping):
        return ["scaffold source must be a file map {path: content}, got "
                + type(files).__name__]

    defects = []

    def _check_required(path: str, label: str, missing_message: str) -> None:
        if path not in files:
            defects.append(missing_message)
            return
        content = files[path]
        if not isinstance(content, str) or not content.strip():
            defects.append(
                "required file '{0}' ({1}) is empty".format(path, label))

    _check_required(
        HOOK_FILE, "Frame_Processing_Hook",
        "missing Frame_Processing_Hook file '{0}'".format(HOOK_FILE))
    _check_required(
        c_source_path(declaration), "C skeleton element",
        "missing C skeleton element source '{0}'".format(
            c_source_path(declaration)))
    for arch in architectures:
        _check_required(
            build_config_path(arch),
            "build configuration for {0}".format(arch),
            "missing build configuration for Target_Architecture '{0}' "
            "('{1}')".format(arch, build_config_path(arch)))
    return defects


def validate_scaffold(files: Any, declaration: Any) -> None:
    """Reject non-buildable scaffold source (Requirements 1.7, 2.6).

    Raises :class:`ScaffoldError` describing every failure found; returns
    None when ``files`` is a buildable scaffold for ``declaration``.
    """
    defects = scaffold_defects(files, declaration)
    if defects:
        raise ScaffoldError("; ".join(defects), defects=defects)


# --------------------------------------------------------------------------
# Escaping and naming helpers
# --------------------------------------------------------------------------

def _c_escape(text: str) -> str:
    """Escape ``text`` for use inside a C string literal."""
    out = []
    for ch in str(text):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20:
            continue  # other control characters have no place in metadata
        else:
            out.append(ch)
    return "".join(out)


def _c_identifiers(parameters: Sequence[ParameterDescriptor]) -> list:
    """A unique, valid C identifier per parameter (declaration order)."""
    identifiers = []
    seen = set()
    for parameter in parameters:
        base = "".join(
            ch if (ch.isascii() and ch.isalnum()) else "_"
            for ch in parameter.name.lower())
        if not base or not (base[0].isalpha() or base[0] == "_"):
            base = "p_" + base
        candidate = base
        suffix = 2
        while candidate in seen:
            candidate = "{0}_{1}".format(base, suffix)
            suffix += 1
        seen.add(candidate)
        identifiers.append(candidate)
    return identifiers


def _int_default(parameter: ParameterDescriptor) -> int:
    return parameter.default if isinstance(parameter.default, int) and not isinstance(parameter.default, bool) else 0


def _float_default(parameter: ParameterDescriptor) -> float:
    if isinstance(parameter.default, (int, float)) and not isinstance(parameter.default, bool):
        return float(parameter.default)
    return 0.0


# --------------------------------------------------------------------------
# Frame_Processing_Hook template (Requirements 1.2, 1.3, 1.4)
# --------------------------------------------------------------------------

_HOOK_TEMPLATE = Template('''\
"""Frame_Processing_Hook for ${display_name}.

Write your per-frame processing logic in :func:`process_frame` below. The
plugin element calls it once for every frame arriving at the node's input
port and emits the returned frame content on the node's output port.

``frame`` is the raw frame payload (``bytes``); ``params`` is a dict
carrying the current value of every parameter declared on this node type,
keyed by the declared parameter name.

Besides the declared parameters, the element always injects the
NEGOTIATED input frame geometry so the hook never has to guess the
incoming layout:

- ``params["frame_width"]`` (int): negotiated frame width in pixels.
- ``params["frame_height"]`` (int): negotiated frame height in pixels.
- ``params["frame_format"]`` (str): negotiated pixel format (e.g.
  ``"RGBA"``, ``"RGB"``, ``"GRAY8"``; empty when caps carry no format).

If your processing CHANGES the frame geometry or format (resize, crop,
colorspace change), also update ``gst_..._sync_out_caps`` in the C
skeleton so downstream elements interpret the emitted bytes correctly.
"""

#: The parameters declared on this node type: every key below is present
#: in the ``params`` dict handed to process_frame (name -> parameter type).
DECLARED_PARAMETERS = {
${declared_parameters}
}


def process_frame(frame, params):
    """Process one frame and return the frame content to emit.

    Args:
        frame: the raw frame bytes arriving at the input port.
        params: dict of declared parameter values, keyed by parameter
            name${param_hint}, plus the negotiated input geometry under
            "frame_width", "frame_height", and "frame_format".

    Returns:
        The frame bytes to emit on the output port.
    """
    # TODO: replace this pass-through with your processing logic.
    return frame
''')


def _render_hook(descriptor: NodeTypeDescriptor) -> str:
    declared = "\n".join(
        "    {0}: {1},".format(repr(parameter.name), repr(parameter.param_type))
        for parameter in descriptor.parameters
    ) or "    # (no parameters declared)"
    param_hint = ""
    if descriptor.parameters:
        param_hint = " (see DECLARED_PARAMETERS)"
    return _HOOK_TEMPLATE.substitute(
        display_name=descriptor.display_name,
        declared_parameters=declared,
        param_hint=param_hint,
    )


# --------------------------------------------------------------------------
# C skeleton element template (Requirements 1.2, 1.3, 1.4)
# --------------------------------------------------------------------------

_C_TEMPLATE = Template('''\
/* gst${element}.c - ${display_name}
 *
 * Generated by the DDA Node_Designer. Skeleton GStreamer element wrapping
 * the embedded Python Frame_Processing_Hook (plugin/frame_processing_hook.py)
 * behind an internal appsink/appsrc bridge, the same bridge approach as the
 * existing emlpython custom-python element: every buffer arriving on the
 * sink pad is pulled from the internal appsink, handed to
 * process_frame(frame, params), and the returned frame content is pushed
 * on the internal appsrc feeding the source pad.
 *
 * You normally only need to edit plugin/frame_processing_hook.py; this file
 * carries the element boilerplate, the GObject properties for the declared
 * parameters, and the params-dict plumbing into the hook.
 */

#ifndef _GNU_SOURCE
#define _GNU_SOURCE /* dladdr: self-locate the Frame_Processing_Hook */
#endif
#include <dlfcn.h>
#include <libgen.h>
#include <string.h>

#include <gst/gst.h>
#include <gst/app/gstappsink.h>
#include <gst/app/gstappsrc.h>
#include <Python.h>

#define PACKAGE "${plugin_ident}"

#define GST_TYPE_${element_upper} (gst_${element}_get_type ())

typedef struct _Gst${element_camel}
{
  GstBin parent;

  GstElement *appsink;          /* internal frame tap (input side)   */
  GstElement *appsrc;           /* internal frame feed (output side) */
  PyObject *hook_module;        /* imported frame_processing_hook    */

  /* Declared parameters, exposed as GObject properties. */
${property_fields}
} Gst${element_camel};

typedef struct _Gst${element_camel}Class
{
  GstBinClass parent_class;
} Gst${element_camel}Class;

enum
{
  PROP_0${property_enum}
};

G_DEFINE_TYPE (Gst${element_camel}, gst_${element}, GST_TYPE_BIN);

/* --------------------------------------------------------------------
 * Parameter plumbing: build the params dict handed to
 * process_frame(frame, params) - one entry per declared parameter,
 * keyed by its declared name (Requirement 1.4).
 * -------------------------------------------------------------------- */
static PyObject *
gst_${element}_params_dict (Gst${element_camel} * self)
{
  PyObject *params = PyDict_New ();
${params_dict_body}
  return params;
}

static void
gst_${element}_set_property (GObject * object, guint prop_id,
    const GValue * value, GParamSpec * pspec)
{
  Gst${element_camel} *self = (Gst${element_camel} *) object;

  switch (prop_id) {
${set_property_cases}
    default:
      G_OBJECT_WARN_INVALID_PROPERTY_ID (object, prop_id, pspec);
      break;
  }
}

static void
gst_${element}_get_property (GObject * object, guint prop_id,
    GValue * value, GParamSpec * pspec)
{
  Gst${element_camel} *self = (Gst${element_camel} *) object;

  switch (prop_id) {
${get_property_cases}
    default:
      G_OBJECT_WARN_INVALID_PROPERTY_ID (object, prop_id, pspec);
      break;
  }
}

/* --------------------------------------------------------------------
 * Frame_Processing_Hook bridge: appsink -> Python -> appsrc
 * (Requirement 1.3).
 * -------------------------------------------------------------------- */

/* Emit the element's OUTPUT caps on the internal appsrc so downstream
 * can negotiate. The default contract is a same-geometry transform:
 * the input caps pass through unchanged. IF YOUR HOOK CHANGES THE FRAME
 * GEOMETRY OR FORMAT (resize, crop, colorspace change), adjust the
 * copied caps here (gst_caps_set_simple width/height/format) to match
 * what process_frame returns, or downstream will misinterpret the
 * buffer bytes. */
static void
gst_${element}_sync_out_caps (Gst${element_camel} * self, GstSample * sample)
{
  GstCaps *in_caps = gst_sample_get_caps (sample);
  GstCaps *out_caps;
  if (in_caps == NULL)
    return;
  out_caps = gst_caps_copy (in_caps);
  gst_app_src_set_caps (GST_APP_SRC (self->appsrc), out_caps);
  gst_caps_unref (out_caps);
}

/* Forward EOS so a bounded run (the workflow engine's single-frame
 * execution model) terminates instead of hanging until the pipeline
 * watchdog fires. */
static void
gst_${element}_on_eos (GstElement * sink, gpointer user_data)
{
  Gst${element_camel} *self = (Gst${element_camel} *) user_data;
  (void) sink;
  gst_app_src_end_of_stream (GST_APP_SRC (self->appsrc));
}

static GstFlowReturn
gst_${element}_process_sample (Gst${element_camel} * self, GstSample * sample)
{
  GstBuffer *buffer;
  GstMapInfo info;
  GstFlowReturn ret = GST_FLOW_ERROR;

  gst_${element}_sync_out_caps (self, sample);

  buffer = gst_sample_get_buffer (sample);
  if (buffer != NULL && gst_buffer_map (buffer, &info, GST_MAP_READ)) {
    GstBuffer *out = NULL;
    PyGILState_STATE gil = PyGILState_Ensure ();
    PyObject *frame = PyBytes_FromStringAndSize ((const char *) info.data,
        (Py_ssize_t) info.size);
    PyObject *params = gst_${element}_params_dict (self);

    /* The NEGOTIATED frame geometry rides along in params so the hook
     * never has to guess (or be configured with) the incoming width /
     * height / pixel format: frame_width, frame_height, frame_format. */
    {
      GstCaps *caps = gst_sample_get_caps (sample);
      GstStructure *s = caps != NULL ? gst_caps_get_structure (caps, 0) : NULL;
      gint fw = 0, fh = 0;
      const gchar *ffmt = NULL;
      if (s != NULL) {
        gst_structure_get_int (s, "width", &fw);
        gst_structure_get_int (s, "height", &fh);
        ffmt = gst_structure_get_string (s, "format");
      }
      if (params != NULL) {
        PyObject *v;
        v = PyLong_FromLong (fw);
        PyDict_SetItemString (params, "frame_width", v);
        Py_XDECREF (v);
        v = PyLong_FromLong (fh);
        PyDict_SetItemString (params, "frame_height", v);
        Py_XDECREF (v);
        v = PyUnicode_FromString (ffmt != NULL ? ffmt : "");
        PyDict_SetItemString (params, "frame_format", v);
        Py_XDECREF (v);
      }
    }

    PyObject *result = PyObject_CallMethod (self->hook_module,
        "process_frame", "OO", frame, params);

    if (result != NULL && PyBytes_Check (result)) {
      char *out_data = NULL;
      Py_ssize_t out_size = 0;
      if (PyBytes_AsStringAndSize (result, &out_data, &out_size) == 0) {
        /* allocate + fill (both GStreamer 1.0 APIs) rather than the
         * newer one-shot memdup helper, so the plugin links on every
         * supported device stack (JetPack 4 ships GStreamer 1.14,
         * JetPack 5 ships 1.16). */
        out = gst_buffer_new_allocate (NULL, (gsize) out_size, NULL);
        gst_buffer_fill (out, 0, out_data, (gsize) out_size);
        gst_buffer_copy_into (out, buffer, GST_BUFFER_COPY_TIMESTAMPS, 0, -1);
      }
    } else {
      GST_ELEMENT_ERROR (self, STREAM, FAILED,
          ("frame_processing_hook.process_frame failed"), (NULL));
      if (PyErr_Occurred ())
        PyErr_Print ();
    }

    Py_XDECREF (result);
    Py_XDECREF (params);
    Py_XDECREF (frame);
    /* Release the GIL BEFORE pushing downstream: the push chains into
     * the rest of the pipeline (tee/queue branches, encoders, sinks)
     * and may synchronize with threads that themselves need the GIL —
     * in an embedding Python host (LocalServer) holding it across the
     * push deadlocks the whole process. */
    PyGILState_Release (gil);
    gst_buffer_unmap (buffer, &info);
    if (out != NULL)
      ret = gst_app_src_push_buffer (GST_APP_SRC (self->appsrc), out);
  }

  gst_sample_unref (sample);
  return ret;
}

static GstFlowReturn
gst_${element}_on_new_sample (GstElement * sink, gpointer user_data)
{
  Gst${element_camel} *self = (Gst${element_camel} *) user_data;
  GstSample *sample = gst_app_sink_pull_sample (GST_APP_SINK (sink));
  if (sample == NULL)
    return GST_FLOW_EOS;
  return gst_${element}_process_sample (self, sample);
}

/* The FIRST buffer arrives as the appsink PREROLL sample — only
 * new-preroll fires for it. It must be processed and pushed too, or
 * downstream never receives a preroll buffer and the whole pipeline
 * hangs in PREROLLING (the deadlock behind "pipeline timed out without
 * completing"). */
static GstFlowReturn
gst_${element}_on_new_preroll (GstElement * sink, gpointer user_data)
{
  Gst${element_camel} *self = (Gst${element_camel} *) user_data;
  GstSample *sample = gst_app_sink_pull_preroll (GST_APP_SINK (sink));
  if (sample == NULL)
    return GST_FLOW_EOS;
  return gst_${element}_process_sample (self, sample);
}

static void
gst_${element}_class_init (Gst${element_camel}Class * klass)
{
  GObjectClass *gobject_class = G_OBJECT_CLASS (klass);
  GstElementClass *element_class = GST_ELEMENT_CLASS (klass);

  gobject_class->set_property = gst_${element}_set_property;
  gobject_class->get_property = gst_${element}_get_property;

${install_properties}
  gst_element_class_set_static_metadata (element_class,
      "${display_name_c}", "${category_c}",
      "${description_c}",
      "DDA Node_Designer <generated>");
}

static void
gst_${element}_init (Gst${element_camel} * self)
{
  GstPad *pad;

  {
    gboolean we_initialized = FALSE;
    PyGILState_STATE gil;
    if (!Py_IsInitialized ()) {
      Py_InitializeEx (0);
      we_initialized = TRUE;
    }
    gil = PyGILState_Ensure ();
    /* The Frame_Processing_Hook travels beside this shared library; the
     * embedded interpreter knows nothing about that directory, so add it
     * to sys.path (idempotent) before the import — self-locating via
     * dladdr so it works in any host process (gst-launch, LocalServer). */
    {
      Dl_info dlinfo;
      if (dladdr ((void *) gst_${element}_get_type, &dlinfo)
          && dlinfo.dli_fname != NULL) {
        char *dup = strdup (dlinfo.dli_fname);
        if (dup != NULL) {
          PyObject *sys_path = PySys_GetObject ("path");
          PyObject *dir = PyUnicode_FromString (dirname (dup));
          if (sys_path != NULL && dir != NULL
              && !PySequence_Contains (sys_path, dir))
            PyList_Insert (sys_path, 0, dir);
          Py_XDECREF (dir);
          free (dup);
        }
      }
    }
    self->hook_module = PyImport_ImportModule ("frame_processing_hook");
    if (self->hook_module == NULL && PyErr_Occurred ())
      PyErr_Print ();
    PyGILState_Release (gil);
    /* Py_InitializeEx leaves this thread holding the GIL; release it or
     * the streaming thread deadlocks in PyGILState_Ensure inside the
     * new-sample callback and no buffer ever flows. */
    if (we_initialized)
      PyEval_SaveThread ();
  }

  self->appsink = gst_element_factory_make ("appsink", NULL);
  self->appsrc = gst_element_factory_make ("appsrc", NULL);
  g_object_set (self->appsink, "emit-signals", TRUE, "sync", FALSE, NULL);
  g_object_set (self->appsrc, "format", GST_FORMAT_TIME, NULL);
  g_signal_connect (self->appsink, "new-sample",
      G_CALLBACK (gst_${element}_on_new_sample), self);
  g_signal_connect (self->appsink, "new-preroll",
      G_CALLBACK (gst_${element}_on_new_preroll), self);
  /* Forward EOS so a bounded run (single-frame workflow) terminates. */
  g_signal_connect (self->appsink, "eos",
      G_CALLBACK (gst_${element}_on_eos), self);

  gst_bin_add_many (GST_BIN (self), self->appsink, self->appsrc, NULL);

  pad = gst_element_get_static_pad (self->appsink, "sink");
  gst_element_add_pad (GST_ELEMENT (self),
      gst_ghost_pad_new ("sink", pad));
  gst_object_unref (pad);

  pad = gst_element_get_static_pad (self->appsrc, "src");
  gst_element_add_pad (GST_ELEMENT (self),
      gst_ghost_pad_new ("src", pad));
  gst_object_unref (pad);
${init_defaults}
}

static gboolean
plugin_init (GstPlugin * plugin)
{
  return gst_element_register (plugin, "${element}", GST_RANK_NONE,
      GST_TYPE_${element_upper});
}

/* The plugin name token MUST match the symbol GStreamer derives from the
 * promoted artifact filename ({plugin}.so from the sanitized node display
 * name): the loader resolves gst_plugin_${plugin_ident}_get_desc and
 * silently rejects the library when it is absent — the element then
 * "registers no elements" at run time. plugin_ident_for() mirrors that
 * filename-to-symbol derivation. */
GST_PLUGIN_DEFINE (GST_VERSION_MAJOR, GST_VERSION_MINOR,
    ${plugin_ident},
    "${description_c}",
    plugin_init, "1.0.0", "LGPL", "${plugin_ident}",
    "https://github.com/awslabs/defect-detection-application")
''')


def _property_snippets(parameters: Sequence[ParameterDescriptor]) -> dict:
    """Per-parameter C snippets: fields, enum entries, param specs,
    set/get cases, params-dict plumbing, and init defaults."""
    identifiers = _c_identifiers(parameters)
    fields = []
    enum_entries = []
    installs = []
    set_cases = []
    get_cases = []
    dict_lines = []
    init_defaults = []

    for parameter, cname in zip(parameters, identifiers):
        prop_enum = "PROP_" + cname.upper()
        gname = _c_escape(cname.replace("_", "-"))
        name_c = _c_escape(parameter.name)
        blurb = _c_escape(parameter.description or parameter.name)
        enum_entries.append(",\n  " + prop_enum)

        if parameter.param_type == PARAM_TYPE_INT:
            fields.append("  gint {0};".format(cname))
            installs.append(
                "  g_object_class_install_property (gobject_class, {0},\n"
                "      g_param_spec_int (\"{1}\", \"{2}\", \"{3}\",\n"
                "          G_MININT, G_MAXINT, {4},\n"
                "          G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));\n"
                .format(prop_enum, gname, name_c, blurb,
                        _int_default(parameter)))
            set_cases.append(
                "    case {0}:\n      self->{1} = g_value_get_int (value);\n"
                "      break;".format(prop_enum, cname))
            get_cases.append(
                "    case {0}:\n      g_value_set_int (value, self->{1});\n"
                "      break;".format(prop_enum, cname))
            py_value = "PyLong_FromLong (self->{0})".format(cname)
            init_defaults.append(
                "  self->{0} = {1};".format(cname, _int_default(parameter)))
        elif parameter.param_type == PARAM_TYPE_FLOAT:
            fields.append("  gdouble {0};".format(cname))
            installs.append(
                "  g_object_class_install_property (gobject_class, {0},\n"
                "      g_param_spec_double (\"{1}\", \"{2}\", \"{3}\",\n"
                "          -G_MAXDOUBLE, G_MAXDOUBLE, {4},\n"
                "          G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));\n"
                .format(prop_enum, gname, name_c, blurb,
                        repr(_float_default(parameter))))
            set_cases.append(
                "    case {0}:\n      self->{1} = g_value_get_double (value);\n"
                "      break;".format(prop_enum, cname))
            get_cases.append(
                "    case {0}:\n      g_value_set_double (value, self->{1});\n"
                "      break;".format(prop_enum, cname))
            py_value = "PyFloat_FromDouble (self->{0})".format(cname)
            init_defaults.append(
                "  self->{0} = {1};".format(
                    cname, repr(_float_default(parameter))))
        elif parameter.param_type == PARAM_TYPE_BOOL:
            default = "TRUE" if parameter.default is True else "FALSE"
            fields.append("  gboolean {0};".format(cname))
            installs.append(
                "  g_object_class_install_property (gobject_class, {0},\n"
                "      g_param_spec_boolean (\"{1}\", \"{2}\", \"{3}\",\n"
                "          {4},\n"
                "          G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));\n"
                .format(prop_enum, gname, name_c, blurb, default))
            set_cases.append(
                "    case {0}:\n      self->{1} = g_value_get_boolean (value);\n"
                "      break;".format(prop_enum, cname))
            get_cases.append(
                "    case {0}:\n      g_value_set_boolean (value, self->{1});\n"
                "      break;".format(prop_enum, cname))
            py_value = "PyBool_FromLong (self->{0})".format(cname)
            init_defaults.append(
                "  self->{0} = {1};".format(cname, default))
        else:  # string, enum, code, model_ref: stored as strings
            default = ("\"{0}\"".format(_c_escape(parameter.default))
                       if isinstance(parameter.default, str) else "NULL")
            fields.append("  gchar *{0};".format(cname))
            installs.append(
                "  g_object_class_install_property (gobject_class, {0},\n"
                "      g_param_spec_string (\"{1}\", \"{2}\", \"{3}\",\n"
                "          {4},\n"
                "          G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));\n"
                .format(prop_enum, gname, name_c, blurb, default))
            set_cases.append(
                "    case {0}:\n      g_free (self->{1});\n"
                "      self->{1} = g_value_dup_string (value);\n"
                "      break;".format(prop_enum, cname))
            get_cases.append(
                "    case {0}:\n      g_value_set_string (value, self->{1});\n"
                "      break;".format(prop_enum, cname))
            py_value = ("PyUnicode_FromString (self->{0} ? self->{0} : \"\")"
                        .format(cname))
            init_defaults.append(
                "  self->{0} = {1};".format(
                    cname,
                    "g_strdup ({0})".format(default) if default != "NULL"
                    else "NULL"))

        dict_lines.append(
            "  {{\n"
            "    /* declared parameter \"{name}\" */\n"
            "    PyObject *value = {py_value};\n"
            "    PyDict_SetItemString (params, \"{name}\", value);\n"
            "    Py_XDECREF (value);\n"
            "  }}".format(name=name_c, py_value=py_value))

    return {
        "property_fields": "\n".join(fields) or "  /* (no parameters declared) */",
        "property_enum": "".join(enum_entries),
        "install_properties": "\n".join(installs),
        "set_property_cases": "\n".join(set_cases) or "    /* no properties */",
        "get_property_cases": "\n".join(get_cases) or "    /* no properties */",
        "params_dict_body": "\n".join(dict_lines) or "  /* no declared parameters */",
        "init_defaults": ("\n\n" + "\n".join(init_defaults)) if init_defaults else "",
    }


def _render_c_source(descriptor: NodeTypeDescriptor, element: str,
                     description: str, plugin_ident: str) -> str:
    snippets = _property_snippets(descriptor.parameters)
    return _C_TEMPLATE.substitute(
        element=element,
        element_upper=element.upper(),
        element_camel=element[:1].upper() + element[1:],
        plugin_ident=plugin_ident,
        display_name=descriptor.display_name,
        display_name_c=_c_escape(descriptor.display_name),
        category_c=_c_escape("Filter/Effect/" + descriptor.category),
        description_c=_c_escape(description),
        **snippets)


# --------------------------------------------------------------------------
# Build configuration template (one per Target_Architecture, Requirement 1.2)
# --------------------------------------------------------------------------

_ARCH_NOTES = {
    "x86_64": "# Native x86_64 build (matches the cloud sandbox and the\n"
              "# Plugin_Simulator runtime).",
    "x86_64_nvidia": "# x86_64 build with the NVIDIA GPU runtime available at\n"
                     "# run time; add CUDA dependencies here if your hook's\n"
                     "# native side needs them.",
    "arm64_jp4": "# Cross build for arm64 Jetson JetPack 4; built with the\n"
                 "# JetPack 4 cross toolchain image.",
    "arm64_jp5": "# Cross build for arm64 Jetson JetPack 5; built with the\n"
                 "# JetPack 5 cross toolchain image.",
    "arm64_jp6": "# Cross build for arm64 Jetson JetPack 6; built with the\n"
                 "# JetPack 6 cross toolchain image.",
}

_MESON_TEMPLATE = Template('''\
# meson.build - ${arch} build configuration for the '${element}' plugin.
${arch_note}

project('gst-${element}', 'c',
  version : '1.0.0',
  meson_version : '>= 0.60',
  default_options : ['warning_level=1', 'buildtype=debugoptimized'])

target_architecture = '${arch}'

gst_dep = dependency('gstreamer-1.0')
gst_app_dep = dependency('gstreamer-app-1.0')
python_dep = dependency('python3-embed')

# dladdr (the hook's sys.path self-location) lives in libdl on older
# glibc (the JetPack 4/5 cross toolchains); on glibc >= 2.34 (JetPack 6,
# modern x86_64) it is merged into libc and this resolves to an empty
# dependency. required:false keeps both cases linking.
cc = meson.get_compiler('c')
dl_dep = cc.find_library('dl', required : false)

plugins_install_dir = join_paths(get_option('libdir'), 'gstreamer-1.0')

# The library base name matters: GStreamer derives the plugin loader
# symbol (gst_plugin_<ident>_get_desc) from the FILENAME. 'gst${plugin_ident}'
# yields libgst${plugin_ident}.so, and the Plugin_Build_Service's promoted
# copy of the artifact derives the same <ident> - both match the
# GST_PLUGIN_DEFINE in the C skeleton. Renaming the library or the
# promoted .so breaks plugin loading silently.
shared_library('gst${plugin_ident}',
  '../../plugin/gst${element}.c',
  dependencies : [gst_dep, gst_app_dep, python_dep, dl_dep],
  install : true,
  install_dir : plugins_install_dir,
)

# The Frame_Processing_Hook travels with the plugin: installed beside the
# shared library so the embedded interpreter can import it.
install_data('../../plugin/frame_processing_hook.py',
  install_dir : plugins_install_dir)
''')


def _render_meson(element: str, arch: str, plugin_ident: str) -> str:
    return _MESON_TEMPLATE.substitute(
        element=element, arch=arch, plugin_ident=plugin_ident,
        arch_note=_ARCH_NOTES.get(arch, "#"))


# --------------------------------------------------------------------------
# README template
# --------------------------------------------------------------------------

_README_TEMPLATE = Template('''\
# ${display_name}

${description}

This Plugin_Scaffold was generated by the DDA Node_Designer. It builds a
GStreamer plugin exposing the `${element}` element, which hands every frame
arriving at the node's input port to your Frame_Processing_Hook and emits
the returned frame content on the node's output port.

## Where to write your code

Edit `plugin/frame_processing_hook.py`:

```python
def process_frame(frame, params):
    return frame
```

- `frame` is the raw frame payload (`bytes`) for one frame.
- `params` is a dict with the current value of every declared parameter,
  keyed by the declared parameter name. The element additionally injects
  the negotiated input frame geometry: `frame_width` (int),
  `frame_height` (int), and `frame_format` (str, e.g. `"RGBA"`), so the
  hook never has to guess the incoming layout.
- Return the frame bytes to emit on the output port.
- If your processing changes the frame geometry or pixel format (resize,
  crop, colorspace change), also update the marked `sync_out_caps`
  section in `plugin/gst${element}.c` so downstream elements interpret
  the emitted bytes correctly.

${parameters_section}
## Project layout

| Path | Purpose |
|---|---|
| `plugin/frame_processing_hook.py` | Frame_Processing_Hook - your per-frame logic |
| `plugin/gst${element}.c` | C skeleton element (appsink/appsrc bridge, embedded Python) |
${build_rows}| `README.md` | this file |

## Building

One `meson.build` is provided per selected Target_Architecture. To build
for one architecture locally:

```sh
meson setup build-<arch> builds/<arch>
meson compile -C build-<arch>
```

Selected Target_Architectures: ${arch_list}.

Submitting this scaffold in the portal builds every selected architecture
in the isolated Plugin_Build_Service and signs the resulting artifacts.
''')


def _render_readme(descriptor: NodeTypeDescriptor, element: str,
                   description: str, architectures: Sequence[str]) -> str:
    if descriptor.parameters:
        rows = "\n".join(
            "| `{0}` | {1} | {2} | {3} |".format(
                parameter.name.replace("|", "\\|"),
                parameter.param_type,
                "yes" if parameter.required else "no",
                (parameter.description or "").replace("|", "\\|").replace("\n", " "))
            for parameter in descriptor.parameters)
        parameters_section = (
            "## Declared parameters\n\n"
            "Each parameter is a GObject property on the `{0}` element and\n"
            "arrives in the hook's `params` dict under its declared name.\n\n"
            "| Name | Type | Required | Description |\n"
            "|---|---|---|---|\n{1}\n\n".format(element, rows))
    else:
        parameters_section = (
            "## Declared parameters\n\nThis node type declares no "
            "parameters; `params` is an empty dict.\n\n")
    build_rows = "".join(
        "| `{0}` | {1} build configuration |\n".format(
            build_config_path(arch), arch)
        for arch in architectures)
    return _README_TEMPLATE.substitute(
        display_name=descriptor.display_name,
        description=description,
        element=element,
        parameters_section=parameters_section,
        build_rows=build_rows,
        arch_list=", ".join(architectures))
