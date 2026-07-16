"""
Tests for the adjust-revision stale-read fix
(.kiro/specs/adjust-revision-stale-read-fix), covering BOTH wave-1
task groups:

- Task 1 — bug condition exploration (Property 1): the adjustment
  paths write the record's revision mapping (arch_revisions / fetches)
  with an update_item and then auto-start the queued builds in the
  SAME invocation via plugin_builds.start_queued_builds. That
  auto-start re-reads the record through
  plugin_records.get_version_item, which issues a plain
  (eventually-consistent) get_item. A stale read returns the pre-write
  item, arch_source_prefix silently falls back to the flat
  source_s3_prefix, and the CodeBuild sourceLocationOverride names the
  WRONG source tree (bugfix.md 1.1/1.2).

- Task 2 — preservation properties (Property 2): everything the fix
  must NOT change — the default get_version_item read call shape
  (3.5), start_queued_builds' idempotency/never-raise semantics (3.3),
  arch_source_prefix resolution for mapped/unmapped/flat records
  (3.1, 3.2), manual retries (3.4), and adjustment fetch-failure
  isolation (3.6). These PASS on unfixed code, capturing the baseline.

The stale read is simulated deterministically (design "Exploratory Bug
Condition Checking"): plugin_records.plugin_table is swapped for a proxy
whose get_item returns a frozen pre-write snapshot UNLESS
ConsistentRead=True is passed. Writes pass through to the real (moto)
table — the write itself is correct; only the read timing is wrong.

EXPECTED OUTCOME ON UNFIXED CODE: every Task 1 test except the
retry-masking edge case FAILS (the failures are the counterexamples
that confirm the root cause; they become the fix-validation tests once
task 3.1 lands), while every Task 2 preservation test PASSES.

Feature: adjust-revision-stale-read-fix, Property 1: Auto-Started
Builds Resolve the Post-Write Source Tree
Feature: adjust-revision-stale-read-fix, Property 2: All Other Reads
and Auto-Start Semantics Are Unchanged
"""
import contextlib
import copy
import json
import uuid

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from conftest import TEST_ENV
from test_plugin_importer import (ImporterEnv, MESON_PLUGIN,
                                  RecordingCodeBuild, REPO_URL)

BUCKET = TEST_ENV["PORTAL_ARTIFACTS_BUCKET"]
FETCH_ARN_PREFIX = ImporterEnv.FETCH_BUILD_ARN_PREFIX
ALL_ARCHS = ["x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6"]

# Task 2 (preservation): the configured per-arch CodeBuild projects and
# an architecture no project is configured for (3.3).
CONFIGURED_ARCHS = sorted(json.loads(TEST_ENV["BUILD_PROJECTS_JSON"]))
UNCONFIGURED_ARCH = "mips64"


# =====================================================================
# Deterministic stale-read stub (isBugCondition from the design):
# get_item returns the pre-write item unless ConsistentRead=True.
# =====================================================================

class StaleReadTable:
    """Proxy over the real (moto) plugin table.

    ``freeze`` captures the CURRENT item as the replica's lagging state.
    Plain ``get_item`` calls on a frozen key return that snapshot
    forever (the eventually-consistent read landing on a stale replica);
    ``ConsistentRead=True`` reads — and every write — pass through to
    the real table. All get_item kwargs are recorded for the
    ConsistentRead assertion.
    """

    def __init__(self, real_table):
        self._real = real_table
        self._stale = {}
        self.get_item_calls = []

    def freeze(self, plugin_id, version=1):
        raw = self._real.get_item(
            Key={"plugin_id": plugin_id, "version": version},
            ConsistentRead=True).get("Item")
        self._stale[(plugin_id, version)] = copy.deepcopy(raw)

    def get_item(self, **kwargs):
        self.get_item_calls.append(copy.deepcopy(kwargs))
        if kwargs.get("ConsistentRead"):
            return self._real.get_item(**kwargs)
        key = kwargs.get("Key") or {}
        frozen = (key.get("plugin_id"), key.get("version"))
        if frozen in self._stale:
            item = self._stale[frozen]
            return {"Item": copy.deepcopy(item)} if item is not None else {}
        return self._real.get_item(**kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


@contextlib.contextmanager
def stale_read_stub(stack, plugin_id, version=1):
    """Swap plugin_records.plugin_table for the stale-read proxy.

    plugin_records.get_version_item resolves plugin_table through the
    plugin_records module globals at call time, so this intercepts the
    auto-start's re-read no matter which module calls it. plugin_importer
    and plugin_builds bound the ORIGINAL plugin_table function at import
    time, so their direct update_item writes keep hitting the real table
    (the write is correct; only the re-read goes stale).
    """
    records = stack.plugin_records
    stub = StaleReadTable(records.plugin_table())
    stub.freeze(plugin_id, version)
    original = records.plugin_table
    records.plugin_table = lambda: stub
    try:
        yield stub
    finally:
        records.plugin_table = original


@contextlib.contextmanager
def recording_codebuild(stack):
    """Capture plugin_builds' StartBuild submissions."""
    builds = stack.plugin_builds
    recorder = RecordingCodeBuild(builds.codebuild)
    original = builds.codebuild
    builds.codebuild = recorder
    try:
        yield recorder
    finally:
        builds.codebuild = original


# =====================================================================
# Record / event helpers
# =====================================================================

def new_record_ids(stack):
    """Fresh usecase + plugin ids and the record's flat source prefix."""
    usecase_id = f"uc-{uuid.uuid4()}"
    stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "Stale Read Exploration Use Case",
        "account_id": "123456789012",
    })
    plugin_id = f"plugin-{uuid.uuid4()}"
    base = f"plugin-sources/{usecase_id}/{plugin_id}/1/"
    return usecase_id, plugin_id, base


def put_imported_record(stack, usecase_id, plugin_id, *, artifacts,
                        requested, fetches=None, arch_revisions=None):
    """Persist a settled repository-import Plugin_Record directly."""
    base = f"plugin-sources/{usecase_id}/{plugin_id}/1/"
    item = {
        "plugin_id": plugin_id,
        "version": 1,
        "usecase_id": usecase_id,
        "name": "gst-plugins-good",
        "kind": "imported",
        "import_status": "imported",
        "source_s3_prefix": base,
        "requested_architectures": list(requested),
        "artifacts": artifacts,
        "provenance": {"repoUrl": REPO_URL, "revision": "default",
                       "importedBy": "user-import", "importedAt": 1,
                       "classification": "good"},
        "created_by": "user-import",
        "created_at": 1,
        "updated_at": 1,
    }
    if fetches is not None:
        item["fetches"] = fetches
    if arch_revisions is not None:
        item["arch_revisions"] = arch_revisions
    stack.tables.plugin_records.put_item(Item=item)
    return item


def fresh_item(stack, plugin_id, version=1):
    """Read the REAL table state (bypasses the stale-read stub)."""
    return stack.tables.plugin_records.get_item(
        Key={"plugin_id": plugin_id, "version": version},
        ConsistentRead=True).get("Item")


def make_admin(stack, usecase_id):
    user_id = f"user-{uuid.uuid4()}"
    stack.tables.user_roles.put_item(Item={
        "user_id": user_id, "usecase_id": usecase_id,
        "role": "UseCaseAdmin",
    })
    return {"user_id": user_id, "email": f"{user_id}@example.com",
            "username": user_id, "role": "Viewer"}


def api_event(method, resource, user, plugin_id, version, body):
    return {
        "httpMethod": method,
        "resource": resource,
        "path": resource.replace("{id}", plugin_id).replace("{v}",
                                                            str(version)),
        "pathParameters": {"id": plugin_id, "v": str(version)},
        "queryStringParameters": None,
        "body": json.dumps(body),
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": user["user_id"],
                    "email": user["email"],
                    "cognito:username": user["username"],
                    "custom:role": user["role"],
                }
            }
        },
    }


def adjustment_fetch_event(item, build_id, slug, status="SUCCEEDED"):
    """EventBridge CodeBuild fetch result with REVISION_SLUG attribution."""
    return {
        "source": "aws.codebuild",
        "detail-type": "CodeBuild Build State Change",
        "detail": {
            "build-status": status,
            "project-name": TEST_ENV["FETCH_PROJECT_NAME"],
            "build-id": FETCH_ARN_PREFIX + build_id,
            "additional-information": {
                "environment": {
                    "environment-variables": [
                        {"name": "USECASE_ID", "value": item["usecase_id"]},
                        {"name": "PLUGIN_ID", "value": item["plugin_id"]},
                        {"name": "PLUGIN_VERSION",
                         "value": str(item["version"])},
                        {"name": "REVISION_SLUG", "value": slug},
                    ],
                },
            },
        },
    }


def arch_starts(recorder, arch):
    return [call for call in recorder.calls
            if call["projectName"] == f"dda-plugin-build-{arch}"]


# =====================================================================
# Bug condition exploration (EXPECTED TO FAIL on unfixed code)
# =====================================================================

class TestAdjustRevisionStaleRead:
    """Counterexample hunt for bugfix.md 1.1 / 1.2 / 1.3."""

    def test_fetch_success_stale_read_builds_from_adjusted_tree(
            self, aws_stack):
        """Bug condition 1.1: an adjustment fetch succeeds,
        _handle_adjustment_fetch_result writes arch_revisions[arch] and
        auto-starts the queued build — the submission MUST name the
        adjusted rev-{slug}/ tree even when the re-read is stale.

        Unfixed code submits the flat source_s3_prefix (the original
        revision's tree).
        """
        arch, slug, revision = "arm64_jp5", "1.16", "1.16"
        build_id = "dda-plugin-fetch:adjust-fetch-1"
        usecase_id, plugin_id, base = new_record_ids(aws_stack)
        # State after adjust_revision's ADJUST_FETCH write: the arch is
        # re-queued and the revision's fetch is in flight with the
        # pending_archs marker. arch_revisions is NOT written yet.
        item = put_imported_record(
            aws_stack, usecase_id, plugin_id,
            requested=[arch, "x86_64"],
            artifacts={arch: {"buildStatus": "queued"},
                       "x86_64": {"buildStatus": "succeeded"}},
            fetches={slug: {"revision": revision,
                            "source_prefix": f"{base}rev-{slug}/",
                            "status": "fetching",
                            "pending_archs": [arch],
                            "fetch_build_id": build_id}},
        )

        with stale_read_stub(aws_stack, item["plugin_id"]), \
                recording_codebuild(aws_stack) as recorder:
            result = aws_stack.plugin_builds.handler(
                adjustment_fetch_event(item, build_id, slug), None)

        assert result == {"recorded": True, "revision_slug": slug,
                          "fetch_status": "succeeded",
                          "import_status": "imported"}
        # The WRITE landed (root cause is not a write problem): the real
        # table maps the arch to the adjusted revision's slug.
        record = fresh_item(aws_stack, item["plugin_id"])
        assert record["arch_revisions"] == {arch: slug}
        assert record["fetches"][slug]["status"] == "succeeded"

        # ...but what did the auto-started build actually compile?
        starts = arch_starts(recorder, arch)
        assert len(starts) == 1, (
            f"expected exactly one auto-started {arch} build, "
            f"got {len(starts)}")
        assert starts[0]["sourceLocationOverride"] == \
            f"{BUCKET}/{base}rev-{slug}/", (
                "auto-started build must compile the adjusted revision's "
                "tree (bugfix 2.1); unfixed code submits the flat "
                "source_s3_prefix — the original revision's tree")
        # Task 4 (end-to-end): the arch entry advanced queued ->
        # building with the started build id persisted.
        record = fresh_item(aws_stack, item["plugin_id"])
        assert record["artifacts"][arch]["buildStatus"] == "building"
        assert record["artifacts"][arch]["buildId"]

    def test_reuse_path_stale_read_builds_from_adjusted_tree(
            self, aws_stack):
        """Bug condition 1.2: adjust_revision ADJUST_REUSE maps the arch
        to an already-fetched revision, re-queues it, and auto-starts —
        the submission MUST name the reused entry's rev-{slug}/ tree.

        Unfixed code hands the stale (pre-write) item to the auto-start:
        with the arch already queued pre-write, the build is submitted
        from the flat source_s3_prefix (no mapping visible); had the
        arch been 'failed' pre-write, the stale item would show nothing
        queued and no build would start at all. Both are the same stale
        re-read.
        """
        arch, slug, revision = "arm64_jp4", "1.16", "1.16"
        usecase_id, plugin_id, base = new_record_ids(aws_stack)
        # An earlier adjustment already synced revision 1.16's tree
        # (fetches entry 'succeeded'); this platform's entry is queued.
        item = put_imported_record(
            aws_stack, usecase_id, plugin_id,
            requested=[arch, "x86_64"],
            artifacts={arch: {"buildStatus": "queued"},
                       "x86_64": {"buildStatus": "succeeded"}},
            fetches={slug: {"revision": revision,
                            "source_prefix": f"{base}rev-{slug}/",
                            "status": "succeeded"}},
        )
        admin = make_admin(aws_stack, item["usecase_id"])

        with stale_read_stub(aws_stack, item["plugin_id"]), \
                recording_codebuild(aws_stack) as recorder:
            response = aws_stack.plugin_importer.handler(api_event(
                "POST", "/plugins/{id}/versions/{v}/adjust-revision",
                admin, item["plugin_id"], 1,
                {"architecture": arch, "revision": revision}), None)

        assert response["statusCode"] == 202, response["body"]
        # The WRITE landed: the real table carries the reuse mapping.
        record = fresh_item(aws_stack, item["plugin_id"])
        assert record["arch_revisions"] == {arch: slug}

        starts = arch_starts(recorder, arch)
        assert len(starts) == 1, (
            f"expected exactly one auto-started {arch} build, "
            f"got {len(starts)}")
        assert starts[0]["sourceLocationOverride"] == \
            f"{BUCKET}/{base}rev-{slug}/", (
                "reuse-path auto-start must compile the reused entry's "
                "tree (bugfix 2.2); unfixed code submits the flat "
                "source_s3_prefix")
        # Task 4 (end-to-end): the re-queued arch entry advanced
        # queued -> building with the started build id persisted.
        record = fresh_item(aws_stack, item["plugin_id"])
        assert record["artifacts"][arch]["buildStatus"] == "building"
        assert record["artifacts"][arch]["buildId"]

    def test_start_queued_builds_rereads_with_consistent_read(
            self, aws_stack):
        """Bug condition 2.3 mechanism: start_queued_builds' re-read of
        the record must pass ConsistentRead=True to get_item.

        Unfixed code never sets the key (get_version_item has no way to
        request it), which IS the stale-read window.
        """
        arch = "arm64_jp6"
        usecase_id, plugin_id, _ = new_record_ids(aws_stack)
        item = put_imported_record(
            aws_stack, usecase_id, plugin_id,
            requested=[arch],
            artifacts={arch: {"buildStatus": "queued"}},
        )

        with stale_read_stub(aws_stack, item["plugin_id"]) as stub, \
                recording_codebuild(aws_stack):
            aws_stack.plugin_builds.start_queued_builds(
                item["plugin_id"], 1)

        reads = [call for call in stub.get_item_calls
                 if (call.get("Key") or {}).get("plugin_id")
                 == item["plugin_id"]]
        assert reads, "start_queued_builds never re-read the record"
        assert any(call.get("ConsistentRead") is True for call in reads), (
            "start_queued_builds' re-read must request ConsistentRead=True "
            "(read-your-own-write after the same-invocation adjustment "
            f"update); observed get_item calls: {reads}")

    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow,
                                     HealthCheck.filter_too_much])
    @given(data=st.data())
    def test_property_1_auto_started_builds_resolve_post_write_tree(
            self, aws_stack, data):
        """Feature: adjust-revision-stale-read-fix, Property 1:
        Auto-Started Builds Resolve the Post-Write Source Tree

        For ANY record x adjusted arch x slug where a same-invocation
        revision-mapping write is followed by the auto-start whose
        eventually-consistent re-read returns the pre-write item
        (isBugCondition true), the auto-started submission's
        sourceLocationOverride SHALL name the just-written
        fetches[arch_revisions[arch]].source_prefix.

        **Validates: Requirements 2.1, 2.2, 2.3**
        """
        arch = data.draw(st.sampled_from(ALL_ARCHS), label="adjusted arch")
        slug = data.draw(st.from_regex(r"[a-z0-9][a-z0-9.\-]{0,10}",
                                       fullmatch=True),
                         label="revision slug")
        others = [a for a in ALL_ARCHS if a != arch]
        extra = data.draw(st.dictionaries(
            st.sampled_from(others),
            st.sampled_from(["building", "succeeded", "failed"]),
            max_size=2), label="other archs")

        artifacts = {arch: {"buildStatus": "queued"}}
        artifacts.update({a: {"buildStatus": s} for a, s in extra.items()})
        usecase_id, plugin_id, base = new_record_ids(aws_stack)
        item = put_imported_record(
            aws_stack, usecase_id, plugin_id,
            requested=[arch, *sorted(extra)],
            artifacts=artifacts,
        )

        with stale_read_stub(aws_stack, item["plugin_id"]) as stub, \
                recording_codebuild(aws_stack) as recorder:
            # The adjustment write (both race sites share this shape):
            # arch_revisions[arch] -> slug with the slug's fetches entry
            # settled — hits the REAL table while the frozen snapshot
            # keeps serving plain reads (the lagging replica).
            aws_stack.tables.plugin_records.update_item(
                Key={"plugin_id": item["plugin_id"], "version": 1},
                UpdateExpression="SET arch_revisions = :ar, fetches = :f",
                ExpressionAttributeValues={
                    ":ar": {arch: slug},
                    ":f": {slug: {"revision": slug,
                                  "source_prefix": f"{base}rev-{slug}/",
                                  "status": "succeeded"}},
                },
            )
            # Same-invocation auto-start immediately after the write.
            aws_stack.plugin_builds.start_queued_builds(
                item["plugin_id"], 1)

        starts = arch_starts(recorder, arch)
        assert len(starts) == 1, (
            f"expected exactly one auto-started {arch} build, "
            f"got {len(starts)}")
        assert starts[0]["sourceLocationOverride"] == \
            f"{BUCKET}/{base}rev-{slug}/", (
                "the auto-started submission must reflect the post-write "
                f"mapping arch_revisions[{arch}]={slug}; unfixed code "
                "resolves the stale item's flat source_s3_prefix")


# =====================================================================
# Edge case: manual retry masks the defect (bugfix 1.3)
# EXPECTED TO PASS on unfixed code — documents the masking behavior.
# =====================================================================

class TestRetryMasksStaleRead:
    def test_manual_retry_after_stale_start_builds_adjusted_tree(
            self, aws_stack):
        """After the stale-read auto-start, a manual retry (POST
        .../build) reads FRESH state and resolves the adjusted
        revision's tree — the first failure looks transient (1.3).
        """
        arch, slug, revision = "arm64_jp5", "1.16", "1.16"
        build_id = "dda-plugin-fetch:adjust-retry-1"
        usecase_id, plugin_id, base = new_record_ids(aws_stack)
        item = put_imported_record(
            aws_stack, usecase_id, plugin_id,
            requested=[arch],
            artifacts={arch: {"buildStatus": "queued"}},
            fetches={slug: {"revision": revision,
                            "source_prefix": f"{base}rev-{slug}/",
                            "status": "fetching",
                            "pending_archs": [arch],
                            "fetch_build_id": build_id}},
        )
        admin = make_admin(aws_stack, item["usecase_id"])

        # First round: adjustment fetch settles and auto-starts under
        # the stale-read window (the field failure).
        with stale_read_stub(aws_stack, item["plugin_id"]), \
                recording_codebuild(aws_stack):
            aws_stack.plugin_builds.handler(
                adjustment_fetch_event(item, build_id, slug), None)

        # Manual retry seconds later: the stub is gone, reads reflect
        # the write, and the build compiles the adjusted tree.
        with recording_codebuild(aws_stack) as retry_recorder:
            response = aws_stack.plugin_builds.handler(api_event(
                "POST", "/plugins/{id}/versions/{v}/build",
                admin, item["plugin_id"], 1,
                {"architectures": [arch]}), None)

        assert response["statusCode"] == 202, response["body"]
        starts = arch_starts(retry_recorder, arch)
        assert len(starts) == 1
        assert starts[0]["sourceLocationOverride"] == \
            f"{BUCKET}/{base}rev-{slug}/", (
                "a manual retry with fresh reads must resolve the "
                "adjusted revision's tree (this masking is why the "
                "defect looks like a flaky build)")


# =====================================================================
# Task 2: preservation properties (EXPECTED TO PASS on unfixed code)
#
# Feature: adjust-revision-stale-read-fix, Property 2: All Other Reads
# and Auto-Start Semantics Are Unchanged
#
# Observation-first methodology: the assertions below transcribe the
# behavior OBSERVED on unfixed code for non-bug-condition inputs (the
# design's Preservation Requirements). They must keep passing,
# unchanged, once task 3.1 lands.
# =====================================================================

class PreservationRecordingTable:
    """Proxy over the real (moto) plugin table that records every
    get_item's kwargs and passes all calls through unchanged — used to
    assert the default read's CALL SHAPE (no ConsistentRead key, 3.5).
    """

    def __init__(self, real_table):
        self._real = real_table
        self.get_item_calls = []

    def get_item(self, **kwargs):
        self.get_item_calls.append(copy.deepcopy(kwargs))
        return self._real.get_item(**kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


class PreservationRecordingCodeBuild:
    """CodeBuild stub that records StartBuild calls and fabricates
    build ids (no real moto submission — fast enough for 100-iteration
    property runs)."""

    def __init__(self):
        self.calls = []
        self._counter = 0

    def start_build(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        self._counter += 1
        return {"build": {
            "id": f"{kwargs['projectName']}:preservation-{self._counter}"}}


class PreservationFailingCodeBuild:
    """CodeBuild stub whose StartBuild always fails — the failure must
    be recorded on the arch entry WITHOUT raising to the caller (3.3).
    """

    MESSAGE = "StartBuild throttled"

    def start_build(self, **kwargs):
        raise RuntimeError(self.MESSAGE)


@contextlib.contextmanager
def _swapped(module, attribute, value):
    """Temporarily replace one module attribute."""
    original = getattr(module, attribute)
    setattr(module, attribute, value)
    try:
        yield value
    finally:
        setattr(module, attribute, original)


def preservation_slug_st():
    """Revision slugs shaped like the adjustment paths produce them."""
    return st.from_regex(r"[a-z0-9][a-z0-9.\-]{0,10}", fullmatch=True)


def preservation_artifact_entry_st():
    """One per-arch artifact entry across the statuses auto-start can
    encounter: only 'queued' entries may be started; the rest must be
    left byte-identical (3.3)."""
    return st.sampled_from(
        ["queued", "building", "succeeded", "failed"]
    ).map(lambda status: {"buildStatus": status})


@st.composite
def preservation_record_items(draw):
    """Record shapes across the non-bug-condition input domain: arch
    subsets, artifact statuses, an optionally unconfigured arch, and
    flat / partially mapped / fully mapped revision layouts (including
    a dangling arch_revisions mapping whose fetches entry is missing —
    which resolves through the flat fallback exactly like an unmapped
    arch)."""
    usecase_id = f"uc-{draw(st.uuids())}"
    plugin_id = f"plugin-{draw(st.uuids())}"
    base = f"plugin-sources/{usecase_id}/{plugin_id}/1/"
    archs = draw(st.lists(st.sampled_from(CONFIGURED_ARCHS),
                          min_size=1, max_size=3, unique=True))
    if draw(st.booleans()):
        archs = archs + [UNCONFIGURED_ARCH]
    artifacts = {arch: draw(preservation_artifact_entry_st())
                 for arch in archs}
    item = {
        "plugin_id": plugin_id,
        "version": 1,
        "usecase_id": usecase_id,
        "name": "gst-plugins-good",
        "kind": "imported",
        "import_status": "imported",
        "source_s3_prefix": base,
        "requested_architectures": list(archs),
        "artifacts": artifacts,
        "provenance": {"repoUrl": REPO_URL, "revision": "default",
                       "importedBy": "user-import", "importedAt": 1,
                       "classification": "good"},
        "created_by": "user-import",
        "created_at": 1,
        "updated_at": 1,
    }
    if draw(st.booleans()):
        mapped = draw(st.lists(st.sampled_from(archs), unique=True))
        arch_revisions = {arch: draw(preservation_slug_st())
                          for arch in mapped}
        fetches = {slug: {"revision": slug,
                          "source_prefix": f"{base}rev-{slug}/",
                          "status": "succeeded"}
                   for slug in set(arch_revisions.values())}
        # Sometimes drop one fetch entry: the dangling mapping falls
        # back to the flat prefix, matching the unmapped case (3.1).
        if fetches and draw(st.booleans()):
            fetches.pop(draw(st.sampled_from(sorted(fetches))))
        item["arch_revisions"] = arch_revisions
        item["fetches"] = fetches
    return item


def preservation_seed_record(stack, item):
    """Persist a generated record shape into the real (moto) table."""
    stack.tables.plugin_records.put_item(Item=item)
    return item


def preservation_expected_prefix(item, arch):
    """Baseline source-resolution oracle, transcribed from the behavior
    observed on unfixed code (3.1, 3.2): per-arch mapping when
    arch_revisions[arch] -> fetches[slug].source_prefix resolves,
    otherwise the flat source_s3_prefix."""
    slug = (item.get("arch_revisions") or {}).get(arch)
    fetch = (item.get("fetches") or {}).get(slug) if slug else None
    prefix = (fetch or {}).get("source_prefix")
    return prefix or item.get("source_s3_prefix") or ""


class TestPreservationDefaultReadCallShape:
    """Requirement 3.5: get_version_item's default read is unchanged —
    the get_item call carries NO ConsistentRead key at all."""

    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow,
                                     HealthCheck.filter_too_much])
    @given(item=preservation_record_items())
    def test_property_2_default_read_has_no_consistent_read_key(
            self, aws_stack, item):
        """Feature: adjust-revision-stale-read-fix, Property 2: All
        Other Reads and Auto-Start Semantics Are Unchanged

        For ANY record, get_version_item(plugin_id, version) without
        the parameter issues a get_item whose kwargs carry ONLY the Key
        (no ConsistentRead — the call is byte-identical to today's) and
        returns the decoded item.

        **Validates: Requirements 3.5**
        """
        preservation_seed_record(aws_stack, item)
        records = aws_stack.plugin_records
        recorder = PreservationRecordingTable(records.plugin_table())
        with _swapped(records, "plugin_table", lambda: recorder):
            got = records.get_version_item(item["plugin_id"], 1)

        (call,) = recorder.get_item_calls
        assert "ConsistentRead" not in call, (
            "the default get_version_item read must not carry a "
            f"ConsistentRead key; observed kwargs: {call}")
        assert call["Key"] == {"plugin_id": item["plugin_id"],
                               "version": 1}
        # The decoded item round-trips (Decimals back to native ints).
        assert got == item

    def test_missing_item_returns_none(self, aws_stack):
        assert aws_stack.plugin_records.get_version_item(
            f"plugin-{uuid.uuid4()}", 1) is None


class TestPreservationAutoStartSemantics:
    """Requirement 3.3: start_queued_builds' idempotency and
    never-raise semantics are the observed baseline."""

    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow,
                                     HealthCheck.filter_too_much])
    @given(item=preservation_record_items())
    def test_property_2_auto_start_semantics_match_baseline(
            self, aws_stack, item):
        """Feature: adjust-revision-stale-read-fix, Property 2: All
        Other Reads and Auto-Start Semantics Are Unchanged

        For ANY record, start_queued_builds starts exactly the
        queued+configured architectures (each from the baseline source
        resolution), leaves already-started and non-queued entries
        byte-identical, and leaves unconfigured architectures queued.

        **Validates: Requirements 3.3**
        """
        preservation_seed_record(aws_stack, item)
        before = fresh_item(aws_stack, item["plugin_id"])
        recorder = PreservationRecordingCodeBuild()
        with _swapped(aws_stack.plugin_builds, "codebuild", recorder):
            result = aws_stack.plugin_builds.start_queued_builds(
                item["plugin_id"], 1)

        artifacts = item["artifacts"]
        expected_started = sorted(
            arch for arch in item["requested_architectures"]
            if artifacts[arch]["buildStatus"] == "queued"
            and arch in CONFIGURED_ARCHS)
        started = sorted(
            call["projectName"].replace("dda-plugin-build-", "")
            for call in recorder.calls)
        assert started == expected_started, (
            "auto-start must submit exactly the queued+configured "
            f"architectures; expected {expected_started}, got {started}")
        # Each submission resolves through the baseline oracle.
        for call in recorder.calls:
            arch = call["projectName"].replace("dda-plugin-build-", "")
            assert call["sourceLocationOverride"] == \
                f"{BUCKET}/{preservation_expected_prefix(item, arch)}"
        # Returned entries mirror the started archs; {} when none.
        assert sorted(result) == expected_started
        # Persisted state: started archs advanced to building; every
        # other entry — non-queued and unconfigured-queued alike — is
        # byte-identical.
        after = fresh_item(aws_stack, item["plugin_id"])
        for arch in item["requested_architectures"]:
            if arch in expected_started:
                assert after["artifacts"][arch]["buildStatus"] == "building"
                assert after["artifacts"][arch]["buildId"]
            else:
                assert after["artifacts"][arch] == \
                    before["artifacts"][arch], (
                        f"{arch} ({artifacts[arch]['buildStatus']}) must "
                        "be left untouched by the auto-start")

    def test_missing_record_returns_empty(self, aws_stack):
        """A missing Plugin_Record returns {} without starting anything
        and without raising (3.3)."""
        recorder = PreservationRecordingCodeBuild()
        with _swapped(aws_stack.plugin_builds, "codebuild", recorder):
            result = aws_stack.plugin_builds.start_queued_builds(
                f"plugin-{uuid.uuid4()}", 1)
        assert result == {}
        assert recorder.calls == []

    def test_start_build_failure_recorded_without_raising(
            self, aws_stack):
        """A StartBuild failure settles the arch entry 'failed' with
        the error in logTail — and never raises to the caller (3.3)."""
        arch = "x86_64"
        usecase_id, plugin_id, _ = new_record_ids(aws_stack)
        put_imported_record(
            aws_stack, usecase_id, plugin_id,
            requested=[arch],
            artifacts={arch: {"buildStatus": "queued"}},
        )

        with _swapped(aws_stack.plugin_builds, "codebuild",
                      PreservationFailingCodeBuild()):
            result = aws_stack.plugin_builds.start_queued_builds(
                plugin_id, 1)

        entry = result[arch]
        assert entry["buildStatus"] == "failed"
        assert PreservationFailingCodeBuild.MESSAGE in entry["logTail"]
        after = fresh_item(aws_stack, plugin_id)
        assert after["artifacts"][arch]["buildStatus"] == "failed"
        assert PreservationFailingCodeBuild.MESSAGE in \
            after["artifacts"][arch]["logTail"]


class TestPreservationArchSourcePrefixResolution:
    """Requirements 3.1 / 3.2: arch_source_prefix resolution is the
    observed baseline for mapped, unmapped, and flat records."""

    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow,
                                     HealthCheck.filter_too_much])
    @given(item=preservation_record_items())
    def test_property_2_resolution_matches_baseline(self, aws_stack, item):
        """Feature: adjust-revision-stale-read-fix, Property 2: All
        Other Reads and Auto-Start Semantics Are Unchanged

        For ANY generated item and EVERY arch — mapped through
        arch_revisions[arch] -> fetches[slug].source_prefix, unmapped
        (flat fallback), dangling-mapped, and unconfigured —
        arch_source_prefix is byte-identical to the baseline oracle.

        **Validates: Requirements 3.1, 3.2**
        """
        for arch in [*ALL_ARCHS, UNCONFIGURED_ARCH]:
            assert aws_stack.plugin_builds.arch_source_prefix(
                item, arch) == preservation_expected_prefix(item, arch)

    def test_flat_single_revision_record_resolves_flat_prefix(
            self, aws_stack):
        """A record with no per-arch mappings builds from the flat
        source_s3_prefix layout for every arch (3.2)."""
        base = "plugin-sources/uc-flat/plugin-flat/1/"
        item = {"plugin_id": "plugin-flat", "version": 1,
                "source_s3_prefix": base}
        for arch in ALL_ARCHS:
            assert aws_stack.plugin_builds.arch_source_prefix(
                item, arch) == base

    def test_mapped_and_unmapped_archs_resolve_independently(
            self, aws_stack):
        """A partially mapped record resolves the mapped arch through
        its rev-{slug}/ tree and every other arch through the flat
        prefix (3.1)."""
        base = "plugin-sources/uc-mixed/plugin-mixed/1/"
        item = {
            "plugin_id": "plugin-mixed", "version": 1,
            "source_s3_prefix": base,
            "arch_revisions": {"arm64_jp5": "1-16"},
            "fetches": {"1-16": {"revision": "1.16",
                                 "source_prefix": f"{base}rev-1-16/",
                                 "status": "succeeded"}},
        }
        assert aws_stack.plugin_builds.arch_source_prefix(
            item, "arm64_jp5") == f"{base}rev-1-16/"
        for arch in [a for a in ALL_ARCHS if a != "arm64_jp5"]:
            assert aws_stack.plugin_builds.arch_source_prefix(
                item, arch) == base


class TestPreservationRetryAndFailurePaths:
    """Requirements 3.4 / 3.6: manual retry and adjustment
    fetch-failure handling are the observed baseline."""

    def test_manual_retry_resubmits_recorded_tree(self, aws_stack):
        """POST .../build re-submits the platform from its CURRENTLY
        RECORDED source tree — the arch's mapped rev-{slug}/ prefix
        (3.4)."""
        arch, slug = "arm64_jp5", "1-16"
        usecase_id, plugin_id, base = new_record_ids(aws_stack)
        item = put_imported_record(
            aws_stack, usecase_id, plugin_id,
            requested=[arch],
            artifacts={arch: {"buildStatus": "failed",
                              "logTail": "meson failed"}},
            arch_revisions={arch: slug},
            fetches={slug: {"revision": "1.16",
                            "source_prefix": f"{base}rev-{slug}/",
                            "status": "succeeded"}},
        )
        admin = make_admin(aws_stack, item["usecase_id"])

        with recording_codebuild(aws_stack) as recorder:
            response = aws_stack.plugin_builds.handler(api_event(
                "POST", "/plugins/{id}/versions/{v}/build",
                admin, item["plugin_id"], 1,
                {"architectures": [arch]}), None)

        assert response["statusCode"] == 202, response["body"]
        starts = arch_starts(recorder, arch)
        assert len(starts) == 1
        assert starts[0]["sourceLocationOverride"] == \
            f"{BUCKET}/{base}rev-{slug}/", (
                "a manual retry must re-submit from the platform's "
                "currently recorded source tree")

    def test_adjustment_fetch_failure_touches_only_affected_arch(
            self, aws_stack):
        """A FAILED adjustment fetch settles the fetches entry 'failed'
        and records the fetch-failure logTail on the affected arch's
        entry ONLY — no builds started, other platforms' entries and
        arch_revisions byte-identical (3.6)."""
        arch, other = "arm64_jp4", "arm64_jp5"
        slug, revision = "1.16", "1.16"
        other_slug = "1-18"
        build_id = "dda-plugin-fetch:adjust-fail-1"
        usecase_id, plugin_id, base = new_record_ids(aws_stack)
        item = put_imported_record(
            aws_stack, usecase_id, plugin_id,
            requested=[arch, other],
            artifacts={arch: {"buildStatus": "queued"},
                       other: {"buildStatus": "succeeded",
                               "s3Key": "plugin-staging/lib.so",
                               "logTail": ""}},
            arch_revisions={other: other_slug},
            fetches={slug: {"revision": revision,
                            "source_prefix": f"{base}rev-{slug}/",
                            "status": "fetching",
                            "pending_archs": [arch],
                            "fetch_build_id": build_id},
                     other_slug: {"revision": "1.18",
                                  "source_prefix": f"{base}rev-{other_slug}/",
                                  "status": "succeeded"}},
        )
        before = fresh_item(aws_stack, item["plugin_id"])

        with recording_codebuild(aws_stack) as recorder:
            result = aws_stack.plugin_builds.handler(
                adjustment_fetch_event(item, build_id, slug,
                                       status="FAILED"), None)

        assert result["recorded"] is True
        assert result["fetch_status"] == "failed"
        # No builds started on the failure path.
        assert recorder.calls == []
        after = fresh_item(aws_stack, item["plugin_id"])
        # The failure surfaces on the affected platform's entry only.
        assert after["artifacts"][arch]["buildStatus"] == "failed"
        assert revision in after["artifacts"][arch]["logTail"]
        assert "could not be fetched" in after["artifacts"][arch]["logTail"]
        # Everything else is byte-identical: the other platform's entry,
        # the prior mapping, and the other revision's fetch entry.
        assert after["artifacts"][other] == before["artifacts"][other]
        assert after["arch_revisions"] == before["arch_revisions"]
        assert after["fetches"][other_slug] == before["fetches"][other_slug]
        # The failed slot settled with its pending marker cleared.
        assert after["fetches"][slug]["status"] == "failed"
        assert "pending_archs" not in after["fetches"][slug]


# =====================================================================
# Task 4: unit tests for the fix specifics
#
# Deterministic unit coverage of the two changed functions
# (get_version_item's consistent_read parameter, start_queued_builds'
# consistent re-read) and the flows around them. Bullets already
# exercised elsewhere in this file are NOT repeated here:
# - default read omits ConsistentRead / returns the decoded item /
#   returns None when missing: TestPreservationDefaultReadCallShape
# - start_queued_builds re-reads with ConsistentRead=True and, under
#   the stale-read stub, submits the adjusted prefix:
#   TestAdjustRevisionStaleRead (including both end-to-end paths with
#   the queued -> building advancement asserted there)
# - StartBuild exception recorded as a failed entry without raising,
#   and missing record returns {}: TestPreservationAutoStartSemantics
# =====================================================================

class TestGetVersionItemConsistentReadMode:
    """Fix specifics: the opt-in consistent_read=True mode of
    plugin_records.get_version_item (design 'Changes Required' 1).

    **Validates: Requirements 2.3, 3.5**
    """

    def test_consistent_read_true_passes_the_key_and_decodes_the_item(
            self, aws_stack):
        """consistent_read=True issues a get_item carrying EXACTLY the
        Key plus ConsistentRead=True — and returns the same decoded
        item as the default mode."""
        usecase_id, plugin_id, _ = new_record_ids(aws_stack)
        item = put_imported_record(
            aws_stack, usecase_id, plugin_id,
            requested=["x86_64"],
            artifacts={"x86_64": {"buildStatus": "succeeded"}},
        )
        records = aws_stack.plugin_records
        recorder = PreservationRecordingTable(records.plugin_table())
        with _swapped(records, "plugin_table", lambda: recorder):
            got = records.get_version_item(plugin_id, 1,
                                           consistent_read=True)

        (call,) = recorder.get_item_calls
        assert call.get("ConsistentRead") is True, (
            "consistent_read=True must pass ConsistentRead=True to "
            f"get_item; observed kwargs: {call}")
        assert call["Key"] == {"plugin_id": plugin_id, "version": 1}
        assert set(call) == {"Key", "ConsistentRead"}, (
            "the consistent-mode call must add ONLY the ConsistentRead "
            f"key; observed kwargs: {call}")
        # The decoded item round-trips in consistent mode too.
        assert got == item

    def test_consistent_read_missing_item_returns_none(self, aws_stack):
        """A missing record returns None in consistent mode, exactly
        like the default mode."""
        assert aws_stack.plugin_records.get_version_item(
            f"plugin-{uuid.uuid4()}", 1, consistent_read=True) is None

    def test_consistent_read_bypasses_the_stale_snapshot(self, aws_stack):
        """Under the stale-read stub, the default read returns the
        frozen pre-write item while consistent_read=True reads through
        to the post-write state — the read-your-own-write mechanism the
        fix relies on."""
        usecase_id, plugin_id, _ = new_record_ids(aws_stack)
        put_imported_record(
            aws_stack, usecase_id, plugin_id,
            requested=["x86_64"],
            artifacts={"x86_64": {"buildStatus": "queued"}},
        )
        records = aws_stack.plugin_records
        with stale_read_stub(aws_stack, plugin_id):
            # Post-freeze write lands on the real table only.
            aws_stack.tables.plugin_records.update_item(
                Key={"plugin_id": plugin_id, "version": 1},
                UpdateExpression="SET updated_at = :t",
                ExpressionAttributeValues={":t": 2},
            )
            stale = records.get_version_item(plugin_id, 1)
            consistent = records.get_version_item(plugin_id, 1,
                                                  consistent_read=True)

        assert stale["updated_at"] == 1
        assert consistent["updated_at"] == 2


class TestStartQueuedBuildsIdempotencyUnits:
    """Fix specifics: deterministic idempotency coverage of
    start_queued_builds on a mixed record (already-building arch
    untouched, unconfigured arch left queued, queued+configured arch
    started from its resolved prefix).

    **Validates: Requirements 3.3**
    """

    def test_mixed_record_starts_only_the_queued_configured_arch(
            self, aws_stack):
        building_entry = {"buildStatus": "building",
                          "buildId": "dda-plugin-build-arm64_jp5:prior",
                          "logTail": ""}
        usecase_id, plugin_id, base = new_record_ids(aws_stack)
        put_imported_record(
            aws_stack, usecase_id, plugin_id,
            requested=["x86_64", "arm64_jp5", UNCONFIGURED_ARCH],
            artifacts={"x86_64": {"buildStatus": "queued"},
                       "arm64_jp5": dict(building_entry),
                       UNCONFIGURED_ARCH: {"buildStatus": "queued"}},
        )

        recorder = PreservationRecordingCodeBuild()
        with _swapped(aws_stack.plugin_builds, "codebuild", recorder):
            result = aws_stack.plugin_builds.start_queued_builds(
                plugin_id, 1)

        # Exactly one submission: the queued+configured arch, from the
        # record's flat prefix.
        assert [c["projectName"] for c in recorder.calls] == \
            ["dda-plugin-build-x86_64"]
        assert recorder.calls[0]["sourceLocationOverride"] == \
            f"{BUCKET}/{base}"
        assert sorted(result) == ["x86_64"]

        after = fresh_item(aws_stack, plugin_id)
        assert after["artifacts"]["x86_64"]["buildStatus"] == "building"
        assert after["artifacts"]["x86_64"]["buildId"]
        # Already-building arch is byte-identical; unconfigured arch is
        # left queued.
        assert after["artifacts"]["arm64_jp5"] == building_entry
        assert after["artifacts"][UNCONFIGURED_ARCH] == \
            {"buildStatus": "queued"}


class TestImportTimeFlowUnderStaleWindow:
    """Fix specifics: the original import fetch-settle ->
    start_queued_builds flow still starts ALL queued architectures
    from the flat prefix — even inside the stale-read window (the
    settle write invisible to plain reads) — with unchanged
    idempotency on duplicate deliveries.

    **Validates: Requirements 3.2, 3.3**
    """

    ARCHS = ["x86_64", "arm64_jp5"]

    def test_import_settle_starts_all_queued_archs_from_flat_prefix(
            self, aws_stack):
        env = ImporterEnv(aws_stack)
        usecase_id = env.create_usecase()
        admin = env.make_user()
        env.assign_role(admin, usecase_id, "UseCaseAdmin")
        status, body = env.import_plugin(admin, {
            "usecase_id": usecase_id,
            "repo_url": REPO_URL,
            "architectures": self.ARCHS,
        })
        assert status == 202, body
        plugin = body["plugin"]
        env.sync_source(plugin, {"meson.build": MESON_PLUGIN,
                                 "gstmyfilter.c": None})
        detail = env.fetch_result_detail(plugin, body["import"]["buildId"])

        # Freeze the pre-settle state ('fetching', no artifacts): the
        # settle write and the auto-start happen in one invocation, so
        # a stale re-read would show NOTHING queued and silently start
        # nothing (design edge case). The consistent re-read must see
        # the queued entries.
        with stale_read_stub(aws_stack, plugin["plugin_id"]), \
                recording_codebuild(aws_stack) as recorder:
            result = env.deliver_fetch_result(detail)
            duplicate = env.deliver_fetch_result(detail)

        assert result == {"recorded": True, "import_status": "imported"}
        # Unchanged idempotency: the duplicate delivery settles nothing
        # and starts nothing.
        assert duplicate == {"recorded": False,
                             "reason": "already recorded"}

        starts = [c for c in recorder.calls
                  if c["projectName"].startswith("dda-plugin-build-")]
        assert sorted(c["projectName"] for c in starts) == \
            sorted(f"dda-plugin-build-{a}" for a in self.ARCHS)
        for call in starts:
            assert call["sourceLocationOverride"] == \
                f"{BUCKET}/{plugin['source_s3_prefix']}", (
                    "import-time auto-start must build from the flat "
                    "source_s3_prefix (no per-arch mappings yet)")

        record = fresh_item(aws_stack, plugin["plugin_id"])
        for arch in self.ARCHS:
            assert record["artifacts"][arch]["buildStatus"] == "building"
            assert record["artifacts"][arch]["buildId"]
