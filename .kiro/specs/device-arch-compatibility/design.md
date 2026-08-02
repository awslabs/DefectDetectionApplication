# Design Document

## Overview

This feature closes two gaps in the DDA `Target_Architecture` deployment-gate contract and adds one small deployments-list UX default. It divides into three independent workstreams that share the `Target_Architecture` vocabulary but touch disjoint code:

- **A. Quick-setup architecture onboarding** — the Station Quick Setup installer detects the station's DDA `Target_Architecture` and reports it; the Quick_Setup_Endpoint records it on the portal Devices table when a device completes provisioning, so quick-setup devices are gate-ready without a manual admin step. (Requirements 1, 2)
- **B. Deploy-screen gated-component filtering** — the Create/Revise Deployment screen filters gated components (vLLM model, LLM-bearing workflow, Plugin) by the selected device's recorded `Target_Architecture`, using a pure predicate that mirrors the authoritative backend gate, hiding incompatible components behind an explainable "incompatible" grouping and surfacing now-incompatible pre-loaded components in revise mode. (Requirements 3, 4, 5)
- **C. Deployments list default sort** — the deployments list defaults to newest-first by `creation_timestamp`. (Requirement 6)

The backend deployment gates (`check_vllm_deployment_gate`, `check_plugin_deployment_gates` in `deployments.py`) remain the sole authoritative enforcement. Workstream B is a client-side aid whose predicate is a twin of the backend gate; workstream A only supplies the `Target_Architecture` value the backend gate already reads.

### Design principles

1. **Contained blast radius.** Workstream A stays within the quick-setup feature (`station_install/quick_setup`, `quick_setup.py`, the QuickSetup Lambda's CDK role). Workstream B stays within the deployment frontend. Neither changes the shared use-case config or the backend gate semantics.
2. **Fail closed, consistently.** A null device `Target_Architecture` and an empty component supported set both mean "incompatible" everywhere — in the backend gate, in the client twin, and in the deploy-screen filter.
3. **Backend authoritative, client predictive.** The screen never becomes the enforcement point; it only predicts the backend verdict to reduce confusion, exactly as the existing `vllmArchGate.ts` twin does.
4. **Non-destructive onboarding.** Recording a `Target_Architecture` from quick-setup never blocks provisioning and never removes the manual admin override.

## Architecture

### Workstream A: quick-setup architecture flow

```
Station (run.sh)                         Quick_Setup_Endpoint (quick_setup.py)      Devices table
─────────────────                        ─────────────────────────────────────     ─────────────
detect_target_architecture()
  ├─ aarch64 → L4T release → jp4/jp5/jp6
  └─ x86_64  → NVIDIA GPU?  → x86_64[_nvidia]
        │
        ▼ (on success)
report_status "completed"
  body += "target_architecture": <arch>  ──►  report_status(event)
                                                 1. validate status/secret (unchanged)
                                                 2. conditional UpdateItem → completed
                                                 3. IF arch valid & present:
                                                      UpdateItem(DEVICES_TABLE,       ──► device_id=<name>
                                                        device_id=device_name,             target_architecture
                                                        SET target_architecture,           usecase_id
                                                            usecase_id, updated_at/by)      updated_by="quick-setup"
                                                 4. audit "record_target_architecture"
```

The gate later reads exactly this attribute via `load_device_gate_info` (`dda-portal-devices`, keyed by `device_id` = IoT Thing name).

### Workstream B: deploy-screen filter

```
CreateDeployment.tsx
  selectedDeviceTargetArchs  (from allDevices[].target_architecture)
        │
        ▼
archCompatibility.ts  (new pure module — twin of backend gate)
  classifyGatedComponent(component)      → 'vllm' | 'plugin' | null
  componentSupportedArchs(component)     → string[]   (fail-closed [])
  isArchCompatibleWithAll(supported, deviceArchs) → boolean
  describeIncompatibility(...)           → reason string
        │
        ▼
useMemo partition → { recommended, compatiblePrivate, compatiblePublic,
                      pluginComponents, incompatibleGatedComponents }
        │
        ▼
Component picker excludes incompatible gated components;
"Incompatible for the selected device(s)" ExpandableSection lists them with reasons;
Alert lists any selected device with no recorded architecture.
```

## Components and Interfaces

### A1. Station architecture detection (`station_install/quick_setup/run.sh` + helper)

A new sourced helper `station_install/quick_setup/detect_arch.sh` exposes one pure function so it is unit-testable in isolation from the orchestrator:

```bash
# detect_target_architecture -> prints one of
#   x86_64 | x86_64_nvidia | arm64_jp4 | arm64_jp5 | arm64_jp6
# or prints nothing when undetermined (never fails the caller).
detect_target_architecture() { ... }
```

Detection rules (Requirement 1):

- **aarch64 / arm64** (Req 1.2): derive the JetPack major from the installed L4T release, in priority order, first hit wins:
  1. `/etc/nv_tegra_release` — parse the `# R<major>` token (`R32`→`arm64_jp4`, `R35`→`arm64_jp5`, `R36`→`arm64_jp6`).
  2. `dpkg-query -W -f '${Version}' nvidia-l4t-core` — parse the leading `<major>.` (`32.*`→jp4, `35.*`→jp5, `36.*`→jp6).
  3. Undetermined if neither resolves to a known major.
- **x86_64 / amd64** (Req 1.3): `x86_64_nvidia` when an NVIDIA GPU runtime is detectable (`nvidia-smi` succeeds, or `/proc/driver/nvidia/version` exists, or `lspci` shows an NVIDIA VGA/3D controller), else `x86_64`.
- **Undetermined** (Req 1.4): any architecture not resolvable to the fixed set prints nothing; the caller treats it as "no arch to report" and does not fail.

The detection is read-only (no `apt`, no writes) so it satisfies Req 1.5 (no end-state change). It runs in a new non-fatal step banner in `run.sh` (`▶ STEP: Detect device architecture`) placed after successful provisioning verification and before the completion report, capturing the value into a `DETECTED_ARCH` variable. A detection error is logged as a warning and leaves `DETECTED_ARCH` empty; it never calls `fail_step`.

### A2. Station completion report (`run.sh` `report_status`)

`report_status` gains the detected architecture in the **completed** body only:

```
{"registration_id","report_secret","status":"completed"[,"target_architecture":"<arch>"]}
```

`target_architecture` is included only when `status == "completed"` and `DETECTED_ARCH` is non-empty. The `failed` body is unchanged. The value is a member of the fixed set by construction (detection only ever emits fixed-set values), so no station-side validation is required beyond the emptiness check.

### A3. Quick_Setup_Endpoint recording (`edge-cv-portal/backend/functions/quick_setup.py`)

`report_status(event)` is extended:

- Read optional `target_architecture` from the body.
- Continue the existing status/secret validation and the conditional `UpdateItem` that drives the registration to `completed` **unchanged** — the architecture is never part of that authentication or the registration transition (Req 2.4: a report with no/invalid arch still completes normally).
- **After** the registration transition to `completed` succeeds (the report is thereby authenticated as originating from this registration's Setup_Bundle, Req 2.5), if `target_architecture` is a member of `TARGET_ARCHITECTURES` (a new module constant mirroring `devices.py`), perform a best-effort write to the Devices table:

```python
def _record_target_architecture(device_name, usecase_id, target_architecture, now):
    # Upsert the portal Devices-table item for this device with the reported
    # DDA Target_Architecture. Keyed by device_id = IoT Thing name, matching
    # load_device_gate_info. Never raises: the registration is already completed.
    dynamodb.Table(DEVICES_TABLE).update_item(
        Key={"device_id": device_name},
        UpdateExpression="SET target_architecture = :ta, usecase_id = :uid, "
                         "updated_at = :now, updated_by = :by",
        ExpressionAttributeValues={
            ":ta": target_architecture, ":uid": usecase_id,
            ":now": now, ":by": "quick-setup",
        },
    )
```

- An architecture value outside the fixed set is ignored (not written), and completion handling is otherwise unchanged (Req 2.3).
- On a successful arch write, record an audit event `record_target_architecture` (resource_type `device`, resource_id = device name, details: architecture value + outcome), consistent with the feature's existing audit taxonomy (Req 2.6).
- The write is best-effort: a DynamoDB failure is logged and swallowed so it cannot turn a successful completion into an error (the device can still be corrected manually).

New module-level constants/env in `quick_setup.py`:
- `DEVICES_TABLE = os.environ.get("DEVICES_TABLE")`
- `TARGET_ARCHITECTURES = ("x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6")` (kept identical to `devices.py`).

The manual admin writer `update_device_flags` in `devices.py` is untouched (Req 2.7).

### A4. Infrastructure (`edge-cv-portal/infrastructure/lib/compute-stack.ts`)

The QuickSetup Lambda already exists with a dedicated role. Two additions, both contained to quick-setup:

- Add `DEVICES_TABLE: props.devicesTable.tableName` to the QuickSetup handler environment.
- Grant the QuickSetup Lambda role write access to the Devices table: `props.devicesTable.grantWriteData(quickSetupRole)` (or a narrower `UpdateItem`-only grant). This is the only new permission and is scoped to the single portal table the gate reads.

This is the one net-new IAM permission in the feature; it is created via CDK (IaC), not manually, and is noted in `station_install/quick_setup/README.md`.

### B1. Shared compatibility module (`edge-cv-portal/frontend/src/pages/deployments/archCompatibility.ts`)

A new pure, UI-free module (so it can be exercised by a fast-check property test, mirroring `vllmArchGate.ts`) that unifies the gated-component compatibility contract:

```typescript
export type GatedKind = 'vllm' | 'plugin';

// Which gated class a component belongs to for client-side arch filtering,
// or null when it is not client-side gateable (non-gated, or an LLM-bearing
// workflow component whose has_llm_inference/packaged_architectures markers
// are not exposed to the client — those stay backend-authoritative).
export function classifyGatedComponent(componentName: string): GatedKind | null;

// The component's Supported_Architecture_Set. For plugins: the listing's
// supported_architectures. For vLLM: the resolved set keyed by component
// name (see B4). Unresolvable → [] so the component fails closed (Req 5.2).
export function componentSupportedArchs(
  component: ComponentInfo,
  vllmArchs: Record<string, string[]>
): string[];

// Exact-name membership, null device arch and empty supported both false
// (fail closed) — the same predicate as evaluate_vllm_arch_gate /
// evaluate_plugin_arch_gate (Req 5.1).
export function isArchCompatible(deviceArch: string | null, supported: string[]): boolean;

// Compatible iff compatible with EVERY selected device arch (Req 3.2).
export function isCompatibleWithAllDevices(
  supported: string[],
  deviceArchs: (string | null)[]
): boolean;

// Human-readable exclusion reason (device arch(s) vs supported set),
// reusing describeVllmArchEntry / describeArchUnsupported phrasing (Req 3.4).
export function describeArchIncompatibility(
  component: ComponentInfo, deviceArchs: Record<string, string | null>,
  supported: string[]
): string;
```

The predicate is intentionally identical in shape to the backend `evaluate_vllm_arch_gate` (exact-name `Set.has`, no cross-arch fallback, fail-closed on null/empty), satisfying Requirement 5.1.

### B2. Create/Revise Deployment filter integration (`CreateDeployment.tsx`)

The existing partition `useMemo` (`recommendedComponents / compatiblePrivate / compatiblePublic / incompatibleComponents / pluginComponents`) is extended:

- Compute `selectedDeviceTargetArchs: Record<deviceId, string | null>` from `allDevices[].target_architecture` for the selected device targets (mirrors the existing `vllmArchWarnings` device-arch map). Only for `targetType === 'devices'`.
- Compute `devicesWithoutRecordedArch: string[]` — selected devices whose `target_architecture` is null (Req 3.5).
- A single `archIncompatReason(comp)` helper returns a reason string (or null) from two sources of truth:
  - **Gated** components (`classifyGatedComponent` non-null): `isCompatibleWithAllDevices(componentSupportedArchs(...), archs)` against the recorded supported set, failing closed on a null device arch (Req 3.1, 3.2, 3.5).
  - **Non-gated** components: the JetPack major inferred from the component name via `inferComponentTargetArchs` (the DDA `-jp5`/`-jp6`/`arm64JP6` naming convention). The backend does not arch-gate these, but a `jp5` build must not be offered for a `jp6` device — both report the coarse `arm64` platform, so the coarse filter alone cannot catch it (Req 3.9). A name with no JetPack token yields `[]` (kept), and a device with no recorded arch is not judged by name (Req 3.10).
- Compatible → remains in its normal list/tab. Incompatible (either source) → collected into `incompatibleGatedComponents` with the reason and **excluded from the addable options** (Req 3.3, 3.4).
- The coarse `isCompatibleWithDevice` arm64/amd64 check still applies to non-gated components with no JetPack token (an x86 build on an arm device); a JetPack-major mismatch is caught by the name inference instead.
- **No device selected** (`targetType !== 'devices'` or empty selection): gated-arch filtering is skipped entirely — the full catalog stays discoverable (Req 5.4).
- **Group targets** (`targetType === 'group'`): per-member `target_architecture` is not resolvable on this screen, so gated-arch filtering is skipped and gated components are not hidden (Req 3.7); the backend gate remains authoritative on submit.

UI additions (Cloudscape, consistent with the existing plugin/vLLM alerts already in the file):
- An `ExpandableSection` titled "Incompatible with the selected device(s)" listing each excluded gated component with its `describeArchIncompatibility` reason (Req 3.4). Rendered only when non-empty.
- An `Alert type="warning"` shown when `devicesWithoutRecordedArch` is non-empty, naming those devices and stating that their DDA architecture must be recorded (via the device's Target Architecture editor) before gated components can be deployed (Req 3.5). This is the exact situation quick-setup Workstream A prevents for new devices.

### B3. Revise-mode surfacing (`CreateDeployment.tsx`)

When revise mode pre-loads `existingDeployment.components` (via `preloadExistingComponents`), a derived set flags any pre-loaded **gated** component that is arch-incompatible with the target device's recorded architecture. In the selected-components table these rows render with a warning `StatusIndicator`/badge and the `describeArchIncompatibility` reason (Req 4.1, 4.2). The component is **not** removed from the pre-loaded set and the user may still remove it or proceed (Req 4.3) — surfacing only, never blocking.

### B4. vLLM supported-set resolution for the catalog

Today the screen resolves vLLM supported architectures only for **selected** components (the `vllmComponentArchs` effect via `apiService.getModel(training_job_id)`). To filter the **catalog**, resolution is extended to cover catalog `model-vllm-*` components as well:

- Broaden the existing resolution effect's `pending` set to include catalog vLLM components (from `allPrivateComponents` + `allPublicComponents`), not just `selectedComponents`, keyed by component name, deduped against already-resolved entries.
- Components still resolving contribute an *undefined* supported set; consistent with the existing warning logic, a gated vLLM component whose set is still resolving is **not** hidden until its set resolves (avoids flicker), then filtered on the next render. An unresolvable record resolves to `[]` → fail closed (Req 5.2).

This keeps Workstream B fully client-side (no backend change), consistent with the "keep contained" constraint and the existing pattern in the file. (Alternative considered: enrich the backend component listing with `supported_architectures` for vLLM components as it already does for plugins — rejected for this iteration to avoid a backend change, but noted as a future simplification.)

### C1. Deployments list default sort (`Deployments.tsx`)

`useTableSort` already accepts a `defaults` argument. The only change:

```typescript
const { items: sortedDeployments, sortingProps } = useTableSort(filteredDeployments, {
  sortingColumn: { sortingField: 'creation_timestamp' },
  sortingDescending: true,
});
```

- The `created` column already declares `sortingField: 'creation_timestamp'`; the hook's default comparator `Date.parse`-es the ISO timestamp and the `sortingDescending: true` default reverses ascending order → newest first (Req 6.1).
- Null/unparseable `creation_timestamp` sorts to the front ascending, i.e. **last** after the descending reverse (Req 6.3) — items are never dropped.
- A stable tie-break is added to the comparator path via a secondary key (`deployment_id`) so equal timestamps keep a deterministic order across loads (Req 6.2). This is done by giving the `created` column a `sortingComparator` (timestamp desc-friendly ascending compare, then `deployment_id`) instead of a bare `sortingField`, so the tie-break is deterministic under the hook's reverse.
- Because the default lives in `useState` initial values, the first user click on any column header overrides it for the session (Req 6.4).

## Data Models

No new tables or schemas. The only persisted change is the existing `target_architecture` string attribute on the `dda-portal-devices` item, now also written by the quick-setup completion path (previously only by `devices.py update_device_flags`). Value domain is unchanged: `{x86_64, x86_64_nvidia, arm64_jp4, arm64_jp5, arm64_jp6}`.

The quick-setup status-report request body gains one optional field:

| Field | Type | Notes |
|-------|------|-------|
| `target_architecture` | string, optional | Present only on `completed` reports when detected; must be in the fixed set or it is ignored. |

## Error Handling

| Condition | Handling | Requirement |
|-----------|----------|-------------|
| Station cannot determine arch | `DETECTED_ARCH` empty; no field reported; provisioning still succeeds | 1.4, 2.4 |
| Report includes invalid arch value | Backend ignores it; registration still completes | 2.3 |
| Devices-table write fails | Logged, swallowed; completion still returns 200 | 2 (non-destructive) |
| Unauthenticated/mismatched report | Existing uniform rejection; no arch write occurs (write is after the authenticated transition) | 2.5 |
| Device has null recorded arch on deploy screen | All gated components incompatible; warning alert names the device(s) | 3.5 |
| vLLM supported set unresolvable | Treated as `[]` → component hidden as incompatible | 5.2 |
| Group target (member arch unknown) | Gated-arch filtering skipped; backend gate authoritative | 3.7 |
| Deployment has null `creation_timestamp` | Sorted last; not dropped | 6.3 |

## Correctness Properties

These are the invariants the property-based tests assert. Each is a pure, deterministic property over generated inputs.

### Property 1: Fail-closed on null device arch

For any component supported set, `isArchCompatible(null, supported)` is `false`.

**Validates: Requirements 3.5, 5.1**

### Property 2: Fail-closed on empty supported set

For any device arch, `isArchCompatible(arch, [])` is `false`.

**Validates: Requirements 5.1, 5.2**

### Property 3: Exact-name membership, no fallback

`isArchCompatible(arch, supported)` is `true` iff `supported` contains `arch` by exact string match; no arm64↔arm64_jp6 or cross-arch coercion.

**Validates: Requirements 5.1**

### Property 4: All-devices conjunction

`isCompatibleWithAllDevices(supported, archs)` is `true` iff `isArchCompatible(a, supported)` holds for every `a` in `archs`; an empty device list yields `true` (no constraint).

**Validates: Requirements 3.2, 5.4**

### Property 5: Twin equivalence with the backend gate

For any manifests and device-arch map, the set of (component, device) pairs the client predicate marks incompatible equals the set `evaluate_vllm_arch_gate` would return, restricted to client-classifiable gated components.

**Validates: Requirements 5.1**

### Property 6: Non-gated exemption

For any component where `classifyGatedComponent` returns `null`, the arch filter never marks it incompatible regardless of device arch.

**Validates: Requirements 3.6, 5.3**

### Property 7: Fixed-set write gate (backend)

`report_status` writes `target_architecture` to the Devices table iff the reported value is a member of the fixed set, and the reached registration status is `completed` independent of the arch value's validity or presence.

**Validates: Requirements 2.2, 2.3, 2.4**

### Property 8: Detection range (station)

`detect_target_architecture` prints either a member of the fixed set or the empty string, for any fixture inputs.

**Validates: Requirements 1.1, 1.4**

### Property 9: Sort placement of missing dates

For any list of deployments, the default sort places every item with an unparseable/absent `creation_timestamp` after every item with a parseable one, and never drops an item.

**Validates: Requirements 6.1, 6.3**

## Testing Strategy

### Property-based tests (mirroring the existing gate-twin test conventions)

- **`archCompatibility.ts` twin equivalence (fast-check).** For arbitrary component supported sets and device arch maps, assert `isArchCompatible` / `isCompatibleWithAllDevices` agree with the backend `evaluate_vllm_arch_gate` semantics: exact-name membership, null-device fail-closed, empty-supported fail-closed, all-devices-must-match. Complements the existing `vllmArchGate.ts` property test.
- **Backend `TARGET_ARCHITECTURES` validation (hypothesis).** For arbitrary strings, `report_status` writes the Devices table iff the reported value is in the fixed set, and never writes for out-of-set values; completion status is reached regardless.

### Unit / integration tests

- **Shell (`detect_arch.sh`).** Table-driven tests over faked `/etc/nv_tegra_release` / `dpkg` / GPU-probe fixtures → expected fixed-set value or empty. Added to the existing quick-setup shell test suite.
- **Backend `report_status`.** (a) completed report with valid arch → Devices-table `UpdateItem` with the value + audit event; (b) completed report with invalid arch → no write, still completed; (c) completed report with no arch → no write, still completed; (d) Devices-table failure → still 200; (e) unauthenticated report → no write.
- **Frontend `CreateDeployment`.** (a) incompatible gated component excluded from options and listed in the incompatible section with a reason; (b) null-arch device → warning alert + all gated components hidden; (c) non-gated component never hidden by arch; (d) no-device / group target → no gated filtering; (e) revise mode surfaces a now-incompatible pre-loaded component without dropping it.
- **Frontend `Deployments`.** Default render orders by `creation_timestamp` desc with a deterministic tie-break; a null-timestamp row sorts last; a header click overrides the default.

### Regression

- Existing quick-setup shell/backend tests, the existing vLLM/plugin gate twin tests, and the existing `CreateDeployment` tests must remain green; the backend gate functions are unmodified.
