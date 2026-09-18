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
 * Default-OFF resolution for the `portalRegistryEnforced` CDK context flag
 * (portal-jwt-role-privilege-escalation Req 2.4, design Decision 4). The
 * returned string is what lands in every portal handler's
 * `PORTAL_REGISTRY_ENFORCED` environment variable, where
 * `shared_utils.registry_enforcement_enabled()` reads it.
 *
 * Off is the safe default and must stay the default until the registry has
 * been backfilled from the pool (task 5.2): with the flag on and a row
 * missing, the portal denies that principal everything — including the
 * bootstrap `admin`. Only an explicit affirmative turns it on: boolean
 * `true`, or a string that is one of '1'/'true'/'yes'/'on'/'enabled' after
 * trimming, case-insensitively — exactly the truthy set
 * `shared_utils._ENFORCEMENT_TRUE_VALUES` accepts, so a value that reads as
 * "on" in CDK context can never deploy as "off" in the Lambda (or the
 * reverse). Absent and every unrecognized value ('0', 'no', 'maybe', typos)
 * resolve to 'false'.
 *
 * The value is normalized to the canonical 'true'/'false' rather than passed
 * through, so the deployed environment is unambiguous when read from the
 * console or a CloudFormation template.
 */
export function portalRegistryEnforced(contextValue: unknown): string {
  if (contextValue === true) return 'true';
  if (
    typeof contextValue === 'string' &&
    ['1', 'true', 'yes', 'on', 'enabled'].includes(
      contextValue.trim().toLowerCase(),
    )
  ) {
    return 'true';
  }
  return 'false';
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
