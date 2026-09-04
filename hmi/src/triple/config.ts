/**
 * Target-workflow name configuration for the Triple_HMI (Requirement 2.5).
 *
 * Pure module (no DOM, no environment access): the caller supplies the two
 * configuration sources — the `workflow` query parameter of the kiosk URL and
 * the build-time `VITE_TRIPLE_WORKFLOW_NAME` value — and this module applies
 * the precedence rules.
 *
 * Precedence: query parameter, then build-time value, then the default. A
 * source that is absent, empty, or whitespace-only falls through to the next
 * one, so a blank query parameter can never blank out the configured name.
 */

/** The workflow the Triple_HMI binds to when nothing is configured (R2.5). */
export const DEFAULT_TRIPLE_WORKFLOW_NAME =
  "blue-plate-detection-guided-inspection";

/** A configuration source value: a string, or absent. */
export type ConfigValue = string | null | undefined;

/** True iff the value carries a usable (non-blank) workflow name. */
function isConfigured(value: ConfigValue): value is string {
  return typeof value === "string" && value.trim() !== "";
}

/**
 * Resolves the Target_Workflow name (Requirement 2.5).
 *
 * @param queryValue The `workflow` query-parameter value, or null/undefined
 *   when the parameter is absent.
 * @param buildTimeValue The build-time `VITE_TRIPLE_WORKFLOW_NAME` value, or
 *   null/undefined when it was not defined at build time.
 * @returns The query value when non-blank, else the build-time value when
 *   non-blank, else `DEFAULT_TRIPLE_WORKFLOW_NAME`. The returned name is
 *   trimmed of surrounding whitespace so it can be compared to a
 *   registration `name` as a case-sensitive exact match.
 */
export function resolveWorkflowName(
  queryValue: ConfigValue,
  buildTimeValue: ConfigValue,
): string {
  if (isConfigured(queryValue)) return queryValue.trim();
  if (isConfigured(buildTimeValue)) return buildTimeValue.trim();
  return DEFAULT_TRIPLE_WORKFLOW_NAME;
}
