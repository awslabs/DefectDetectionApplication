# Bugfix Requirements Document

## Introduction

This spec covers three distinct bugs reported in the edge-cv-portal workflow/node designer:

1. **Generate-workflow temperature handling**: The Bedrock invocations behind workflow generation (`workflow_generator.py`) and node generation (`node_generator.py`, which reuses `get_bedrock_configuration()`) always send a sampling temperature because `DEFAULT_BEDROCK_CONFIG` bakes in `temperature: 0.2`. Newer Anthropic models (e.g. Opus 4.x-class models) reject requests that set temperature at all, failing generation with a "deprecated parameter" style `BEDROCK_INVOCATION_FAILED` error. When no explicit temperature value has been configured by an admin or provided per-request, the temperature parameter must be omitted from the invocation entirely. The PortalAdmin Bedrock settings form (`BedrockConfigurationSettings.tsx`) and its backend validation (`data_accounts.py`) currently refuse a blank temperature, so an admin cannot even unset it.

2. **Input-type custom nodes carry a default input port**: The node-designer create and registration wizards (`CreateWizard.tsx`, `RegistrationWizard.tsx`) seed the port declaration with one input port ("in") and one output port ("out") regardless of the selected palette category. When the category is `input` (a source node), the wizard still presents an input port — contradicting the wizard's own Port_Guidance ("Input nodes typically declare no inputs and one VideoFrames output") and leaving users unable to tell what is actually required for inputs vs outputs per node kind.

3. **Node import selects every plugin by default**: In the import flow (`ImportView.tsx`), when an official module's individual plugin list loads, every plugin is checked by default (`setSelectedModulePlugins(allPluginNames(plugins))`). The user must uncheck everything they do not want instead of opting in to what to import.

## Bug Analysis

### Current Behavior (Defect)

**Bug 1 — Temperature always sent to Bedrock**

1.1 WHEN a workflow generation is invoked and no temperature has been explicitly configured in the portal Bedrock settings and no per-request temperature override is given THEN the system sends the default temperature 0.2 in the Bedrock inference configuration, and models that reject the temperature parameter fail the generation with a deprecated-parameter invocation error

1.2 WHEN a node (plugin scaffold) generation is invoked and no temperature has been explicitly configured and no override is given THEN the system sends the default temperature 0.2 in the Bedrock inference configuration, failing on models that reject the temperature parameter

1.3 WHEN a PortalAdmin clears the Temperature field in the Bedrock configuration settings form THEN the system rejects the save with "Temperature must be between 0 and 1", so the configured temperature cannot be unset

**Bug 2 — Input-category nodes presented with an input port**

1.4 WHEN a user creates or registers a custom node and selects the `input` palette category THEN the system keeps presenting the default input port row ("in"), implying an input port is expected for a source node

1.5 WHEN a user selects a palette category in either wizard THEN the system leaves the untouched default port rows (one "in" input, one "out" output) unchanged, so the presented ports contradict the category's typical arrangement and the user cannot tell what is required for inputs and outputs per node kind

**Bug 3 — Module import checks all plugins by default**

1.6 WHEN an official module's plugin list loads during node import THEN the system checks every enumerated plugin by default, requiring the user to uncheck unwanted plugins instead of opting in

### Expected Behavior (Correct)

**Bug 1 — Omit temperature when no value is given**

2.1 WHEN a workflow generation is invoked and no temperature has been explicitly configured in the portal Bedrock settings and no per-request temperature override is given THEN the system SHALL omit the temperature parameter entirely from the Bedrock inference configuration

2.2 WHEN a node (plugin scaffold) generation is invoked and no temperature has been explicitly configured and no override is given THEN the system SHALL omit the temperature parameter entirely from the Bedrock inference configuration

2.3 WHEN a PortalAdmin clears the Temperature field in the Bedrock configuration settings form and saves THEN the system SHALL accept the save and store the temperature as unset, meaning no temperature is sent at invocation

**Bug 2 — Port defaults and requirements follow the node kind**

2.4 WHEN a user selects the `input` palette category in either wizard and the port rows are still the untouched defaults THEN the system SHALL present no input port rows (and one VideoFrames output), matching the category's typical arrangement

2.5 WHEN a user selects any palette category in either wizard and the port rows are still the untouched defaults THEN the system SHALL adjust the default port rows to that category's typical arrangement, so the presented inputs and outputs reflect what the node kind requires

2.6 WHEN a user views the ports step for a selected category THEN the system SHALL clearly state what is required for inputs and outputs for that node kind (e.g. input nodes: no inputs, one output; output nodes: at least one input, no outputs)

**Bug 3 — Import selection is opt-in**

2.7 WHEN an official module's plugin list loads during node import THEN the system SHALL check no plugins by default, so the user explicitly opts in to what to import

2.8 WHEN the module plugin list is available and the user has selected no plugins THEN the system SHALL require an explicit selection (individual checks or "Select all") before the import proceeds, instead of silently importing the whole module

### Unchanged Behavior (Regression Prevention)

**Bug 1**

3.1 WHEN a temperature has been explicitly configured in the portal Bedrock settings THEN the system SHALL CONTINUE TO send that temperature in the Bedrock inference configuration

3.2 WHEN a valid per-request temperature override (a number in [0, 1]) is supplied to POST /workflows/generate THEN the system SHALL CONTINUE TO send that temperature for that invocation, suppressing top_p

3.3 WHEN an out-of-range or non-numeric per-request temperature is supplied THEN the system SHALL CONTINUE TO reject the request with 400 INVALID_TEMPERATURE

3.4 WHEN both a temperature and a top_p would apply to an invocation THEN the system SHALL CONTINUE TO never send temperature and top_p together (temperature wins when set; top_p is sent only when temperature is explicitly configured as null with a configured top_p)

3.5 WHEN the GenerateChatPanel temperature field is left blank THEN the system SHALL CONTINUE TO omit the temperature key from the generate request payload

**Bug 2**

3.6 WHEN a user has edited the port rows (renamed, retyped, added, or removed any port) and then changes the palette category THEN the system SHALL CONTINUE TO preserve the user-edited port rows without rewriting them

3.7 WHEN a non-input palette category (preprocessing, inference, post_processing, output) is selected THEN the system SHALL CONTINUE TO present that category's expected input and output ports per its typical arrangement

3.8 WHEN a user declares any valid port arrangement, including one diverging from the category's typical arrangement THEN the system SHALL CONTINUE TO accept the declaration (guidance stays advisory, non-blocking)

3.9 WHEN the declared ports diverge from the selected category's typical arrangement THEN the system SHALL CONTINUE TO show the dismissable Port_Guidance divergence advisory

3.10 WHEN a Port_Scan is applied over untouched default port rows THEN the system SHALL CONTINUE TO replace the defaults with the pad-derived suggestions, and over user-edited rows SHALL CONTINUE TO merge additively

**Bug 3**

3.11 WHEN the user checks individual plugins from the module plugin list THEN the system SHALL CONTINUE TO import exactly the selected subset (recorded as selected_plugins)

3.12 WHEN the user selects every plugin (e.g. via "Select all") THEN the system SHALL CONTINUE TO import the whole module (a full selection serializes to no selected_plugins parameter, today's whole-module behavior)

3.13 WHEN the module plugin list fails to load THEN the system SHALL CONTINUE TO proceed with the whole-module import as a non-blocking fallback

3.14 WHEN a plugin-set import lands in pending_selection THEN the system SHALL CONTINUE TO default that selection dialog to no plugins selected and require at least one plugin before submission
