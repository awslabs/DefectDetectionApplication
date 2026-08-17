#!/usr/bin/env python3
"""Cost model for the opensource-generation-models exploration (task 7.3).

Pure arithmetic over:
- live us-east-1 on-demand rates (captured from the AWS Pricing API, see RATES)
- measured Phase C per-image latencies (see LATENCY_SECONDS)
- the three Usage_Profiles defined in `artifacts/cost-model.md`

Generates the estimate grid that `artifacts/cost-model.md` embeds. Contains no
AWS calls: rates are frozen constants with their capture date so the document is
reproducible.

Property 9 (design.md): on-demand cost must be monotonically non-decreasing in
images-per-day. `assert_on_demand_monotonic` is the sanity check; it is exercised
by `tests/test_cost_model.py`.

Usage: python3 cost_model.py            # markdown grid to stdout
       python3 cost_model.py --check     # run the monotonicity sanity check only
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Pricing (us-east-1, captured 2026-08-17 from the AWS Pricing API:
# aws pricing get-products --service-code AmazonEC2 / AmazonSageMaker)
# ---------------------------------------------------------------------------

RATES_CAPTURED_UTC = "2026-08-17T23:40:00Z"

EC2_RATES_USD_PER_HOUR: Dict[str, float] = {
    "g5.xlarge": 1.006,
    "g5.2xlarge": 1.212,
    "g6.xlarge": 0.8048,
    "g6.2xlarge": 0.9776,
    "g6e.xlarge": 1.861,
    "g6e.2xlarge": 2.24208,
    "g6e.4xlarge": 3.00424,
    "g6e.8xlarge": 4.52856,
    "g6e.12xlarge": 10.49264,
    "p4d.24xlarge": 21.957642,
}

SAGEMAKER_RATES_USD_PER_HOUR: Dict[str, float] = {
    "ml.g5.xlarge": 1.408,
    "ml.g5.2xlarge": 1.515,
    "ml.g6e.xlarge": 2.61,
    "ml.g6e.2xlarge": 2.80,
    "ml.g6e.4xlarge": 3.76,
    "ml.g6e.8xlarge": 5.6607,
    "ml.g6e.12xlarge": 13.12,
}

# Amazon Nova Canvas per-image list price (Bedrock, us-east-1, Pricing API
# usagetype USE1-NovaCanvas-*). Inpainting bills as image-to-image (I2I).
NOVA_CANVAS_USD_PER_IMAGE = {
    "i2i-1024-standard": 0.04,
    "i2i-1024-premium": 0.06,
    "i2i-2048-standard": 0.06,
    "i2i-2048-premium": 0.08,
}
NOVA_CANVAS_BASELINE_PER_IMAGE = NOVA_CANVAS_USD_PER_IMAGE["i2i-1024-standard"]

HOURS_PER_MONTH_ALWAYS_ON = 730.0

# ---------------------------------------------------------------------------
# Measured Phase C latencies (seconds per image) — benchmark-results/*/run-001
# ---------------------------------------------------------------------------

LATENCY_SECONDS: Dict[str, float] = {
    # median / steady-state values from notes.md
    "flux.1-fill-dev-inpaint": 36.7,
    "flux.1-dev-t2i": 36.0,
    "flux.1-schnell-inpaint": 20.3,
    "flux.1-schnell-t2i": 21.0,
    "flux.2-dev-edit": 81.5,
    "hunyuanimage-2.1-t2i": 39.7,
    "pixart-alpha-t2i": 6.7,
    "pixart-sigma-t2i": 7.0,
}

# Measured model_load_seconds (warm HF cache) — the Phase C cold-start proxy.
MODEL_LOAD_SECONDS: Dict[str, float] = {
    "flux.1-fill-dev-inpaint": 4.44,
    "flux.1-dev-t2i": 4.44,
    "flux.1-schnell-inpaint": 4.18,
    "flux.1-schnell-t2i": 4.18,
    "flux.2-dev-edit": 38.11,
    "hunyuanimage-2.1-t2i": 18.92,
    "pixart-alpha-t2i": 375.8,
    "pixart-sigma-t2i": 554.0,
}

# Per-hosting-option *provisioning* component of Cold_Start_Time, in seconds.
# These are ESTIMATES from AWS documentation (no SageMaker endpoint was created —
# task 4.4 was skipped by user decision). The model-load component added on top
# is MEASURED (MODEL_LOAD_SECONDS), so each on-demand cold start below is
# "documented estimate + Phase C measurement".
PROVISION_SECONDS_ESTIMATE: Dict[str, float] = {
    "sagemaker-async": 360.0,        # instance provision + container + weights
    "sagemaker-realtime-stz": 360.0,
    "ec2-stopstart": 120.0,          # boot + service start (weights on EBS)
    "ecs-eks": 450.0,                # node provision + 15-25 GB image pull
}


def cold_start_seconds(hosting_key: str, latency_key: str) -> float:
    """Estimated provisioning time plus the measured model-load time."""
    return (PROVISION_SECONDS_ESTIMATE[hosting_key]
            + MODEL_LOAD_SECONDS[latency_key])


@dataclass(frozen=True)
class UsageProfile:
    """A generation workload level (Req 4.2)."""

    name: str
    images_per_day: int
    active_days_per_month: int
    concurrency: int
    sessions_per_month: int
    description: str

    @property
    def images_per_month(self) -> int:
        return self.images_per_day * self.active_days_per_month


PROFILES: List[UsageProfile] = [
    UsageProfile(
        name="dev-light",
        images_per_day=50,
        active_days_per_month=22,
        concurrency=1,
        sessions_per_month=44,
        description="one engineer iterating on prompts, business hours only "
                    "(2 bursts/day)",
    ),
    UsageProfile(
        name="steady-team",
        images_per_day=500,
        active_days_per_month=22,
        concurrency=2,
        sessions_per_month=132,
        description="a team generating datasets during working hours "
                    "(6 bursts/day)",
    ),
    UsageProfile(
        name="production-sustained",
        images_per_day=5000,
        active_days_per_month=30,
        concurrency=4,
        sessions_per_month=240,
        description="continuous 24x7 dataset production (8 bursts/day)",
    ),
]


def images_per_instance_hour(latency_seconds: float) -> float:
    """Throughput of one resident instance at the measured latency."""
    if latency_seconds <= 0:
        raise ValueError("latency_seconds must be positive")
    return 3600.0 / latency_seconds


def instances_needed(profile: UsageProfile, latency_seconds: float) -> int:
    """Instances an always-on deployment needs: enough for the profile's
    concurrency and enough throughput for its monthly volume."""
    per_hour = images_per_instance_hour(latency_seconds)
    capacity_bound = profile.images_per_month / (
        HOURS_PER_MONTH_ALWAYS_ON * per_hour)
    needed = max(profile.concurrency, capacity_bound)
    return max(1, int(needed) + (1 if needed > int(needed) else 0))


def always_on_monthly_cost(rate_usd_per_hour: float, instances: int,
                           hours: float = HOURS_PER_MONTH_ALWAYS_ON) -> float:
    """Persistent capacity: instances x hours x rate (Req 4.3)."""
    if rate_usd_per_hour < 0 or instances < 0 or hours < 0:
        raise ValueError("negative inputs are not valid")
    return rate_usd_per_hour * instances * hours


def on_demand_monthly_cost(rate_usd_per_hour: float, latency_seconds: float,
                           images_per_month: int, sessions_per_month: int,
                           cold_start_seconds: float,
                           idle_window_seconds: float = 300.0) -> float:
    """Scale-to-zero capacity (Req 4.3, 4.5).

    Billed instance-seconds = generation work (images x latency) plus, once per
    session, the scale-up time and the idle window before scale-in. Monotonically
    non-decreasing in ``images_per_month`` and in ``sessions_per_month``.
    """
    if min(rate_usd_per_hour, latency_seconds, images_per_month,
           sessions_per_month, cold_start_seconds, idle_window_seconds) < 0:
        raise ValueError("negative inputs are not valid")
    work_seconds = images_per_month * latency_seconds
    overhead_seconds = sessions_per_month * (
        cold_start_seconds + idle_window_seconds)
    return rate_usd_per_hour * (work_seconds + overhead_seconds) / 3600.0


def bedrock_baseline_monthly_cost(images_per_month: int,
                                  usd_per_image: float =
                                  NOVA_CANVAS_BASELINE_PER_IMAGE) -> float:
    """Per-image API baseline (Req 4.4). No capacity cost, no cold start."""
    if images_per_month < 0 or usd_per_image < 0:
        raise ValueError("negative inputs are not valid")
    return images_per_month * usd_per_image


def per_image_cost(monthly_cost: float, images_per_month: int) -> Optional[float]:
    if images_per_month <= 0:
        return None
    return monthly_cost / images_per_month


def assert_on_demand_monotonic(rate_usd_per_hour: float = 2.80,
                               latency_seconds: float = 36.7,
                               sessions_per_month: int = 44,
                               cold_start_seconds: float = 420.0,
                               max_images_per_day: int = 5000) -> None:
    """Property 9 sanity check: more images never lowers on-demand cost.

    Sweeps images-per-day from 0 to ``max_images_per_day`` and asserts the
    monthly on-demand estimate is non-decreasing. Raises AssertionError with the
    offending pair if the invariant breaks.
    """
    previous = -1.0
    for images_per_day in range(0, max_images_per_day + 1, 10):
        cost = on_demand_monthly_cost(
            rate_usd_per_hour, latency_seconds, images_per_day * 22,
            sessions_per_month, cold_start_seconds)
        assert cost >= previous, (
            f"on-demand cost decreased at {images_per_day} images/day: "
            f"{cost} < {previous}")
        previous = cost


# ---------------------------------------------------------------------------
# Grid generation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Combination:
    """One (model, hosting option, availability mode) row of the grid."""

    model: str
    latency_key: str
    hosting_option: str
    availability_mode: str
    instance_type: str
    rate: float
    cold_start_key: Optional[str]


def viable_combinations() -> List[Combination]:
    """Every (benchmarked model) x (viable Hosting_Option) x (Availability_Mode)
    combination the hosting comparison rated viable for that model."""
    sm = SAGEMAKER_RATES_USD_PER_HOUR
    ec2 = EC2_RATES_USD_PER_HOUR
    rows: List[Combination] = []

    medium = [
        ("FLUX.1-Fill-dev (inpaint)", "flux.1-fill-dev-inpaint"),
        ("FLUX.1-schnell (inpaint)", "flux.1-schnell-inpaint"),
    ]
    for model, key in medium:
        rows += [
            Combination(model, key, "SageMaker real-time", "always-on",
                        "ml.g6e.2xlarge", sm["ml.g6e.2xlarge"], None),
            Combination(model, key, "SageMaker async", "on-demand",
                        "ml.g6e.2xlarge", sm["ml.g6e.2xlarge"],
                        "sagemaker-async"),
            Combination(model, key, "EC2 + inference server", "always-on",
                        "g6e.2xlarge", ec2["g6e.2xlarge"], None),
            Combination(model, key, "EC2 stop/start", "on-demand",
                        "g6e.2xlarge", ec2["g6e.2xlarge"], "ec2-stopstart"),
            Combination(model, key, "ECS/EKS GPU", "always-on",
                        "g6e.2xlarge", ec2["g6e.2xlarge"], None),
            Combination(model, key, "ECS/EKS GPU", "on-demand",
                        "g6e.2xlarge", ec2["g6e.2xlarge"], "ecs-eks"),
        ]

    large = [
        ("FLUX.2 [dev] (edit, NF4)", "flux.2-dev-edit"),
        ("HunyuanImage-2.1 (t2i, NF4)", "hunyuanimage-2.1-t2i"),
    ]
    for model, key in large:
        rows += [
            # >60 s per image, so SageMaker real-time is excluded by the
            # invocation ceiling for flux.2; async only.
            Combination(model, key, "SageMaker async", "on-demand",
                        "ml.g6e.8xlarge", sm["ml.g6e.8xlarge"],
                        "sagemaker-async"),
            Combination(model, key, "EC2 + inference server", "always-on",
                        "g6e.8xlarge", ec2["g6e.8xlarge"], None),
            Combination(model, key, "EC2 stop/start", "on-demand",
                        "g6e.8xlarge", ec2["g6e.8xlarge"], "ec2-stopstart"),
        ]
    for model, key in [("HunyuanImage-2.1 (t2i, NF4)",
                        "hunyuanimage-2.1-t2i")]:
        rows.append(
            Combination(model, key, "SageMaker real-time", "always-on",
                        "ml.g6e.8xlarge", sm["ml.g6e.8xlarge"], None))

    small = [
        ("PixArt-alpha (t2i)", "pixart-alpha-t2i"),
        ("PixArt-Sigma (t2i)", "pixart-sigma-t2i"),
    ]
    for model, key in small:
        rows += [
            Combination(model, key, "SageMaker real-time", "always-on",
                        "ml.g5.2xlarge", sm["ml.g5.2xlarge"], None),
            Combination(model, key, "SageMaker async", "on-demand",
                        "ml.g5.2xlarge", sm["ml.g5.2xlarge"],
                        "sagemaker-async"),
            Combination(model, key, "EC2 + inference server", "always-on",
                        "g5.2xlarge", ec2["g5.2xlarge"], None),
            Combination(model, key, "EC2 stop/start", "on-demand",
                        "g5.2xlarge", ec2["g5.2xlarge"], "ec2-stopstart"),
        ]
    return rows


def grid_rows() -> List[dict]:
    out = []
    for combo in viable_combinations():
        latency = LATENCY_SECONDS[combo.latency_key]
        row = {
            "model": combo.model,
            "hosting_option": combo.hosting_option,
            "availability_mode": combo.availability_mode,
            "instance_type": combo.instance_type,
            "rate": combo.rate,
            "latency": latency,
            "costs": {},
        }
        for profile in PROFILES:
            if combo.availability_mode == "always-on":
                count = instances_needed(profile, latency)
                cost = always_on_monthly_cost(combo.rate, count)
                row["costs"][profile.name] = (cost, f"{count} inst")
            else:
                cold = cold_start_seconds(combo.cold_start_key,
                                          combo.latency_key)
                cost = on_demand_monthly_cost(
                    combo.rate, latency, profile.images_per_month,
                    profile.sessions_per_month, cold)
                # An on-demand deployment can never beat leaving it on.
                capped = min(
                    cost,
                    always_on_monthly_cost(
                        combo.rate, instances_needed(profile, latency)))
                note = "≈always-on" if capped < cost else ""
                row["costs"][profile.name] = (capped, note)
        out.append(row)
    return out


def to_markdown() -> str:
    lines = [
        f"<!-- generated by benchmark-harness/cost_model.py; rates captured "
        f"{RATES_CAPTURED_UTC} -->",
        "",
        "| Model | Hosting_Option | Mode | Instance | $/hr | Measured s/img | "
        + " | ".join(f"{p.name} $/mo" for p in PROFILES) + " |",
        "|---|---|---|---|---|---|" + "---|" * len(PROFILES),
    ]
    for row in grid_rows():
        cells = []
        for profile in PROFILES:
            cost, note = row["costs"][profile.name]
            cells.append(f"{cost:,.0f}" + (f" ({note})" if note else ""))
        lines.append(
            f"| {row['model']} | {row['hosting_option']} | "
            f"{row['availability_mode']} | {row['instance_type']} | "
            f"{row['rate']:.4f} | {row['latency']:.1f} | "
            + " | ".join(cells) + " |")
    lines += ["", "| Baseline | " + " | ".join(
        f"{p.name} $/mo" for p in PROFILES) + " |",
        "|---|" + "---|" * len(PROFILES)]
    lines.append(
        "| Amazon Nova Canvas (Bedrock, $0.04/image I2I) | "
        + " | ".join(
            f"{bedrock_baseline_monthly_cost(p.images_per_month):,.0f}"
            for p in PROFILES) + " |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="run the monotonicity sanity check and exit")
    args = ap.parse_args()
    if args.check:
        assert_on_demand_monotonic()
        print("monotonicity sanity check: PASS")
        return
    print(to_markdown())


if __name__ == "__main__":
    main()
