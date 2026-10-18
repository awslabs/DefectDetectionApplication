"""The CloudWatch console deep link PortalAdmins get for a Pair's stream
(workflow-debug-mode design §7.2).

    https://<region>.console.aws.amazon.com/cloudwatch/home?region=<region>
        #logsV2:log-groups/log-group/<enc(group)>/log-events/<enc(stream)>

``enc(name)`` percent-encodes every reserved character and then writes each
``%`` as ``$25``, the encoding the logsV2 console expects: a ``/`` becomes
``$252F``, so ``/dda/workflow-debug/<uc>`` is ``$252Fdda$252Fworkflow-debug$252F<uc>``.
The console's timestamp column shows the sink's write time; the record's own
``ts`` and ``receivedMs`` are inside each event.
"""
import re
import urllib.parse

__all__ = ["enc", "build"]

_REGION_RE = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d$")


def enc(name: str) -> str:
    return urllib.parse.quote(str(name), safe="").replace("%", "$25")


def build(region: str, group: str, stream: str) -> str:
    if not isinstance(region, str) or not _REGION_RE.match(region):
        raise ValueError("invalid region")
    return (
        "https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
        "#logsV2:log-groups/log-group/{group}/log-events/{stream}"
    ).format(region=region, group=enc(group), stream=enc(stream))
