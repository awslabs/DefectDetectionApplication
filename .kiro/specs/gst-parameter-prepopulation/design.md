# Design Document: GStreamer Parameter Pre-population

## Overview

This feature removes the hand-typing from the Node_Designer's Parameters wizard step by capturing each plugin's real GObject property metadata (the `gst-inspect-1.0` view: name, GType, default, flags, ranges, enum values, blurb) from the built binary and pre-populating the parameter declaration list with correctly typed Parameter_Suggestions.

The pipeline is: **capture at build time → store with the artifact → serve mapped suggestions on demand → merge in the wizard without overwriting user work**.

- **Capture** happens inside the existing x86_64 CodeBuild plugin build (`dda-plugin-build`): after the artifact is promoted, a new Python GI script loads the freshly built `.so` in the build container's GStreamer runtime and emits an Introspection_Report JSON, uploaded next to the artifact (Requirement 1). Capture is best-effort — a failed introspection never fails a successful build (1.4).
- **Storage** is an S3 object next to the promoted `.so` plus a small status stanza on the Plugin_Record's x86_64 artifact entry, following the existing `s3Key`/`signature` artifact-entry pattern.
- **Serving** is a new `GET /plugins/{id}/versions/{v}/gst-properties` route on the existing `plugin_records.py` Lambda. It loads the report, filters Base_Class_Properties (Requirement 4), applies the pure Type_Mapping (Requirements 2, 3), and returns per-element Parameter_Suggestions in the exact `ParameterDeclaration` wire shape `declaration.ts` and `catalog.custom` already validate.
- **The wizards** share a new scan module (`scan.ts`) with pure merge logic (Requirement 6) and a scan panel used by the Parameters step. The Registration_Wizard auto-scans on reaching the step when the list is empty and offers manual refresh (Requirement 5); the Create_Wizard shows the scan-requires-a-build notice since no Plugin_Artifact exists yet (5.6). Every degraded case keeps the manual flow untouched (Requirement 7).

### Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Where introspection runs | **At build time in the x86_64 CodeBuild container** (extend `dda-plugin-build` + `Dockerfile.x86_64`), not on-demand in the Fargate simulate sandbox | Evaluated both (see below). Build-time capture gives the wizard instant data (S3 read, no cold start), reuses the container that just built the `.so` with a matching GStreamer 1.20 runtime, adds zero new infrastructure, and naturally versions the report with the artifact. The Fargate sandbox route would cost a 1–2 minute task cold start *per wizard visit*, a new state-machine/Lambda orchestration path, and per-scan Fargate spend — bad UX for a step the user reaches interactively |
| Introspection arch | x86_64 only | GObject property declarations are made in element class-init code and are architecture-independent; the x86_64 build is the gating artifact everywhere else in the designer (simulator, registration guard), so it is the single source of truth. Per-arch capture would add JetPack GI tooling to four more images for no informational gain |
| Introspection mechanism | A JSON-emitting **Python GI script** (`dda-gst-introspect`) shipped in the build image, run with `GST_PLUGIN_PATH` pointed at the build output; not text-parsing `gst-inspect-1.0` output | `Gst.Registry` + `GObject.list_properties()` yields structured pspec data (owner type, ranges, enum values) directly; parsing `gst-inspect-1.0`'s human-oriented text is fragile across versions. Requires adding `python3-gi` + `gir1.2-gstreamer-1.0` to `Dockerfile.x86_64` only |
| Report storage | S3 object `{artifact}.gstinspect.json` next to the promoted `.so`; artifact entry gains `gstIntrospection: {status, s3Key?, message?, gstVersion?, capturedAt}` | Follows the artifact-adjacent `.sig` pattern; keeps the Plugin_Record item small; the serving Lambda already has read access to the Plugin_Library custom prefix |
| Where Type_Mapping runs | Server-side pure module `gst_properties.py` (Lambda), returning ready-to-use `ParameterDeclaration` wire shapes | One implementation validated by hypothesis property tests against the same rules `catalog.custom` enforces; the frontend only converts wire shape → form rows and merges, keeping its logic trivially fast-check-testable |
| Base-class filtering rule | A property is a Base_Class_Property iff its pspec owner GType differs from the element's own GType (recorded at capture time as `owner` per property; filtering decided at serve time) | `g_param_spec` ownership is the ground truth GStreamer itself uses; overridden properties get the subclass as owner, which satisfies Requirement 4.2 automatically. Recording the raw owner (not a boolean only) keeps the report re-interpretable if the rule evolves |
| Required heuristic | Required ⇔ no usable default: string default `NULL`/empty, or default unconvertible to the mapped paramType; everything else optional with the default carried | GObject properties always technically have defaults, so "has a meaningful default" is the only reliable signal (Requirement 3); the user can flip the flag — pre-population is an aid, not a lock (3.3) |
| Merge semantics | Pure function over form rows: existing rows always win, new names append, name collisions reported as `alreadyDeclared` | "No silent overwrite" (Requirement 6) reduced to a total, order-preserving, idempotent merge that is easy to property-test |

### Evaluated Alternative: On-demand Introspection in the Plugin_Simulator Sandbox

The Fargate simulate sandbox (`HARNESS_MODE=simulate`, `node-designer-stack.ts`) already stages a plugin's x86_64 `.so` and has GStreamer + Python GI, so a `HARNESS_MODE=introspect` mode was considered:

| | Build-time CodeBuild capture (chosen) | On-demand Fargate sandbox |
|---|---|---|
| Wizard latency | ~0 (S3 read of stored report) | 1–2 min Fargate cold start per scan |
| New infrastructure | None (script + package in one Dockerfile) | New state-machine branch or ECS RunTask path, run tracking, polling API |
| Cost | Seconds inside an already-running build | A Fargate task per scan |
| Freshness | Report always matches the exact artifact bytes just built | Same artifact, no freshness gain |
| Existing-plugin backfill | Old builds lack reports until rebuilt (handled as scan-unavailable, Requirement 7.4) | Could introspect old artifacts on demand |

The only advantage of the sandbox route is backfilling pre-existing builds, which Requirement 7.4 explicitly handles as a degraded case (rebuild to get a report). Chosen: build-time capture.

## Architecture

```mermaid
graph TB
    subgraph "x86_64 CodeBuild (dda-plugin-build)"
        BUILD[meson/autotools build] --> PROMOTE[promote .so to Plugin_Library]
        PROMOTE --> INTRO[dda-gst-introspect<br/>Python GI, GST_PLUGIN_PATH=build output]
        INTRO -->|best effort| RPT[upload {plugin}.so.gstinspect.json<br/>next to the artifact]
    end
    RPT --> S3[(Portal S3<br/>Plugin_Library custom prefix)]
    EVT[EventBridge build result] --> PB[plugin_builds.py<br/>SUCCEEDED path]
    PB -->|record gstIntrospection stanza<br/>on the x86_64 artifact entry| DDB[(PluginRecords)]
    PB -.->|validates report exists,<br/>size + JSON shape| S3

    subgraph "Serving"
        API[GET /plugins/id/versions/v/gst-properties<br/>plugin_records.py] --> DDB
        API --> S3
        API --> MAP[gst_properties.py<br/>pure: filter base-class,<br/>Type_Mapping, required heuristic]
    end

    subgraph "Frontend Parameters_Step"
        RW[RegistrationWizard] --> PANEL[ParameterScanPanel + useParameterScan]
        CW[CreateWizard] --> PANEL
        PANEL --> APIC[nodeDesignerApi.getGstProperties]
        APIC --> API
        PANEL --> MERGE[scan.ts pure merge:<br/>existing rows win, new append]
    end
```

Flow summary:

1. `dda-plugin-build` (x86_64 project only) runs `dda-gst-introspect <build-output-dir>` after artifact promotion. The script writes `report.json`; the build script uploads it as `{PLUGIN_NAME}.so.gstinspect.json` under the same Plugin_Library custom prefix. Any introspection failure logs a warning and uploads a `{"status":"failed",...}` report — the build result is untouched (1.4).
2. `plugin_builds.py`'s SUCCEEDED handler (the same code path that re-checksums and re-signs the promoted artifact) heads the report object, parses and bounds-checks it, and records `gstIntrospection` on the x86_64 artifact entry: `{status: "captured", s3Key, gstVersion, capturedAt}` or `{status: "failed"|"missing", message}`.
3. `GET /plugins/{id}/versions/{v}/gst-properties` (node-designer read permission, same RBAC/404 conventions as the other plugin routes) resolves the artifact entry and either returns `{available: false, reason}` — distinguishing `no_x86_64_build` / `introspection_failed` / `not_captured` (1.6, 7.4, 8.3) — or loads the report from S3 and returns it with derived suggestions per element: base-class-filtered (4.1), type-mapped (2.x), required-classified (3.x), plus the skipped list with reasons (2.5).
4. The wizards' Parameters step renders `ParameterScanPanel`. The Registration_Wizard fetches on step entry; if the report is available and the parameter list is empty it merges automatically (5.1); a "Scan plugin properties" button re-runs on demand (5.2); a factory selector appears when the report has multiple elements, defaulting to the wizard's element factory (5.4). The Create_Wizard passes no plugin context, so the panel renders the static "scanning requires a built plugin" notice (5.6). All scan states are additive UI — the Add/edit/remove controls and step navigation never depend on scan state (5.5, 7.x).

## Components and Interfaces

### 1. `plugin-build-images/dda-gst-introspect` (new script, shipped in `Dockerfile.x86_64`)

Python 3 GI script; stdout is exactly one Introspection_Report JSON document.

```
usage: dda-gst-introspect <plugin-scan-dir> [plugin-file-basename]
```

- Sets `GST_PLUGIN_PATH` to `<plugin-scan-dir>`, calls `Gst.init`, then enumerates element factories whose plugin filename lives under the scan dir (so only the freshly built plugin's elements are reported, never the distro's).
- For each factory: `factory.load()`, create the element (`Gst.ElementFactory.make`), and for every `GObject.list_properties()` pspec record:
  `name`, `gtype` (`pspec.value_type.name`, e.g. `gint`, `gchararray`, or the GEnum type name), `owner` (`pspec.owner_type.name`), `writable` (`GObject.ParamFlags.WRITABLE`), `blurb`, `default` (from `pspec.default_value`, JSON-scalar or null), `min`/`max` for ranged numeric pspecs, and `enumValues: [{value, nick}]` for GEnum pspecs.
- Elements that cannot be instantiated are recorded with `instantiationError` and an empty property list; a scan dir registering no factories yields `status: "failed"` with a diagnostic (1.4).
- Never exits non-zero for content problems: content failures are encoded in the document so the calling build script stays a dumb pipe.

`Dockerfile.x86_64` additions: `python3-gi`, `gir1.2-gstreamer-1.0` (runtime GI bindings; the GStreamer runtime libraries are already present via the dev packages).

### 2. `dda-plugin-build` (extended, x86_64 behavior)

After the promote step, when `TARGET_ARCH=x86_64`:

```sh
dda-gst-introspect "$PROMOTE_STAGE_DIR" "$PRIMARY_SO_BASENAME" > report.json || echo '{"status":"failed",...}' > report.json
aws s3 cp report.json "s3://$ARTIFACTS_BUCKET/$PLUGIN_LIBRARY_CUSTOM_PREFIX/$USECASE_ID/x86_64/$PLUGIN_NAME.so.gstinspect.json"
```

Both steps are wrapped so no failure propagates to the build exit code (1.4). Non-x86_64 architectures are untouched.

### 3. `plugin_builds.py` (extended SUCCEEDED path)

`record_promoted_artifact` (the existing re-checksum/re-sign step) additionally:

- `get_object` of the report key; enforce a size cap (256 KiB) and `json.loads` + shape check via the pure validator (component 4).
- Writes the artifact-entry stanza:
  - parseable report with `status: "captured"` → `gstIntrospection: {status: "captured", s3Key, gstVersion, capturedAt}`
  - report with `status: "failed"` → `{status: "failed", message}`
  - missing/oversized/malformed object → `{status: "failed", message: <diagnostic>}` (8.3 handled at write time too)
- Never alters the build status (1.4).

### 4. `backend/functions/gst_properties.py` (new pure module + route logic)

Pure functions (no boto3 at module scope; hypothesis-testable):

```python
GTYPE_INT = {"gint", "guint", "gint64", "guint64", "glong", "gulong", "guchar"}
GTYPE_FLOAT = {"gfloat", "gdouble"}

def parse_report(document: Any) -> Report            # 8.1, 8.3 — raises ReportError on malformed input
def serialize_report(report: Report) -> dict          # 8.1, 8.2 — inverse of parse_report
def is_base_class_property(prop, element_gtype) -> bool   # 4.1, 4.2: prop.owner != element_gtype
def map_property(prop) -> Suggestion | Skipped        # 2.1–2.6, 3.1, 3.2
def suggestions_for_element(element) -> {suggestions: [ParameterDeclaration], skipped: [{name, reason}]}
```

`map_property` rules (Requirements 2, 3):

| GType | paramType | constraints | notes |
|---|---|---|---|
| gint, guint, gint64, guint64, glong, gulong, guchar | `int` | `{min, max}` when ranged | default carried as int |
| gfloat, gdouble | `float` | `{min, max}` when ranged | default carried as float |
| gboolean | `bool` | — | default carried as bool |
| gchararray | `string` | — | `NULL`/empty default ⇒ required, no default (3.1) |
| GEnum (`enumValues` present) | `enum` | `{values: [nicks]}` | default carried as the default's nick |
| anything else, or `writable: false` | **skipped** | — | `{name, reason}` in the skipped list (2.5) |

- `description` = blurb, else `"<name> (<gtype>) property of the plugin element"` (2.4).
- `examples` = `[default]` when a usable default exists, else a type-appropriate synthesized example (`min` for ranged ints/floats, first enum nick, `"value"` for required strings) so every suggestion satisfies `parametersStepErrors` and `catalog.custom` (2.6).
- `required` = true iff no usable default (3.1); otherwise `required: false` and `default` set (3.2).

Route handler (registered in `plugin_records.py`'s router):

```
GET /plugins/{id}/versions/{v}/gst-properties
  → 404 (uniform, cross-tenant safe) when record/read-permission missing
  → 200 {available: false, reason: "no_x86_64_build" | "not_captured" | "introspection_failed",
         message?} (1.6, 7.1, 7.2, 7.4, 8.3)
  → 200 {available: true, gstVersion, capturedAt,
         elements: [{factory, suggestions: [ParameterDeclaration...],
                     skipped: [{name, reason}...]}]}   (1.5)
```

Malformed stored JSON maps to `introspection_failed` (8.3), never a 500.

### 5. `frontend/src/pages/node-designer/scan.ts` (new pure module)

```ts
export interface ScanElement { factory: string; suggestions: ParameterDeclaration[];
                               skipped: { name: string; reason: string }[]; }
export interface GstPropertiesResponse { available: boolean; reason?: string; message?: string;
                                         elements?: ScanElement[]; }

/** ParameterDeclaration wire shape -> ParameterForm raw-text rows. */
export function formFromSuggestion(s: ParameterDeclaration): ParameterForm;

/** Requirement 6: existing rows unchanged, new names appended, collisions reported. */
export function mergeSuggestions(existing: ParameterForm[], suggestions: ParameterDeclaration[]):
  { parameters: ParameterForm[]; added: string[]; alreadyDeclared: string[] };

/** 5.4: pick the element whose factory matches, else the single element, else null (user chooses). */
export function pickElement(elements: ScanElement[], preferredFactory?: string): ScanElement | null;
```

Name matching is exact on the trimmed parameter name (declaration names are launch-safe identifiers already).

### 6. `frontend/src/pages/node-designer/ParameterScanPanel.tsx` (new component) + wizard wiring

Props: `{ pluginId?: string; version?: number; preferredFactory?: string; parameters: ParameterForm[]; onMerge(result): void }`.

- **No plugin context** (Create_Wizard): renders a static info `Alert` — "Property scanning pre-populates this list from the built plugin. This plugin has not been built yet; declare parameters manually, or rescan from the registration wizard after the first successful build." (5.6, 7.1).
- **With plugin context** (Registration_Wizard): on mount fetches `nodeDesignerApi.getGstProperties(pluginId, version)`;
  - `available: false` → informational (`no_x86_64_build` / `not_captured`) or error (`introspection_failed` with message) Alert; manual flow untouched (7.1, 7.2, 7.4);
  - fetch failure → error Alert, manual flow untouched (7.3);
  - available + empty parameter list → auto-merge once (5.1); otherwise wait for the user;
  - "Scan plugin properties" button re-runs the merge any time (5.2);
  - multi-element reports render a factory `Select`, pre-selected via `pickElement` with the wizard's default element factory (5.4);
  - after a merge: outcome summary "Added N parameters from `<factory>`; M already declared: …; K skipped: name (reason)…" (5.3, 6.3, 2.5);
  - scanned-row provenance: the panel reports `added` names up to the wizard, which tags those rows with a "from scan" badge until the row is edited (6.4).
- The panel is purely additive: it renders above the existing Add-parameter UI, never disables it, and step gating (`parametersStepErrors`) is unchanged (5.5).

`RegistrationWizard.tsx` embeds the panel in the Parameters step with `plugin.plugin_id`, `plugin.version`, and `defaultElementFactory(plugin.name)`; `CreateWizard.tsx` embeds it without plugin context.

### 7. `frontend/src/pages/node-designer/api.ts` (extended)

```ts
getGstProperties(pluginId: string, version: number): Promise<GstPropertiesResponse>
// GET /plugins/{pluginId}/versions/{version}/gst-properties
```

## Data Models

### Introspection_Report (S3 JSON, version 1)

```json
{
  "reportVersion": 1,
  "status": "captured",
  "message": null,
  "gstVersion": "1.20.3",
  "capturedAt": "2026-02-14T12:00:00Z",
  "elements": [
    {
      "factory": "myblur",
      "elementGType": "GstMyBlur",
      "instantiationError": null,
      "properties": [
        { "name": "radius", "gtype": "gint", "owner": "GstMyBlur", "writable": true,
          "blurb": "Blur radius in pixels", "default": 5, "min": 0, "max": 100,
          "enumValues": null },
        { "name": "mode", "gtype": "GstMyBlurMode", "owner": "GstMyBlur", "writable": true,
          "blurb": "Blur mode", "default": "gaussian", "min": null, "max": null,
          "enumValues": [ { "value": 0, "nick": "gaussian" }, { "value": 1, "nick": "box" } ] },
        { "name": "name", "gtype": "gchararray", "owner": "GstObject", "writable": true,
          "blurb": "The name of the object", "default": null, "min": null, "max": null,
          "enumValues": null }
      ]
    }
  ]
}
```

Failed capture: `{"reportVersion": 1, "status": "failed", "message": "<diagnostic>", "elements": []}`.

### Plugin_Record x86_64 artifact-entry addition

```json
"artifacts": {
  "x86_64": {
    "buildStatus": "succeeded", "s3Key": "...", "checksum": "...", "signature": "...",
    "gstIntrospection": {
      "status": "captured",
      "s3Key": "workflow-plugins/custom/{usecase}/x86_64/{plugin}.so.gstinspect.json",
      "gstVersion": "1.20.3",
      "capturedAt": "2026-02-14T12:00:00Z"
    }
  }
}
```

`status` ∈ `captured | failed`; absent stanza = build predates this feature (`not_captured`, 7.4).

### Parameter_Suggestion (API response entry)

Exactly the `ParameterDeclaration` wire shape (`declaration.ts` / `catalog.custom`):

```json
{ "name": "radius", "paramType": "int", "required": false, "default": 5,
  "constraints": { "min": 0, "max": 100 },
  "description": "Blur radius in pixels", "examples": [5] }
```

### ParameterForm mapping (frontend, `formFromSuggestion`)

| ParameterDeclaration | ParameterForm (raw text) |
|---|---|
| `name` | `name` |
| `paramType` | `paramType` |
| `required` | `required` |
| `default` (absent → `''`) | `defaultValue` = `String(default)` |
| `description` | `description` |
| `examples[0]` | `example` = `String(examples[0])` |
| `constraints.values` (enum) | `enumValues` = values joined with `', '` |

Numeric `min`/`max` constraints ride along in the assembled declaration on submit (the form keeps them attached to the row so `buildRegistrationDeclaration` re-emits them); they have no editable UI in this feature.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Introspection report round-trip

*For any* valid Introspection_Report structure, `parse_report(serialize_report(report))` SHALL produce an equivalent report, and `serialize_report` output SHALL survive a JSON dump/load cycle unchanged.

**Validates: Requirements 8.1, 8.2**

### Property 2: Malformed report documents are rejected, not crashed on

*For any* arbitrary JSON value that does not satisfy the Introspection_Report shape, `parse_report` SHALL raise `ReportError` (and the route SHALL therefore answer with the `introspection_failed` unavailability reason), never an unhandled exception.

**Validates: Requirements 8.3, 1.6**

### Property 3: Type mapping is total and correctly typed over writable known GTypes

*For any* generated GStreamer_Property whose GType is in the known mapping set and which is writable, `map_property` SHALL produce a Parameter_Suggestion whose `paramType` matches the GType class per the mapping table (int/float/bool/string/enum), whose `constraints` carry the property's min/max when the property is ranged and the enum nicks when it is a GEnum, and whose `default`, when present, is convertible to the mapped paramType.

**Validates: Requirements 2.1, 2.2, 2.3**

### Property 4: Unknown or non-writable properties are always skipped with a reason

*For any* generated GStreamer_Property that has an unmapped GType or is not writable, `map_property` SHALL yield a skipped entry carrying the property name and a non-empty reason, and SHALL yield no Parameter_Suggestion.

**Validates: Requirements 2.5**

### Property 5: Every suggestion passes declaration validation

*For any* generated GStreamer_Property that maps to a Parameter_Suggestion, the suggestion SHALL satisfy the declaration validation rules: non-empty name, non-empty description (blurb or synthesized fallback), at least one example value valid for the paramType, non-empty `values` for enum, and `min <= max` when both are present — i.e. `catalog.custom`'s parameter validation accepts it and `parametersStepErrors` reports no error for its form row.

**Validates: Requirements 2.4, 2.6**

### Property 6: Required classification follows the usable-default rule

*For any* generated GStreamer_Property that maps to a Parameter_Suggestion, the suggestion SHALL be required exactly when the property lacks a usable default (string default null/empty, or default not convertible to the mapped paramType), and when optional it SHALL carry the property default as the declaration default.

**Validates: Requirements 3.1, 3.2**

### Property 7: Base-class filtering keeps exactly the element's own properties

*For any* generated element with a mix of properties whose `owner` equals or differs from the element's GType, the derived suggestions SHALL include every writable mappable property owned by the element's own GType (including names that shadow base-class names) and none owned by a different GType.

**Validates: Requirements 4.1, 4.2**

### Property 8: Merge never changes existing declarations

*For any* existing parameter form list and any suggestion list, every entry of `mergeSuggestions(existing, suggestions).parameters` at an index below `existing.length` SHALL deep-equal the corresponding existing entry, in the same order.

**Validates: Requirements 6.1**

### Property 9: Merge appends exactly the new names and reports the rest

*For any* existing parameter form list and any suggestion list, the merged list SHALL equal the existing list plus, in suggestion order, exactly those suggestions whose trimmed name matches no existing trimmed name; `added` SHALL list those appended names; `alreadyDeclared` SHALL list exactly the colliding suggestion names.

**Validates: Requirements 6.2, 6.3**

### Property 10: Merge is idempotent

*For any* existing parameter form list and any suggestion list, merging the same suggestions into an already-merged list SHALL return the list unchanged with an empty `added` set (running the scan twice adds nothing).

**Validates: Requirements 6.1, 6.2**

### Property 11: Suggestion-to-form conversion round-trips through form assembly

*For any* Parameter_Suggestion, converting it with `formFromSuggestion` and assembling it back with `declaration.ts`'s `parameterFromForm` conversion path SHALL reproduce the suggestion's name, paramType, required flag, default, description, first example, and enum values.

**Validates: Requirements 2.6, 3.3**

### Property 12: Element picking prefers the wizard's factory

*For any* non-empty element list and any preferred factory name, `pickElement` SHALL return the element whose factory equals the preferred name when one exists; else the sole element when the list has exactly one; else null.

**Validates: Requirements 5.4**

## Error Handling

| Failure | Where | Behavior |
|---|---|---|
| Introspection script fails / no factories register / element won't instantiate | CodeBuild | `status: "failed"` (or per-element `instantiationError`) report uploaded; build success untouched (1.4) |
| Report upload fails | CodeBuild | Logged warning; build success untouched; `plugin_builds.py` records `{status: "failed", message: "report missing"}` (1.4, 1.6) |
| Report object missing / oversized / malformed JSON | `plugin_builds.py` SUCCEEDED path | Artifact stanza `{status: "failed", message}`; never alters build status |
| Stored report malformed at read time | `gst_properties` route | `{available: false, reason: "introspection_failed"}` — no 500 (8.3) |
| No successful x86_64 artifact | route | `{available: false, reason: "no_x86_64_build"}` (1.6); wizard shows the build-first notice (7.1) |
| Artifact predates feature (no stanza) | route | `{available: false, reason: "not_captured"}` (7.4) |
| RBAC / missing record | route | Uniform 404, matching the existing cross-tenant-safe convention |
| Scan API request fails | wizard | Error Alert; manual flow and navigation unchanged (7.3, 5.5) |
| Introspection failed status | wizard | Failure Alert with diagnostic; manual flow unchanged (7.2) |

## Testing Strategy

Both test layers follow the repo's existing conventions: **backend** pytest + hypothesis under `edge-cv-portal/backend/tests/` (moto for S3/DynamoDB where handlers are exercised), **frontend** vitest + fast-check under the existing node-designer test layout.

**Property-based tests** (one test per Correctness Property, ≥ 100 iterations, tagged `Feature: gst-parameter-prepopulation, Property {n}: {title}`):

- Backend (hypothesis): Properties 1–7 against `gst_properties.py` pure functions, with generators for reports, GStreamer_Property records (random GTypes drawn from mapped + unmapped sets, optional ranges, enum value lists, null/empty/valued defaults, owner GTypes), and arbitrary JSON for Property 2. Property 5 cross-checks against `workflow_core.catalog.custom`'s real parameter validator.
- Frontend (fast-check): Properties 8–12 against `scan.ts` (and `declaration.ts` conversion for Property 11), with arbitraries for `ParameterForm` rows and `ParameterDeclaration` suggestions.

**Unit / example tests**:

- `dda-gst-introspect`: exercised in the test-sandbox container image (which has GStreamer + GI) against a stock element (e.g. `videoflip`: GEnum `method`, base-class `name`/`qos`) asserting report shape, owner recording, enum capture — integration-style, 1–2 examples.
- `plugin_builds.py` stanza recording: examples for captured / failed / missing / oversized report objects (moto S3).
- Route handler: examples for each unavailability reason, the available path, RBAC 404s (matching `test_plugin_simulator.py` conventions).
- `ParameterScanPanel` + wizard wiring (vitest, jsdom): auto-scan on empty list, no auto-scan when rows exist, manual rescan, factory selector on multi-element reports, degraded notices for each reason, "from scan" badge cleared on edit, Create_Wizard static notice, navigation never blocked by scan state.

**Integration tests**: extend the existing sandbox/container integration suite with one end-to-end capture example (build-image script → report JSON → parse_report). No property tests for CodeBuild/S3 wiring — infrastructure behavior does not vary meaningfully with input.
