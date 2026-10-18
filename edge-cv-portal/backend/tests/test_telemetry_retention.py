"""``telemetry.retention`` (workflow-debug-mode tasks.md 3.3;
workflow-telemetry-dashboards design §12.4): foreign rules kept verbatim, the
three ids replaced in place, absent ones appended, idempotent, a bucket
already carrying the other two ids gets the third appended and the others
replaced, ``debug_images_rule_present``, and the dashboards' "Media retention"
readiness ``media_retention_ready`` (dashboards tasks.md 2.2: missing rules,
``Enabled``/``Suspended``/never-enabled versioning, a clip-days mismatch is
reported but ready, the debug id is ignored).
"""
import copy

import pytest

from telemetry import retention

FOREIGN_TUNING = {
    "ID": "DDAWorkflowTuningJobs", "Filter": {"Prefix": "workflow-tuning/jobs/"},
    "Status": "Enabled", "Expiration": {"Days": 30},
    "NoncurrentVersionExpiration": {"NoncurrentDays": 1},
}
FOREIGN_OPERATOR = {
    "ID": "operator-archive", "Filter": {"And": {"Prefix": "archive/", "Tags": [{"Key": "k", "Value": "v"}]}},
    "Status": "Disabled", "Transitions": [{"Days": 30, "StorageClass": "GLACIER"}],
}


def ids(rules):
    return [rule["ID"] for rule in rules]


class TestTelemetryRules:
    def test_the_three_rules_as_the_designs_write_them(self):
        rules = retention.telemetry_rules(clip_days=90, debug_days=7)
        assert rules == [
            {"ID": "DDATelemetryClips", "Filter": {"Prefix": "dda-telemetry/clips/"}, "Status": "Enabled",
             "Expiration": {"Days": 90}, "NoncurrentVersionExpiration": {"NoncurrentDays": 1}},
            {"ID": "DDATelemetryLive", "Filter": {"Prefix": "dda-telemetry/live/"}, "Status": "Enabled",
             "Expiration": {"Days": 1}, "NoncurrentVersionExpiration": {"NoncurrentDays": 1}},
            {"ID": "DDATelemetryDebugImages", "Filter": {"Prefix": "dda-telemetry/debug/"}, "Status": "Enabled",
             "Expiration": {"Days": 7}, "NoncurrentVersionExpiration": {"NoncurrentDays": 1}},
        ]
        assert retention.TELEMETRY_RULE_IDS == ("DDATelemetryClips", "DDATelemetryLive", "DDATelemetryDebugImages")

    def test_defaults(self):
        rules = retention.telemetry_rules()
        assert rules[0]["Expiration"] == {"Days": 90}
        assert rules[2]["Expiration"] == {"Days": 7}

    @pytest.mark.parametrize("debug_days", [0, 31, -1, "7"])
    def test_debug_days_range(self, debug_days):
        with pytest.raises(ValueError):
            retention.telemetry_rules(debug_days=debug_days)

    @pytest.mark.parametrize("debug_days", [1, 30])
    def test_debug_days_bounds_are_inclusive(self, debug_days):
        assert retention.telemetry_rules(debug_days=debug_days)[2]["Expiration"] == {"Days": debug_days}

    def test_clip_days_must_be_positive(self):
        with pytest.raises(ValueError):
            retention.telemetry_rules(clip_days=0)


class TestMerge:
    def test_empty_bucket_gets_the_three_rules(self):
        merged, changed = retention.merge_telemetry_rules([], clip_days=90, debug_days=7)
        assert changed is True
        assert merged == retention.telemetry_rules(clip_days=90, debug_days=7)
        assert retention.merge_telemetry_rules(None, clip_days=90, debug_days=7) == (merged, True)

    def test_foreign_rules_kept_verbatim_and_in_order(self):
        existing = [copy.deepcopy(FOREIGN_TUNING), copy.deepcopy(FOREIGN_OPERATOR)]
        merged, changed = retention.merge_telemetry_rules(existing, clip_days=90, debug_days=7)
        assert changed is True
        assert merged[0] == FOREIGN_TUNING
        assert merged[1] == FOREIGN_OPERATOR
        assert ids(merged) == ["DDAWorkflowTuningJobs", "operator-archive",
                               "DDATelemetryClips", "DDATelemetryLive", "DDATelemetryDebugImages"]

    def test_the_three_ids_are_replaced_in_place(self):
        stale = retention.telemetry_rules(clip_days=30, debug_days=3)
        existing = [stale[2], copy.deepcopy(FOREIGN_TUNING), stale[0], stale[1]]
        merged, changed = retention.merge_telemetry_rules(existing, clip_days=90, debug_days=7)
        assert changed is True
        assert ids(merged) == ["DDATelemetryDebugImages", "DDAWorkflowTuningJobs", "DDATelemetryClips", "DDATelemetryLive"]
        assert merged[0]["Expiration"] == {"Days": 7}
        assert merged[2]["Expiration"] == {"Days": 90}
        assert merged[1] == FOREIGN_TUNING

    def test_idempotent(self):
        existing = [copy.deepcopy(FOREIGN_OPERATOR)]
        merged, changed = retention.merge_telemetry_rules(existing, clip_days=90, debug_days=7)
        assert changed is True
        again, changed_again = retention.merge_telemetry_rules(merged, clip_days=90, debug_days=7)
        assert changed_again is False
        assert again == merged

    def test_bucket_with_the_other_two_ids_gets_the_third_appended_and_the_others_replaced(self):
        """The dashboards landed first with clip days 60: the debug mode's
        landing appends DDATelemetryDebugImages and brings the other two to
        the requested values."""
        dashboards_first = retention.telemetry_rules(clip_days=60, debug_days=7)[:2]
        existing = [copy.deepcopy(FOREIGN_TUNING)] + dashboards_first
        merged, changed = retention.merge_telemetry_rules(existing, clip_days=90, debug_days=5)
        assert changed is True
        assert ids(merged) == ["DDAWorkflowTuningJobs", "DDATelemetryClips", "DDATelemetryLive", "DDATelemetryDebugImages"]
        assert merged[1]["Expiration"] == {"Days": 90}
        assert merged[2]["Expiration"] == {"Days": 1}
        assert merged[3] == retention.telemetry_rules(debug_days=5)[2]
        assert sum(1 for rule in merged if rule["ID"] == "DDATelemetryDebugImages") == 1

    def test_debug_first_then_the_same_values_is_unchanged(self):
        debug_first = retention.telemetry_rules(clip_days=90, debug_days=7)
        merged, changed = retention.merge_telemetry_rules(debug_first, clip_days=90, debug_days=7)
        assert changed is False
        assert merged == debug_first

    def test_duplicates_of_our_ids_collapse_to_one(self):
        rule = retention.telemetry_rules()[0]
        merged, changed = retention.merge_telemetry_rules([rule, dict(rule), copy.deepcopy(FOREIGN_TUNING)])
        assert changed is True
        assert ids(merged) == ["DDATelemetryClips", "DDAWorkflowTuningJobs", "DDATelemetryLive", "DDATelemetryDebugImages"]

    def test_non_dict_entries_are_kept_verbatim(self):
        merged, _changed = retention.merge_telemetry_rules(["opaque"])
        assert merged[0] == "opaque"


class TestDebugImagesRulePresent:
    def test_present_when_the_rule_is_enabled_on_the_prefix(self):
        assert retention.debug_images_rule_present(retention.telemetry_rules()) is True
        assert retention.debug_images_rule_present([retention.telemetry_rules()[2]]) is True

    def test_absent_without_the_id(self):
        assert retention.debug_images_rule_present([]) is False
        assert retention.debug_images_rule_present(None) is False
        assert retention.debug_images_rule_present(retention.telemetry_rules()[:2]) is False
        assert retention.debug_images_rule_present([FOREIGN_TUNING]) is False

    def test_disabled_or_misprefixed_rule_does_not_count(self):
        rule = copy.deepcopy(retention.telemetry_rules()[2])
        rule["Status"] = "Disabled"
        assert retention.debug_images_rule_present([rule]) is False
        rule = copy.deepcopy(retention.telemetry_rules()[2])
        rule["Filter"] = {"Prefix": "dda-telemetry/clips/"}
        assert retention.debug_images_rule_present([rule]) is False
        rule = copy.deepcopy(retention.telemetry_rules()[2])
        del rule["Expiration"]
        assert retention.debug_images_rule_present([rule]) is False

    def test_the_legacy_top_level_prefix_shape_counts(self):
        rule = {"ID": "DDATelemetryDebugImages", "Prefix": "dda-telemetry/debug/", "Status": "Enabled",
                "Expiration": {"Days": 7}}
        assert retention.debug_images_rule_present([rule]) is True
        rule = {"ID": "DDATelemetryDebugImages", "Filter": {"And": {"Prefix": "dda-telemetry/debug/", "Tags": []}},
                "Status": "Enabled", "Expiration": {"Days": 7}}
        assert retention.debug_images_rule_present([rule]) is True


class TestMediaRetentionReady:
    def test_ready_with_both_rules_and_no_versioning(self):
        rules = retention.telemetry_rules(clip_days=90)
        assert retention.media_retention_ready(rules, None) == (True, None)
        assert retention.media_retention_ready(rules, "") == (True, None)
        assert retention.media_retention_ready(rules, "Suspended") == (True, None)
        assert retention.media_retention_ready(rules[:2], None) == (True, None)

    def test_missing_rules(self):
        rules = retention.telemetry_rules()
        assert retention.media_retention_ready([], None) == (False, "media_retention_unset")
        assert retention.media_retention_ready(None, None) == (False, "media_retention_unset")
        assert retention.media_retention_ready([rules[0]], None) == (False, "media_retention_unset")
        assert retention.media_retention_ready([rules[1]], None) == (False, "media_retention_unset")
        assert retention.media_retention_ready([FOREIGN_TUNING, rules[2]], None) == (False, "media_retention_unset")

    def test_enabled_versioning_refuses_even_with_the_rules(self):
        rules = retention.telemetry_rules()
        assert retention.media_retention_ready(rules, "Enabled") == (False, "media_versioned")
        # The rules are checked first: an unset bucket with versioning reports the rules.
        assert retention.media_retention_ready([], "Enabled") == (False, "media_retention_unset")

    def test_disabled_or_misprefixed_rules_do_not_count(self):
        rules = retention.telemetry_rules()
        clips = copy.deepcopy(rules[0])
        clips["Status"] = "Disabled"
        assert retention.media_retention_ready([clips, rules[1]], None) == (False, "media_retention_unset")
        live = copy.deepcopy(rules[1])
        live["Filter"] = {"Prefix": "dda-telemetry/clips/"}
        assert retention.media_retention_ready([rules[0], live], None) == (False, "media_retention_unset")
        live = copy.deepcopy(rules[1])
        del live["Expiration"]
        assert retention.media_retention_ready([rules[0], live], None) == (False, "media_retention_unset")

    def test_clip_days_mismatch_is_reported_but_ready(self):
        rules = retention.telemetry_rules(clip_days=30)
        assert retention.media_retention_ready(rules, None, clip_days=90) == (True, None)
        assert retention.clip_rule_days(rules) == 30
        assert retention.clip_rule_days(retention.telemetry_rules(clip_days=90)) == 90
        assert retention.clip_rule_days([]) is None
        assert retention.clip_rule_days(None) is None

    def test_the_debug_id_is_ignored(self):
        rules = retention.telemetry_rules()
        debug = copy.deepcopy(rules[2])
        debug["Status"] = "Disabled"
        assert retention.media_retention_ready([rules[0], rules[1], debug], None) == (True, None)
        assert retention.media_retention_ready([rules[0], rules[1]], "Suspended") == (True, None)

    def test_legacy_prefix_shapes_count(self):
        clips = {"ID": "DDATelemetryClips", "Prefix": "dda-telemetry/clips/", "Status": "Enabled",
                 "Expiration": {"Days": 90}}
        live = {"ID": "DDATelemetryLive", "Filter": {"And": {"Prefix": "dda-telemetry/live/", "Tags": []}},
                "Status": "Enabled", "Expiration": {"Days": 1}}
        assert retention.media_retention_ready([clips, live], None) == (True, None)
