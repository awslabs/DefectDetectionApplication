/**
 * Default-ON resolution for the `deployGroundedSamWorker` CDK context flag
 * (portal-deploy-flag-hardening Req 1). Flag-less deploys deleted the live
 * DdaGroundedSamWorker four times; the safe outcome is now the default one.
 * Only an explicit false — boolean `false` or a string equal to 'false'
 * case-insensitively after trimming — omits the worker (the deliberate
 * teardown path, Req 2.2). Absent, true, 'true', and every unrecognized
 * value (e.g. '0', 'no', typos) deploy it: an unintended deploy is cheap
 * and ECR-cached; an unintended deletion breaks live pre-labeling.
 */
export function groundedSamWorkerEnabled(contextValue: unknown): boolean {
  if (contextValue === false) return false;
  if (
    typeof contextValue === 'string' &&
    contextValue.trim().toLowerCase() === 'false'
  ) {
    return false;
  }
  return true;
}

/**
 * Normalizes the `cloudFrontDomain` CDK context value to the bare domain
 * (portal-deploy-flag-hardening Req 3). Consumers prepend the scheme
 * themselves (storage-stack CORS `https://${…}`, backend link/CORS
 * builders), so a scheme-prefixed context value produced `https://https://…`
 * live on 2026-09-07. Strips surrounding whitespace, at most one leading
 * `http://`/`https://` (case-insensitive), and all trailing slashes; every
 * other character passes through unchanged. Returns undefined when the
 * input is absent or empty after normalization, preserving today's
 * absent-context behavior (wildcard CORS, no env entries).
 */
export function normalizeCloudFrontDomain(
  contextValue: unknown,
): string | undefined {
  if (typeof contextValue !== 'string') return undefined;
  let domain = contextValue.trim();
  domain = domain.replace(/^https?:\/\//i, '');
  domain = domain.replace(/\/+$/, '');
  return domain.length > 0 ? domain : undefined;
}
