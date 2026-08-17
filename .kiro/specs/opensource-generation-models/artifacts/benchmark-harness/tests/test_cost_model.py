"""Tests for the cost model companion script (task 7.3).

Covers the Property 9 monotonicity sanity check (on-demand cost never falls as
images-per-day rises) plus the example-based arithmetic and coverage checks the
cost-model document relies on.

**Validates: Requirements 4.1, 4.2, 4.3, 4.4**
"""

import pytest

import cost_model as cm


class TestMonotonicity:
    """Property 9: on-demand cost is monotonically non-decreasing in images."""

    def test_sanity_check_passes_for_the_documented_defaults(self):
        cm.assert_on_demand_monotonic()

    @pytest.mark.parametrize("rate,latency,cold", [
        (2.80, 36.7, 420.0),
        (2.24208, 20.3, 180.0),
        (5.6607, 81.5, 420.0),
        (1.515, 7.0, 420.0),
    ])
    def test_monotonic_across_every_priced_shape(self, rate, latency, cold):
        cm.assert_on_demand_monotonic(rate_usd_per_hour=rate,
                                      latency_seconds=latency,
                                      cold_start_seconds=cold,
                                      max_images_per_day=1000)

    def test_monotonic_in_sessions_too(self):
        previous = -1.0
        for sessions in range(0, 400, 10):
            cost = cm.on_demand_monthly_cost(2.80, 36.7, 11000, sessions, 420.0)
            assert cost >= previous
            previous = cost

    def test_more_images_strictly_increases_cost(self):
        low = cm.on_demand_monthly_cost(2.80, 36.7, 1_100, 44, 420.0)
        high = cm.on_demand_monthly_cost(2.80, 36.7, 11_000, 44, 420.0)
        assert high > low


class TestArithmetic:
    def test_always_on_is_instances_times_hours_times_rate(self):
        assert cm.always_on_monthly_cost(2.80, 1) == pytest.approx(2.80 * 730)
        assert cm.always_on_monthly_cost(2.80, 2) == pytest.approx(2.80 * 1460)

    def test_on_demand_with_zero_images_still_pays_session_overhead(self):
        cost = cm.on_demand_monthly_cost(2.80, 36.7, 0, 44, 420.0)
        expected = 2.80 * 44 * (420.0 + 300.0) / 3600.0
        assert cost == pytest.approx(expected)

    def test_on_demand_with_no_sessions_and_no_images_is_free(self):
        assert cm.on_demand_monthly_cost(2.80, 36.7, 0, 0, 420.0) == 0.0

    def test_bedrock_baseline_is_linear_per_image(self):
        assert cm.bedrock_baseline_monthly_cost(1_100) == pytest.approx(44.0)
        assert cm.bedrock_baseline_monthly_cost(150_000) == pytest.approx(6000.0)

    def test_negative_inputs_rejected(self):
        with pytest.raises(ValueError):
            cm.always_on_monthly_cost(-1.0, 1)
        with pytest.raises(ValueError):
            cm.on_demand_monthly_cost(2.80, 36.7, -5, 1, 420.0)
        with pytest.raises(ValueError):
            cm.bedrock_baseline_monthly_cost(-1)

    def test_zero_latency_rejected(self):
        with pytest.raises(ValueError):
            cm.images_per_instance_hour(0)

    def test_instances_needed_respects_concurrency_and_throughput(self):
        dev = cm.PROFILES[0]
        prod = cm.PROFILES[2]
        # dev-light at 36.7 s/img: concurrency 1 dominates
        assert cm.instances_needed(dev, 36.7) == 1
        # production-sustained (150k images/mo) at 36.7 s/img needs throughput
        # beyond concurrency 4: 150000 * 36.7 / 3600 / 730 = 2.09 -> 4 wins
        assert cm.instances_needed(prod, 36.7) == 4
        # a slow model at production volume becomes throughput-bound
        assert cm.instances_needed(prod, 300.0) > 4


class TestCoverage:
    """Property 9 coverage half: every combination x every profile has a cost."""

    def test_three_or_more_usage_profiles_defined(self):
        assert len(cm.PROFILES) >= 3
        names = {p.name for p in cm.PROFILES}
        assert names == {"dev-light", "steady-team", "production-sustained"}

    def test_every_combination_has_an_estimate_for_every_profile(self):
        rows = cm.grid_rows()
        assert rows
        for row in rows:
            for profile in cm.PROFILES:
                cost, _note = row["costs"][profile.name]
                assert cost > 0

    def test_every_benchmarked_model_appears_in_the_grid(self):
        models = {row["model"].split(" (")[0] for row in cm.grid_rows()}
        assert models == {
            "FLUX.1-Fill-dev", "FLUX.1-schnell", "FLUX.2 [dev]",
            "HunyuanImage-2.1", "PixArt-alpha", "PixArt-Sigma",
        }

    def test_both_availability_modes_present_for_every_model(self):
        by_model = {}
        for row in cm.grid_rows():
            by_model.setdefault(row["model"], set()).add(
                row["availability_mode"])
        for model, modes in by_model.items():
            assert modes == {"always-on", "on-demand"}, model

    def test_latencies_are_the_measured_phase_c_values(self):
        assert cm.LATENCY_SECONDS["flux.1-fill-dev-inpaint"] == 36.7
        assert cm.LATENCY_SECONDS["flux.1-schnell-inpaint"] == 20.3
        assert cm.LATENCY_SECONDS["flux.2-dev-edit"] == 81.5

    def test_markdown_renders_all_rows_and_the_baseline(self):
        md = cm.to_markdown()
        assert "Amazon Nova Canvas" in md
        assert md.count("\n|") >= len(cm.grid_rows())
