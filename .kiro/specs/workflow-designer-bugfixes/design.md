# Workflow Designer Bugfixes Design

## Overview

This design covers three independent bugs in the edge-cv-portal, fixed together under one spec because they were reported together and each is small and well-isolated:

1. **Bug 1 — Temperature always sent to Bedrock**: `DEFAULT_BEDROCK_CONFIG` in `workflow_generator.py` (mirrored in `data_accounts.py`) bakes in `temperature: 0.2` and `top_p: 0.9`. When an admin has never configured a temperature and no per-request override is given, the fallback default is sent to the Bedrock Converse API, and newer Anthropic models that reject the temperature parameter fail generation with `BEDROCK_INVOCATION_FAILED`. Additionally the settings form (`BedrockConfigurationSettings.tsx`) and backend validation (`validate_bedrock_configuration` in `data_accounts.py`) refuse a blank temperature, so the parameter cannot even be unset. The fix makes "unset" the default (no sampling parameter sent unless explicitly configured or overridden) and makes blank/unset a valid stored state end to end.

2. **Bug 2 — Default ports ignore the palette category**: `CreateWizard.tsx` and `RegistrationWizard.tsx` seed the port declaration with one input ("in") and one output ("out") regardless of the selected palette category, so an `input` (source) node is presented with an input port, contradicting the wizard's own Port_Guidance. The fix derives the default port rows from the selected category's typical arrangement (`CATEGORY_ARRANGEMENTS` in `portGuidance.ts`), rewrites untouched default rows when the category changes, and states each kind's input/output requirements on the ports step — while preserving user-edited rows and keeping the guidance advisory.

3. **Bug 3 — Module import selects every plugin by default**: `ImportView.tsx` seeds the module plugin selection with `allPluginNames(plugins)`, so the user must opt out instead of opting in. The fix seeds the selection empty and relies on the already-existing selection gate (`moduleSelectionIncomplete` blocks `formComplete`) so an explicit selection (individual checks or "Select all") is required before the import proceeds. Serialization semantics are untouched: a full selection still serializes to no `selected_plugins` parameter (whole module).

All backend invocation-path logic (`invoke_generation` in both generators) already omits `None` sampling parameters, so Bug 1 is primarily a defaults/validation/settings-round-trip fix, not an invocation-logic fix.

## Glossary

- **Bug_Condition (C)**: The condition under which a bug manifests. Each of the three bugs has its own condition, formalized below (`isBugCondition1/2/3`).
- **Property (P)**: The desired behavior for inputs where the bug condition holds, formalized in the Correctness Properties section.
- **Preservation**: Behavior for all inputs where the bug condition does NOT hold, which must be byte-for-byte identical before and after the fix (F(X) = F'(X)).
- **Bedrock_Configuration**: The settings-table item `{setting_key: 'bedrock_configuration', value: {model_id, region, max_tokens, temperature, top_p, timeout_seconds}}`, written by `data_accounts.update_bedrock_configuration_setting()` and read by `workflow_generator.get_bedrock_configuration()` (reused by `node_generator.py`).
- **DEFAULT_BEDROCK_CONFIG**: The fallback configuration constant, duplicated (deliberately mirrored) in `edge-cv-portal/backend/functions/workflow_generator.py` and `edge-cv-portal/backend/functions/data_accounts.py`.
- **inference_config**: The `inferenceConfig` dict passed to `bedrock-runtime.converse()` by `invoke_generation()` in `workflow_generator.py` and `node_generator.py`: `{maxTokens}` plus at most one of `temperature` / `topP`.
- **Untouched_Defaults**: Port rows exactly equal to the wizard-seeded defaults, never renamed, retyped, added to, or removed from. Today detected by `portScan.isUntouchedDefaults()` as exactly one "in" input + one "out" output, both VideoFrames; generalized by this fix to "the default arrangement of any palette category".
- **CATEGORY_ARRANGEMENTS**: The per-category typical port arrangement data in `edge-cv-portal/frontend/src/pages/node-designer/portGuidance.ts` (input: no inputs + one VideoFrames output; preprocessing: VideoFrames→VideoFrames; inference: VideoFrames→InferenceMeta; post_processing: InferenceMeta→EventSignal; output: at-least-one input + no outputs).
- **Port_Scan**: The pad-derived port pre-population in the Registration wizard (`portScan.ts`): suggestions replace Untouched_Defaults wholesale, otherwise merge additively.
- **Module plugin selection**: The checkbox list in `ImportView.tsx` over `GET /plugin-modules?module=<name>` results; serialized by `selectedPluginsParam()` in `importFlow.ts` (full/empty selection → `undefined` → whole-module import).

## Bug Details

### Bug 1 — Temperature omitted when unset

#### Bug Condition

The bug manifests on any workflow or node generation where no temperature was explicitly stored in the Bedrock settings and no per-request override is supplied: `get_bedrock_configuration()` falls back to `DEFAULT_BEDROCK_CONFIG['temperature'] = 0.2`, so `invoke_generation()` always puts `temperature` into the inference config. It also manifests in the settings path: a PortalAdmin clearing the Temperature field is rejected client-side (`validate()` in `BedrockConfigurationSettings.tsx`: `form.temperature.trim() === ''` → error) and server-side (`validate_bedrock_configuration()`: `not _is_number(temperature)` → error).

**Formal Specification:**
```
FUNCTION isBugCondition1(input)
  INPUT: input of type GenerationRequest | SettingsSave
  OUTPUT: boolean

  IF input is GenerationRequest THEN
    RETURN storedBedrockConfig.temperature is ABSENT   // never configured
           AND input.temperature_override is ABSENT
    // buggy result: inference_config contains temperature = 0.2
  ELSE  // SettingsSave
    RETURN input.temperature_field is BLANK
    // buggy result: save rejected with "Temperature must be between 0 and 1"
  END IF
END FUNCTION
```

#### Examples

- No Bedrock configuration was ever stored; a user prompts the workflow generator against an Opus 4.x-class model. Expected: invocation succeeds with no `temperature` in `inferenceConfig`. Actual: `inferenceConfig = {maxTokens: 4096, temperature: 0.2}` and the model rejects the request (`BEDROCK_INVOCATION_FAILED`, deprecated-parameter style error).
- Same store state, node scaffold generation (`node_generator.py` calls `get_bedrock_configuration()` and its own identical `invoke_generation` inference-config logic). Same failure.
- A PortalAdmin opens the Bedrock settings, clears the Temperature input, saves. Expected: save accepted, temperature stored unset. Actual: client validation error "Temperature must be between 0 and 1"; even bypassing the client, the backend returns 400 "temperature must be a number between 0 and 1".
- Edge case: existing test `test_default_configuration_sends_temperature_without_top_p` in `edge-cv-portal/backend/tests/test_workflow_generation.py` codifies the buggy behavior (`assert inference_config["temperature"] == 0.2`) and must be inverted by the fix.

### Bug 2 — Input-kind nodes and port defaults

#### Bug Condition

The bug manifests whenever a wizard's port rows are still the seeded defaults (`inputs: [{name: 'in', portType: 'VideoFrames'}]`, `outputs: [{name: 'out', portType: 'VideoFrames'}]` from `initialForm()` in `CreateWizard.tsx` and the detail-load seeding in `RegistrationWizard.tsx`) and the selected palette category's typical arrangement differs from those defaults. Selecting a category only patches `form.category`; the port rows never change.

**Formal Specification:**
```
FUNCTION isBugCondition2(input)
  INPUT: input of type WizardState  // {category, inputs, outputs, portRowsUntouched}
  OUTPUT: boolean

  RETURN input.portRowsUntouched                        // defaults never edited
         AND arrangementOf(input.category) != multiset(input.inputs, input.outputs)
  // buggy result: presented rows stay one "in" input + one "out" output
  //               instead of the category's typical arrangement
END FUNCTION
```

#### Examples

- User selects category `input` in the Create wizard without touching the ports step. Expected: no input port rows, one VideoFrames output. Actual: an "in" input row is presented, contradicting the guidance text "Input nodes typically declare no inputs and one VideoFrames output" rendered by `PortGuidancePanel` directly above it.
- User selects `inference`. Expected default: one VideoFrames input, one InferenceMeta output. Actual: both ports VideoFrames.
- User selects `output`. Expected default: at least one input (default one VideoFrames input), no outputs. Actual: an "out" output row is presented.
- Edge case: user selects `preprocessing` (the initial category). The current defaults happen to equal the preprocessing arrangement, so no visible defect — the fixed defaults must be identical to today's for this category.
- Edge case: user renames "in" to "video", then changes category. Rows are user-edited; they must be preserved exactly (this already works today and must continue).

### Bug 3 — Import default selection

#### Bug Condition

The bug manifests whenever an official module's plugin list loads in `ImportView.tsx`: the load effect runs `setSelectedModulePlugins(allPluginNames(plugins))`, so every plugin is checked and `formComplete` is immediately true — the import can proceed with zero explicit user selection. Note the serialization subtlety: a full selection and an empty selection both serialize to `selected_plugins = undefined` (whole module) via `selectedPluginsParam()`, so "default all" and "default none without a gate" are behaviorally identical on the wire; the required behavior is default none **plus** the explicit selection gate.

**Formal Specification:**
```
FUNCTION isBugCondition3(input)
  INPUT: input of type ModulePluginListLoad  // non-empty plugin list for the chosen module
  OUTPUT: boolean

  RETURN input.plugins.length > 0
  // buggy result: selectedModulePlugins = allPluginNames(input.plugins)
  //               (every plugin checked; import proceeds with no explicit opt-in)
END FUNCTION
```

#### Examples

- User picks `gst-plugins-good` (74 plugins enumerated). Expected: 0 of 74 selected, import blocked until the user checks plugins or clicks "Select all". Actual: 74 of 74 selected, import proceeds immediately as a whole-module import.
- Edge case: the module plugin list fails to load (`isModuleListingUnavailable`) or is empty. `modulePluginNames.length > 0` is false, so no selection gate applies and the whole-module import proceeds — this fallback must be preserved.
- Edge case: the `pending_selection` dialog (post-fetch plugin-set selection) already defaults to no selection and requires at least one plugin (`pluginSelectionError`); it is unaffected and must stay that way.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors — Bug 1:**
- An explicitly stored temperature keeps being sent (`inferenceConfig.temperature`), with `topP` suppressed (Req 3.1, 3.4).
- A valid per-request override (number in [0, 1], booleans excluded) keeps replacing the configured temperature for that invocation only and suppresses `topP` (Req 3.2).
- Invalid overrides keep rejecting with 400 `INVALID_TEMPERATURE` before any Bedrock call (Req 3.3).
- `temperature` and `topP` are never sent together; `topP` is sent only when no temperature applies and a top_p is explicitly configured (Req 3.4). Note: after the fix, "never configured" and "explicitly configured as null" both resolve to `temperature = None` in the effective config — both mean "omit temperature", and `topP` flows exactly when a top_p was explicitly stored. This is intentional: the fixed defaults make the two states indistinguishable, and the pre-fix distinction only existed because the buggy 0.2 default masked the unset state.
- `GenerateChatPanel.tsx` blank-field behavior: a blank temperature input keeps omitting the `temperature` key from the request payload entirely (Req 3.5) — this code is already correct and is not modified.
- All other Bedrock_Configuration validation rules (model_id, region, max_tokens, timeout clamp ≤ 60 s) and the stored item shape are unchanged.

**Unchanged Behaviors — Bug 2:**
- User-edited port rows (any rename, retype, addition, removal) survive category changes untouched (Req 3.6).
- Non-input categories keep presenting their typical arrangements (Req 3.7) — for `preprocessing` the fixed defaults are byte-identical to today's seeds.
- Any valid port arrangement remains accepted; guidance stays advisory and never gates the wizard (`portsStepErrors` unchanged) (Req 3.8).
- The dismissable divergence advisory (`guidanceDivergence` + `PortGuidancePanel` alert) keeps firing exactly as before (Req 3.9).
- Port_Scan semantics: suggestions replace Untouched_Defaults wholesale and merge additively over user-edited rows (Req 3.10). `isUntouchedDefaults` is generalized (see Fix Implementation) so the new category-shaped defaults still count as untouched.

**Unchanged Behaviors — Bug 3:**
- A checked subset imports exactly that subset (`selected_plugins` recorded) (Req 3.11).
- Selecting every plugin ("Select all") serializes to no `selected_plugins` parameter — whole-module import, today's wire behavior exactly (Req 3.12); `selectedPluginsParam()` is not modified.
- Listing-unavailable / empty-list whole-module fallback is non-blocking (Req 3.13); the gate only applies when `modulePluginNames.length > 0`.
- The `pending_selection` dialog keeps defaulting to no selection with `pluginSelectionError` requiring at least one plugin (Req 3.14); untouched by this fix.

**Scope:**
All inputs outside the three bug conditions are completely unaffected:
- Bug 1: any invocation where a temperature applies (stored or overridden), any settings save with a numeric temperature, all non-temperature settings fields, GenerateChatPanel behavior.
- Bug 2: any wizard interaction after the user edits port rows; every step other than Ports; the divergence alert and Port_Scan behavior over edited rows.
- Bug 3: manual repository URL imports, the pending_selection dialog, classification/acknowledgment/architecture logic, all `importFlow.ts` helpers.

## Hypothesized Root Cause

These are confirmed root causes — all three were located by reading the code:

1. **Bug 1 — a fallback default that should be "unset" is a concrete value**:
   - `workflow_generator.DEFAULT_BEDROCK_CONFIG` (line ~125) and its mirror `data_accounts.DEFAULT_BEDROCK_CONFIG` (line ~91) carry `'temperature': 0.2, 'top_p': 0.9`. `get_bedrock_configuration()` starts from `dict(DEFAULT_BEDROCK_CONFIG)`, so with nothing stored the effective temperature is 0.2 and `invoke_generation()` (both generators, logic `if temperature is not None: send it`) sends it.
   - Downstream, the invocation logic is already correct for `None` — the null-carrying merge in `workflow_generator.get_bedrock_configuration()` (`if key in stored` for temperature/top_p) proves unset was anticipated but the defaults never allowed it to occur without an explicit stored null.
   - `data_accounts.validate_bedrock_configuration()` requires temperature (and top_p) to be numbers, and `read_stored_bedrock_configuration()` uses `if stored.get(key) is not None` for every key, so a stored null would be hidden behind the 0.2 default on read — the settings API can neither store nor faithfully report "unset".
   - `BedrockConfigurationSettings.tsx` `validate()` rejects a blank temperature/top_p field, and the loader does `String(config.temperature)` which would render a null as the literal string "null".

2. **Bug 2 — static seeds with no category linkage**: `initialForm()` in `CreateWizard.tsx` and the detail-load form seed in `RegistrationWizard.tsx` hard-code `inputs: [{...emptyPort(), name: 'in'}], outputs: [{...emptyPort(), name: 'out'}]`. The category `Select.onChange` handlers patch only `category`. `CATEGORY_ARRANGEMENTS` already encodes the correct per-category arrangements but is used solely for display/divergence, never for seeding.

3. **Bug 3 — deliberate default-all seeding**: the module plugin load effect in `ImportView.tsx` (line ~258, comment "Default: ALL plugins selected (whole-module import)") seeds the full list. The selection gate infrastructure (`moduleSelectionIncomplete` → `formComplete`, plus the `errorText` on the checkbox FormField) already exists but never triggers because the selection is never empty on load.

## Correctness Properties

Property 1: Bug Condition — Temperature omitted when unset

_For any_ Bedrock configuration store state in which no temperature value was explicitly stored (nothing stored at all, a stored item without the temperature key, or a stored null) and any generation request without a temperature override, the fixed `get_bedrock_configuration()` + `invoke_generation()` pipeline (workflow and node generators alike) SHALL produce an `inferenceConfig` containing no `temperature` key — and no `topP` key unless a top_p was explicitly stored; and _for any_ settings save with a blank/null temperature (or top_p), the fixed validation SHALL accept the save and store the parameter as unset, round-tripping as unset through GET and the settings form.

**Validates: Requirements 2.1, 2.2, 2.3**

Property 2: Preservation — Explicit temperature behavior unchanged

_For any_ input where the bug condition does NOT hold — a temperature explicitly stored as a number in [0, 1], or a per-request override supplied — the fixed pipeline SHALL produce exactly the same result as the original: the applicable temperature sent as `inferenceConfig.temperature` with `topP` suppressed; invalid overrides (out of range, non-numeric, boolean) rejected with 400 `INVALID_TEMPERATURE` before any Bedrock call; `temperature` and `topP` never sent together; a blank GenerateChatPanel field omitting the `temperature` key from the request payload; and all non-temperature settings validation rules unchanged.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

Property 3: Bug Condition — Default ports follow the selected category

_For any_ palette category in `CATEGORY_ARRANGEMENTS` selected in either wizard while the port rows are Untouched_Defaults (equal to the default arrangement of any category), the fixed wizards SHALL present default port rows whose port-type multisets exactly match that category's typical arrangement (in particular: category `input` → zero input rows and one VideoFrames output; `output` → one input row and zero outputs), every seeded row SHALL carry a non-empty name (so `portsStepErrors` stays clean), `guidanceDivergence(category, inputs, outputs)` SHALL be null for the seeded rows, and the ports step SHALL state that category's input/output requirements.

**Validates: Requirements 2.4, 2.5, 2.6**

Property 4: Preservation — Edited rows, advisory guidance, and Port_Scan unchanged

_For any_ port rows that are NOT Untouched_Defaults (any rename, retype, addition, or removal), the fixed wizards SHALL leave the rows exactly unchanged across any sequence of category changes; `guidanceDivergence` and `portsStepErrors` SHALL answer identically to the original for all inputs; guidance SHALL remain non-blocking; and `applySuggestions` SHALL preserve its replace-over-untouched-defaults / merge-over-edited semantics, with the generalized untouched detection answering true for exactly the per-category default arrangements (including today's in/out pair, which equals the preprocessing defaults).

**Validates: Requirements 3.6, 3.7, 3.8, 3.9, 3.10**

Property 5: Bug Condition — Import selection defaults to none with an explicit gate

_For any_ non-empty module plugin list loading in the import view (including after switching modules), the fixed view SHALL seed the selection empty (0 of N selected) and SHALL keep the import blocked (`formComplete` false, gate message shown) until the user explicitly selects at least one plugin individually or via "Select all".

**Validates: Requirements 2.7, 2.8**

Property 6: Preservation — Import serialization and fallbacks unchanged

_For any_ selection state, the fixed view SHALL serialize exactly as the original: a proper subset serializes to that subset as `selected_plugins`; a full selection (or, when no list is available, no selection) serializes to no `selected_plugins` parameter (whole-module import); an unavailable or empty plugin list SHALL never block the import; and the post-fetch `pending_selection` dialog SHALL keep its default-none, at-least-one-required behavior.

**Validates: Requirements 3.11, 3.12, 3.13, 3.14**

## Fix Implementation

### Changes Required

#### Bug 1 — Temperature omitted when unset

**File**: `edge-cv-portal/backend/functions/workflow_generator.py`

1. **`DEFAULT_BEDROCK_CONFIG`**: change `'temperature': 0.2` → `'temperature': None` and `'top_p': 0.9` → `'top_p': None`, updating the comment: unset by default; sampling parameters are sent only when explicitly configured (or overridden per-request). Both defaults must change together — leaving `top_p: 0.9` would start sending `topP` on every unconfigured invocation, violating Req 3.4 ("top_p only with an explicitly configured top_p") and re-introducing the same rejection risk.
2. **No other changes**: `get_bedrock_configuration()` already carries stored nulls for temperature/top_p (`if key in stored`), and `invoke_generation()` already omits `None` values. The temperature-override path in `generate_workflow()` is untouched.

**File**: `edge-cv-portal/backend/functions/node_generator.py`

3. **No changes**: it imports `get_bedrock_configuration` from `workflow_generator` and its `invoke_generation` already omits `None`. Fixed transitively; covered by tests.

**File**: `edge-cv-portal/backend/functions/data_accounts.py`

4. **`DEFAULT_BEDROCK_CONFIG` mirror**: same change as (1) — the two constants must stay identical ("Must mirror workflow_generator.DEFAULT_BEDROCK_CONFIG").
5. **`read_stored_bedrock_configuration()`**: special-case `temperature` / `top_p` exactly like `workflow_generator.get_bedrock_configuration()` does (`if key in stored: config[key] = stored[key]` instead of `if stored.get(key) is not None`), so a stored null reads back as unset rather than being replaced by the default.
6. **`validate_bedrock_configuration()`**: accept `None` for `temperature` and `top_p` (`if temperature is not None and (not _is_number(temperature) or not (0 <= temperature <= 1))`). Numbers outside [0, 1] and non-numeric values keep rejecting. All other field validations unchanged.
7. **`update_bedrock_configuration_setting()`**: no logic change needed — `if key in body: config[key] = body[key]` already carries an explicit JSON `null` through the merge, validation now accepts it, and the written `value` dict stores it as a DynamoDB null. Verify `_native_to_dynamo` passes `None` through (it converts floats to Decimal; `None` must survive).

**File**: `edge-cv-portal/frontend/src/components/BedrockConfigurationSettings.tsx`

8. **`validate()`**: blank temperature and blank top_p become valid (meaning "unset — omitted at invocation"); non-blank values keep requiring a number in [0, 1].
9. **Load effect**: map a null/undefined `config.temperature` / `config.top_p` to `''` instead of `String(null)`.
10. **`handleSave()`**: send `temperature: form.temperature.trim() === '' ? null : Number(form.temperature)` (same for `top_p`) — the explicit `null` is required because the backend merges provided keys over the current configuration, so omitting the key would keep the old value instead of unsetting it.
11. **Constraint text**: Temperature / Top P fields note "0 to 1 — leave blank to let the model use its default (parameter is omitted)".

**Existing tests to update**: `test_workflow_generation.py::test_default_configuration_sends_temperature_without_top_p` asserts the buggy `temperature == 0.2` default and becomes "default configuration sends no sampling parameters"; `test_bedrock_configuration.py` gains blank/null acceptance cases (its invalid-value rejections for 1.5 / -0.1 / non-numbers stay).

#### Bug 2 — Category-driven default ports

**File**: `edge-cv-portal/frontend/src/pages/node-designer/declaration.ts` (new pure helpers; alternatively `portGuidance.ts`, but `declaration.ts` owns `PortForm`)

1. **`defaultPortsForCategory(category: string): { inputs: PortForm[]; outputs: PortForm[] }`**: derived from `CATEGORY_ARRANGEMENTS`:
   - `input`: `inputs: []`, `outputs: [{name: 'out', portType: 'VideoFrames'}]`
   - `preprocessing`: `[{name: 'in', portType: 'VideoFrames'}]` / `[{name: 'out', portType: 'VideoFrames'}]` (byte-identical to today's seeds)
   - `inference`: `[{name: 'in', portType: 'VideoFrames'}]` / `[{name: 'out', portType: 'InferenceMeta'}]`
   - `post_processing`: `[{name: 'in', portType: 'InferenceMeta'}]` / `[{name: 'out', portType: 'EventSignal'}]`
   - `output` (`'at-least-one'`): `[{name: 'in', portType: 'VideoFrames'}]` / `[]` — one concrete VideoFrames input as the seeded representative of "at least one input of any type"
   - unknown category: fall back to the preprocessing shape (today's seeds).
   Every seeded row has a non-empty name so `portsStepErrors` never fires on defaults, and each concrete arrangement yields `guidanceDivergence === null` (the `output` seed satisfies `'at-least-one'`).
2. **`isDefaultPortArrangement(inputs: PortForm[], outputs: PortForm[]): boolean`**: true exactly when the rows deep-equal `defaultPortsForCategory(c)` for some category `c`. This is the generalized Untouched_Defaults notion — any rename, retype, addition, or removal makes it false.

**File**: `edge-cv-portal/frontend/src/pages/node-designer/portScan.ts`

3. **`isUntouchedDefaults()`**: delegate to `isDefaultPortArrangement()` (import from `declaration.ts`; `portScan.ts` already imports `PortForm` from there). Today's in/out pair equals the preprocessing defaults, so all current true-cases stay true; the new category defaults additionally count as untouched, keeping Port_Scan's replace-over-defaults semantics coherent with the new seeding (Req 3.10). `applySuggestions()` and `removalBlockReason()` are untouched.

**File**: `edge-cv-portal/frontend/src/pages/node-designer/CreateWizard.tsx`

4. **`initialForm()`**: seed `...defaultPortsForCategory('preprocessing')` instead of the hard-coded rows (no behavioral change for the initial state).
5. **Palette category `Select.onChange`**: when `isDefaultPortArrangement(form.inputs, form.outputs)` is true, patch `{category, ...defaultPortsForCategory(newCategory)}`; otherwise patch `{category}` only (Req 2.5, 3.6).

**File**: `edge-cv-portal/frontend/src/pages/node-designer/RegistrationWizard.tsx`

6. **Detail-load form seed** (line ~138): seed `...defaultPortsForCategory('preprocessing')`. When the wizard prefills from an existing registered declaration, those rows come from the declaration, not the seeds, and will not match a default arrangement unless coincidentally identical — accepted edge case, consistent with today's `isUntouchedDefaults` semantics.
7. **Palette category `Select.onChange`**: same untouched-defaults rewrite as (5).

**File**: `edge-cv-portal/frontend/src/pages/node-designer/portGuidance.ts` + `PortGuidancePanel.tsx`

8. **Per-kind requirements statement (Req 2.6)**: add a pure `arrangementRequirements(category)` helper deriving explicit lines from `CATEGORY_ARRANGEMENTS` (e.g. input → "Inputs: none · Outputs: 1 × VideoFrames"; output → "Inputs: at least one (any port type) · Outputs: none") and render it in `PortGuidancePanel` under the existing arrangement summary, clearly labeled as the typical requirement per node kind. The panel stays purely advisory — no contribution to step gating (Req 3.8).

#### Bug 3 — Opt-in import selection

**File**: `edge-cv-portal/frontend/src/pages/node-designer/ImportView.tsx`

1. **Module plugin load effect** (line ~258): `setSelectedModulePlugins([])` instead of `allPluginNames(plugins)`; update the comment ("Default: none selected — the user opts in explicitly"). The effect already resets the selection to `[]` on module change, so switching modules also lands on none-selected.
2. **Checkbox FormField copy** (line ~859): description becomes opt-in wording, e.g. "Choose which of the module's plugins to import and build. No plugins are selected by default — select individual plugins or use Select all to import the whole module." The existing `errorText` ("Select at least one plugin to import") now shows on the pristine empty state and serves as the visible gate message (Req 2.8).
3. **No other changes**: the gate (`moduleSelectionIncomplete` → `formComplete`), `selectedPluginsParam()`, `moduleSelectionSummary()`, "Select all"/"Clear" buttons, the listing-unavailable fallback, and the `pending_selection` dialog are all already correct and untouched.

**File**: `edge-cv-portal/frontend/src/pages/node-designer/importFlow.ts`

4. **Optional pure extraction for testability**: `moduleSelectionIncomplete(source, availableNames, selectedNames): boolean` mirroring the inline gate expression, used by the component and property-tested directly. (Small, safe refactor; the inline expression may alternatively stay and be covered by component tests.)

## Testing Strategy

### Validation Approach

Two phases per bug: first run exploratory tests asserting the *correct* behavior against the UNFIXED code to surface counterexamples and confirm the root causes; then implement the fixes and verify fix checking (bug inputs now behave correctly) and preservation checking (non-bug inputs unchanged, F(X) = F'(X)). Backend property tests use **hypothesis** (established convention: `edge-cv-portal/backend/tests/test_property_*.py`, fixtures in `tests/conftest.py` with moto/mocked Bedrock clients as in `test_workflow_generation.py`). Frontend property tests use **fast-check + vitest** (already a devDependency; component behavior via the existing React Testing Library patterns, e.g. `GenerateChatPanel.test.tsx`).

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples demonstrating each bug BEFORE fixing, confirming the root cause analysis (all three are already confirmed by code reading; the exploratory runs document the failures).

**Test Plan**: Write the fix-checking tests below and run them on the unfixed code.

**Test Cases**:
1. **Unset temperature omitted (backend)**: with an empty settings table, POST /workflows/generate and assert `converse` received no `temperature` in `inferenceConfig` (will fail on unfixed code: receives 0.2).
2. **Node generator unset temperature**: same assertion through `node_generator`'s generation turn (will fail on unfixed code).
3. **Blank temperature save**: PUT bedrock-configuration with `{"temperature": null}` expecting 200 (will fail on unfixed code: 400 validation error).
4. **Input-category defaults (frontend)**: render CreateWizard, select category `input`, assert zero input rows and one VideoFrames output row (will fail on unfixed code: "in" row present).
5. **Import default selection (frontend)**: render ImportView with a mocked module plugin list, assert 0 selected and import blocked (will fail on unfixed code: all selected, import enabled).

**Expected Counterexamples**:
- `inferenceConfig == {maxTokens: 4096, temperature: 0.2}` with nothing stored
- 400 "temperature must be a number between 0 and 1" for a null temperature save
- Untouched port rows `in/out` after selecting category `input`; `selectedModulePlugins.length === plugins.length` after list load

### Fix Checking

**Goal**: Verify that for all inputs where a bug condition holds, the fixed code produces the expected behavior.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition1(input) OR isBugCondition2(input) OR isBugCondition3(input) DO
  result := fixedSystem(input)
  ASSERT expectedBehavior(result)   // Properties 1, 3, 5
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where no bug condition holds, the fixed code produces the same result as the original.

**Pseudocode:**
```
FOR ALL input WHERE NOT (isBugCondition1(input) OR isBugCondition2(input) OR isBugCondition3(input)) DO
  ASSERT originalSystem(input) = fixedSystem(input)   // Properties 2, 4, 6
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many test cases automatically across the input domain (arbitrary stored configs, arbitrary port-row edits, arbitrary plugin lists/selections)
- It catches edge cases manual unit tests miss (temperature 0 vs None vs absent key; port rows coincidentally equal to another category's defaults; full-selection vs empty-selection serialization)
- It provides strong guarantees that behavior is unchanged for all non-buggy inputs

**Test Plan**: The original behavior for non-bug inputs is already pinned by existing suites — `test_workflow_generation.py` (explicit/override/null-temperature/top_p rules, INVALID_TEMPERATURE), `test_bedrock_configuration.py` (validation and item shape), `GenerateChatPanel.test.tsx` (blank-field omission), the portScan/portGuidance frontend tests, and `test_plugin_import_selection.py` / importFlow tests. Keeping those suites green (with the one inverted default-behavior test noted above) plus the new property tests constitutes preservation checking.

**Test Cases**:
1. **Explicit temperature preservation (hypothesis)**: for any stored temperature in [0, 1] and optional override, the fixed pipeline sends exactly the temperature the original rules dictate, `topP` suppressed.
2. **Edited port rows preservation (fast-check)**: for any port rows not equal to any category's defaults and any category-change sequence, rows are returned unchanged.
3. **Selection serialization preservation (fast-check)**: for any plugin list and selection, `selectedPluginsParam` answers as today (subset → subset; full/empty/unavailable → undefined).

### Unit Tests

- Backend (`edge-cv-portal/backend/tests`): default config omits both sampling parameters; stored-null temperature with stored top_p sends `topP` only; PUT accepts `{"temperature": null}` / `{"top_p": null}` and the value round-trips as null through GET; `read_stored_bedrock_configuration` carries stored nulls; existing invalid-value rejections unchanged; node-generator turn with unset temperature sends no sampling parameters.
- Frontend (vitest + RTL): `defaultPortsForCategory` per-category shapes; wizard category change rewrites untouched defaults and preserves edited rows (both wizards); `PortGuidancePanel` renders per-kind requirement lines; Bedrock settings form accepts blank temperature/top_p, loads null as blank, saves blank as null; ImportView seeds empty selection, blocks import until selection, "Select all" then import serializes to no `selected_plugins`.

### Property-Based Tests

- **hypothesis** (backend, `edge-cv-portal/backend/tests/test_property_*.py`): generate arbitrary stored-config variants (absent item, missing keys, explicit nulls, numeric values, Decimal-encoded values) × optional request overrides; assert Property 1 (no `temperature`/unexpected `topP` when unset) and Property 2 (exact original inference config and 400 behavior otherwise; `temperature` and `topP` never both present in any generated case).
- **fast-check** (frontend): generate categories × port-row edit sequences; assert Property 3 (untouched defaults match `CATEGORY_ARRANGEMENTS` multisets, names non-empty, `guidanceDivergence` null) and Property 4 (edited rows invariant under category changes; `isDefaultPortArrangement` true exactly for per-category defaults; `applySuggestions` replace/merge semantics unchanged over both old and new default shapes).
- **fast-check** (frontend): generate plugin lists × selection actions; assert Property 5 (initial selection empty, gate blocks until non-empty) and Property 6 (`selectedPluginsParam` / `moduleSelectionSummary` unchanged for all selections; empty-or-unavailable list never blocks).

### Integration Tests

- Full generate flow (mock Bedrock, empty settings table): prompt → 200 with a definition, `converse` called with `inferenceConfig = {maxTokens: 4096}` only; then store a temperature via the settings PUT and confirm the next generation sends it; then PUT `{"temperature": null}` and confirm it is omitted again.
- Create wizard end-to-end: select `input`, keep the seeded ports, submit — declaration posts with `inputs: []` and one VideoFrames output; Registration wizard: edit a port, change category twice, run a Port_Scan — edited rows merge additively.
- Import flow end-to-end (mocked API): choose a module, verify import blocked at 0 selected; select a subset → request carries `selected_plugins`; "Select all" → request carries no `selected_plugins`; simulate listing failure → import proceeds whole-module.
