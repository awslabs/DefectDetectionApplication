"""``workflow_debug.log_reads`` (workflow-debug-mode design §10.1; tasks.md
3.4): a fifth ``GetLogEvents`` within a second is refused with
``retry_after_ms`` and no stub call; the token returns after 250 ms; the
``ThrottlingException`` mapping to 3000 ms; a 50,000-event drain in 5 calls
of ``limit=10000`` over at least 2 s.
"""
import pytest
from botocore.exceptions import ClientError

from workflow_debug import log_reads as lr


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def pacer(clock, monkeypatch):
    monkeypatch.delenv("DEBUG_GET_LOG_EVENTS_PER_SECOND", raising=False)
    monkeypatch.delenv("DEBUG_FILTER_LOG_EVENTS_PER_SECOND", raising=False)
    return lr.reset(clock=clock)


class TestRetryAfter:
    def test_rounded_up_to_250_ms_steps(self):
        assert lr.retry_after_ms(0.001) == 250
        assert lr.retry_after_ms(0.25) == 250
        assert lr.retry_after_ms(0.251) == 500
        assert lr.retry_after_ms(1.0) == 1000
        assert lr.retry_after_ms(0) == 250
        assert lr.retry_after_ms(-1) == 250


class TestBuckets:
    def test_default_rates(self, pacer):
        assert pacer.buckets["get"].rate == 4.0
        assert pacer.buckets["filter"].rate == 2.0

    def test_rates_from_the_environment(self, clock, monkeypatch):
        monkeypatch.setenv("DEBUG_GET_LOG_EVENTS_PER_SECOND", "8")
        monkeypatch.setenv("DEBUG_FILTER_LOG_EVENTS_PER_SECOND", "1")
        pacer = lr.reset(clock=clock)
        assert pacer.buckets["get"].rate == 8.0 and pacer.buckets["filter"].rate == 1.0
        monkeypatch.setenv("DEBUG_GET_LOG_EVENTS_PER_SECOND", "nonsense")
        monkeypatch.setenv("DEBUG_FILTER_LOG_EVENTS_PER_SECOND", "0")
        pacer = lr.reset(clock=clock)
        assert pacer.buckets["get"].rate == 4.0 and pacer.buckets["filter"].rate == 2.0

    def test_fifth_get_within_a_second_is_refused_without_a_call(self, pacer, clock):
        stub_calls = []
        for _ in range(4):
            lr.take("get")
            stub_calls.append("GetLogEvents")
        with pytest.raises(lr.ReadThrottled) as excinfo:
            lr.take("get")
        assert excinfo.value.retry_after_ms == 250
        assert excinfo.value.api == "get"
        assert len(stub_calls) == 4

    def test_the_token_returns_after_250_ms(self, pacer, clock):
        for _ in range(4):
            lr.take("get")
        with pytest.raises(lr.ReadThrottled):
            lr.take("get")
        clock.now += 0.25
        lr.take("get")
        with pytest.raises(lr.ReadThrottled) as excinfo:
            lr.take("get")
        assert excinfo.value.retry_after_ms == 250

    def test_filter_bucket_holds_two_per_second(self, pacer, clock):
        lr.take("filter")
        lr.take("filter")
        with pytest.raises(lr.ReadThrottled) as excinfo:
            lr.take("filter")
        assert excinfo.value.retry_after_ms == 500
        clock.now += 0.5
        lr.take("filter")

    def test_the_two_buckets_are_independent(self, pacer):
        for _ in range(4):
            lr.take("get")
        lr.take("filter")
        with pytest.raises(lr.ReadThrottled):
            lr.take("get")

    def test_the_burst_never_exceeds_one_second(self, pacer, clock):
        clock.now += 60
        for _ in range(4):
            lr.take("get")
        with pytest.raises(lr.ReadThrottled):
            lr.take("get")

    def test_continuous_refill_fraction(self, pacer, clock):
        for _ in range(4):
            lr.take("get")
        clock.now += 0.6  # 2.4 tokens back
        lr.take("get")
        lr.take("get")
        with pytest.raises(lr.ReadThrottled) as excinfo:
            lr.take("get")
        # 0.6 token short of the next one at 4/s = 0.15 s -> rounded up to 250 ms
        assert excinfo.value.retry_after_ms == 250


class TestThrottlingMapping:
    def test_throttling_exception_maps_to_3000_ms(self):
        error = ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "GetLogEvents")
        mapped = lr.map_client_error(error, "get")
        assert isinstance(mapped, lr.ReadThrottled)
        assert mapped.retry_after_ms == 3000 and mapped.api == "get"

    def test_other_errors_are_not_mapped(self):
        error = ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, "GetLogEvents")
        assert lr.map_client_error(error) is None


class TestPacedDrain:
    def test_fifty_thousand_events_in_five_calls_over_at_least_two_seconds(self, pacer, clock):
        pages = [[{"id": "e%d" % (p * 10_000 + i)} for i in range(10_000)] for p in range(5)]
        calls = []

        def fetch_page(token, limit):
            calls.append((token, limit, clock.now))
            index = int(token or 0)
            next_token = str(index + 1) if index + 1 < len(pages) else None
            return pages[index], next_token

        started = clock.now
        events, count = lr.paced_drain(fetch_page, clock=clock, sleep=clock.sleep, pacer_obj=pacer)
        assert count == 5 and len(calls) == 5
        assert len(events) == 50_000
        assert all(limit == 10_000 for _, limit, _ in calls)
        assert [token for token, _, _ in calls] == [None, "1", "2", "3", "4"]
        assert clock.now - started >= 2.0
        gaps = [calls[i + 1][2] - calls[i][2] for i in range(4)]
        assert all(gap >= 0.5 - 1e-9 for gap in gaps)

    def test_a_short_window_stops_at_its_end(self, pacer, clock):
        def fetch_page(token, limit):
            return [{"id": "a"}], None

        events, count = lr.paced_drain(fetch_page, clock=clock, sleep=clock.sleep, pacer_obj=pacer)
        assert count == 1 and events == [{"id": "a"}]

    def test_the_cap_of_fifty_thousand_events(self, pacer, clock):
        def fetch_page(token, limit):
            return [{"id": "x"}] * 10_000, "more"

        events, count = lr.paced_drain(fetch_page, clock=clock, sleep=clock.sleep, pacer_obj=pacer)
        assert count == 5 and len(events) == 50_000

    def test_an_empty_bucket_is_waited_out(self, clock):
        pacer = lr.reset(clock=clock)
        for _ in range(4):
            pacer.take("get")

        def fetch_page(token, limit):
            return [{"id": "a"}], None

        lr.paced_drain(fetch_page, clock=clock, sleep=clock.sleep, pacer_obj=pacer)
        assert clock.sleeps and clock.sleeps[0] == 0.25
