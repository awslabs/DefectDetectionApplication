/**
 * Pure URL builders for every LocalServer route the HMI consumes.
 *
 * The HMI is served same-origin with the API by default (the `/hmi` static
 * mount on the LocalServer), in which case every URL below is a root-relative
 * path exactly as before. When the bundle is hosted detached from the
 * LocalServer (a static server on another port, or another machine), the
 * configured API_Base from `base.ts` is prefixed so the requests still reach
 * the device; the LocalServer already allows cross-origin callers. Every dynamic path
 * segment and query value is encoded with `encodeURIComponent`, so arbitrary
 * ids, node names, ports, and tokens can never break out of their URL part.
 *
 * JSON routes are called through `apiFetch` and carry the Session_Token in
 * the `Authorization` header; the image routes (`/output-image`,
 * `/node-image`) are loaded via `<img src>` which cannot carry headers, so
 * their builders embed the Session_Token as the `token` query parameter,
 * matching the LocalServer's token-in-query image serving
 * (Requirements 1.3, 5.5).
 */

import { getApiBase } from "./base";

const enc = encodeURIComponent;

/**
 * API_Base prefix for every builder below. Empty (same-origin) unless the
 * host page configured one, so a bundle served by the LocalServer's own
 * `/hmi` mount produces exactly the URLs it always did.
 */
function base(): string {
  return getApiBase();
}

// --------------------------------------------------------------------------
// Auth
// --------------------------------------------------------------------------

/** `POST /local-auth/login` — Session_Token issuance. */
export function loginUrl(): string {
  return `${base()}/local-auth/login`;
}

/**
 * `GET /local-auth/status` — the unauthenticated probe that says whether a
 * login form is needed at all.
 *
 * It lives here, with the other builders, specifically so it carries the
 * API_Base: the entry points used to hold this path as a local constant, which
 * meant a detached bundle asked its own static server for the status and got a
 * 404, leaving the login form up on a device that issues no tokens.
 */
export function localAuthStatusUrl(): string {
  return `${base()}/local-auth/status`;
}

// --------------------------------------------------------------------------
// Registrations and executions (bearer-authenticated JSON routes)
// --------------------------------------------------------------------------

/** `GET /workflows/registrations` — workflow discovery + retry probe. */
export function registrationsUrl(): string {
  return `${base()}/workflows/registrations`;
}

/**
 * `GET /workflows/registrations/{registrationId}/executions?limit=N` — the
 * additive bounded recent-executions route polled every 2 seconds.
 */
export function registrationExecutionsUrl(
  registrationId: string,
  limit: number = 10,
): string {
  return `${base()}/workflows/registrations/${enc(registrationId)}/executions?limit=${enc(String(limit))}`;
}

/** `GET /workflows/executions/{executionId}/results` — results inventory. */
export function executionResultsUrl(executionId: string): string {
  return `${base()}/workflows/executions/${enc(executionId)}/results`;
}

/** `GET /workflows/executions/{executionId}/metadata` — verdict metadata. */
export function executionMetadataUrl(executionId: string): string {
  return `${base()}/workflows/executions/${enc(executionId)}/metadata`;
}

// --------------------------------------------------------------------------
// Image routes (token-in-query; loaded via <img src>)
// --------------------------------------------------------------------------

/**
 * `GET /workflows/executions/{executionId}/output-image?token=` — the run's
 * base output image, with the Session_Token as an encoded `token` query
 * parameter (Requirements 1.3, 5.5).
 */
export function outputImageUrl(executionId: string, token: string): string {
  return `${base()}/workflows/executions/${enc(executionId)}/output-image?token=${enc(token)}`;
}

/**
 * `GET /workflows/executions/{executionId}/node-image?nodeId=&port=&token=`
 * — a node's persisted frame for `(nodeId, port)`, with the Session_Token as
 * an encoded `token` query parameter (Requirements 1.3, 5.5).
 */
export function nodeImageUrl(
  executionId: string,
  nodeId: string,
  port: string,
  token: string,
): string {
  return (
    `${base()}/workflows/executions/${enc(executionId)}/node-image` +
    `?nodeId=${enc(nodeId)}&port=${enc(port)}&token=${enc(token)}`
  );
}
