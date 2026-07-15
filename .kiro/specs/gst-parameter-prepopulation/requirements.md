# Requirements Document

## Introduction

This feature extends the Custom Node Designer (spec: custom-node-designer) so that the Parameters wizard step no longer starts as an empty hand-typed list. Every GStreamer element declares its configurable properties as standard GObject property metadata (what `gst-inspect-1.0` shows: property name, GType, default value, readable/writable flags, numeric ranges, enum values, and a description). The Node_Designer captures that metadata from the plugin's actual built binary and uses it to pre-populate the parameter declaration list in both the Create wizard and the Registration wizard with correct parameter names, data types, defaults, constraints, and descriptions. Pre-population is an aid, not a lock: users can edit, remove, or add parameters exactly as before, and the existing manual flow keeps working whenever no introspection data is available (no successful x86_64 build yet, or introspection failed).

## Glossary

- **Node_Designer**: The Portal capability (spec: custom-node-designer) for creating, importing, building, simulating, and registering Custom_Node_Types.
- **Parameters_Step**: The parameter-declaration step of the Node_Designer wizards — the Create wizard (`CreateWizard.tsx`, declaring parameters before scaffold generation) and the Registration wizard (`RegistrationWizard.tsx`, declaring parameters of the Custom_Node_Type registration).
- **Registration_Wizard**: The Node_Designer wizard that registers a Custom_Node_Type for a built Plugin_Record version.
- **Create_Wizard**: The Node_Designer wizard that collects a declaration and generates a Plugin_Scaffold; no Plugin_Record or Plugin_Artifact exists while it runs.
- **Plugin_Record**: The stored metadata for a created, generated, or imported plugin, including per-architecture Plugin_Artifact entries (spec: custom-node-designer).
- **Plugin_Artifact**: A built plugin binary (`.so`) for one Target_Architecture stored in the Plugin_Library (spec: custom-node-designer).
- **Plugin_Build_Service**: The per-Target_Architecture CodeBuild-based build pipeline (`plugin_builds.py`, `dda-plugin-build`, `plugin-build-images/`) that compiles plugin source into Plugin_Artifacts.
- **GStreamer_Property**: One GObject property declared by a GStreamer element class: property name, GType name, default value, readable/writable/controllable flags, numeric range (minimum/maximum) where applicable, enum values with nicks where applicable, and blurb (description).
- **Property_Introspection**: The act of loading a built Plugin_Artifact into a GStreamer runtime and reading each registered element's GStreamer_Property metadata (the `gst-inspect-1.0` equivalent via `Gst.ElementFactory` / GObject class property listing).
- **Introspection_Report**: The stored, structured result of Property_Introspection for one Plugin_Artifact: per element factory, the list of GStreamer_Property entries, plus the GStreamer version and a capture status.
- **Base_Class_Property**: A GStreamer_Property inherited from GStreamer base classes rather than declared by the element's own class (for example `name` and `parent` from GstObject, `qos` from GstVideoFilter/GstBaseTransform) — noise for parameter declaration.
- **Parameter_Suggestion**: One pre-populated parameter declaration derived from a GStreamer_Property: the declaration wire shape (name, paramType, required, default, constraints, description, examples) used by `declaration.ts` and validated by the Custom_Node_Type registration backend.
- **Type_Mapping**: The deterministic conversion from a GStreamer_Property's GType metadata to a Parameter_Suggestion's paramType and constraints (for example gint→int with min/max, GEnum→enum with allowed values).
- **Parameter_Scan**: The Parameters_Step action that fetches the Introspection_Report for the wizard's Plugin_Record version, applies the Type_Mapping, and merges the resulting Parameter_Suggestions into the parameter list.
- **Merge**: Combining Parameter_Suggestions with parameters already present in the wizard form without overwriting any user-entered declaration.

## Requirements

### Requirement 1: Capture Property Metadata from Built Plugins

**User Story:** As a computer vision engineer, I want the portal to read my plugin's actual GStreamer element properties from the built binary, so that parameter pre-population reflects what the plugin really declares instead of what I remember to type.

#### Acceptance Criteria

1. WHEN the Plugin_Build_Service completes a successful x86_64 build of a Plugin_Artifact, THE Plugin_Build_Service SHALL perform Property_Introspection on the built Plugin_Artifact and SHALL store the resulting Introspection_Report with the Plugin_Record version's x86_64 artifact entry.
2. THE Introspection_Report SHALL record, for each element factory the Plugin_Artifact registers, every GStreamer_Property with property name, GType name, default value, writability flag, numeric minimum and maximum where the GType is a ranged numeric type, enum values with their nicks where the GType is a GEnum type, and the property blurb.
3. THE Introspection_Report SHALL record which GStreamer_Property entries are Base_Class_Properties, determined by the GObject class that declared the property.
4. IF Property_Introspection fails or produces no element factories, THEN THE Plugin_Build_Service SHALL store an Introspection_Report with a failure status and diagnostic message and SHALL preserve the build's success status.
5. WHEN a Plugin_Record version's Introspection_Report is requested through the Node_Designer API by a user with node-designer read access, THE Node_Designer SHALL return the stored Introspection_Report together with the derived Parameter_Suggestions.
6. IF a Plugin_Record version has no successful x86_64 Plugin_Artifact or no stored Introspection_Report, THEN THE Node_Designer API SHALL respond with a machine-readable unavailability reason distinguishing "no successful x86_64 build" from "introspection failed" from "introspection not captured".

### Requirement 2: GType to Parameter Type Mapping

**User Story:** As a computer vision engineer, I want each scanned property converted to the correct parameter declaration type with its default, constraints, and description, so that the pre-populated declarations pass validation and match the plugin's real contract.

#### Acceptance Criteria

1. THE Type_Mapping SHALL convert integer GTypes (gint, guint, gint64, guint64, glong, gulong, guchar) to paramType `int`, floating-point GTypes (gfloat, gdouble) to paramType `float`, gboolean to paramType `bool`, gchararray to paramType `string`, and GEnum GTypes to paramType `enum` with the enum's value nicks as the allowed values constraint.
2. WHEN a GStreamer_Property declares a numeric range, THE Type_Mapping SHALL record the minimum and maximum in the Parameter_Suggestion's constraints.
3. WHEN a GStreamer_Property declares a default value, THE Type_Mapping SHALL carry the default value into the Parameter_Suggestion converted to the mapped paramType, and SHALL use the default value as the Parameter_Suggestion's example value.
4. WHEN a GStreamer_Property declares a blurb, THE Type_Mapping SHALL use the blurb as the Parameter_Suggestion's description; IF the blurb is empty, THEN THE Type_Mapping SHALL supply a description naming the property and its GType.
5. IF a GStreamer_Property's GType has no defined conversion (for example GstCaps, GstStructure, object, or boxed types) or the property is not writable, THEN THE Type_Mapping SHALL exclude the property from the Parameter_Suggestions and SHALL record it in the Parameter_Scan result as skipped with the reason.
6. THE Type_Mapping SHALL produce Parameter_Suggestions that satisfy the Parameters_Step validation rules for their paramType (non-empty name, non-empty description, a valid example value, and non-empty allowed values for enum).

### Requirement 3: Required versus Optional Classification

**User Story:** As a computer vision engineer, I want scanned parameters sensibly classified as required or optional, so that the declaration guides workflow authors without me re-deriving which properties must be set.

#### Acceptance Criteria

1. THE Type_Mapping SHALL classify a Parameter_Suggestion as required when the GStreamer_Property has no usable default for its mapped paramType (a string property whose default is NULL or empty, or a property whose default value cannot be converted to the mapped paramType).
2. THE Type_Mapping SHALL classify every other Parameter_Suggestion as optional with the property's default value carried as the declaration default.
3. WHEN Parameter_Suggestions are merged into the Parameters_Step, THE Parameters_Step SHALL leave the required flag, and every other field of each pre-populated parameter, editable by the user exactly like a manually added parameter.

### Requirement 4: Filter Inherited Base-Class Properties

**User Story:** As a computer vision engineer, I want only the plugin's own properties offered as parameters, so that the list is not polluted with GStreamer plumbing like `name`, `parent`, or `qos`.

#### Acceptance Criteria

1. WHEN deriving Parameter_Suggestions from an Introspection_Report, THE Node_Designer SHALL exclude every Base_Class_Property.
2. WHEN an element's own class re-declares (overrides) a property name that also exists on a base class, THE Node_Designer SHALL treat the property as the element's own and include it.

### Requirement 5: Parameter Scan in the Wizards

**User Story:** As a computer vision engineer, I want the Parameters step pre-populated automatically when scan data exists, and a manual refresh control, so that I start from the plugin's real parameters with minimal clicking.

#### Acceptance Criteria

1. WHEN a user reaches the Parameters_Step of the Registration_Wizard and the wizard's Plugin_Record version has an available Introspection_Report and the form's parameter list is empty, THE Parameters_Step SHALL automatically run the Parameter_Scan and populate the list with the Parameter_Suggestions.
2. THE Parameters_Step SHALL provide a manual scan control that runs the Parameter_Scan on demand.
3. WHEN a Parameter_Scan completes, THE Parameters_Step SHALL display the scan outcome: the number of parameters added, the element factory scanned, and any skipped properties with their reasons.
4. WHERE a Plugin_Artifact registers more than one element factory, THE Parameters_Step SHALL let the user choose which element factory's Parameter_Suggestions to merge, defaulting to the wizard's element factory when one matches.
5. WHILE a Parameter_Scan is in progress, THE Parameters_Step SHALL keep the manual parameter controls (add, edit, remove) usable and SHALL never block step navigation on the scan.
6. WHEN a user reaches the Parameters_Step of the Create_Wizard, THE Parameters_Step SHALL display that scanning requires a built plugin and SHALL keep the manual parameter flow unchanged, since no Plugin_Artifact exists during creation.

### Requirement 6: Merge Without Silent Overwrite

**User Story:** As a computer vision engineer, I want scan results merged with parameters I already declared without losing my edits, so that refreshing the scan never destroys my work.

#### Acceptance Criteria

1. WHEN a Parameter_Scan merges Parameter_Suggestions into a parameter list, THE Merge SHALL keep every existing parameter declaration unchanged.
2. WHEN a Parameter_Suggestion's name does not match any existing parameter name, THE Merge SHALL append the Parameter_Suggestion to the list.
3. WHEN a Parameter_Suggestion's name matches an existing parameter name, THE Merge SHALL keep the existing declaration and SHALL report the name in the scan outcome as already declared.
4. WHEN a Parameter_Scan adds parameters, THE Parameters_Step SHALL identify which entries came from the scan until the user edits them.

### Requirement 7: Degraded and Failure Behavior

**User Story:** As a computer vision engineer, I want the wizard to work exactly as before when scanning is impossible or fails, so that pre-population never becomes a gate on declaring parameters.

#### Acceptance Criteria

1. IF the wizard's Plugin_Record version has no successful x86_64 Plugin_Artifact, THEN THE Parameters_Step SHALL display an informational notice that scanning requires a successful x86_64 build and SHALL keep the manual parameter flow unchanged.
2. IF the stored Introspection_Report has a failure status, THEN THE Parameters_Step SHALL display the introspection failure with its diagnostic message and SHALL keep the manual parameter flow unchanged.
3. IF the Parameter_Scan API request fails, THEN THE Parameters_Step SHALL display the error and SHALL keep the manual parameter flow unchanged.
4. WHEN a Plugin_Record version predates Property_Introspection (a successful build with no stored Introspection_Report), THE Parameters_Step SHALL treat it as scan-unavailable per the unavailability reason and SHALL keep the manual parameter flow unchanged.

### Requirement 8: Introspection Report Serialization

**User Story:** As a developer, I want the Introspection_Report stored and served in a stable JSON shape, so that the build pipeline, the API, and both wizards agree on the data.

#### Acceptance Criteria

1. THE Node_Designer SHALL serialize Introspection_Reports to JSON for storage and SHALL parse stored JSON back into the Introspection_Report structure.
2. FOR ALL valid Introspection_Reports, serializing then parsing SHALL produce an equivalent Introspection_Report (round-trip property).
3. IF a stored Introspection_Report document is malformed, THEN THE Node_Designer API SHALL respond with the "introspection failed" unavailability reason instead of an internal error.
