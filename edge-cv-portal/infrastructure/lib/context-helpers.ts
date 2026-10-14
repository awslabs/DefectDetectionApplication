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
/**
 * Resolves the `detectorExportImage` CDK context value
 * (detector-checkpoint-import design D2). This is the Export_Image a
 * Conversion_Job runs. It lands in ModelConverterHandler's
 * `DETECTOR_EXPORT_IMAGE`, and every Conversion_Record carries it.
 *
 * - Absent or blank: returns '' (conversion is not configured). Convert then
 *   returns 503 and inspect reports `convertible: false` with that reason
 *   (Requirement 4.8).
 * - Otherwise the value MUST be an ECR image pinned by digest,
 *   `<account>.dkr.ecr.<region>.amazonaws.com/<repository>@sha256:<64 hex>`.
 *   A tag is mutable, so it cannot be the image a record says it ran; any
 *   other value fails the synth.
 */
export function detectorExportImage(contextValue: unknown): string {
  if (contextValue === undefined || contextValue === null) return '';
  if (typeof contextValue !== 'string') {
    throw new Error(
      `detectorExportImage must be a string (an ECR image URI pinned by digest); got ${typeof contextValue}`,
    );
  }
  const uri = contextValue.trim();
  if (uri.length === 0) return '';
  const pinned =
    /^\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com(\.cn)?\/[a-z0-9]+(?:[._\/-][a-z0-9]+)*@sha256:[0-9a-f]{64}$/;
  if (!pinned.test(uri)) {
    throw new Error(
      'detectorExportImage must be an ECR image pinned by digest ' +
        '(<account>.dkr.ecr.<region>.amazonaws.com/<repository>@sha256:<digest>, as ' +
        `detector-export-image/build-and-push.sh --push prints); got ${uri}`,
    );
  }
  return uri;
}

/** SSM parameter bin/app.ts falls back to for `detectorExportImage`. */
export const DETECTOR_EXPORT_IMAGE_SSM_PARAMETER = '/dda-portal/detector-export-image';

/**
 * The default `detectorExportImage` context bin/app.ts hands to the App
 * (detector-checkpoint-import task 10). This follows the trusted-account
 * precedent: env `DETECTOR_EXPORT_IMAGE` first, then the SSM parameter
 * {@link DETECTOR_EXPORT_IMAGE_SSM_PARAMETER}.
 *
 * App-props context is overridden by every CLI or cdk.json value, so
 * `-c detectorExportImage=...` still wins. `-c detectorExportImage=` (blank)
 * still disables conversion. What this adds: a routine deploy that passes no
 * `-c` at all (deploy-infrastructure.sh, and deploy-frontend.sh's compute
 * redeploy) keeps the configured image instead of silently resetting it and
 * turning every convert into a 503.
 *
 * Returns undefined when neither source has a value. A failed SSM read (no
 * parameter, no credentials) also counts as no value. Validation stays with
 * {@link detectorExportImage}, so a bad value from either source still fails
 * the synth.
 */
export function detectorExportImageDefault(
  env: Record<string, string | undefined>,
  readSsmParameter: (name: string) => string | undefined,
): string | undefined {
  const fromEnv = (env.DETECTOR_EXPORT_IMAGE ?? '').trim();
  if (fromEnv) return fromEnv;
  let fromSsm = '';
  try {
    fromSsm = (readSsmParameter(DETECTOR_EXPORT_IMAGE_SSM_PARAMETER) ?? '').trim();
  } catch {
    return undefined;
  }
  return fromSsm && fromSsm !== 'None' ? fromSsm : undefined;
}
