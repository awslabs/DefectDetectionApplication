"""``telemetry.fold`` (workflow-telemetry-dashboards design §7.3, §8.1;
tasks.md 3.2): order-independence and duplicate-insensitivity; the reference
fold over raw Summaries equals the hour fold over minute items and the day
fold over hour items for sum and max measures; ``bucket_minute`` on a boundary
Summary and a boundary Event; ``count_gaps`` on ``seq`` 5, 7, 6 -> 0 and on
5, 8 -> 2 attributed to the minute of 8; ``p50mean`` weighting and
``p95max``; the ``worst`` ordering; the owner filter.
"""
import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from telemetry import fold

INSTANCE = "0f1e2d3c-4b5a-4968-8776-65544332211a"
OTHER_INSTANCE = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5e"
T0 = 1_792_000_000_000           # 2026-10-14T17:46:40Z, an aligned epoch ms (multiple of 10 s)
MINUTE = fold.bucket_minute("event", T0)


def summary_item(seq, *, event_ms=None, received_ms=None, producer="engine", instance=INSTANCE, idle=False,
                 runs=(1, 0, 0), latency=None, per_class=None, state="READY", waits=0, fps=None, telemetry=(0, 0, 0),
                 ext=None, outcomes=None, people=None):
    event_ms = event_ms if event_ms is not None else MINUTE + 10_000 * ((seq - 1) % 6 + 1)
    received_ms = received_ms if received_ms is not None else event_ms + 400
    fields = {
        "schema": "dda.summary.v1", "intervalStart": event_ms - 10_000, "intervalEnd": event_ms,
        "emittedAt": event_ms + 100, "deviceId": "dev", "workflowId": "wf", "workflowVersion": "1",
        "producer": {"name": producer, "version": "1", "instance": instance}, "seq": seq, "intervalS": 10, "idle": idle,
        "telemetry": {"queueDrops": telemetry[0], "publishFailures": telemetry[1], "eventsDropped": telemetry[2]},
    }
    if not idle:
        if producer == "engine":
            fields["runs"] = {"completed": runs[0], "failed": runs[1], "dropped": runs[2]}
            fields["model"] = {"state": state, "models": {}, "waits": waits, "waitedMs": waits * 100}
            if latency is not None:
                p50, p95, maximum, n = latency
                fields["latency"] = {"p50Ms": p50, "p95Ms": p95, "maxMs": maximum, "n": n}
            if per_class is not None:
                fields["detections"] = {"runsWithDetections": 1, "total": sum(v[0] for v in per_class.values()),
                                        "perClass": {k: {"total": v[0], "max": v[1]} for k, v in per_class.items()},
                                        "truncated": False}
            if fps is not None:
                fields["continuous"] = {"skippedNoNewFrame": 1, "paused": fps == 0, "effectiveFps": fps}
            if outcomes is not None:
                fields["outcomes"] = {"anomalous": outcomes[0], "normal": outcomes[1]}
            if people is not None:
                fields["people"] = {"seen": people}
        if ext is not None:
            fields["ext"] = ext
    return {"pk": "P#uc#dev#wf", "sk": "S#{0:013d}#{1:08x}".format(received_ms, seq), "summary": fields,
            "receivedMs": received_ms, "eventMs": event_ms, "tsSource": "device", "producer": producer,
            "instance": instance, "seq": seq, "idle": idle}


def event_item(event_type, event_ms=None):
    return {"pk": "P#uc#dev#wf", "sk": "E#{0:013d}#id".format(event_ms or MINUTE + 5), "type": event_type}


# --------------------------------------------------------------------------- buckets and keys
class TestBuckets:
    def test_a_summary_on_the_boundary_buckets_into_the_preceding_minute(self):
        assert fold.bucket_minute("summary", MINUTE + 60_000) == MINUTE
        assert fold.bucket_minute("summary", MINUTE + 60_001) == MINUTE + 60_000
        assert fold.bucket_minute("summary", MINUTE + 1) == MINUTE

    def test_an_event_on_the_boundary_buckets_into_the_following_minute(self):
        assert fold.bucket_minute("event", MINUTE + 60_000) == MINUTE + 60_000
        assert fold.bucket_minute("event", MINUTE + 59_999) == MINUTE
        assert fold.bucket_minute("live", MINUTE + 60_000) == MINUTE + 60_000

    def test_keys_round_trip(self):
        assert fold.minute_key(MINUTE) == "20261014" + "1746"   # yyyyMMdd + HHmm
        assert fold.minute_of_key(fold.minute_key(MINUTE)) == MINUTE
        assert fold.hour_key(MINUTE) == "2026101417" and fold.hour_of_key("2026101417") == MINUTE - 46 * 60_000
        assert fold.day_key(MINUTE) == "20261014" and fold.day_of_key("20261014") % fold.DAY_MS == 0
        assert len(fold.minute_key(MINUTE)) == 12 and len(fold.hour_key(MINUTE)) == 10 and len(fold.day_key(MINUTE)) == 8

    def test_the_shared_tolerance(self):
        assert fold.SUMMARY_EVENT_TIME_TOLERANCE_MS == 300_000


# --------------------------------------------------------------------------- the minute fold
class TestFoldMinute:
    def test_sums_maxima_and_counts(self):
        items = [summary_item(1, runs=(3, 1, 0), per_class={"helmet": (2, 2)}, state="READY", waits=1,
                              telemetry=(1, 0, 0), outcomes=(1, 2), people=2),
                 summary_item(2, runs=(4, 0, 2), per_class={"helmet": (5, 3), "vest": (1, 1)}, state="LOADING",
                              telemetry=(0, 2, 0), outcomes=(0, 4), people=5),
                 summary_item(3, idle=True, telemetry=(0, 0, 3))]
        folded = fold.fold_minute(items, [event_item("clip_ready"), event_item("clip_ready"), event_item("run_error")], gaps=1)
        measures = folded["measures"]
        assert measures["runs_completed"] == 7 and measures["runs_failed"] == 1 and measures["runs_dropped"] == 2
        assert measures["detections_total"] == 8 and measures["runs_with_detections"] == 2
        assert measures["detections_class_total"] == {"helmet": 7, "vest": 1}
        assert measures["detections_class_max"] == {"helmet": 3, "vest": 1}
        assert measures["model_state_worst"] == "LOADING" and measures["model_waits"] == 1 and measures["model_wait_ms"] == 100
        assert measures["anomalous"] == 1 and measures["normal"] == 6 and measures["people_seen_max"] == 5
        assert measures["telemetry_queue_drops"] == 1 and measures["telemetry_publish_failures"] == 2
        assert measures["telemetry_events_dropped"] == 3, "a Heartbeat's counters fold"
        assert measures["summaries"] == 2 and folded["summariesFolded"] == 2, "Heartbeats are not counted"
        assert measures["gaps"] == 1 and folded["gaps"] == 1
        assert folded["events"] == {"clip_ready": 2, "run_error": 1}
        assert measures["events_clip_ready"] == 2 and measures["events_run_error"] == 1
        assert "events_violation_confirmed" not in measures
        assert "latency_p50_ms" not in measures and "continuous_fps" not in measures

    def test_p50mean_is_weighted_by_n_and_p95max_is_the_maximum(self):
        items = [summary_item(1, latency=(100, 200, 250, 1)), summary_item(2, latency=(400, 500, 900, 3))]
        measures = fold.fold_minute(items, [])["measures"]
        assert measures["latency_p50_ms"] == 325          # (100*1 + 400*3) / 4
        assert measures["latency_p95_ms"] == 500
        assert measures["latency_max_ms"] == 900
        assert fold.fold_minute(items, [])["weights"] == {"latency_p50_ms": 4}

    def test_mean_last_and_worst(self):
        items = [summary_item(1, fps=4.0, state="READY"), summary_item(2, fps=2.0, state="UNKNOWN"),
                 summary_item(3, fps=0.0, state="LOADING")]
        measures = fold.fold_minute(items, [])["measures"]
        assert measures["continuous_fps"] == 2
        assert measures["continuous_paused"] is True, "the latest Summary's value"
        assert measures["continuous_skipped_no_new_frame"] == 3
        assert measures["model_state_worst"] == "UNKNOWN"
        reversed_measures = fold.fold_minute(list(reversed(items)), [])["measures"]
        assert reversed_measures["continuous_paused"] is True

    def test_the_worst_ordering(self):
        order = ["NONE", "READY", "UNLOADING", "LOADING", "INCOMPLETE", "NOT_DEPLOYED", "UNKNOWN", "UNAVAILABLE"]
        for index, state in enumerate(order):
            items = [summary_item(i + 1, state=s) for i, s in enumerate(order[:index + 1])]
            assert fold.fold_minute(items, [])["measures"]["model_state_worst"] == state

    def test_duplicates_fold_once(self):
        item = summary_item(1, runs=(5, 0, 0))
        twin = dict(item, sk="S#other", receivedMs=item["receivedMs"] + 50)
        folded = fold.fold_minute([item, twin, dict(item)], [])
        assert folded["measures"]["runs_completed"] == 5 and folded["summariesFolded"] == 1

    def test_different_instances_are_distinct(self):
        items = [summary_item(1, runs=(5, 0, 0)), summary_item(1, runs=(5, 0, 0), instance=OTHER_INSTANCE)]
        assert fold.fold_minute(items, [])["measures"]["runs_completed"] == 10

    def test_owner_filter(self):
        ext = {"ppe": {"people": {"seen": 3, "compliant": 2, "violating": 1},
                       "violations": {"frames": 4, "byReason": {"no-helmet": {"frames": 4, "objects": 5}}},
                       "clipsOpened": 1, "frames": 60}}
        helper = summary_item(1, producer="ppe-clipper", instance=OTHER_INSTANCE, ext=ext)
        helper["summary"]["runs"] = {"completed": 99, "failed": 0, "dropped": 0}   # not the helper's block
        engine = summary_item(1, runs=(2, 0, 0), ext={"ppe": {"people": {"seen": 9, "compliant": 0, "violating": 0},
                                                             "violations": {"frames": 0, "byReason": {}},
                                                             "clipsOpened": 0, "frames": 1}})
        other = summary_item(2, producer="someone", instance=OTHER_INSTANCE, ext=ext)
        measures = fold.fold_minute([helper, engine, other], [])["measures"]
        assert measures["runs_completed"] == 2, "the helper's runs block is not folded"
        assert measures["ppe_people_seen_max"] == 9, "engine is allowed on ext.ppe; 'someone' is not"
        assert measures["ppe_violation_frames"] == 4 and measures["ppe_clips_opened"] == 1 and measures["ppe_frames"] == 61
        assert measures["ppe_reason_frames"] == {"no-helmet": 4} and measures["ppe_reason_objects"] == {"no-helmet": 5}

    def test_empty_inputs(self):
        folded = fold.fold_minute([], [])
        assert folded["measures"] == {"summaries": 0, "gaps": 0} and folded["events"] == {}
        assert folded["summariesFolded"] == 0 and folded["weights"] == {}


# --------------------------------------------------------------------------- hour and day folds
def reference(items):
    return fold.fold_minute(items, [])["measures"]


SUM_AND_MAX = [m["id"] for m in fold.measure_specs()
               if m["source"] == "summary" and m["fold"] in ("sum", "max") and not m.get("derived")]


def minute_items(items):
    by_minute = {}
    for item in items:
        by_minute.setdefault(fold.bucket_minute("summary", item["eventMs"]), []).append(item)
    out = []
    for minute in sorted(by_minute):
        folded = fold.fold_minute(by_minute[minute], [])
        folded["sk"] = "M#" + fold.minute_key(minute)
        out.append(folded)
    return out


class TestFoldItems:
    def test_hour_and_day_folds_equal_the_reference_for_sum_and_max(self):
        items = []
        seq = 1
        for minute_index in range(130):          # a little over two hours, crossing a day boundary
            for _ in range(random.Random(minute_index).randint(0, 3)):
                event_ms = fold.day_of_key("20261015") - 60 * 60_000 + minute_index * 60_000 + 10_000 * (seq % 6 + 1)
                items.append(summary_item(seq, event_ms=event_ms, runs=(seq % 5, seq % 2, 0),
                                          per_class={"a": (seq % 7, seq % 3), "b": (1, 1)}, waits=seq % 2,
                                          latency=(100 + seq, 200 + seq, 300 + seq, 1 + seq % 4), outcomes=(1, 0), people=seq % 9))
                seq += 1
        minutes = minute_items(items)
        hours = {}
        for item in minutes:
            hours.setdefault(fold.hour_key(fold.minute_of_key(item["sk"][2:])), []).append(item)
        hour_items = []
        for key in sorted(hours):
            folded = fold.fold_items(hours[key])
            folded["sk"] = "H#" + key
            hour_items.append(folded)
        days = {}
        for item in hour_items:
            days.setdefault(fold.day_key(fold.hour_of_key(item["sk"][2:])), []).append(item)
        for key, hour_group in days.items():
            day = fold.fold_items(hour_group)
            day_start = fold.day_of_key(key)
            raw = [i for i in items if day_start <= fold.bucket_minute("summary", i["eventMs"]) < day_start + fold.DAY_MS]
            expected = reference(raw)
            for measure_id in SUM_AND_MAX:
                assert day["measures"].get(measure_id) == expected.get(measure_id), (key, measure_id)
            assert day["measures"]["summaries"] == expected["summaries"]
            assert day["measures"]["model_state_worst"] == expected["model_state_worst"]
        for key, minute_group in hours.items():
            hour = fold.fold_items(minute_group)
            hour_start = fold.hour_of_key(key)
            raw = [i for i in items if hour_start <= fold.bucket_minute("summary", i["eventMs"]) < hour_start + fold.HOUR_MS]
            expected = reference(raw)
            for measure_id in SUM_AND_MAX:
                assert hour["measures"].get(measure_id) == expected.get(measure_id), (key, measure_id)
            # The weighted p50 mean is exact too, through the recorded weights.
            assert hour["measures"]["latency_p50_ms"] == expected["latency_p50_ms"]

    def test_gaps_events_and_last_fold_up(self):
        first = fold.fold_minute([summary_item(1, fps=1.0)], [event_item("clip_ready")], gaps=2)
        first["sk"] = "M#" + fold.minute_key(MINUTE)
        second = fold.fold_minute([summary_item(2, fps=3.0)], [event_item("clip_ready"), event_item("run_error")], gaps=1)
        second["sk"] = "M#" + fold.minute_key(MINUTE + 60_000)
        hour = fold.fold_items([second, first])
        assert hour["gaps"] == 3 and hour["measures"]["gaps"] == 3
        assert hour["events"] == {"clip_ready": 2, "run_error": 1} and hour["measures"]["events_clip_ready"] == 2
        assert hour["measures"]["continuous_fps"] == 2 and hour["measures"]["continuous_paused"] is False
        assert hour["summariesFolded"] == 2 and hour["measures"]["summaries"] == 2

    def test_idempotent_and_order_independent(self):
        minutes = minute_items([summary_item(i, runs=(i, 0, 0), per_class={"x": (i, i)}) for i in range(1, 13)])
        once = fold.fold_items(minutes)
        again = fold.fold_items(list(reversed(minutes)))
        assert once["measures"] == again["measures"]
        assert fold.fold_items([once]) ["measures"]["runs_completed"] == once["measures"]["runs_completed"]


# --------------------------------------------------------------------------- gaps
class TestCountGaps:
    def test_out_of_order_arrivals_yield_no_gap(self):
        items = [summary_item(5, event_ms=MINUTE + 10_000), summary_item(7, event_ms=MINUTE + 30_000),
                 summary_item(6, event_ms=MINUTE + 20_000)]
        assert fold.count_gaps(items, MINUTE) == 0

    def test_a_real_gap_is_attributed_to_the_minute_of_the_later_member(self):
        items = [summary_item(5, event_ms=MINUTE - 10_000), summary_item(8, event_ms=MINUTE + 20_000)]
        assert fold.count_gaps(items, MINUTE) == 2
        assert fold.count_gaps(items, MINUTE - 60_000) == 0
        assert fold.count_gaps(items, MINUTE + 60_000) == 0

    def test_a_new_instance_is_not_a_gap(self):
        items = [summary_item(4000, event_ms=MINUTE + 10_000), summary_item(1, event_ms=MINUTE + 20_000, instance=OTHER_INSTANCE)]
        assert fold.count_gaps(items, MINUTE) == 0

    def test_a_retransmission_is_one_seq(self):
        items = [summary_item(5, event_ms=MINUTE + 10_000), summary_item(5, event_ms=MINUTE + 10_000, received_ms=MINUTE + 11_000),
                 summary_item(6, event_ms=MINUTE + 20_000)]
        assert fold.count_gaps(items, MINUTE) == 0

    def test_heartbeats_share_the_sequence(self):
        items = [summary_item(5, event_ms=MINUTE + 10_000), summary_item(6, event_ms=MINUTE + 20_000, idle=True),
                 summary_item(9, event_ms=MINUTE + 50_000)]
        assert fold.count_gaps(items, MINUTE) == 2

    def test_a_boundary_summary_counts_in_the_preceding_minute(self):
        items = [summary_item(1, event_ms=MINUTE + 50_000), summary_item(3, event_ms=MINUTE + 60_000)]
        assert fold.count_gaps(items, MINUTE) == 1
        assert fold.count_gaps(items, MINUTE + 60_000) == 0


# --------------------------------------------------------------------------- properties
seqs = st.lists(st.integers(min_value=1, max_value=60), min_size=0, max_size=25)


@settings(max_examples=100, deadline=None)
@given(seqs=seqs, shuffle=st.randoms(use_true_random=False))
def test_fold_minute_is_order_independent_and_duplicate_insensitive(seqs, shuffle):
    items = [summary_item(s, runs=(s, s % 3, 0), per_class={"c{0}".format(s % 4): (s, s % 5)}, waits=s % 2,
                          latency=(s * 10, s * 20, s * 30, 1 + s % 3), state=["READY", "LOADING", "UNKNOWN"][s % 3])
             for s in seqs]
    duplicated = items + [dict(i) for i in items]
    shuffled = list(duplicated)
    shuffle.shuffle(shuffled)
    assert fold.fold_minute(items, [])["measures"] == fold.fold_minute(shuffled, [])["measures"]
    assert fold.fold_minute(items, [])["summariesFolded"] == len(set(seqs))


@settings(max_examples=100, deadline=None)
@given(seqs=st.lists(st.integers(min_value=1, max_value=40), min_size=1, max_size=20, unique=True))
def test_count_gaps_equals_the_missing_seqs_when_all_arrive_in_one_minute(seqs):
    items = [summary_item(s, event_ms=MINUTE + 1 + (s % 59_000)) for s in seqs]
    expected = max(seqs) - min(seqs) + 1 - len(seqs)
    assert fold.count_gaps(items, MINUTE) == expected


@settings(max_examples=60, deadline=None)
@given(groups=st.lists(st.lists(st.integers(min_value=0, max_value=50), min_size=1, max_size=5), min_size=1, max_size=6))
def test_sums_and_maxima_fold_exactly_at_every_level(groups):
    seq = 1
    items = []
    minutes = []
    for group_index, group in enumerate(groups):
        minute_items_raw = []
        for value in group:
            item = summary_item(seq, event_ms=MINUTE + group_index * 60_000 + 10_000, runs=(value, 0, 0),
                                per_class={"k": (value, value)})
            seq += 1
            items.append(item)
            minute_items_raw.append(item)
        folded = fold.fold_minute(minute_items_raw, [])
        folded["sk"] = "M#{0}".format(fold.minute_key(MINUTE + group_index * 60_000))
        minutes.append(folded)
    hour = fold.fold_items(minutes)
    expected = fold.fold_minute(items, [])["measures"]
    assert hour["measures"]["runs_completed"] == expected["runs_completed"]
    assert hour["measures"]["detections_class_total"] == expected["detections_class_total"]
    assert hour["measures"]["detections_class_max"] == expected["detections_class_max"]
    assert hour["measures"]["summaries"] == expected["summaries"]
