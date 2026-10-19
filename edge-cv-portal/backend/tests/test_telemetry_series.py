"""``telemetry.series`` (workflow-telemetry-dashboards design §8.3; tasks.md
3.4): the boundary table 6 h, 6 h + 1 min, 60 d, 60 d + 1 h, 400 d, 400 d +
1 d; the point caps; ``invalid_device``, ``invalid_measure``, every template
measure when ``measures`` is absent; the per-Device Query with its projection."""
import pytest

from telemetry import fold, schemas, series

T0 = 1_792_000_000_000
H = fold.HOUR_MS
D = fold.DAY_MS
DASHBOARD = {"device_ids": ["dev-a", "dev-b"], "template_id": "generic-detector"}
TEMPLATE = schemas.TEMPLATES["generic-detector"]


class TestResolution:
    @pytest.mark.parametrize("span, expected", [
        (60_000, "minute"), (6 * H, "minute"), (6 * H + 60_000, "hour"), (60 * D, "hour"), (60 * D + H, "day"),
        (400 * D, "day"),
    ])
    def test_boundary_table(self, span, expected):
        assert series.resolution_for(T0, T0 + span) == expected

    def test_over_four_hundred_days(self):
        with pytest.raises(series.RangeTooLong):
            series.resolution_for(T0, T0 + 400 * D + D)
        with pytest.raises(series.RangeTooLong):
            series.resolution_for(T0, T0 + 400 * D + 1)

    def test_to_must_be_after_from(self):
        with pytest.raises(series.InvalidQuery) as caught:
            series.resolution_for(T0, T0)
        assert caught.value.code == "invalid_range"
        with pytest.raises(series.InvalidQuery):
            series.resolution_for(T0 + 1, T0)

    def test_point_caps_follow_from_the_rule(self):
        assert series.POINT_CAPS == {"minute": 360, "hour": 1440, "day": 400}
        assert series.SIX_HOURS_MS // fold.MINUTE_MS == 360
        assert series.SIXTY_DAYS_MS // fold.HOUR_MS == 1440
        assert series.FOUR_HUNDRED_DAYS_MS // fold.DAY_MS == 400
        assert series.refresh_seconds("minute") == 60 and series.refresh_seconds("hour") == 300
        assert series.refresh_seconds("day") == 900

    def test_key_range(self):
        low, high = series.key_range("minute", T0 + 1, T0 + 15 * 60_000)
        assert low == "M#" + fold.minute_key(fold.bucket_minute("event", T0))
        assert high == "M#" + fold.minute_key(fold.bucket_minute("event", T0 + 15 * 60_000 - 1))
        low, high = series.key_range("hour", T0, T0 + 2 * D)
        assert low.startswith("H#") and high.startswith("H#") and low < high
        low, high = series.key_range("day", T0, T0 + 100 * D)
        assert low == "D#" + fold.day_key(T0) and high == "D#" + fold.day_key(T0 + 100 * D - 1)
        # A `to` exactly on a bucket boundary does not fetch the next bucket.
        minute = fold.bucket_minute("event", T0)
        low, high = series.key_range("minute", minute, minute + 60_000)
        assert low == high == "M#" + fold.minute_key(minute)


class TestValidate:
    def test_defaults_to_every_template_measure_and_every_device(self):
        query = series.validate({"from": str(T0), "to": str(T0 + H)}, DASHBOARD, TEMPLATE)
        assert query["resolution"] == "minute" and query["device_ids"] == ["dev-a", "dev-b"]
        assert query["measures"] == schemas.template_measures(TEMPLATE) and query["all_measures"] is True
        assert "runs_completed" in query["measures"] and "model_state_worst" in query["measures"]

    def test_device_and_measures(self):
        query = series.validate({"from": T0, "to": T0 + H, "device_id": "dev-b", "measures": "runs_completed, latency_p95_ms"},
                                DASHBOARD, TEMPLATE)
        assert query["device_ids"] == ["dev-b"] and query["measures"] == ["runs_completed", "latency_p95_ms"]
        assert query["all_measures"] is False

    def test_refusals(self):
        with pytest.raises(series.InvalidQuery) as caught:
            series.validate({"to": T0 + H}, DASHBOARD, TEMPLATE)
        assert caught.value.code == "invalid_range" and caught.value.extra["field"] == "from"
        with pytest.raises(series.InvalidQuery) as caught:
            series.validate({"from": "soon", "to": T0 + H}, DASHBOARD, TEMPLATE)
        assert caught.value.code == "invalid_range"
        with pytest.raises(series.InvalidQuery) as caught:
            series.validate({"from": T0, "to": T0 + H, "device_id": "dev-z"}, DASHBOARD, TEMPLATE)
        assert caught.value.code == "invalid_device"
        with pytest.raises(series.InvalidQuery) as caught:
            series.validate({"from": T0, "to": T0 + H, "measures": "runs_completed,ppe_frames"}, DASHBOARD, TEMPLATE)
        assert caught.value.code == "invalid_measure" and caught.value.extra["measure"] == "ppe_frames"
        with pytest.raises(series.InvalidQuery) as caught:
            series.validate({"from": T0, "to": T0 + H, "measures": "runs_completed,nope,other"}, DASHBOARD, TEMPLATE)
        assert caught.value.extra["measure"] == "nope", "the first offender is named"
        with pytest.raises(series.InvalidQuery) as caught:
            series.validate({"from": T0, "to": T0 + H, "measures": ",".join(["runs_completed"] * 65)}, DASHBOARD, TEMPLATE)
        assert caught.value.code == "invalid_measure"
        with pytest.raises(series.RangeTooLong):
            series.validate({"from": T0, "to": T0 + 401 * D}, DASHBOARD, TEMPLATE)

    def test_the_ppe_template_allows_the_event_measures(self):
        template = schemas.TEMPLATES["ppe-compliance"]
        query = series.validate({"from": T0, "to": T0 + H, "measures": "events_clip_ready"}, DASHBOARD, template)
        assert query["measures"] == ["events_clip_ready"]


class FakeTable:
    def __init__(self, items):
        self.items = items
        self.calls = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        return {"Items": self.items}


class TestQuery:
    def test_one_query_per_device_with_the_projection(self):
        table = FakeTable([{"sk": "M#" + fold.minute_key(T0), "measures": {"runs_completed": 3}, "events": {}, "gaps": 1,
                            "summariesFolded": 6, "expires_at": 9_999_999_999}])
        items = series.query_device(table, "P#u#d#w", "minute", T0, T0 + H, ["runs_completed", "runs_failed"], now_s=1)
        assert len(table.calls) == 1
        call = table.calls[0]
        assert call["ProjectionExpression"] == "#sk, #ev, #gaps, #sf, #ex, #ms.#m0, #ms.#m1"
        assert call["ExpressionAttributeNames"]["#m0"] == "runs_completed" and call["ExpressionAttributeNames"]["#m1"] == "runs_failed"
        assert "FilterExpression" in call
        assert len(items) == 1
        table = FakeTable([])
        series.query_device(table, "P#u#d#w", "hour", T0, T0 + 2 * D, None, now_s=1)
        assert table.calls[0]["ProjectionExpression"].endswith("#ms")

    def test_points(self):
        from decimal import Decimal
        minute = fold.bucket_minute("event", T0)
        items = [
            {"sk": "M#" + fold.minute_key(minute + 60_000), "measures": {"runs_completed": Decimal("4"), "continuous_fps": Decimal("2.5")},
             "events": {"clip_ready": Decimal("1")}, "gaps": Decimal("0"), "summariesFolded": Decimal("6")},
            {"sk": "M#" + fold.minute_key(minute), "measures": {"runs_completed": Decimal("3")}, "events": {}, "gaps": 2,
             "summariesFolded": 6},
            {"sk": "junk"},
        ]
        points = series.points(items, "minute")
        assert [p["t"] for p in points] == [minute, minute + 60_000]
        assert points[0]["measures"] == {"runs_completed": 3} and points[0]["gaps"] == 2 and points[0]["summaries"] == 6
        assert points[1]["measures"] == {"runs_completed": 4, "continuous_fps": 2.5} and points[1]["events"] == {"clip_ready": 1}
        day = series.points([{"sk": "D#" + fold.day_key(T0), "measures": {}}], "day")
        assert day[0]["t"] == fold.day_of_key(fold.day_key(T0))

    def test_point_cap(self):
        minute = fold.bucket_minute("event", T0)
        items = [{"sk": "M#" + fold.minute_key(minute + i * 60_000), "measures": {}} for i in range(400)]
        assert len(series.points(items, "minute")) == 360
