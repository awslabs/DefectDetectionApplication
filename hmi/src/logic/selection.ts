/**
 * Registration filtering, labeling, default selection, and availability
 * checks (Requirements 2.2, 2.4, 2.5, 2.6, 2.7, 8.5).
 *
 * Pure functions over parsed API payloads. All behavior derives solely from
 * API fields — nothing is keyed to specific workflow names or workflowIds
 * (Requirement 2.6). List order is always the order returned by
 * `GET /workflows/registrations`, which the fallback rules depend on.
 */

import type { Execution, Registration } from "../api/types";

/**
 * The registration status the HMI treats as active. The backend's
 * ACTIVE_STATUSES also includes "invalid", but invalid registrations can
 * never run (trigger returns 409), so only "registered" is offered
 * (design Research Findings; Requirement 2.2).
 */
export const ACTIVE_STATUS = "registered";

/** True iff the registration is active (`registered` status). */
export function isActiveRegistration(registration: Registration): boolean {
  return registration.status === ACTIVE_STATUS;
}

/**
 * The registrations to offer for selection: exactly those with active
 * status, in the order returned by the API (Requirement 2.2).
 */
export function activeRegistrations(
  registrations: readonly Registration[],
): Registration[] {
  return registrations.filter(isActiveRegistration);
}

/**
 * Display label for a registration: its `name` when present and non-empty,
 * its `workflowId` otherwise (Requirement 2.2).
 */
export function registrationLabel(registration: Registration): string {
  const { name } = registration;
  return name !== null && name !== "" ? name : registration.workflowId;
}

/**
 * Default workflow selection when the operator has made no choice
 * (Requirements 2.4, 2.7):
 *
 * - The active registration whose most recent Workflow_Run (largest
 *   `startedAt` among its runs) has the latest `startedAt` overall; ties
 *   keep the earlier registration in API order.
 * - When no active registration has any run: the first active registration
 *   in API order.
 * - When there are zero active registrations: null (the no-workflows
 *   message state, Requirement 2.5).
 *
 * `runsByRegistration` maps registrationId → that registration's known runs
 * (any order); registrations absent from the map are treated as having no
 * runs.
 */
export function selectDefaultRegistration(
  registrations: readonly Registration[],
  runsByRegistration: ReadonlyMap<string, readonly Execution[]>,
): Registration | null {
  const actives = activeRegistrations(registrations);
  const firstActive = actives[0];
  if (firstActive === undefined) return null;

  let best: Registration | null = null;
  let bestStartedAt = -Infinity;
  for (const registration of actives) {
    const runs = runsByRegistration.get(registration.registrationId) ?? [];
    for (const run of runs) {
      // Strict > keeps the first registration in API order on ties.
      if (run.startedAt > bestStartedAt) {
        best = registration;
        bestStartedAt = run.startedAt;
      }
    }
  }

  // No active registration has any run → first active in API order (2.7).
  return best ?? firstActive;
}

/**
 * Availability of the displayed registration against a fresh registrations
 * payload (Requirements 8.5, 2.5).
 */
export type AvailabilityResult =
  | { kind: "available"; registration: Registration }
  | { kind: "unavailable"; alternatives: Registration[] }
  | { kind: "no-workflows" };

/**
 * Checks whether the displayed registration is still available
 * (Requirement 8.5):
 *
 * - Present with active status → available.
 * - Absent or non-active while other actives exist → unavailable, offering
 *   exactly the remaining active registrations as alternatives.
 * - Zero active registrations → the no-workflows state (Requirement 2.5).
 */
export function checkDisplayedAvailability(
  registrations: readonly Registration[],
  displayedRegistrationId: string,
): AvailabilityResult {
  const actives = activeRegistrations(registrations);
  const displayed = actives.find(
    (r) => r.registrationId === displayedRegistrationId,
  );
  if (displayed !== undefined) {
    return { kind: "available", registration: displayed };
  }
  if (actives.length > 0) {
    return { kind: "unavailable", alternatives: actives };
  }
  return { kind: "no-workflows" };
}
