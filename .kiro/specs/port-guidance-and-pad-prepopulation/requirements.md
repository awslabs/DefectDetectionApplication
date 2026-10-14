# Requirements Document

## Introduction

This feature extends the Custom Node Designer (specs: custom-node-designer, gst-parameter-prepopulation) so that users understand what Ports are and choose Port types deliberately instead of guessing. Today the Ports step of the wizards defaults to one input "in" and one output "out", both typed VideoFrames, with no explanation of what a Port is, how workflow connections use it, or which of the three Port_Types (VideoFrames, InferenceMeta, EventSignal) fits which situation.

The feature has three parts. First, the Ports step explains Ports in place: what they are, how the Workflow_Designer connects them, and how to choose a Port_Type, including typical arrangements per palette category. Second, the build-time GStreamer introspection pipeline (the same pipeline that captures element properties for parameter pre-population) additionally captures each element's static Pad_Templates — name template, direction (sink/src), presence (always/sometimes/request), and caps. Third, the Registration wizard's Ports step uses the captured Pad_Templates to pre-populate and guide the port declaration: always-present sink pads suggest input Ports, always-present src pads suggest output Ports, and caps beginning with `video/x-raw` map confidently to VideoFrames. InferenceMeta and EventSignal are DDA semantic concepts that GStreamer caps cannot express, so those suggestions stay user-confirmed with guidance. Sometimes/request pads (for example `src_%u`) do not correspond to fixed declared Ports and are surfaced as advisory notes with a caveat rather than silently added.

Pre-population is an aid, not a lock: introspection capture remains x86_64-only, existing stored version-1 reports without pad data must keep working unchanged, and the manual port flow keeps working whenever no pad data is available.

## Glossary

- **Node_Designer**: The Portal capability (spec: custom-node-designer) for creating, importing, building, simulating, and registering Custom_Node_Types.
- **Workflow_Designer**: The Portal capability where users assemble workflows by connecting node Ports on a canvas.
- **Port**: One declared connection point of a Custom_Node_Type — an input Port receives data from an upstream node, an output Port sends data to a downstream node. Workflow connections join an output Port to an input Port of a compatible Port_Type.
- **Port_Type**: The declared data type of a Port, one of the Node_Type_Catalog types: VideoFrames (a stream of video frames), InferenceMeta (inference results such as detections or classifications attached to frames), EventSignal (discrete trigger or notification events).
- **Ports_Step**: The port-declaration step of the Node_Designer wizards — the Create wizard (`CreateWizard.tsx`) and the Registration wizard (`RegistrationWizard.tsx`).
- **Registration_Wizard**: The Node_Designer wizard that registers a Custom_Node_Type for a built Plugin_Record version.
- **Create_Wizard**: The Node_Designer wizard that collects a declaration and generates a Plugin_Scaffold; no Plugin_Record or Plugin_Artifact exists while it runs.
- **Port_Guidance**: The explanatory content the Ports_Step displays: what a Port is, how workflow connections use Ports, what each Port_Type carries, and which Port_Type arrangements are typical for each palette category.
- **Plugin_Record**: The stored metadata for a created, generated, or imported plugin, including per-architecture Plugin_Artifact entries (spec: custom-node-designer).
- **Plugin_Artifact**: A built plugin binary (`.so`) for one Target_Architecture stored in the Plugin_Library (spec: custom-node-designer).
- **Plugin_Build_Service**: The per-Target_Architecture CodeBuild-based build pipeline (`plugin_builds.py`, `dda-plugin-build`, `plugin-build-images/`) that compiles plugin source into Plugin_Artifacts and runs build-time introspection on x86_64.
- **Property_Introspection**: The act of loading a built Plugin_Artifact into a GStreamer runtime and reading each registered element's metadata (spec: gst-parameter-prepopulation); this feature extends it to also read static Pad_Templates.
- **Introspection_Report**: The stored, structured result of Property_Introspection for one Plugin_Artifact (spec: gst-parameter-prepopulation); this feature extends it with per-element Pad_Template data.
- **Pad_Template**: One static pad template a GStreamer element class declares: the name template (for example `sink`, `src`, `src_%u`), the Pad_Direction, the Pad_Presence, and the Caps.
- **Pad_Direction**: The data direction of a Pad_Template: sink (the element receives data) or src (the element produces data).
- **Pad_Presence**: The availability of a Pad_Template's pads: always (a pad exists on every element instance), sometimes (a pad may appear during runtime), or request (a pad is created on demand, often with a numbered name template such as `src_%u`).
- **Caps**: The GStreamer capabilities string of a Pad_Template describing the media formats the pad accepts or produces (for example `video/x-raw`, `video/x-raw(memory:NVMM)`, `ANY`).
- **Port_Suggestion**: One pre-populated Port declaration derived from a Pad_Template: the Port name, the Port_Type, whether the mapping is confident or needs user confirmation, and the derivation reason.
- **Confident_Suggestion**: A Port_Suggestion whose Port_Type mapping is derived directly from the Caps (Caps beginning with `video/x-raw` map to VideoFrames).
- **Unconfirmed_Suggestion**: A Port_Suggestion whose Port_Type cannot be derived from the Caps because InferenceMeta and EventSignal are DDA semantic concepts not expressible in Caps; the user confirms the Port_Type with guidance.
- **Unmapped_Pad**: A Pad_Template that does not map to a declared Port (Pad_Presence sometimes or request), surfaced to the user as an advisory note with a caveat.
- **Port_Scan**: The Ports_Step action that fetches the Introspection_Report for the wizard's Plugin_Record version, derives Port_Suggestions and Unmapped_Pads from the Pad_Templates, and applies the Port_Suggestions to the port lists.
- **Untouched_Defaults**: The wizard-supplied initial port lists (one input named "in" and one output named "out", both VideoFrames) that the user has not edited.

## Requirements

### Requirement 1: Explain Ports in the Ports Step

**User Story:** As a computer vision engineer who has never declared a node type before, I want the Ports step to explain what ports are and how workflows use them, so that I understand what I am declaring instead of guessing.

#### Acceptance Criteria

1. WHEN a user reaches the Ports_Step of the Create_Wizard or the Registration_Wizard, THE Ports_Step SHALL display Port_Guidance within the Ports_Step view, without requiring navigation away from the Ports_Step, explaining what a Port is and stating the connection rule the Workflow_Designer enforces: a workflow connection joins an output Port to an input Port of a compatible Port_Type.
2. THE Port_Guidance SHALL describe each Port_Type in the Node_Type_Catalog (VideoFrames, InferenceMeta, EventSignal) with the data the Port_Type carries and at least one usage example per Port_Type that names a node role and states whether that role uses the Port_Type as an input or an output.
3. THE Port_Guidance SHALL describe the distinction between input Ports (data the node receives) and output Ports (data the node produces).
4. THE Ports_Step SHALL display the Port_Guidance when no Plugin_Record, Plugin_Artifact, or Introspection_Report exists, and SHALL NOT issue any network request solely to render the Port_Guidance.
5. THE Ports_Step SHALL display identical Port_Guidance content for the Port definition, the input/output distinction, and the Port_Type descriptions in both the Create_Wizard and the Registration_Wizard.

### Requirement 2: Pre-Selection Guidance by Palette Category

**User Story:** As a computer vision engineer, I want the Ports step to tell me which port arrangement is typical for the kind of node I am building, so that I pick sensible port types for my node's role without trial and error.

#### Acceptance Criteria

1. WHEN the Ports_Step is displayed and a palette category is selected, THE Ports_Step SHALL display the typical input and output Port_Type arrangement defined for the selected category, with a guidance arrangement defined for each of the five palette categories (input, preprocessing, inference, post_processing, output; for example: preprocessing nodes typically declare one VideoFrames input and one VideoFrames output, inference nodes typically declare one VideoFrames input and one InferenceMeta output, output nodes typically declare at least one input and no outputs).
2. WHEN the user changes the selected palette category, THE Ports_Step SHALL replace the displayed category guidance with the newly selected category's guidance without requiring the user to navigate away from and back to the Ports_Step.
3. THE Ports_Step SHALL accept any port declaration that passes the existing port validation rules, regardless of whether the declaration matches the displayed category guidance.
4. IF the user's declared ports differ from the displayed category guidance in the count of input ports, the count of output ports, or the Port_Type of any declared port, THEN THE Ports_Step SHALL display a non-blocking advisory warning that identifies which side of the declaration diverges (inputs, outputs, or both) while continuing to accept the declaration.
5. WHEN the user edits the port declaration so that it no longer diverges from the displayed category guidance, THE Ports_Step SHALL remove the advisory warning.

### Requirement 3: Capture Pad Templates in the Introspection Report

**User Story:** As a computer vision engineer, I want the portal to read my plugin's actual pad templates from the built binary, so that port pre-population reflects what the element really declares instead of what I remember from the source.

#### Acceptance Criteria

1. WHEN the Plugin_Build_Service performs Property_Introspection on a successfully built x86_64 Plugin_Artifact, THE Plugin_Build_Service SHALL record in the Introspection_Report, for each element factory, every static Pad_Template with its name template, Pad_Direction, Pad_Presence, and Caps string.
2. IF reading the Pad_Templates of one element factory fails, THEN THE Plugin_Build_Service SHALL record that element with an empty Pad_Template list and a diagnostic message describing the read failure, SHALL preserve the element's property data unchanged, and SHALL preserve the build's success status.
3. IF an Introspection_Report including Pad_Template data exceeds the existing 256 KiB size cap, THEN THE Plugin_Build_Service SHALL record the introspection outcome as failed with a diagnostic message indicating the size cap was exceeded and SHALL preserve the build's success status.
4. WHERE a Pad_Template's Caps string exceeds 4096 characters, THE Plugin_Build_Service SHALL record the first 4096 characters of the Caps string together with a machine-readable truncation indicator on that Pad_Template rather than omitting the Pad_Template.
5. WHEN an element factory declares no static Pad_Templates, THE Plugin_Build_Service SHALL record that element with an empty Pad_Template list and no diagnostic message, distinguishable from the read-failure case in criterion 2.

### Requirement 4: Report Compatibility and Serialization

**User Story:** As a developer, I want the extended report shape parsed and served without breaking existing stored reports, so that plugins built before this feature keep working exactly as before.

#### Acceptance Criteria

1. WHEN the Node_Designer parses a stored Introspection_Report that contains Pad_Template data, THE Node_Designer SHALL parse, for each element factory, every Pad_Template with its name template, Pad_Direction, Pad_Presence, Caps string, and Caps truncation marker, alongside the element's existing property data.
2. WHEN the Node_Designer parses a stored version-1 Introspection_Report that contains no Pad_Template data, THE Node_Designer SHALL parse the report successfully, SHALL report an empty Pad_Template list for every element factory, and SHALL produce element property data field-for-field identical to the parse result produced before this feature for the same stored report.
3. WHEN the Node_Designer serializes a valid Introspection_Report containing Pad_Template data and then parses the serialized document, THE Node_Designer SHALL produce an Introspection_Report in which every report-level field, every element field, and every Pad_Template field (name template, Pad_Direction, Pad_Presence, Caps string, Caps truncation marker) equals the corresponding field of the original report.
4. IF a stored Introspection_Report contains malformed Pad_Template data — a Pad_Template collection that is not a list, a Pad_Template entry that is not a record, or a Pad_Template entry with a missing or mistyped field, a Pad_Direction outside the set {sink, src}, or a Pad_Presence outside the set {always, sometimes, request} — THEN THE Node_Designer API SHALL reject the entire report without returning any partial Pad_Template data and SHALL respond with the existing "introspection failed" unavailability reason instead of an internal error.
5. WHEN an Introspection_Report is requested through the existing gst-properties API route, THE Node_Designer SHALL return, for each element factory, the derived Port_Suggestions and Unmapped_Pads alongside the existing Parameter_Suggestions.
6. WHEN the gst-properties API route serves any stored Introspection_Report, THE Node_Designer SHALL return the existing Parameter_Suggestion and skipped-property response fields with unchanged names, structure, and values compared to the response produced before this feature for the same stored report.
7. IF the stored Introspection_Report is a version-1 report containing no Pad_Template data, THEN THE Node_Designer API SHALL return an empty Port_Suggestion list and an empty Unmapped_Pad list for every element factory, each accompanied by a machine-readable reason indicating the report predates pad capture.
8. IF the stored Introspection_Report contains Pad_Template data and the requested element factory's Pad_Template list is empty, THEN THE Node_Designer API SHALL return an empty Port_Suggestion list and an empty Unmapped_Pad list for that element factory, each accompanied by a machine-readable reason indicating the element declares no pad templates.

### Requirement 5: Derive Port Suggestions from Pad Templates

**User Story:** As a computer vision engineer, I want the declared pads of my element converted into sensible port suggestions, so that the declared ports match the element's real connection points.

#### Acceptance Criteria

1. WHEN deriving Port_Suggestions from an element's Pad_Templates, THE Node_Designer SHALL map each Pad_Template with Pad_Presence always and Pad_Direction sink to an input Port_Suggestion, and each Pad_Template with Pad_Presence always and Pad_Direction src to an output Port_Suggestion, using the Pad_Template's name template as the suggested Port name and preserving the order in which the Pad_Templates appear in the Introspection_Report.
2. WHEN a mapped Pad_Template's Caps string, including a Caps string marked as truncated, begins with the exact case-sensitive characters `video/x-raw`, THE Node_Designer SHALL produce a Confident_Suggestion with Port_Type VideoFrames.
3. WHEN a Pad_Template maps to a Port_Suggestion per criterion 5.1 and its Caps string does not begin with the exact case-sensitive characters `video/x-raw`, THE Node_Designer SHALL produce an Unconfirmed_Suggestion carrying the Caps string and Port_Type defaulted to VideoFrames, and the derivation reason SHALL state that InferenceMeta and EventSignal are DDA semantic concepts the Caps cannot express.
4. WHEN an element declares a Pad_Template with Pad_Presence sometimes or request, THE Node_Designer SHALL report the Pad_Template as an Unmapped_Pad with its name template, Pad_Direction, Pad_Presence, and a caveat explaining that such pads do not correspond to fixed declared Ports.
5. THE Node_Designer SHALL derive only Port_Suggestions that satisfy the existing Ports_Step validation rules (non-empty Port name, Port_Type from the Node_Type_Catalog).
6. IF using a Pad_Template's name template as the Port name would violate the existing Ports_Step validation rules, THEN THE Node_Designer SHALL exclude that Pad_Template from the Port_Suggestions and SHALL report it as an Unmapped_Pad with a caveat stating that its name template is not a valid Port name.
7. WHEN the same Introspection_Report Pad_Template data is derived more than once, THE Node_Designer SHALL produce identical Port_Suggestions and Unmapped_Pads on every derivation.

### Requirement 6: Port Scan in the Registration Wizard

**User Story:** As a computer vision engineer, I want the Ports step of the Registration wizard pre-populated from the built plugin's pads, so that I start from the element's real connection points instead of the generic defaults.

#### Acceptance Criteria

1. WHEN a user reaches the Ports_Step of the Registration_Wizard, the wizard's Plugin_Record version has an available Introspection_Report containing Pad_Template data, and the port lists are the Untouched_Defaults, THE Ports_Step SHALL automatically run the Port_Scan without any user action and, on scan completion with at least one Port_Suggestion, SHALL replace the Untouched_Defaults with the derived Port_Suggestions.
2. WHEN a Port_Scan completes and the user has edited the port lists, THE Ports_Step SHALL keep every user-edited Port declaration unchanged and SHALL report each Port_Suggestion whose name exactly matches (case-sensitive comparison) the name of an existing Port as already declared, without modifying that Port.
3. THE Ports_Step SHALL provide a manual scan control that runs the Port_Scan on demand.
4. WHEN a Port_Scan completes, THE Ports_Step SHALL display the scan outcome: the Port_Suggestions applied, each Unconfirmed_Suggestion with its Caps string and confirmation guidance, and each Unmapped_Pad with its caveat.
5. WHILE an Unconfirmed_Suggestion's Port_Type has not been confirmed or edited by the user, THE Ports_Step SHALL display a visible indicator on that Port marking it as needing Port_Type confirmation, distinct from the presentation of confirmed Ports.
6. WHERE a Plugin_Artifact registers more than one element factory, THE Ports_Step SHALL derive Port_Suggestions from the same element factory the Parameter_Scan selects, selecting the element factory whose name exactly matches the wizard's declared element factory when one matches.
7. WHILE a Port_Scan is in progress, THE Ports_Step SHALL keep the manual port controls (add, edit, remove) usable, SHALL disable the manual scan control so that no second Port_Scan starts concurrently, and SHALL never block step navigation on the scan.
8. WHEN a Port_Scan applies Port_Suggestions, THE Ports_Step SHALL leave every applied Port editable and removable by the user exactly like a manually added Port.
9. IF removing a scan-applied Port would make the declaration invalid under the existing port validation rules, or would remove a Port that the existing registered declaration depends on when the Registration_Wizard is updating an already-registered Custom_Node_Type, THEN THE Ports_Step SHALL block the removal and SHALL display the reason the Port is required.
10. IF a Port_Scan completes with zero derived Port_Suggestions, THEN THE Ports_Step SHALL leave the existing port lists unchanged and SHALL display the scan outcome indicating that no Port_Suggestions were derived, including any Unmapped_Pads with their caveats.
11. WHEN a Port_Scan completes and the user has edited the port lists, THE Ports_Step SHALL add each Port_Suggestion whose name matches no existing Port to the port lists as an applied Port, alongside the user-edited Ports.

### Requirement 7: Degraded and Failure Behavior

**User Story:** As a computer vision engineer, I want the Ports step to work exactly as before when pad data is unavailable, so that port pre-population never becomes a gate on declaring ports.

#### Acceptance Criteria

1. IF the wizard's Plugin_Record version has no successful x86_64 Plugin_Artifact, THEN THE Ports_Step SHALL display an informational notice stating that port pre-population requires a successful x86_64 build, SHALL still display the Port_Guidance, SHALL keep the current port lists unchanged, and SHALL keep the manual port flow (adding, editing, and removing port rows) usable.
2. IF the stored Introspection_Report predates pad capture (contains no Pad_Template data), THEN THE Ports_Step SHALL display an informational notice stating that pad data is unavailable for this build, SHALL still display the Port_Guidance, SHALL keep the current port lists unchanged, and SHALL keep the manual port flow (adding, editing, and removing port rows) usable.
3. IF the stored Introspection_Report has a failure status or the Port_Scan API request fails, THEN THE Ports_Step SHALL display the failure with its diagnostic message, SHALL still display the Port_Guidance, SHALL keep the current port lists unchanged, SHALL keep the manual port flow (adding, editing, and removing port rows) usable, and SHALL keep the manual scan control available so the user can retry the Port_Scan.
4. WHEN a user reaches the Ports_Step of the Create_Wizard, THE Ports_Step SHALL display the Port_Guidance and the category guidance, SHALL state that port pre-population requires a built plugin, and SHALL NOT run the Port_Scan or display the manual scan control, since no Plugin_Artifact exists during creation.
5. IF a Port_Scan completes against an Introspection_Report that contains Pad_Template data but derives zero Port_Suggestions (the element declares no Pad_Templates with Pad_Presence always), THEN THE Ports_Step SHALL display an informational notice that the element declares no always-present pads, SHALL keep the current port lists unchanged, and SHALL keep the manual port flow (adding, editing, and removing port rows) usable.
6. WHILE pad data is unavailable for any reason described in criteria 7.1 through 7.3, THE Ports_Step SHALL accept any port declaration that passes the existing port validation rules and SHALL NOT block step navigation or declaration submission on pad-data availability.
