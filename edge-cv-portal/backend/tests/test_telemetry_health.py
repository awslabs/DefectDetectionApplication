"""``telemetry.health`` (workflow-telemetry-dashboards design §9.1; tasks.md
3.4): a Summary then three Heartbeats keeps the values and reads ``idle``; a
producer clock 90 s behind still reads ``flowing``; the hint for
``x86_64_nvidia`` versus ``x86_64``, ``arm64_jp5`` and a missing record."""
from telemetry import health

T0 = 1_792_000_000_000
SETTINGS = {"summaryIntervalS": 10, "idleHeartbeatS": 60}


def test_flow_windows():
    assert health.flow_window_ms(SETTINGS) == {"flowing": 25_000, "idle": 120_000}
    assert health.flow_window_ms(None) == {"flowing": 25_000, "idle": 120_000}
    assert health.flow_window_ms({"summaryIntervalS": 60, "idleHeartbeatS": 300}) == {"flowing": 125_000, "idle": 600_000}


def test_a_summary_then_three_heartbeats_keeps_the_values_and_reads_idle():
    last = {"receivedMs": T0, "lastRunMs": T0, "seq": 8, "summary": {"runs": {"completed": 3, "failed": 0, "dropped": 0}}}
    now = T0
    assert health.flow(last, None, SETTINGS, now) == "flowing"
    lasthb = None
    for seq in (9, 10, 11):
        now = T0 + 60_000 * (seq - 8)
        lasthb = {"receivedMs": now, "seq": seq}
        assert health.flow(last, lasthb, SETTINGS, now) == "idle"
    assert last["summary"]["runs"]["completed"] == 3, "the values are untouched by Heartbeats"
    ages = health.ages(last, lasthb, now)
    assert ages["lastSummaryAgeS"] == 180 and ages["lastRunAgeS"] == 180 and ages["heartbeatAgeS"] == 0
    assert ages["noRunIn48h"] is False


def test_a_producer_clock_ninety_seconds_behind_still_reads_flowing():
    last = {"receivedMs": T0, "eventMs": T0 - 90_000, "summary": {"intervalEnd": T0 - 90_000}}
    assert health.flow(last, None, SETTINGS, T0 + 20_000) == "flowing"


def test_flowing_boundary_then_idle_then_silent():
    last = {"receivedMs": T0}
    lasthb = {"receivedMs": T0}
    assert health.flow(last, lasthb, SETTINGS, T0 + 25_000) == "flowing"
    assert health.flow(last, lasthb, SETTINGS, T0 + 25_001) == "idle"
    assert health.flow(last, lasthb, SETTINGS, T0 + 120_000) == "idle"
    assert health.flow(last, lasthb, SETTINGS, T0 + 120_001) == "silent"
    assert health.flow(None, None, SETTINGS, T0) == "silent"
    assert health.flow({"receivedMs": "junk"}, None, SETTINGS, T0) == "silent"


def test_ages_without_a_run():
    last = {"receivedMs": T0}
    ages = health.ages(last, None, T0 + 5000)
    assert ages["lastSummaryAgeS"] == 5 and ages["lastRunAgeS"] is None and ages["noRunIn48h"] is True
    old = {"receivedMs": T0, "lastRunMs": T0 - 48 * 3600 * 1000 - 1}
    assert health.ages(old, None, T0)["noRunIn48h"] is True
    recent = {"receivedMs": T0, "lastRunMs": T0 - 48 * 3600 * 1000}
    assert health.ages(recent, None, T0)["lastRunAgeS"] == 48 * 3600
    assert health.ages(None, None, T0) == {"lastSummaryAgeS": None, "lastRunAgeS": None, "heartbeatAgeS": None,
                                          "noRunIn48h": True}


def test_variant_hint():
    assert health.variant_hint("silent", 3, "x86_64_nvidia") == health.VARIANT_HINT
    assert "amd64Nvidia" in health.VARIANT_HINT and "mqttproxy:2" in health.VARIANT_HINT
    assert health.variant_hint("silent", 3, "x86_64") is None
    assert health.variant_hint("silent", 3, "arm64_jp5") is None
    assert health.variant_hint("silent", 3, None) is None
    assert health.variant_hint("flowing", 3, "x86_64_nvidia") is None
    assert health.variant_hint("idle", 3, "x86_64_nvidia") is None
    assert health.variant_hint("silent", 0, "x86_64_nvidia") is None
    assert health.variant_hint("silent", "junk", "x86_64_nvidia") is None
    assert health.variant_hint("silent", "2", "x86_64_nvidia") == health.VARIANT_HINT
