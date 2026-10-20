"""The ingest's redaction callable (workflow-telemetry-dashboards design §2.2,
§2.4): ``workflow_core.stream_url.redact`` from the ``workflow_core`` layer
the stack attaches to the ingest function. The ingest imports ``redact`` from
this module and from nowhere else, so ``test_telemetry_redaction_import_boundary.py``
can prove the dependency on the layer with a subprocess whose ``sys.path``
lacks it.
"""
from workflow_core.stream_url import redact  # noqa: F401

__all__ = ["redact"]
