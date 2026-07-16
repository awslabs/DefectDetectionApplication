# Design Document

## Overview

This feature extends the Custom Node Designer's Ports step in three coordinated layers:

1. **Static Port_Guidance** (Requirements 1, 2): a shared, purely static guidance panel rendered inside the Ports step of both wizards. It explains what a Port is, the Workflow_Designer connection rule, the input/output distinction, and each of the three Port_Types with usage examples. It also shows the typical port arrangement for the selected palette category and raises a non-blocking advisory when the declared ports diverge from that arrangement.

2. **Pad_Template capture** (Requirements 3, 4): the existing build-time introspection pipeline (`dda-gst-introspect` → S3 report → `plugin_builds.py` stanza validation → `gst_properties.py` parsing → `GET /plugins/{id}/versions/{v}/gst-properties`) is extended so each report element additionally carries the element's static Pad_Templates. The extension is strictly additive to the version-1 report shape: an optional per-element `pads` field. Stored legacy reports without the field keep parsing and serving exactly as before.

3. **Port_Scan pre-population** (Requirements 5, 6, 7): the backend derives Port_Suggestions and Unmapped_Pads from the captured Pad_Templates with a pure, deterministic function, and the Registration wizard's Ports step gains a scan panel (mirroring the existing ParameterScanPanel) that auto-populates untouched default port lists, additively merges into user-edited lists, and surfaces unconfirmed suggestions and unmapped pads. The Create wizard shows guidance only — no scan, since no Plugin_Artifact exists during creation.

Every degraded state (no x86_64 build, pre-pad-capture report, failed introspection, request failure, zero suggestions) falls back to the exact manual flow that exists today; pad data is never a gate.

### Key Design Decisions

- **No report version bump.** Pad data is an optional `pads` field on each report element, kept at `reportVersion: 1`. A version bump would make new reports unreadable by not-yet-deployed readers and force migration handling; an optional field keeps old readers and old reports working in both directions (Requirement 4.2). Absence of the field is the machine-readable signal that the report predates pad capture (Requirement 4.7).
- **Strict validation when the field is present.** `parse_report` validates pad entries as strictly as it validates properties: malformed pad data raises `ReportError`, which the route already maps to the `introspection_failed` unavailability reason (Requirement 4.4). No partial pad data ever leaves the parser.
- **Derivation is server-side and pure.** `gst_properties.py` gains `ports_for_element`, a pure sibling of `suggestions_for_element`, so the derivation is Hypothesis-testable and both scan results ride the same route response (Requirement 4.5).
- **Frontend mirrors the parameter-scan architecture.** Pure merge/detection helpers live in a new `portScan.ts` (like `scan.ts`); a `PortScanPanel` component (like `ParameterScanPanel`) owns fetch/apply state and communicates upward through a single `onApply` callback; provenance indicators (unconfirmed Port_Type) live in wizard state exactly like the existing `scannedNames` set.
- **Guidance is one shared component with static data.** `PortGuidancePanel` renders from a pure data module (`portGuidance.ts`) with no network access, guaranteeing identical content in both wizards (Requirements 1.4, 1.5) and making the category-divergence rule a pure, property-testable function.

## Architecture

```mermaid
graph TD
    subgraph "x86_64 build (CodeBuild)"
        BUILD[dda-plugin-build] --> INTROSPECT[dda-gst-introspect<br/>+ pad template capture]
        INTROSPECT -->|report JSON incl. pads| S3[(S3: {plugin}.so.gstinspect.json)]
    end

    S3 --> STANZA[plugin_builds.py<br/>build_gst_introspection_stanza<br/>256 KiB cap + parse_report validation<br/>UNCHANGED CODE]
    STANZA --> DDB[(Plugin_Record artifact entry<br/>gstIntrospection stanza)]

    subgraph "GET /plugins/{id}/versions/{v}/gst-properties"
        ROUTE[plugin_records.py<br/>get_version_gst_properties]
        ROUTE --> PARSE[gst_properties.parse_report<br/>+ optional pads field]
        PARSE --> PSUG[suggestions_for_element<br/>UNCHANGED]
        PARSE --> PORTS[ports_for_element<br/>NEW: Port_Suggestions + Unmapped_Pads]
    end

    DDB --> ROUTE
    S3 --> ROUTE

    subgraph "Frontend Ports step"
        GUIDE[PortGuidancePanel<br/>static portGuidance.ts data<br/>both wizards]
        SCAN[PortScanPanel<br/>Registration wizard only]
        MERGE[portScan.ts pure helpers<br/>applySuggestions / isUntouchedDefaults]
        SCAN --> MERGE
    end

    ROUTE -->|portSuggestions, unmappedPads,<br/>padsReason per element| SCAN
```

Data flow for a Port_Scan: the panel calls the existing `nodeDesignerApi.getGstProperties` client; the route parses the stored report and returns, per element, the existing `suggestions`/`skipped` plus the new `portSuggestions`/`unmappedPads`/`padsReason`; the panel picks the element with the existing `pickElement` helper (same selection as the Parameter_Scan, Requirement 6.6) and applies the suggestions through the pure merge in `portScan.ts`.

### What does NOT change

- `plugin_builds.py`: `gst_report_key`, `build_gst_introspection_stanza`, the 256 KiB cap, and `INTROSPECTION_ARCH` are untouched. The stanza validates the extended report through the same `parse_report` call; an oversized report (now more likely with pads) already yields a failed stanza with the size-cap diagnostic while the build stays succeeded (Requirement 3.3).
- The route's unavailability taxonomy (`no_x86_64_build`, `not_captured`, `introspection_failed`) and the existing per-element `suggestions`/`skipped` response fields (Requirement 4.6).
- `dda-plugin-build`'s `run_introspection` shell step: it remains a dumb pipe for the report document.
- The Parameters step, `ParameterScanPanel`, `scan.ts` merge helpers, and all parameter wire shapes.

## Components and Interfaces

### 1. Capture: `dda-gst-introspect` (plugin-build-images/)

`describe_factory` is extended to capture static pad templates from the **loaded factory** (pad templates are class-level metadata available from `factory.get_static_pad_templates()` — instantiation is not required, so elements with an `instantiationError` can still report pads when the factory loaded).

New pure helpers (module top level, importable without GI — the `gi` imports stay inside `scan()`):

```python
MAX_CAPS_LEN = 4096

def truncate_caps(caps):
    """(caps_str, truncated_flag): first MAX_CAPS_LEN chars and whether
    truncation occurred (Requirement 3.4)."""
    if len(caps) > MAX_CAPS_LEN:
        return caps[:MAX_CAPS_LEN], True
    return caps, False

def describe_pad_templates(factory, Gst):
    """(pads, pads_error): the pad list and None, or [] and a diagnostic
    when reading any template of this factory fails (Requirement 3.2)."""
```

`describe_pad_templates` maps each `Gst.StaticPadTemplate` via the enum tables `Gst.PadDirection.SINK/SRC -> 'sink'/'src'` and `Gst.PadPresence.ALWAYS/SOMETIMES/REQUEST -> 'always'/'sometimes'/'request'`; the caps string comes from `template.get_caps().to_string()`. Any exception while reading an element's templates (including an unmappable direction/presence value) degrades that one element to `pads: []` with a `padsError` diagnostic, leaving its property data and the report status untouched (Requirement 3.2). An element that genuinely declares no templates records `pads: []` with `padsError: null` (Requirement 3.5).

Both the load-failure and instantiation-failure early returns in `describe_factory` also emit `pads: []` with a `padsError` describing the failure (pad reading was not possible), keeping every element in a new report explicitly pad-captured.

### 2. Parsing/serialization: `backend/functions/gst_properties.py`

New constants and dataclass:

```python
PAD_DIRECTION_SINK, PAD_DIRECTION_SRC = 'sink', 'src'
VALID_PAD_DIRECTIONS = (PAD_DIRECTION_SINK, PAD_DIRECTION_SRC)
PAD_PRESENCE_ALWAYS = 'always'
VALID_PAD_PRESENCES = ('always', 'sometimes', 'request')
MAX_CAPS_LEN = 4096

@dataclass(frozen=True)
class PadTemplate:
    """One static Pad_Template captured from the element factory."""
    name: str            # name template, e.g. 'sink', 'src', 'src_%u'
    direction: str       # 'sink' | 'src'
    presence: str        # 'always' | 'sometimes' | 'request'
    caps: str            # caps string, at most MAX_CAPS_LEN chars
    caps_truncated: bool # True when capture truncated the caps (3.4)
```

`ReportElement` gains two optional fields with legacy-compatible defaults:

```python
@dataclass(frozen=True)
class ReportElement:
    factory: str
    element_gtype: str
    instantiation_error: Optional[str] = None
    properties: List[GstProperty] = field(default_factory=list)
    pads: Optional[List[PadTemplate]] = None  # None = not captured (legacy)
    pads_error: Optional[str] = None          # meaningful only when pads is not None
```

**Domain invariant**: `pads_error` is non-None only when `pads == []` (a per-element read failure, Requirement 3.2). Generators and serialization respect this.

Parsing (`_parse_element`):
- `pads` key **absent** → `pads=None, pads_error=None` (legacy report, Requirement 4.2). Any stray `padsError` without `pads` is ignored.
- `pads` key **present** → must be a list; each entry must be an object with all five fields present and correctly typed: `name` str, `direction` in `VALID_PAD_DIRECTIONS`, `presence` in `VALID_PAD_PRESENCES`, `caps` str of length ≤ `MAX_CAPS_LEN`, `capsTruncated` bool. `padsError` parses as optional str. Any violation raises `ReportError` — the route already maps that to `introspection_failed` (Requirement 4.4). The caps length bound makes the truncation contract enforceable in the pure module.

Serialization (`_serialize_element`): when `pads is None` both keys are omitted (byte-identical output to today for legacy-shaped reports); otherwise `pads` (each pad as `{name, direction, presence, caps, capsTruncated}`) and `padsError` are emitted explicitly. This preserves the round-trip identity `parse_report(serialize_report(r)) == r` over the whole (extended) valid domain (Requirement 4.3).

### 3. Derivation: `ports_for_element` (new, pure, in `gst_properties.py`)

```python
PORT_TYPE_VIDEO_FRAMES = 'VideoFrames'
CONFIDENT_CAPS_PREFIX = 'video/x-raw'   # exact, case-sensitive (5.2)

PADS_REASON_NOT_CAPTURED = 'pads_not_captured'   # report predates pad capture (4.7)
PADS_REASON_NO_TEMPLATES = 'no_pad_templates'    # element declares none (4.8)
PADS_REASON_READ_FAILED = 'pads_read_failed'     # per-element capture failure (3.2)

def ports_for_element(element: ReportElement) -> Dict[str, Any]:
    """{'portSuggestions': [...], 'unmappedPads': [...],
        'padsReason': str | None, 'padsMessage': str | None}"""
```

Reason classification (mutually exclusive, Requirements 4.7, 4.8):

| Element state | `padsReason` | `padsMessage` |
|---|---|---|
| `pads is None` (legacy) | `pads_not_captured` | `None` |
| `pads == []`, `pads_error` set | `pads_read_failed` | the diagnostic |
| `pads == []`, no error | `no_pad_templates` | `None` |
| `pads` non-empty | `None` | `None` |

Derivation walks `element.pads` **in report order** (Requirement 5.1); each pad lands in exactly one output list:

1. `presence != 'always'` → **Unmapped_Pad** `{name, direction, presence, caveat}` with the caveat that sometimes/request pads are created at runtime and do not correspond to fixed declared Ports (Requirement 5.4).
2. `presence == 'always'` but `name.strip()` is empty (the existing Ports_Step validation rule: non-empty Port name) → **Unmapped_Pad** with the caveat that the name template is not a valid Port name (Requirement 5.6).
3. Otherwise → **Port_Suggestion**:
   - `direction`: `sink` → `'input'`, `src` → `'output'` (Requirement 5.1)
   - `name`: the pad's name template verbatim
   - `portType`: always `'VideoFrames'` (the only caps-derivable catalog type, Requirement 5.5)
   - `confident`: `caps.startswith('video/x-raw')` — truncated caps included, since truncation preserves the prefix (Requirement 5.2)
   - `reason`: confident → states the caps begin with `video/x-raw`; unconfirmed → states that InferenceMeta and EventSignal are DDA semantic concepts GStreamer caps cannot express and the Port_Type needs user confirmation (Requirement 5.3)
   - `caps`, `capsTruncated`: carried through for display (Requirement 6.4)

The function is pure over immutable inputs — determinism (Requirement 5.7) is by construction and verified by property test.

### 4. Route: `get_version_gst_properties` (plugin_records.py)

The per-element response entry is extended in place; existing fields keep their exact names, structure, and values (Requirement 4.6):

```python
derived = suggestions_for_element(element)        # unchanged
ports = ports_for_element(element)                # new
elements.append({
    'factory': element.factory,
    'suggestions': derived['suggestions'],        # unchanged (4.6)
    'skipped': derived['skipped'],                # unchanged (4.6)
    'portSuggestions': ports['portSuggestions'],  # new (4.5)
    'unmappedPads': ports['unmappedPads'],        # new (4.5)
    'padsReason': ports['padsReason'],            # new (4.7, 4.8)
    'padsMessage': ports['padsMessage'],          # new (3.2 surfacing)
})
```

No other route logic changes: malformed pad data is caught inside the existing `parse_report` call and flows through the existing `ReportError → introspection_failed` mapping (Requirement 4.4).

### 5. Frontend: static guidance (`portGuidance.ts` + `PortGuidancePanel.tsx`)

`portGuidance.ts` — pure data and one pure function, no imports beyond `types.ts`:

```typescript
/** Static Port_Guidance content (1.1–1.3): definition, connection rule,
 *  input/output distinction, per-type description + usage example. */
export const PORT_DEFINITION: string;
export const CONNECTION_RULE: string;
export const INPUT_OUTPUT_DISTINCTION: string;
export const PORT_TYPE_GUIDANCE: Record<PortType, {
  carries: string;           // the data the type carries (1.2)
  example: string;           // names a node role + input/output usage (1.2)
}>;

/** Typical arrangement per palette category (2.1). 'at-least-one' models
 *  the output category's "at least one input of any type". */
export interface CategoryArrangement {
  inputs: PortType[] | 'at-least-one';
  outputs: PortType[];
  summary: string;           // human-readable arrangement text
}
export const CATEGORY_ARRANGEMENTS: Record<NodeCategory, CategoryArrangement>;

/** Divergence of a declaration from the category arrangement (2.4, 2.5):
 *  null when counts and the multiset of port types match on both sides;
 *  otherwise flags exactly the diverging side(s). Pure and deterministic. */
export function guidanceDivergence(
  category: string,
  inputs: PortForm[],
  outputs: PortForm[]
): { inputs: boolean; outputs: boolean } | null;
```

Arrangements: `input` → no inputs, one VideoFrames output; `preprocessing` → one VideoFrames input, one VideoFrames output; `inference` → one VideoFrames input, one InferenceMeta output; `post_processing` → one InferenceMeta input, one EventSignal output; `output` → at least one input, no outputs (Requirement 2.1). Divergence compares port counts and the multiset of declared port types per side (order-insensitive; `'at-least-one'` diverges only when the side is empty).

`PortGuidancePanel.tsx` — one shared component used verbatim by both wizards (Requirement 1.5):

```typescript
export interface PortGuidancePanelProps {
  category: string;                      // drives the arrangement box (2.1, 2.2)
  inputs: PortForm[];                    // divergence advisory input (2.4)
  outputs: PortForm[];
}
```

Renders (all static, no network requests — Requirement 1.4): the Port definition + connection rule + input/output distinction and the three Port_Type descriptions inside a Cloudscape `ExpandableSection` (default-expanded header text, collapsible detail); the selected category's arrangement summary (re-renders on the `category` prop, Requirement 2.2); and, when `guidanceDivergence` is non-null, a dismissable non-blocking `Alert type="info"` naming the diverging side(s) (Requirement 2.4) that disappears when the divergence resolves (Requirement 2.5). The panel never contributes to `portsStepErrors` or step gating (Requirement 2.3).

### 6. Frontend: port scan pure helpers (`portScan.ts`)

Wire types (extending the `GstPropertiesResponse` shapes in `scan.ts`):

```typescript
export type PadsReason = 'pads_not_captured' | 'no_pad_templates' | 'pads_read_failed';

export interface PortSuggestion {
  name: string;
  direction: 'input' | 'output';
  portType: string;          // always 'VideoFrames' today
  confident: boolean;        // Confident_Suggestion vs Unconfirmed_Suggestion
  caps: string;
  capsTruncated: boolean;
  reason: string;
}

export interface UnmappedPad {
  name: string;
  direction: 'sink' | 'src';
  presence: 'sometimes' | 'request' | 'always';
  caveat: string;
}

// ScanElement (scan.ts) gains optional fields the old backend simply omits:
//   portSuggestions?: PortSuggestion[];
//   unmappedPads?: UnmappedPad[];
//   padsReason?: PadsReason | null;
//   padsMessage?: string | null;
```

Pure functions:

```typescript
/** Exactly the wizard-supplied initial lists: one input {name:'in'} and one
 *  output {name:'out'}, both VideoFrames (Untouched_Defaults, 6.1). */
export function isUntouchedDefaults(inputs: PortForm[], outputs: PortForm[]): boolean;

/** Apply Port_Suggestions to the port lists (6.1, 6.2, 6.10, 6.11).
 *  untouched && suggestions.length > 0: both sides replaced by the
 *  suggestions partitioned by direction, in suggestion order (6.1).
 *  Otherwise additive merge: every existing port kept unchanged; each
 *  suggestion whose trimmed name exactly (case-sensitively) matches an
 *  existing port name on either side is reported alreadyDeclared (6.2);
 *  the rest are appended to their side in order and reported applied
 *  (6.11). Empty suggestions always leave the lists unchanged (6.10). */
export function applySuggestions(
  inputs: PortForm[],
  outputs: PortForm[],
  suggestions: PortSuggestion[],
  untouched: boolean
): {
  inputs: PortForm[];
  outputs: PortForm[];
  applied: string[];           // names newly added/applied
  alreadyDeclared: string[];   // names kept as declared (6.2)
  unconfirmed: string[];       // applied names with confident === false (6.5)
};

/** Update-mode removal protection (6.9): the reason a port cannot be
 *  removed, or null. Blocks removing a port whose trimmed name appears on
 *  the same side of the existing registered declaration. */
export function removalBlockReason(
  side: 'inputs' | 'outputs',
  portName: string,
  existingDeclaration: Record<string, unknown> | null
): string | null;
```

Element selection reuses `pickElement` from `scan.ts` unchanged, so the Port_Scan and Parameter_Scan always agree on the factory (Requirement 6.6).

### 7. Frontend: `PortScanPanel.tsx`

Mirrors `ParameterScanPanel` structurally (fetch on mount, one auto-apply per mount, manual button, outcome summary, unavailability alerts, all state local, upward communication through one callback):

```typescript
export interface PortScanApplyResult {
  inputs: PortForm[];
  outputs: PortForm[];
  applied: string[];
  alreadyDeclared: string[];
  unconfirmed: string[];       // names to mark as needing confirmation (6.5)
}

export interface PortScanPanelProps {
  pluginId: string;
  version: number;
  preferredFactory?: string;   // same preference as the Parameter_Scan (6.6)
  inputs: PortForm[];          // latest lists via ref, like ParameterScanPanel
  outputs: PortForm[];
  onApply: (result: PortScanApplyResult) => void;
}
```

Behavior:

- **Fetch on mount** via `nodeDesignerApi.getGstProperties` (the Wizard renders only the active step, so mount coincides with reaching the Ports step). **Auto-scan** applies at most once per mount, and only when the response is available, the picked element has `padsReason == null` with at least one Port_Suggestion, and `isUntouchedDefaults(inputsRef, outputsRef)` holds at apply time (Requirement 6.1). Like the parameter panel, the merge reads the wizard's latest lists through refs so concurrent user edits are never clobbered (Requirement 6.7).
- **Manual scan button** ("Scan plugin pads") re-fetches and applies on demand (Requirement 6.3); `loading` disables the button so no second scan runs concurrently while manual port controls and navigation stay untouched (Requirement 6.7). The button doubles as the retry control after a failure (Requirement 7.3).
- **Outcome summary** (Requirement 6.4): applied port names per side, already-declared names (Requirement 6.2), each Unconfirmed_Suggestion with its caps string and confirmation guidance, and each Unmapped_Pad with its name, direction, presence, and caveat. A scan yielding zero suggestions reports that outcome — including any Unmapped_Pads — and leaves the lists unchanged (Requirements 6.10, 7.5).
- **Degraded states** (Requirement 7): `available:false, reason:'no_x86_64_build'` → info notice that pre-population requires a successful x86_64 build (7.1); element `padsReason:'pads_not_captured'` → info notice that pad data is unavailable for this build (7.2 — note the report itself is `available: true`, parameters still scan); `reason:'introspection_failed'` or a thrown request error → error alert with the diagnostic and the retry button (7.3); `padsReason:'pads_read_failed'` → error alert with `padsMessage`; `padsReason:'no_pad_templates'` / zero always-pads → info notice (7.5). Every one of these renders beside — never instead of — the untouched manual port controls (7.6).

### 8. Frontend: wizard integration

**RegistrationWizard.tsx** Ports step:

- Renders `PortGuidancePanel` (with `form.category`, `form.inputs`, `form.outputs`) above the port containers, then `PortScanPanel`.
- New state `unconfirmedPortNames: Set<string>` (mirroring `scannedNames`): populated from `onApply`'s `unconfirmed` list; each unconfirmed port row shows a warning `Badge` ("confirm type") plus its caps/reason inline (Requirement 6.5). Editing the port's name or type — including re-selecting the same type, which acts as the confirmation gesture — drops the name from the set, switching the row to the confirmed presentation.
- `onApply` flows through the ordinary `patch({inputs, outputs})` path, so applied ports are indistinguishable from manual ones for editing, removal, validation, and step gating (Requirement 6.8) and `portsStepErrors` is untouched.
- The remove-port handler consults `removalBlockReason(side, port.name, existing?.declaration ?? null)`; a non-null reason disables the remove button and shows the reason as the row's `FormField` warning text (Requirement 6.9). This applies to every port row (scan-applied or manual) in update mode, since the registered declaration's dependence does not distinguish provenance.

**CreateWizard.tsx** Ports step:

- Renders `PortGuidancePanel` only, plus a static info line that port pre-population requires a built plugin (available later in the Registration wizard). No `PortScanPanel`, no scan control, no network request (Requirement 7.4) — the same static-only pattern the Parameters step already uses for `ParameterScanPanel`'s no-plugin notice.

## Data Models

### Extended Introspection_Report (version 1, additive)

```json
{
  "reportVersion": 1,
  "status": "captured",
  "message": null,
  "gstVersion": "1.20.3",
  "capturedAt": "2025-01-01T00:00:00Z",
  "elements": [
    {
      "factory": "myfilter",
      "elementGType": "GstMyFilter",
      "instantiationError": null,
      "properties": [ { "...unchanged property shape..." : "..." } ],
      "pads": [
        { "name": "sink", "direction": "sink", "presence": "always",
          "caps": "video/x-raw, format=(string){ RGB, BGR }",
          "capsTruncated": false },
        { "name": "src_%u", "direction": "src", "presence": "request",
          "caps": "ANY", "capsTruncated": false }
      ],
      "padsError": null
    }
  ]
}
```

- `pads` / `padsError` absent entirely → legacy report (pads not captured).
- `pads: [], padsError: "<diagnostic>"` → per-element read failure (3.2).
- `pads: [], padsError: null` → element declares no static pad templates (3.5).
- `caps` is at most 4096 characters; `capsTruncated: true` marks capture-time truncation (3.4). The parser rejects longer caps as malformed.

### gst-properties route response (per element, additive)

```json
{
  "factory": "myfilter",
  "suggestions": [ "...unchanged ParameterDeclaration shapes..." ],
  "skipped":     [ "...unchanged {name, reason} shapes..." ],
  "portSuggestions": [
    { "name": "sink", "direction": "input", "portType": "VideoFrames",
      "confident": true, "caps": "video/x-raw, ...", "capsTruncated": false,
      "reason": "the pad's caps begin with video/x-raw" }
  ],
  "unmappedPads": [
    { "name": "src_%u", "direction": "src", "presence": "request",
      "caveat": "request pads are created at runtime and do not correspond to fixed declared Ports" }
  ],
  "padsReason": null,
  "padsMessage": null
}
```

Top-level response fields (`available`, `reason`, `message`, `gstVersion`, `capturedAt`, `elements`) are unchanged.

### Frontend form state

`PortForm` (`declaration.ts`) is unchanged — applied suggestions become ordinary `{name, portType}` rows. Scan provenance lives only in the wizard's `unconfirmedPortNames: Set<string>` state, exactly like the parameter scan's `scannedNames`, so declaration assembly and submission are untouched.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system-essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Extended report round-trip

*For any* valid Introspection_Report — with pad data (including truncated caps and per-element pad read failures), without pad data, or mixed per element — `parse_report(serialize_report(report))` equals the original report field-for-field, and the serialized document survives a real `json.dumps`/`json.loads` cycle unchanged.

**Validates: Requirements 4.1, 4.3**

### Property 2: Legacy reports parse compatibly

*For any* valid Introspection_Report, serializing it and then deleting the `pads` and `padsError` keys from every element (producing exactly the pre-feature document shape) parses successfully to a report in which every element has `pads=None` and `pads_error=None` and every other report-level, element-level, and property field equals the original.

**Validates: Requirements 4.2**

### Property 3: Malformed pad data is rejected, not crashed on

*For any* valid pad-bearing report document broken by a single targeted pad mutation — a `pads` value that is not a list, a pad entry that is not an object, a dropped or mistyped pad field, a `direction` outside {sink, src}, a `presence` outside {always, sometimes, request}, or a `caps` string longer than 4096 characters — `parse_report` raises `ReportError` (and nothing else).

**Validates: Requirements 4.4**

### Property 4: Pad data never changes parameter suggestions

*For any* report element, `suggestions_for_element` produces identical suggestions and skipped lists whether the element carries pad data or has it stripped (`pads=None, pads_error=None`).

**Validates: Requirements 4.6**

### Property 5: Pads-reason classification is total and exclusive

*For any* report element, `ports_for_element` returns `padsReason == 'pads_not_captured'` iff `pads is None`, `'pads_read_failed'` (with the diagnostic as `padsMessage`) iff `pads == []` with a `pads_error`, `'no_pad_templates'` iff `pads == []` without one, and `None` iff `pads` is non-empty — and whenever a reason is set, both `portSuggestions` and `unmappedPads` are empty.

**Validates: Requirements 4.7, 4.8**

### Property 6: Derivation partitions the pads

*For any* element with a non-empty pad list, every Pad_Template appears in exactly one of `portSuggestions` or `unmappedPads`: a pad with presence `always` and a non-whitespace name template becomes a Port_Suggestion whose direction is `input` for `sink` and `output` for `src` and whose name is the name template verbatim; a pad with presence `sometimes` or `request` becomes an Unmapped_Pad carrying its name, direction, presence, and a caveat; a pad whose name template is empty or whitespace-only becomes an Unmapped_Pad with the invalid-name caveat; and both output lists preserve the pads' report order.

**Validates: Requirements 5.1, 5.4, 5.6**

### Property 7: Caps prefix decides confidence

*For any* derived Port_Suggestion, `portType` is `VideoFrames`, and `confident` is true iff the pad's caps string begins with the exact case-sensitive characters `video/x-raw` (truncated caps included); every non-confident suggestion carries the pad's caps string and a reason stating that InferenceMeta and EventSignal are DDA semantic concepts the caps cannot express.

**Validates: Requirements 5.2, 5.3**

### Property 8: Derived suggestions are valid and derivation is deterministic

*For any* report element, every derived Port_Suggestion satisfies the existing Ports_Step validation rules (non-empty trimmed name, portType in the Node_Type_Catalog), and calling `ports_for_element` twice on the same element yields deeply equal results.

**Validates: Requirements 5.5, 5.7**

### Property 9: Caps truncation is bounded and marked

*For any* string, `truncate_caps` returns a prefix of the input of at most 4096 characters together with a flag that is true iff the input exceeds 4096 characters; when the flag is true the returned string is exactly 4096 characters.

**Validates: Requirements 3.4**

### Property 10: Untouched defaults are replaced by the suggestions

*For any* non-empty Port_Suggestion list, applying it over the Untouched_Defaults (`untouched=true`) yields input and output lists that are exactly the suggestions partitioned by direction, in suggestion order, each as `{name, portType}`; the applied names are exactly the suggestion names and the unconfirmed names are exactly the non-confident suggestions' names.

**Validates: Requirements 6.1**

### Property 11: Merge preserves edits and appends exactly the new names

*For any* user-edited port lists and any Port_Suggestion list, the additive merge (`untouched=false`) keeps every existing port unchanged and in place; every suggestion whose trimmed name exactly (case-sensitively) matches an existing port's trimmed name on either side is reported in `alreadyDeclared` without modifying that port; every other suggestion is appended to its direction's list in order and reported in `applied`; and an empty suggestion list returns both lists identical to the inputs.

**Validates: Requirements 6.2, 6.10, 6.11**

### Property 12: Category divergence flags exactly the diverging sides

*For any* palette category and any port declaration lists, `guidanceDivergence` returns null iff each side's port count and multiset of port types match the category's arrangement (with the output category's `at-least-one` input rule diverging only on an empty input side); otherwise the returned flags are true for exactly the side(s) whose count or type multiset differs.

**Validates: Requirements 2.4, 2.5**

## Error Handling

| Condition | Layer | Behavior |
|---|---|---|
| Pad templates of one factory unreadable | dda-gst-introspect | That element records `pads: []` + `padsError` diagnostic; properties and report status untouched; build success preserved (3.2) |
| Factory load / instantiation failure | dda-gst-introspect | Existing `instantiationError` behavior; element additionally records `pads: []` + `padsError` (pad read impossible) |
| Caps string > 4096 chars | dda-gst-introspect | First 4096 chars stored with `capsTruncated: true`; pad kept (3.4) |
| Report (with pads) > 256 KiB | plugin_builds (unchanged) | Failed stanza with the existing size-cap diagnostic; build stays succeeded (3.3) |
| Malformed pad data in stored report | gst_properties / route | `parse_report` raises `ReportError`; route answers `available:false, reason:'introspection_failed'` with the diagnostic — no partial pad data, never a 500 (4.4) |
| Legacy report (no `pads` keys) | route | `available:true`; per element `padsReason:'pads_not_captured'`, empty suggestion/unmapped lists; parameter fields unchanged (4.2, 4.7) |
| Element declares no pad templates | route | `padsReason:'no_pad_templates'`, empty lists (4.8) |
| Per-element pad read failure in report | route | `padsReason:'pads_read_failed'` with `padsMessage` |
| No successful x86_64 build | PortScanPanel | Info notice (pre-population needs an x86_64 build); guidance shown; manual flow and navigation untouched (7.1, 7.6) |
| `padsReason:'pads_not_captured'` | PortScanPanel | Info notice (pad data unavailable for this build); manual flow untouched (7.2) |
| `reason:'introspection_failed'` or fetch error | PortScanPanel | Error alert with diagnostic; manual scan button remains as the retry control; manual flow untouched (7.3) |
| Scan yields zero suggestions | PortScanPanel | Lists unchanged; outcome notice incl. any Unmapped_Pads with caveats (6.10, 7.5) |
| Create wizard (no Plugin_Artifact) | CreateWizard | Guidance + category guidance + static "requires a built plugin" note; no scan, no scan control, no request (7.4) |
| Blocked port removal (update mode) | RegistrationWizard | Remove control disabled with the displayed reason from `removalBlockReason` (6.9) |
| Declaration diverges from category guidance | PortGuidancePanel | Non-blocking info advisory naming the diverging side(s); never gates steps or submission (2.3, 2.4) |

## Testing Strategy

Dual approach, matching the repo's existing conventions: property-based tests verify the universal properties above; example-based tests cover UI wiring, route envelopes, and degraded scenarios. Property tests run a minimum of 100 iterations and each carries the tag comment **Feature: port-guidance-and-pad-prepopulation, Property {number}: {property_text}**, one property per test.

### Backend (pytest + Hypothesis, `edge-cv-portal/backend/tests/`)

Property tests (one file per property, following `test_property_gst_*.py`):

- `test_property_pad_report_roundtrip.py` — Property 1. Extends the report generators of `test_property_gst_report_roundtrip.py` with pad strategies: valid directions/presences, caps up to and at the 4096 boundary with matching `capsTruncated`, elements with `pads=None`, `pads=[]` (± `pads_error`), and populated lists.
- `test_property_pad_legacy_compat.py` — Property 2 (serialize, strip pad keys, re-parse, compare).
- `test_property_pad_report_rejection.py` — Property 3, mirroring the targeted-mutation approach of `test_property_gst_report_rejection.py` with pad-directed mutations.
- `test_property_pad_suggestions_unchanged.py` — Property 4.
- `test_property_pad_reason_classification.py` — Property 5.
- `test_property_pad_derivation_partition.py` — Property 6 (generators include whitespace-only name templates and all presences).
- `test_property_pad_caps_confidence.py` — Property 7 (caps generated with/without the `video/x-raw` prefix, case variants, truncated variants).
- `test_property_pad_suggestion_validity.py` — Property 8.
- `test_property_pad_caps_truncation.py` — Property 9, loading `plugin-build-images/dda-gst-introspect` via `importlib.util.spec_from_file_location` (its top-level imports are GI-free; only `scan()` touches GI) and testing `truncate_caps` directly.

Example/unit tests:

- Extend `test_plugin_gst_properties_route.py`: a stored pads-bearing report returns `portSuggestions`/`unmappedPads`/`padsReason` per element with the existing `suggestions`/`skipped` byte-identical to a pad-free control (4.5, 4.6); a legacy stored report answers `pads_not_captured` (4.7); a stored report with malformed pads answers `available:false, reason:'introspection_failed'` (4.4); empty-pad-list element answers `no_pad_templates` (4.8).
- Extend the plugin_builds stanza tests: an oversized pads-bearing report yields the failed size-cap stanza (3.3).

Pad-template capture itself (3.1, 3.2 capture side) is integration-verified in the x86_64 build image against a real sample plugin; the pytest suite validates everything downstream of the report document.

### Frontend (vitest + fast-check, `edge-cv-portal/frontend/src/pages/node-designer/`)

Shared arbitraries in `portScanArbitraries.ts` (mirroring `scanArbitraries.ts`): port forms, Port_Suggestions (confident/unconfirmed, both directions), category/port-list pairs.

Property tests (one file per property, following `*.property.test.ts`, `numRuns: 100`):

- `portReplaceDefaults.property.test.ts` — Property 10.
- `portMergePreservation.property.test.ts` — Property 11.
- `categoryDivergence.property.test.ts` — Property 12.

Component/example tests:

- `PortGuidancePanel.test.tsx` — guidance content present (all three Port_Types with carries + example, connection rule, input/output distinction) (1.1–1.3); all five category arrangements defined and displayed, swap on category change (2.1, 2.2); divergence advisory appears/disappears and never blocks (2.3–2.5); no network calls (1.4).
- `PortScanPanel.test.tsx` — auto-scan once over untouched defaults (6.1); manual scan button + disabled-while-loading (6.3, 6.7); outcome rendering with unconfirmed caps/guidance and unmapped caveats (6.4); factory selection with `preferredFactory` (6.6); each degraded state (`no_x86_64_build`, `pads_not_captured`, `introspection_failed`, fetch error with retry, `no_pad_templates`/zero suggestions) rendering beside a usable manual flow (7.1–7.3, 7.5, 7.6).
- `RegistrationWizard.test.tsx` (extend) — guidance panel present on the Ports step (1.5); unconfirmed badge shown after apply and cleared on edit/confirm (6.5); applied ports editable/removable via the ordinary controls (6.8); removal blocked with reason in update mode (6.9); navigation unblocked during/after scans (6.7, 7.6).
- `CreateWizard.test.tsx` (extend) — guidance + category guidance rendered, "requires a built plugin" note, no scan control, no gst-properties request (1.5, 7.4).
- `portScan.test.ts` — concrete unit cases for `isUntouchedDefaults`, `removalBlockReason`, and boundary examples complementing the properties.
