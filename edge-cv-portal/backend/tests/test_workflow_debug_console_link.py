"""``workflow_debug.console_link`` (workflow-debug-mode design §7.2): the
pinned ``$252Fdda$252Fworkflow-debug$252F<uc>`` example.
"""
import pytest

from workflow_debug import console_link

UC = "2f6d3b1a-5c0e-4d7f-9a8b-1c2d3e4f5a6b"
WF = "0a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"


def test_the_pinned_example():
    url = console_link.build("us-east-1", "/dda/workflow-debug/" + UC, "core-thing/" + WF)
    assert url == (
        "https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1"
        "#logsV2:log-groups/log-group/$252Fdda$252Fworkflow-debug$252F" + UC +
        "/log-events/core-thing$252F" + WF
    )
    assert "$252Fdda$252Fworkflow-debug$252F" + UC in url


def test_enc_percent_encodes_then_writes_percent_as_dollar_25():
    assert console_link.enc("/dda/workflow-debug/uc") == "$252Fdda$252Fworkflow-debug$252Fuc"
    assert console_link.enc("a b") == "a$2520b"
    assert console_link.enc("site_line-3_cam_01/" + WF) == "site_line-3_cam_01$252F" + WF
    assert console_link.enc("plain-name") == "plain-name"


def test_region_appears_twice_and_gov_regions_are_accepted():
    url = console_link.build("us-gov-west-1", "/g", "s")
    assert url.startswith("https://us-gov-west-1.console.aws.amazon.com/cloudwatch/home?region=us-gov-west-1#")


@pytest.mark.parametrize("region", ["", "US-EAST-1", "us-east", "../x", None])
def test_invalid_region_is_refused(region):
    with pytest.raises(ValueError):
        console_link.build(region, "/g", "s")
