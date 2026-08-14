# Bugfix Requirements Document

## Introduction

Publishing any vLLM model to Greengrass fails with a `ConflictException` from
`CreateComponentVersion`. The portal reports:

> vLLM component publish failed: jetson-xavier-jp6: An error occurred
> (ConflictException) when calling the CreateComponentVersion operation:
> Component [model-vllm-qwen3-vl-8b-instruct : 1.0.0] for account
> [164152369890] already exists and can't be created again with tags.;
> jetson-xavier-jp7: (same ConflictException). Failed step:
> greengrass_registration

Four defects compound into this failure:

1. **Duplicate component identity per publish attempt.** `publish_component`
   loops over every packaged target but, for vLLM records, keeps one fixed
   component name (`model-vllm-{safe_model_name}`) and one shared component
   version for all of them. Since `vllm_supported_architectures()` now returns
   `['arm64_jp6', 'arm64_jp7']` (two targets: `jetson-xavier-jp6`,
   `jetson-xavier-jp7`), the second `create_component_version` call always
   collides on the same name:version. This makes **every** vLLM publish fail,
   not just one record — the single-target era hid it.
2. **Rollback silently fails, leaving permanent orphans.** The vLLM
   all-or-nothing atomicity gate calls `greengrass.delete_component(...)` for
   every component version it created, catching and only warning on failure.
   The portal Lambda role does not grant `greengrass:DeleteComponent`, so the
   rollback is always denied and the first target's component version survives
   cloud-side.
3. **Retries are wedged forever.** `next_vllm_component_version()` derives the
   next `N.0.0` purely from the record's own publish history. A failed attempt
   deliberately writes no publish state (for retryability), so the history
   stays null and the function recomputes `1.0.0` — which now exists as the
   defect-2 orphan. Every retry therefore conflicts on the *first* target too.
4. **JP7 has no target mappings, so it is silently stamped as amd64.** In
   `greengrass_publish.py` the module-level `TARGET_TO_LOCAL_SERVER` and
   `TARGET_TO_PLATFORM` maps have no `jetson-xavier-jp7` entry — the
   `jp7-vllm-enablement` spec (task 6.2) added `arm64_jp7` to
   `vllm_supported_architectures()` and `packaging.VLLM_ARCH_TO_TARGET` but not
   to these two maps. `TARGET_TO_PLATFORM.get(target, 'amd64')` therefore
   defaults the JP7 target to `amd64`, which makes
   `resolve_local_server_component` take its `platform == 'amd64'` branch and
   return `aws.edgeml.dda.LocalServer.amd64` instead of raising `PublishError`.
   The fail-closed guarantee established by the `localserver-arch-naming` spec
   is thus **silently bypassed for JP7**. This is a latent defect surfaced
   during design, independent of defect 1: it would **survive a naming-only
   fix**, yielding a JP7 vLLM component that publishes cleanly and is then
   mis-stamped with an amd64 platform manifest and an amd64 `LocalServer`
   dependency on an aarch64 Thor device.

Impact on the reported record (training_id
`1e05eb99-ca55-4325-9be0-15874979e6a3`, model `Qwen3-VL-8B-Instruct`, usecase
`645504ce-a60a-4009-8349-7548c0025cd3`): component
`model-vllm-qwen3-vl-8b-instruct` exists cloud-side at exactly version `1.0.0`
(single manifest, platform `linux/aarch64`, tagged `dda-portal:managed=true`
with that training-id) while the record's `published_components` **and**
`published_component` are both null. That same mismatch is why the model is not
selectable on the JP7 (Thor, thing `jetson-thor1`, `target_architecture`
`arm64_jp7`) revise-deployment screen: the deploy screen resolves a vLLM
component's supported architectures from `published_component`, gets nothing,
and fails closed (hidden).

The fix approach is **per-JetPack components** — each supported architecture
gets its own component, name-suffixed exactly like vision model components
already are, so each component carries precisely the right `LocalServer`
dependency. Sharing one component across both JetPacks is not viable:
`generate_vllm_component_recipe()` places a HARD dependency on
`resolve_local_server_component(target, platform)`
(`aws.edgeml.dda.LocalServer.arm64JP6` vs `.arm64JP7`), Greengrass
`ComponentDependencies` is top-level rather than per-manifest, and both targets
are platform `linux/aarch64` so manifests cannot discriminate them either. A
shared component would put an unsatisfiable HARD dependency on every device of
the other JetPack.

## Bug Analysis

### Current Behavior (Defect)

**Defect 1 — duplicate (name, version) create per vLLM publish**

1.1 WHEN a vLLM model record is published and `vllm_supported_architectures()`
yields more than one architecture THEN the system issues one
`create_component_version` call per target using the identical component name
and the identical component version, so every call after the first fails with
`ConflictException` and the whole publish returns 502 with
`failed_step: greengrass_registration`.

1.2 WHEN any vLLM model record is published under the current JP6+JP7
architecture set THEN the system fails deterministically, regardless of model,
usecase, weights, or fit-check outcome — vLLM publishing is entirely broken.

1.3 WHEN a vLLM publish partially succeeds (first target created, later target
conflicting) THEN the system produces one component version whose recipe
carries a HARD dependency on exactly one JetPack's `LocalServer` variant while
its recorded/advertised `supported_architectures` claims **every** supported
architecture, so the architecture set the component advertises does not match
the architecture its dependency can actually satisfy.

**Defect 2 — atomicity rollback is denied, creating orphans**

1.4 WHEN the vLLM atomicity gate rolls back the component versions created
during a failed attempt THEN the system's `greengrass:DeleteComponent` call is
denied by the portal Lambda's IAM policy, the exception is caught and logged as
a warning only, and the component version survives as an orphan.

1.5 WHEN rollback fails THEN the system still reports the publish as retryable
and writes no publish state, so the operator is given no indication that a
cloud-side orphan now exists and must be cleaned up manually.

**Defect 3 — version derivation cannot see cloud-side reality**

1.6 WHEN a vLLM publish is retried after a failed attempt THEN
`next_vllm_component_version()` reads only the record's `published_components`
list and `published_component` map — both still null because failed attempts
write no state — and returns `1.0.0` again, which conflicts with the orphan
left by the earlier attempt, so the retry fails on the first target.

1.7 WHEN `next_vllm_component_version()` runs THEN the system never queries
Greengrass for the versions that actually exist, despite the function's
docstring claiming it accounts for "versions from failed attempts that may
still exist cloud-side".

**Downstream consequences**

1.8 WHEN a vLLM component version exists in Greengrass but its backing record
has no `published_component` THEN the deployment screen resolves the component's
supported architecture set to `[]` and hides the model from every device,
including the JP7 device it was intended for.

1.9 WHEN the orphan `model-vllm-qwen3-vl-8b-instruct:1.0.0` exists in account
`164152369890` THEN the affected record cannot be published by any number of
retries, even after the code defects are fixed, because `1.0.0` remains taken
for that component name until the orphan is removed or the version derivation
starts observing it.

**Defect 4 — JP7 target unmapped, so the fail-closed resolver is bypassed**

1.10 WHEN a component is published for the packaging target
`jetson-xavier-jp7` THEN the system finds no `jetson-xavier-jp7` entry in
either `TARGET_TO_LOCAL_SERVER` or `TARGET_TO_PLATFORM` in
`greengrass_publish.py`, because `jp7-vllm-enablement` task 6.2 added
`arm64_jp7` to `vllm_supported_architectures()` and
`packaging.VLLM_ARCH_TO_TARGET` but not to these two maps.

1.11 WHEN the platform for that target is determined via
`platform = TARGET_TO_PLATFORM.get(target, 'amd64')` THEN the system falls
through to the `'amd64'` default for a JetPack 7 (aarch64 Thor) target.

1.12 WHEN `resolve_local_server_component('jetson-xavier-jp7', 'amd64')` is
then called THEN the system takes its `platform == 'amd64'` branch and returns
`aws.edgeml.dda.LocalServer.amd64` instead of raising `PublishError`, so the
fail-closed guarantee established by the `localserver-arch-naming` spec is
silently bypassed for JP7 — the amd64 platform default satisfies the resolver,
so it never fails closed.

1.13 WHEN the JP7 component recipe is generated from that resolution THEN the
system emits a manifest whose `Platform` is `{os: linux, architecture: amd64}`
and a HARD `ComponentDependencies` entry on the amd64 `LocalServer`, so an
aarch64 Thor device receives a component mis-stamped for the wrong
architecture and pointing at the wrong LocalServer variant.

1.14 WHEN a JP7 target is published with those mappings missing THEN the
publish SUCCEEDS rather than failing, so nothing surfaces the error to the
operator: no `PublishError`, no failed target, and no warning — the defect is
only observable on the device.

### Expected Behavior (Correct)

**Fix 1 — per-JetPack component names, one create per distinct identity**

2.1 WHEN a vLLM model record is published for N supported architectures THEN
the system SHALL give each target its own component name using the existing
vision-model convention `f"{component_name}-{target_suffix}"` (e.g.
`model-vllm-qwen3-vl-8b-instruct-jetson-xavier-jp6` and
`model-vllm-qwen3-vl-8b-instruct-jetson-xavier-jp7`), inventing no new naming
vocabulary.

2.2 WHEN a vLLM publish issues `create_component_version` calls THEN the system
SHALL issue exactly one call per distinct `(component_name, component_version)`
pair and SHALL never attempt a create for an identity it has already attempted
in the same publish.

2.3 WHEN a vLLM record's supported architecture set contains JP6 and JP7 (and
`arm64_jp5` when `JP5_VLLM_ENABLED` is on) THEN the system SHALL publish a
DEPLOYABLE component for every one of them and SHALL return success with one
`published_components` entry per architecture.

2.4 WHEN the system generates each per-JetPack component's recipe THEN it SHALL
advertise in `DefaultConfiguration.supported_architectures` exactly the single
`Target_Architecture` that component is for, and its HARD `LocalServer`
dependency SHALL be that architecture's variant, so the architecture gate
becomes exact per component: the JP6 component is rejected for a JP7 device and
the JP7 component is rejected for a JP6 device.

2.5 WHEN the system writes vLLM publish state onto the record THEN it SHALL
record, per published component, that component's own name, version, and single
supported architecture, in a shape that supports N components per record.

2.6 WHEN a per-JetPack component name is derived THEN it SHALL still satisfy
every existing consumer: the backend `model-` prefix validation,
`isVllmModelComponent`'s `model-vllm-` prefix test, the frontend
`inferComponentTargetArchs` JetPack token match
`/(?:jp|jetpack)(4|5|6|7)(?![0-9])/`, and the Greengrass component-name length
and character-set limits.

**Fix 2 — rollback that actually works**

2.7 WHEN the vLLM atomicity gate rolls back component versions created during a
failed attempt THEN the portal Lambda role SHALL be authorized to call
`greengrass:DeleteComponent`, scoped to the Greengrass components resource ARN,
so the rollback deletes those versions and leaves no orphans.

2.8 WHEN a rollback delete nonetheless fails THEN the system SHALL keep its
best-effort, non-throwing behaviour (the publish failure remains the reported
error) while logging enough detail to identify the surviving version.

**Fix 3 — version derivation grounded in Greengrass**

2.9 WHEN the system derives the next vLLM component version for a component
name THEN it SHALL derive it from the versions that actually exist in
Greengrass for that name, mirroring `workflow_packaging.py`'s
`_existing_component_versions()` / `next_component_version()` pattern, so a
prior attempt's orphan can never wedge a retry.

2.10 WHEN the next version is derived THEN it SHALL be strictly greater than
every version already existing in Greengrass for that component name, and SHALL
remain a major-only `N.0.0` bump (the Greengrass on-device component store
reuses artifacts across patch/minor revisions, which is why patch/minor bumps
are not used).

**Fix 4 — suffix-aware resolution, backend**

2.11 WHEN the deployment gate resolves a `model-vllm-*` component name back to
its backing record THEN `load_vllm_model_record` SHALL be suffix-aware: it SHALL
resolve names carrying any known target suffix (the closed vocabulary of
`packaging.VLLM_ARCH_TO_TARGET` values) to the originating record, in addition
to resolving legacy unsuffixed names.

2.12 WHEN the deployment gate computes a `model-vllm-*` component's supported
architecture set THEN `vllm_component_architectures` SHALL return the
architecture(s) of that **specific** component rather than the record-wide set.

**Fix 5 — suffix-aware resolution, frontend**

2.13 WHEN the deployment screen resolves supported architectures for
`model-vllm-*` components THEN `vllmArchGate.ts`, `archCompatibility.ts`, and
`CreateDeployment.tsx` SHALL key `vllmComponentArchs` by the exact (suffixed)
component name and resolve each one to that component's own architecture via
the model detail API (`models.py` returns `published_component` to the client),
so the correct per-JetPack component shows as deployable.

2.14 WHEN the JP7 device `jetson-thor1` (recorded `target_architecture`
`arm64_jp7`) is targeted THEN the system SHALL show the record's JP7 component
as selectable and SHALL show its JP6 component as incompatible.

**Fix 6 — operational recovery**

2.15 WHEN the orphan component version `model-vllm-qwen3-vl-8b-instruct:1.0.0`
blocks the affected record THEN it SHALL be deleted (or otherwise made
non-blocking by the cloud-side version derivation of 2.9) so that record can
publish. Deleting it is a live AWS mutation and SHALL require explicit user
confirmation before execution.

2.16 WHEN the code and infrastructure fixes are complete THEN a portal deploy
(Lambda code plus infrastructure for the IAM change) SHALL be required before
the fix takes effect in the account, and this SHALL be stated as an explicit
step rather than assumed.

**Fix 7 — JP7 target mappings, and no more silent amd64 defaulting**

2.17 WHEN `greengrass_publish.py` resolves the packaging target
`jetson-xavier-jp7` THEN both module-level maps SHALL carry an entry for it:
`TARGET_TO_LOCAL_SERVER['jetson-xavier-jp7']` SHALL be
`'aws.edgeml.dda.LocalServer.arm64JP7'` and
`TARGET_TO_PLATFORM['jetson-xavier-jp7']` SHALL be `'aarch64'`.

2.18 WHEN the JP7 component recipe is generated THEN its manifest platform
SHALL be `aarch64` (`{os: linux, architecture: aarch64}`) and its HARD
`ComponentDependencies` entry SHALL be on the `arm64JP7` `LocalServer` variant,
matching the aarch64 Thor device it is deployed to.

2.19 WHEN a packaging target is not mapped in **both**
`TARGET_TO_LOCAL_SERVER` and `TARGET_TO_PLATFORM` THEN the system SHALL fail
closed for that target with a `PublishError` rather than silently defaulting
its platform to `amd64` and resolving the amd64 `LocalServer`, so a future
aarch64 target added to `packaging.VLLM_ARCH_TO_TARGET` without these two map
entries cannot repeat this defect.

### Unchanged Behavior (Regression Prevention)

**Vision (non-vLLM) publish**

3.1 WHEN a non-vLLM model record is published THEN the system SHALL CONTINUE TO
name each target's component `f"{component_name}-{target_suffix}"`, use the
caller-supplied component version, write only `published_components` and
`updated_at`, and apply no all-or-nothing atomicity gate.

3.2 WHEN a non-vLLM publish encounters a target whose `LocalServer` variant
cannot be resolved THEN the system SHALL CONTINUE TO fail closed for that
target with a `PublishError`, recording it as a failed target without creating a
component version.

**Device-side runtime identity**

3.3 WHEN a vLLM component's lifecycle invokes `vllm_model_prep.py` THEN the
system SHALL CONTINUE TO pass `--model_name` as `_safe_model_name(model_name)`,
unchanged by the component rename, so the Triton model identity is unaffected.
`--component_name` is used for logging only (its argparse help says
"(logging)"; `prepare()` binds it to `component` and only logs it), so
suffixing the component name has zero device-side runtime impact and does not
regress the intent of the `vllm-model-name-mismatch` spec, which fixed the
Triton-facing *model* name.

**Backward compatibility with already-published records**

3.4 WHEN a deployment references an existing vLLM component published under the
OLD unsuffixed name with a record-wide `supported_architectures` THEN the system
SHALL CONTINUE TO resolve that component to its record and to that recorded
architecture set, and the gate SHALL NOT start failing closed on it.

3.5 WHEN an existing deployment that already includes an old-style unsuffixed
vLLM component is revised THEN the system SHALL CONTINUE TO treat it as
deployable on the devices it was previously deployable on.

**Architecture gate semantics**

3.6 WHEN a target device has no recorded `Target_Architecture` (null or absent)
THEN `evaluate_vllm_arch_gate` SHALL CONTINUE TO treat it as incompatible (fail
closed).

3.7 WHEN a vLLM component's supported architecture set is empty (unresolvable
record) THEN the gate SHALL CONTINUE TO treat every device as incompatible.

3.8 WHEN a target device's architecture is `arm64_jp4` THEN the gate SHALL
CONTINUE TO reject it with reason `JP4_UNSUPPORTED` and the message "JetPack 4
does not support vLLM inference", and `arm64_jp4` SHALL CONTINUE TO never appear
in any vLLM component's supported set.

3.9 WHEN architectures are compared THEN the system SHALL CONTINUE TO match by
exact name with no cross-architecture fallback, and SHALL CONTINUE TO return one
entry per (component, device) miss in the 409 `VLLM_ARCH_UNSUPPORTED` details.

3.10 WHEN a deployment contains an LLM-bearing workflow component
(`dda.workflow.*` with `has_llm_inference`) THEN the system SHALL CONTINUE TO
gate it on the version item's `packaged_architectures`, unchanged.

3.11 WHEN a deployment contains no vLLM-bearing component THEN the system SHALL
CONTINUE TO contribute zero gate findings, so pre-feature validation applies
verbatim — jp4 included.

**vLLM publish gates and atomicity**

3.12 WHEN the vLLM preflight fit check runs THEN the system SHALL CONTINUE TO
fail the publish with 422 and full sizing findings when every supported
architecture fails, proceed and record `skip_fit_check` when the override is
supplied, and proceed annotated as `unverified` when the weight estimate cannot
be determined — all before any component registration.

3.13 WHEN a vLLM publish has any failing target THEN the system SHALL CONTINUE
TO be all-or-nothing: no publish state is written onto the record, the response
is a retryable 502 with `failed_step: greengrass_registration`, and a failure
audit event is logged.

3.14 WHEN a vLLM publish succeeds THEN the system SHALL CONTINUE TO create the
Models-table record, set `published = True`, and log a success audit event.

**IAM and security baseline**

3.15 WHEN the portal Lambda IAM policy is synthesized THEN it SHALL CONTINUE TO
grant exactly the Greengrass actions it grants today plus
`greengrass:DeleteComponent`, with no widening of any other statement and no
wildcard resource for the new action; the new action is scoped to the Greengrass
components resource ARN because rollback only ever deletes component versions
the portal itself just created.

3.16 WHEN the security preservation gate compares the synthesized policy to
`test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.template.json`
(and its `.unfixed` variant) THEN the baseline SHALL be re-recorded per that
gate's documented protocol with a note explaining the intentional
`DeleteComponent` grant — the gate SHALL CONTINUE TO fail on any unexplained
policy drift.

3.17 WHEN the backend test suites run THEN the existing suites
`test_vllm_publish_fit_gate.py`, `test_deployment_plugin_gates.py`, and
`test_vision_model_packaging_preservation.py` SHALL CONTINUE TO pass, run
separately per the suite convention
(`PYTHONPATH=src/backend:test/backend-test python3 -m pytest <suite> -q -p no:cacheprovider --noconftest`),
and the 4 known-acceptable local-only `cdk.out` drift failures under
`test/backend-test/security/` SHALL CONTINUE TO be treated as pre-existing and
left alone.

**Existing target mappings and fail-closed resolution**

3.18 WHEN a component is published for an existing amd64/x86 target
(`x86_64-cpu`, `x86_64-cuda`) THEN the system SHALL CONTINUE TO resolve it to
`aws.edgeml.dda.LocalServer.amd64` with platform `amd64`, and the existing
aarch64 targets (`jetson-xavier`, `jetson-xavier-jp5`, `jetson-xavier-jp6`,
`arm64-cpu`) SHALL CONTINUE TO resolve to the LocalServer variant and platform
they resolve to today.

3.19 WHEN `resolve_local_server_component` is given a genuinely unknown target
THEN it SHALL CONTINUE TO fail closed with a `PublishError` rather than
selecting a bare/untagged aarch64 name, preserving the `localserver-arch-naming`
guarantee for every target other than the newly mapped `jetson-xavier-jp7`.

### Bug Conditions and Properties

**Key definitions.** `F` is the current (unfixed) code; `F'` is the fixed code.
`archs(X)` is `vllm_supported_architectures()` for record `X`;
`target(a)` is `packaging.VLLM_ARCH_TO_TARGET[a]`; `suffix(a)` is
`target(a).replace('_', '-')`.

#### Defect 1 — duplicate component identity

```pascal
FUNCTION isBugCondition_1(X)
  INPUT: X of type PublishRequest
  OUTPUT: boolean

  RETURN is_vllm_record(X.record) AND |archs(X.record)| > 1
END FUNCTION
```

```pascal
// Property: Fix Checking - one create per distinct component identity
FOR ALL X WHERE isBugCondition_1(X) DO
  calls ← createComponentVersionCalls(publish'(X))
  ASSERT |calls| = |archs(X.record)|
  ASSERT allDistinct({ (c.name, c.version) FOR c IN calls })
  FOR ALL a IN archs(X.record) DO
    c ← the call WHERE c.name = derive_vllm_component_name(X.record) + "-" + suffix(a)
    ASSERT c EXISTS
    ASSERT c.recipe.DefaultConfiguration.supported_architectures = [a]
    ASSERT c.recipe.ComponentDependencies CONTAINS localServerVariant(a)
    ASSERT c.name MATCHES jetpackToken(a)      // frontend inference still works
    ASSERT c.name STARTSWITH "model-vllm-"     // gate discriminator preserved
    ASSERT isValidGreengrassComponentName(c.name)
  END FOR
END FOR
```

#### Defect 2 — rollback denied, orphan survives

```pascal
FUNCTION isBugCondition_2(X)
  INPUT: X of type FailedVllmPublishAttempt
  OUTPUT: boolean

  // Any vLLM publish attempt that created >= 1 component version and then
  // hit the atomicity gate.
  RETURN is_vllm_record(X.record)
     AND |X.createdArns| > 0
     AND anyTargetFailed(X)
END FUNCTION
```

```pascal
// Property: Fix Checking - rollback is authorized and leaves no orphans
FOR ALL X WHERE isBugCondition_2(X) DO
  ASSERT iamPolicy'(portalLambdaRole) ALLOWS greengrass:DeleteComponent
                                      ON greengrassComponentsArn
  FOR ALL arn IN X.createdArns DO
    ASSERT deleteComponentAttempted(rollback'(X), arn)
  END FOR
  ASSERT rollback'(X) RAISES nothing   // best effort preserved
END FOR
```

#### Defect 3 — version derivation blind to cloud-side state

```pascal
FUNCTION isBugCondition_3(X)
  INPUT: X of type (record, componentName, existingCloudVersions)
  OUTPUT: boolean

  // The record's own publish history does not cover what exists cloud-side:
  // classically, an orphan from a failed attempt with a null history.
  RETURN is_vllm_record(X.record)
     AND EXISTS v IN X.existingCloudVersions
           WHERE major(v) >= historyDerivedMajor(X.record)
END FUNCTION
```

```pascal
// Property: Fix Checking - derived version dominates everything that exists
FOR ALL X WHERE isBugCondition_3(X) DO
  next ← next_vllm_component_version'(X.componentName, X.existingCloudVersions)
  ASSERT next MATCHES "^\d+\.0\.0$"
  FOR ALL v IN X.existingCloudVersions DO
    ASSERT major(next) > major(v)
  END FOR
END FOR
```

#### Defect 4 — JP7 target unmapped, fail-closed resolver bypassed

```pascal
FUNCTION isBugCondition_4(X)
  INPUT: X of type RecipeGeneration          // (record, arch, target)
  OUTPUT: boolean

  // A packaging target that packaging.VLLM_ARCH_TO_TARGET can produce but
  // that greengrass_publish.py does not map in BOTH module-level maps.
  // Classically jetson-xavier-jp7: TARGET_TO_PLATFORM.get(target, 'amd64')
  // yields 'amd64', which satisfies resolve_local_server_component's amd64
  // branch, so it never fails closed.
  RETURN X.target IN values(packaging.VLLM_ARCH_TO_TARGET)
     AND (X.target NOT IN keys(TARGET_TO_LOCAL_SERVER)
          OR X.target NOT IN keys(TARGET_TO_PLATFORM))
END FUNCTION
```

```pascal
// Property: Fix Checking - recipe platform and LocalServer dependency both
// correspond to the architecture the component is for
FOR ALL X WHERE is_vllm_record(X.record) DO
  FOR ALL a IN archs(X.record) DO
    t      ← target(a)
    recipe ← generate_vllm_component_recipe'(X.record, a, t)
    ASSERT t IN keys(TARGET_TO_LOCAL_SERVER')
    ASSERT t IN keys(TARGET_TO_PLATFORM')
    ASSERT recipe.Manifests[0].Platform.architecture = platformFor(a)
    ASSERT recipe.ComponentDependencies CONTAINS localServerVariant(a)
    ASSERT dependencyType(recipe.ComponentDependencies,
                          localServerVariant(a)) = HARD
  END FOR
END FOR

// Property: Fix Checking - an unmapped target fails closed, never defaults
FOR ALL X WHERE isBugCondition_4(X) DO
  ASSERT generate_vllm_component_recipe'(X.record, X.arch, X.target)
           RAISES PublishError
  ASSERT NOT createComponentVersionAttempted(publish'(X), X.target)
END FOR
```

Where `platformFor(arm64_jp5) = platformFor(arm64_jp6) = platformFor(arm64_jp7)
= 'aarch64'` and `localServerVariant(arm64_jp7) =
'aws.edgeml.dda.LocalServer.arm64JP7'`.

#### Round-trip resolution and the arch gate

```pascal
// Property: Fix Checking - published name resolves back to record and arch
FOR ALL X WHERE is_vllm_record(X.record) DO
  FOR ALL a IN archs(X.record) DO
    name ← derive_vllm_component_name(X.record) + "-" + suffix(a)
    ASSERT load_vllm_model_record'(name) = X.record
    ASSERT vllm_component_architectures'(X.record, name) = [a]
  END FOR
END FOR
```

```pascal
// Property: Fix Checking - per-JetPack arch gate is exact, fail-closed kept
FOR ALL a IN archs(X.record), FOR ALL b IN allArchitectures DO
  manifest ← { name(a): { version: v, architectures: [a] } }
  ASSERT evaluate_vllm_arch_gate(manifest, {d: a}) = []          // compatible
  ASSERT b ≠ a IMPLIES evaluate_vllm_arch_gate(manifest, {d: b}) ≠ []
  ASSERT evaluate_vllm_arch_gate(manifest, {d: NULL}) ≠ []       // fail closed
  ASSERT evaluate_vllm_arch_gate({name(a): {version: v,
                                            architectures: []}},
                                 {d: a}) ≠ []                    // fail closed
END FOR
```

#### Preservation

```pascal
// Property: Preservation Checking
FOR ALL X WHERE NOT (isBugCondition_1(X) OR isBugCondition_2(X)
                     OR isBugCondition_3(X) OR isBugCondition_4(X)) DO
  ASSERT F(X) = F'(X)
END FOR
```

Concretely, preservation covers:

```pascal
// Vision (non-vLLM) publish is byte-for-byte unchanged
FOR ALL X WHERE NOT is_vllm_record(X.record) DO
  ASSERT publish(X) = publish'(X)
END FOR

// Legacy unsuffixed vLLM components keep resolving exactly as before
FOR ALL name WHERE isVllmModelComponent(name)
              AND NOT hasKnownTargetSuffix(name) DO
  ASSERT load_vllm_model_record'(name) = load_vllm_model_record(name)
  ASSERT vllm_component_architectures'(record, name)
       = vllm_component_architectures(record)
END FOR

// Triton model identity is untouched by the component rename
FOR ALL X WHERE is_vllm_record(X.record) DO
  ASSERT prepArgs'(X).model_name = _safe_model_name(X.record.model_name)
END FOR

// Every already-mapped target resolves exactly as before, and a genuinely
// unknown target still fails closed
FOR ALL t IN keys(TARGET_TO_LOCAL_SERVER) DO
  ASSERT TARGET_TO_LOCAL_SERVER'(t) = TARGET_TO_LOCAL_SERVER(t)
  ASSERT TARGET_TO_PLATFORM'(t)     = TARGET_TO_PLATFORM(t)
END FOR
FOR ALL t WHERE t NOT IN keys(TARGET_TO_LOCAL_SERVER')
             AND platformOf(t) ≠ 'amd64' DO
  ASSERT resolve_local_server_component'(t, platformOf(t)) RAISES PublishError
END FOR
```
