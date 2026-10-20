"""``telemetry.leases`` (workflow-telemetry-dashboards design §9.3; tasks.md
3.4) on the moto config table: interleavings under N of 1, 5 and 20; renewal
keeps ``started_at``; the ninth Workflow on a Device -> ``device_live_limit``;
a full use case -> ``usecase_slots``; the 30-minute limit; release and the
lapsed-Pair tidy-up."""
import sys
import uuid

import boto3
import pytest

import telemetry_tables

T0 = 1_792_000_000_000


@pytest.fixture
def leases(aws_stack, monkeypatch):
    telemetry_tables.set_env(monkeypatch)
    telemetry_tables.ensure_telemetry_tables()
    monkeypatch.delenv("TELEMETRY_LIVE_MAX_LEASES", raising=False)
    monkeypatch.delenv("TELEMETRY_LIVE_MAX_VIEW_MINUTES", raising=False)
    for name in list(sys.modules):
        if name == "telemetry" or name.startswith("telemetry."):
            sys.modules.pop(name, None)
    from telemetry import leases as module
    module.reset_clients()
    return module


@pytest.fixture
def table():
    return telemetry_tables.tables()["config"]


def uc():
    return str(uuid.uuid4())


class TestEnvironment:
    def test_defaults_and_ranges(self, leases, monkeypatch):
        assert leases.max_leases() == 5 and leases.max_view_minutes() == 30
        monkeypatch.setenv("TELEMETRY_LIVE_MAX_LEASES", "20")
        assert leases.max_leases() == 20
        monkeypatch.setenv("TELEMETRY_LIVE_MAX_LEASES", "99")
        assert leases.max_leases() == 20, "clamped to the range"
        monkeypatch.setenv("TELEMETRY_LIVE_MAX_LEASES", "junk")
        assert leases.max_leases() == 5
        monkeypatch.setenv("TELEMETRY_LIVE_MAX_VIEW_MINUTES", "240")
        assert leases.max_view_minutes() == 240
        assert leases.LEASE_SECONDS == 60 and leases.DEVICE_LIVE_LIMIT == 8


class TestAcquire:
    def test_take_then_renew_keeps_started_at(self, leases, table):
        usecase_id = uc()
        first = leases.acquire(table, usecase_id, "dash", "sub-a", "dev-1", "wf-1", T0)
        assert first.granted and first.slot == 0 and first.started_at == T0 and first.first_grant is True
        assert first.lease_expires_ms == T0 + 60_000 and first.pair_expires_ms == T0 + 60_000
        renewed = leases.acquire(table, usecase_id, "dash", "sub-a", "dev-1", "wf-1", T0 + 20_000)
        assert renewed.granted and renewed.slot == 0 and renewed.started_at == T0 and renewed.first_grant is False
        assert renewed.lease_expires_ms == T0 + 80_000 and renewed.pair_expires_ms == T0 + 80_000
        items = leases.read_partition(table, usecase_id)
        slots = leases.slots_of(items)
        assert set(slots) == {0} and slots[0]["holder"] == "dash#sub-a" and slots[0]["started_at"] == T0
        holder = leases.holder_of(items, "dash", "sub-a")
        assert holder["slot"] == 0 and holder["leaseExpiresMs"] == T0 + 80_000
        assert slots[0]["expires_at"] == (T0 + 80_000) // 1000 + 3600, "the TTL is the lease expiry plus an hour"

    @pytest.mark.parametrize("n", [1, 5, 20])
    def test_interleavings_fill_exactly_n_slots(self, leases, table, monkeypatch, n):
        monkeypatch.setenv("TELEMETRY_LIVE_MAX_LEASES", str(n))
        usecase_id = uc()
        results = []
        for index in range(n + 2):
            results.append(leases.acquire(table, usecase_id, "dash-{0}".format(index % 3), "sub-{0}".format(index),
                                          "dev-1", "wf-1", T0 + index))
        granted = [r for r in results if r.granted]
        assert len(granted) == n and sorted(r.slot for r in granted) == list(range(n))
        refused = [r for r in results if not r.granted]
        assert len(refused) == 2 and all(r.reason == "usecase_slots" for r in refused)
        # A renewal by an existing holder still works while the use case is full.
        renewed = leases.acquire(table, usecase_id, "dash-0", "sub-0", "dev-1", "wf-1", T0 + 30_000)
        assert renewed.granted and renewed.slot == 0
        # Once the other slots lapse the next viewer takes the first freed index;
        # with one slot the renewed holder still owns it.
        late = leases.acquire(table, usecase_id, "dash-9", "sub-late", "dev-1", "wf-1", T0 + 61_000)
        if n > 1:
            assert late.granted and late.slot == 1 and late.pair_expires_ms == T0 + 121_000
        else:
            assert not late.granted and late.reason == "usecase_slots" and late.pair_expires_ms == T0 + 90_000

    def test_pair_expiry_is_the_latest_among_the_pairs_slots(self, leases, table):
        usecase_id = uc()
        leases.acquire(table, usecase_id, "dash", "sub-a", "dev-1", "wf-1", T0)
        second = leases.acquire(table, usecase_id, "dash", "sub-b", "dev-1", "wf-1", T0 + 15_000)
        assert second.pair_expires_ms == T0 + 75_000
        other = leases.acquire(table, usecase_id, "dash", "sub-c", "dev-2", "wf-1", T0 + 20_000)
        assert other.pair_expires_ms == T0 + 80_000, "another Device's Pair is separate"
        renewed = leases.acquire(table, usecase_id, "dash", "sub-a", "dev-1", "wf-1", T0 + 20_000)
        assert renewed.pair_expires_ms == T0 + 80_000

    def test_the_ninth_workflow_on_a_device_is_device_live_limit(self, leases, table, monkeypatch):
        monkeypatch.setenv("TELEMETRY_LIVE_MAX_LEASES", "20")
        usecase_id = uc()
        for index in range(8):
            assert leases.acquire(table, usecase_id, "dash", "sub-{0}".format(index), "dev-1", "wf-{0}".format(index), T0).granted
        ninth = leases.acquire(table, usecase_id, "dash", "sub-9", "dev-1", "wf-9", T0 + 1)
        assert not ninth.granted and ninth.reason == "device_live_limit"
        # The same Workflow again is fine (it is not another Workflow), as is another Device.
        assert leases.acquire(table, usecase_id, "dash", "sub-10", "dev-1", "wf-0", T0 + 2).granted
        assert leases.acquire(table, usecase_id, "dash", "sub-11", "dev-2", "wf-9", T0 + 3).granted
        assert leases.device_other_workflows(leases.slots_of(leases.read_partition(table, usecase_id)), "dev-1", "wf-9", T0 + 3) == 8

    def test_a_full_use_case_is_usecase_slots(self, leases, table):
        usecase_id = uc()
        for index in range(5):
            assert leases.acquire(table, usecase_id, "dash", "sub-{0}".format(index), "dev-{0}".format(index), "wf", T0).granted
        sixth = leases.acquire(table, usecase_id, "dash", "sub-5", "dev-5", "wf", T0 + 1)
        assert not sixth.granted and sixth.reason == "usecase_slots" and sixth.pair_expires_ms == 0

    def test_the_thirty_minute_limit(self, leases, table):
        usecase_id = uc()
        leases.acquire(table, usecase_id, "dash", "sub-a", "dev-1", "wf-1", T0)
        now = T0
        while now < T0 + 30 * 60_000 - 20_000:
            now += 20_000
            assert leases.acquire(table, usecase_id, "dash", "sub-a", "dev-1", "wf-1", now).granted
        at_limit = leases.acquire(table, usecase_id, "dash", "sub-a", "dev-1", "wf-1", T0 + 30 * 60_000)
        assert not at_limit.granted and at_limit.reason == "live_viewing_limit" and at_limit.started_at == T0
        # After the lease lapses the viewer starts a fresh 30 minutes.
        fresh = leases.acquire(table, usecase_id, "dash", "sub-a", "dev-1", "wf-1", T0 + 30 * 60_000 + 61_000)
        assert fresh.granted and fresh.started_at == T0 + 30 * 60_000 + 61_000 and fresh.first_grant is True

    def test_custom_view_limit(self, leases, table, monkeypatch):
        monkeypatch.setenv("TELEMETRY_LIVE_MAX_VIEW_MINUTES", "5")
        usecase_id = uc()
        leases.acquire(table, usecase_id, "dash", "sub-a", "dev-1", "wf-1", T0)
        now = T0
        while now < T0 + 4 * 60_000 + 40_000:
            now += 20_000
            assert leases.acquire(table, usecase_id, "dash", "sub-a", "dev-1", "wf-1", now).granted
        assert leases.acquire(table, usecase_id, "dash", "sub-a", "dev-1", "wf-1", T0 + 5 * 60_000 + 5_000).reason == "live_viewing_limit"

    def test_a_lost_race_retries_once_then_reports_full(self, leases, table, monkeypatch):
        from botocore.exceptions import ClientError
        usecase_id = uc()
        calls = []

        def cancelled(items):
            calls.append(items)
            raise ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "x"}}, "TransactWriteItems")

        result = leases.acquire(table, usecase_id, "dash", "sub-a", "dev-1", "wf-1", T0, transact=cancelled)
        assert not result.granted and result.reason == "usecase_slots" and len(calls) == 2
        with pytest.raises(ClientError):
            def broken(items):
                raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}}, "TransactWriteItems")
            leases.acquire(table, usecase_id, "dash", "sub-a", "dev-1", "wf-1", T0, transact=broken)


class TestReleaseAndTidy:
    def test_release_ends_the_lease_and_deletes_the_holder(self, leases, table):
        usecase_id = uc()
        leases.acquire(table, usecase_id, "dash", "sub-a", "dev-1", "wf-1", T0)
        assert leases.release(table, usecase_id, "dash", "sub-a") is True
        items = leases.read_partition(table, usecase_id)
        assert leases.holder_of(items, "dash", "sub-a") is None
        slots = leases.slots_of(items)
        assert slots[0]["leaseExpiresMs"] == 0
        assert leases.acquire(table, usecase_id, "dash", "sub-b", "dev-1", "wf-1", T0 + 1).slot == 0, "the slot is free again"
        assert leases.release(table, usecase_id, "dash", "nobody") is False

    def test_lapsed_pairs_and_tidy(self, leases, table):
        usecase_id = uc()
        leases.acquire(table, usecase_id, "dash", "sub-a", "dev-1", "wf-1", T0)
        leases.acquire(table, usecase_id, "dash", "sub-b", "dev-1", "wf-1", T0 + 5_000)
        leases.acquire(table, usecase_id, "dash", "sub-c", "dev-2", "wf-1", T0 + 50_000)
        slots = leases.slots_of(leases.read_partition(table, usecase_id))
        assert leases.lapsed_pairs(slots, T0 + 30_000) == []
        lapsed = leases.lapsed_pairs(slots, T0 + 70_000)
        assert lapsed == [("dev-1", "wf-1", [0, 1])], "dev-2's lease is still live"
        assert leases.lapsed_pairs(slots, T0 + 120_000) == [("dev-1", "wf-1", [0, 1]), ("dev-2", "wf-1", [2])]
        assert leases.tidy_pair(table, usecase_id, [0, 1], T0 + 70_000) == 2
        assert set(leases.slots_of(leases.read_partition(table, usecase_id))) == {2}
        # A slot that was re-taken meanwhile is not deleted.
        leases.acquire(table, usecase_id, "dash", "sub-d", "dev-3", "wf-2", T0 + 70_000)
        assert leases.tidy_pair(table, usecase_id, [0], T0 + 70_000) == 0
