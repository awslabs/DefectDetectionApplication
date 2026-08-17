# Copyright 2026 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Bug-condition exploration suite (task 1, portal leg / case 5) for
vllm-model-reload-after-backend-restart.

**Property 5 (exploration form): workflow packaging emits ONLY
platform-suffixed vLLM model component dependencies.**

Both tests assert the FIXED expected behavior (bugfix.md 2.6), so on the
UNFIXED tree they are EXPECTED TO FAIL — the failures are the
counterexamples proving defect 1.6: ``resolve_model_components``
short-circuits on the vLLM singular ``published_component`` map and hands
its ``component_name`` — deliberately kept as the UNSUFFIXED base name
(``model-vllm-{safe_model_name}``, the component_name-index GSI key for
legacy readers) — straight to ``model_component_dependencies``, which
emits it verbatim as a HARD dependency. On jetson-thor1 that emitted
``model-vllm-qwen3-vl-8b-instruct >=0.0.0`` (workflows 6.0.0/7.0.0),
whose JP6-era artifact HARD-depends on
``aws.edgeml.dda.LocalServer.arm64JP6 >=1.0.0`` — dragging
LocalServer.arm64JP6 1.0.59 onto the JP7 Thor.

Both incident record shapes are seeded (bugfix.md A2 / design Decision 5):

- Case 5a — LEGACY record: the singular map carries ONLY the unsuffixed
  base name (no per-JetPack ``components`` evidence). Fixed behavior:
  packaging FAILS CLOSED naming the model and the uncovered architecture.
  Unfixed: the base name resolves and is emitted verbatim.
- Case 5b — MODERN record: the singular map carries the unsuffixed base
  name PLUS platform-suffixed per-JetPack ``components`` entries (the
  greengrass_publish.py write-back shape). Fixed behavior: resolution for
  ``['arm64_jp7']`` yields only the platform-suffixed JP7 component and
  the emitted dependency set contains ONLY platform-suffixed names.
  Unfixed: the singular short-circuit wins and the base name is emitted.

The SAME suite validates the fix when it passes after implementation
(task 3.7).

Harness: conftest ``aws_stack`` (session-scoped moto) plus a
training-jobs Model_Registry table with the production
``usecase-training-index`` GSI shape — the
``test_workflow_packaging_vllm_resolution_preservation.py`` /
``test_vllm_multi_arch_publish_*`` seeding conventions. Honesty guard:
no real account, Greengrass, or deployment is touched.

Run from ``edge-cv-portal/backend`` WITH conftest (no ``--noconftest``):
    python3 -m pytest tests/test_vllm_workflow_arch_dependency_exploration.py \
        -q -p no:cacheprovider

**Validates: Requirements 1.6**
"""
import inspect
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = "test-vllm-workflow-arch-dep-training-jobs"

ACCOUNT_ID = "123456789012"

#: The incident's exact identities (jetson-thor1, 2026-08-16/17).
MODEL_NAME = "Qwen3-VL-8B-Instruct"
BASE_COMPONENT = "model-vllm-qwen3-vl-8b-instruct"
JP6_TARGET = "jetson-xavier-jp6"
JP7_TARGET = "jetson-xavier-jp7"
SUFFIXED_JP6 = f"{BASE_COMPONENT}-{JP6_TARGET}"
SUFFIXED_JP7 = f"{BASE_COMPONENT}-{JP7_TARGET}"


# ---------------------------------------------------------------------------
# Fixture (the resolution-preservation harness pattern)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def packaging_env(aws_stack):
    """The training-jobs Model_Registry table (production GSI shape) plus a
    freshly imported workflow_packaging bound to it inside moto."""
    import boto3

    os.environ["TRAINING_JOBS_TABLE"] = TRAINING_JOBS_TABLE_NAME

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=TRAINING_JOBS_TABLE_NAME,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "training_id", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-training-index",
            "KeySchema": [{"AttributeName": "usecase_id", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )

    # Re-import so the module binds the table name above and
    # moto-intercepted clients (dependencies-exploration pattern).
    for module_name in ("workflow_packaging", "node_catalog_resolution",
                        "model_registry_snapshot"):
        sys.modules.pop(module_name, None)
    import workflow_packaging

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        packaging=workflow_packaging,
        training_table=resource.Table(TRAINING_JOBS_TABLE_NAME),
    )
    os.environ.pop("TRAINING_JOBS_TABLE", None)
    sys.modules.pop("workflow_packaging", None)


# ---------------------------------------------------------------------------
# Seeding helpers (the incident record shapes)
# ---------------------------------------------------------------------------

def fresh_usecase_id():
    return f"uc-vllm-arch-dep-exploration-{uuid.uuid4()}"


def _component_arn(name, version="1.0.0"):
    return (f"arn:aws:greengrass:{REGION}:{ACCOUNT_ID}:components:"
            f"{name}:versions:{version}")


def _per_jetpack_entry(target, arch):
    """One per-JetPack ``components`` entry exactly as the vLLM publish
    write-back leaves it (greengrass_publish.py published_component_map)."""
    name = f"{BASE_COMPONENT}-{target}"
    return {
        "component_name": name,
        "component_version": "1.0.0",
        "target": target,
        "architecture": arch,
        "supported_architectures": [arch],
        "component_arn": _component_arn(name),
    }


def seed_record(training_table, usecase_id, published_component,
                published_components=None):
    item = {
        "training_id": f"tr-{uuid.uuid4()}",
        "usecase_id": usecase_id,
        "model_name": MODEL_NAME,
        "model_type": "vllm",
        "source": "vllm",
        "created_at": 1,
        "component_name": BASE_COMPONENT,
        "published": True,
        "published_component": published_component,
    }
    if published_components is not None:
        item["published_components"] = published_components
    training_table.put_item(Item=item)
    return item


def seed_legacy_record(training_table, usecase_id):
    """The LEGACY incident shape: the singular map carries ONLY the
    unsuffixed base name (the JP6-era ``model-vllm-qwen3-vl-8b-instruct``
    v1.0.0 artifact's record shape) — no per-JetPack evidence at all."""
    return seed_record(training_table, usecase_id, {
        "component_name": BASE_COMPONENT,
        "component_version": "1.0.0",
        "runtime": "vllm",
        "supported_architectures": ["arm64_jp6"],
    })


def seed_modern_record(training_table, usecase_id):
    """The MODERN incident shape: the singular map keeps the unsuffixed
    base name (the component_name-index GSI key for legacy readers) PLUS
    platform-suffixed per-JetPack ``components`` entries covering JP6 and
    JP7 — the write-back shape greengrass_publish.py produces since the
    multi-arch publish fix."""
    entries = [
        _per_jetpack_entry(JP6_TARGET, "arm64_jp6"),
        _per_jetpack_entry(JP7_TARGET, "arm64_jp7"),
    ]
    published_component = {
        "component_name": BASE_COMPONENT,
        "component_version": "1.0.0",
        "supported_architectures": ["arm64_jp6", "arm64_jp7"],
        "runtime": "vllm",
        "component_arns": {
            entry["target"]: entry["component_arn"] for entry in entries
        },
        "components": entries,
    }
    plural = [
        {
            "component_name": entry["component_name"],
            "component_version": entry["component_version"],
            "target": entry["target"],
            "status": "published",
            "component_arn": entry["component_arn"],
            "supported_architectures": entry["supported_architectures"],
        }
        for entry in entries
    ]
    return seed_record(training_table, usecase_id, published_component,
                       published_components=plural)


def resolve(packaging, model_names, usecase_id, archs):
    """Signature-tolerant resolve_model_components call (the
    resolution-preservation seam): pass archs when accepted."""
    function = packaging.resolve_model_components
    parameters = inspect.signature(function).parameters
    if "archs" in parameters:
        return function(model_names, usecase_id, archs=list(archs))
    positional = [p for p in parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if len(positional) >= 3:
        return function(model_names, usecase_id, list(archs))
    return function(model_names, usecase_id)


def platform_suffixes(packaging):
    """Every valid publish-target suffix a platform-suffixed vLLM model
    component name may carry, read from the module's own target
    vocabulary."""
    targets = set(packaging.ARCH_TO_PUBLISH_TARGET.values())
    for extra in packaging.ARCH_TO_EXTRA_PUBLISH_TARGETS.values():
        targets.update(extra)
    return {f"-{target}" for target in targets}


# ---------------------------------------------------------------------------
# Case 5a — LEGACY unsuffixed-only record: fail closed for JP7
# ---------------------------------------------------------------------------

def test_case_5a_legacy_unsuffixed_record_fails_closed_for_jp7(packaging_env):
    """A legacy record whose only publish evidence is the unsuffixed base
    name must FAIL CLOSED for ``['arm64_jp7']`` — no platform-suffixed
    published component covers the selected architecture, and the base
    name must NEVER be emitted (bugfix.md 2.6).

    Counterexample on the unfixed tree: the singular short-circuit
    resolves the record verbatim and the emitted dependency set is the
    incident's exact arch-agnostic HARD dependency
    ``model-vllm-qwen3-vl-8b-instruct >=0.0.0``.

    **Validates: Requirements 1.6**
    """
    usecase_id = fresh_usecase_id()
    seed_legacy_record(packaging_env.training_table, usecase_id)

    try:
        resolved = resolve(packaging_env.packaging, [MODEL_NAME],
                           usecase_id, ["arm64_jp7"])
    except packaging_env.packaging.PackagingError as err:
        # Fixed behavior: fail closed naming the model and the uncovered
        # architecture.
        assert MODEL_NAME in err.message, (
            f"fail-closed error must name the model: {err.message!r}")
        assert "arm64_jp7" in err.message, (
            f"fail-closed error must name the uncovered architecture: "
            f"{err.message!r}")
        return

    dependencies = packaging_env.packaging.model_component_dependencies(
        resolved)
    pytest.fail(
        "counterexample (defect 1.6): the legacy unsuffixed-only record "
        "did NOT fail closed for ['arm64_jp7'] — resolve_model_components "
        "short-circuited on the singular published_component map "
        "(resolved: {!r}) and model_component_dependencies emitted the "
        "arch-agnostic base name verbatim: {!r} — the incident's exact "
        "HARD dependency ('{}' >=0.0.0), which dragged the JP6-era "
        "artifact (HARD-depending on aws.edgeml.dda.LocalServer.arm64JP6 "
        ">=1.0.0) and LocalServer.arm64JP6 1.0.59 onto the JP7 Thor".format(
            resolved, dependencies, BASE_COMPONENT)
    )


# ---------------------------------------------------------------------------
# Case 5b — MODERN record: only platform-suffixed names are emitted
# ---------------------------------------------------------------------------

def test_case_5b_modern_record_emits_only_suffixed_dependencies(packaging_env):
    """A modern record carrying platform-suffixed per-JetPack
    ``components`` evidence must resolve ``['arm64_jp7']`` to the
    platform-suffixed JP7 component and emit ONLY platform-suffixed
    dependency names — never the unsuffixed base name (bugfix.md 2.6).

    Counterexample on the unfixed tree: the singular short-circuit hands
    the unsuffixed ``component_name`` straight to the dependency emitter
    even though the suffixed JP7 evidence sits right next to it in the
    same map — the emitted set is ``{'model-vllm-qwen3-vl-8b-instruct'}``
    instead of ``{'model-vllm-qwen3-vl-8b-instruct-jetson-xavier-jp7'}``.

    **Validates: Requirements 1.6**
    """
    usecase_id = fresh_usecase_id()
    seed_modern_record(packaging_env.training_table, usecase_id)

    resolved = resolve(packaging_env.packaging, [MODEL_NAME],
                       usecase_id, ["arm64_jp7"])
    dependencies = packaging_env.packaging.model_component_dependencies(
        resolved)

    assert dependencies, (
        "harness precondition failed: no model dependency emitted at all "
        f"(resolved: {resolved!r})")
    suffixes = platform_suffixes(packaging_env.packaging)
    unsuffixed = sorted(
        name for name in dependencies
        if not any(name.endswith(suffix) for suffix in suffixes))
    assert BASE_COMPONENT not in dependencies and not unsuffixed, (
        "counterexample (defect 1.6): workflow packaging emitted "
        "non-platform-suffixed vLLM model dependency name(s) {!r} for "
        "archs ['arm64_jp7'] (full emitted set: {!r}; resolved: {!r}) — "
        "the singular published_component short-circuit hands the "
        "unsuffixed base name to model_component_dependencies verbatim "
        "even though the platform-suffixed Per_JetPack_Component evidence "
        "('{}') is present in the same record".format(
            unsuffixed, dependencies, resolved, SUFFIXED_JP7)
    )
    assert set(dependencies) == {SUFFIXED_JP7}, (
        "the resolved JP7 dependency set must be exactly the "
        "platform-suffixed JP7 component; got {!r}".format(
            sorted(dependencies))
    )
