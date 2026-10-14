/**
 * Target_Workflow binding for the Triple_HMI (Requirements 2.2, 2.3, 2.4,
 * 2.7, 8.5, 8.8).
 *
 * Pure module over a parsed `GET /workflows/registrations` payload. The
 * Triple_HMI has no workflow selector: it binds by name, and it re-evaluates
 * this same function on **every** registrations payload. That is what makes
 * the deploy / undeploy / redeploy transitions fall out with no extra logic:
 *
 * - a payload with an active name match yields `bound` — the first bind
 *   (2.2, 2.3) and the automatic re-bind after a not-deployed period (2.4,
 *   8.8) are the same evaluation;
 * - a payload where the target is absent or inactive yields `not-deployed` —
 *   the initial not-deployed message (2.4) and the return to it after the
 *   bound registration goes away (2.7, 8.5) are also the same evaluation.
 *
 * Nothing here is keyed to a specific workflow name: the target name comes
 * from `triple/config.ts` (Requirement 2.5).
 */

import type { Registration } from "../api/types";
import { isActiveRegistration } from "../logic/selection";

/**
 * The result of evaluating a registrations payload against the target name.
 * Mirrors the `binding` slice of `TripleAppState` minus its `pending` state,
 * which only covers "no payload received yet".
 */
export type BindingResult =
  | { state: "bound"; registration: Registration }
  | { state: "not-deployed" };

/**
 * The candidates for the Target_Workflow: active registrations
 * (`registered`, the backend's ACTIVE_STATUSES semantics reused unchanged
 * from `logic/selection.ts`) whose `name` is a **case-sensitive exact**
 * match for the target name, in the order the API returned them
 * (Requirement 2.2). Payload order is preserved because the tie-break of
 * Requirement 2.3 depends on it.
 */
export function targetCandidates(
  registrations: readonly Registration[],
  targetName: string,
): Registration[] {
  return registrations.filter(
    (registration) =>
      isActiveRegistration(registration) && registration.name === targetName,
  );
}

/**
 * True iff the registration carries a usable `registeredAt` value.
 *
 * `parseRegistration` types `registeredAt` as a number, but this guard is
 * defensive about payloads that reach here unparsed: anything that is not a
 * finite number counts as *lacking* a `registeredAt` value in the sense of
 * Requirement 2.3, and so never wins the recency comparison.
 */
function hasRegisteredAt(registration: Registration): boolean {
  const { registeredAt } = registration;
  return typeof registeredAt === "number" && Number.isFinite(registeredAt);
}

/**
 * Binds the Target_Workflow from a registrations payload (Requirements 2.2,
 * 2.3, 2.4, 2.7, 8.5, 8.8).
 *
 * @param registrations The parsed `GET /workflows/registrations` payload, in
 *   API response order.
 * @param targetName The resolved target workflow name (see
 *   `resolveWorkflowName`), compared as a case-sensitive exact string match.
 * @returns `bound` with the selected registration when at least one active
 *   registration matches the name: the single candidate when there is one,
 *   otherwise the candidate with the most recent `registeredAt`, with equal
 *   or missing values resolved to the first such candidate in payload order.
 *   `not-deployed` when no active candidate matches — the message state that
 *   keeps re-checking and re-binds automatically.
 */
export function bindTargetWorkflow(
  registrations: readonly Registration[],
  targetName: string,
): BindingResult {
  const candidates = targetCandidates(registrations, targetName);

  const first = candidates[0];
  // Zero candidates → not-deployed (2.4, 2.7, 8.5). This is the *only* way
  // to reach not-deployed, so the state is a function of the payload alone.
  if (first === undefined) return { state: "not-deployed" };

  let selected = first;
  let selectedHasValue = hasRegisteredAt(first);

  for (const candidate of candidates.slice(1)) {
    if (!hasRegisteredAt(candidate)) continue;
    // Strict `>` keeps the earlier candidate in payload order on ties, and a
    // candidate lacking a value is always overtaken by one that has one.
    if (!selectedHasValue || candidate.registeredAt > selected.registeredAt) {
      selected = candidate;
      selectedHasValue = true;
    }
  }

  return { state: "bound", registration: selected };
}
