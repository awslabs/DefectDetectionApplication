/**
 * Pure URL builders for every LocalServer route the HMI consumes.
 *
 * The HMI is served same-origin with the API (the `/hmi` static mount on the
 * LocalServer), so all URLs are root-relative paths. Every dynamic path
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

const enc = encodeURIComponent;

// --------------------------------------------------------------------------
// Auth
// --------------------------------------------------------------------------

/** `POST /local-auth/login` — Session_Token issuance. */
export function loginUrl(): string {
  return "/local-auth/login";
}

// --------------------------------------------------------------------------
// Registrations and executions (bearer-authenticated JSON routes)
// --------------------------------------------------------------------------

/** `GET /workflows/registrations` — workflow discovery + retry probe. */
export function registrationsUrl(): string {
  return "/workflows/registrations";
}

/**
 * `GET /workflows/registrations/{registrationId}/executions?limit=N` — the
 * additive bounded recent-executions route polled every 2 seconds.
 */
export function registrationExecutionsUrl(
  registrationId: string,
  limit: number = 10,
): string {
  return `/workflows/registrations/${enc(registrationId)}/executions?limit=${enc(String(limit))}`;
}

/** `GET /workflows/executions/{executionId}/results` — results inventory. */
export function executionResultsUrl(executionId: string): string {
  return `/workflows/executions/${enc(executionId)}/results`;
}

/** `GET /workflows/executions/{executionId}/metadata` — verdict metadata. */
export function executionMetadataUrl(executionId: string): string {
  return `/workflows/executions/${enc(executionId)}/metadata`;
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
  return `/workflows/executions/${enc(executionId)}/output-image?token=${enc(token)}`;
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
    `/workflows/executions/${enc(executionId)}/node-image` +
    `?nodeId=${enc(nodeId)}&port=${enc(port)}&token=${enc(token)}`
  );
}
