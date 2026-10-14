/**
 * Example-level template-shape assertions for portal-deploy-flag-hardening.
 *
 * Deliberately CHEAP synths only: the StorageStack stages no Lambda/layer
 * assets, so its synth is fast and safe to repeat per context shape. There
 * are NO ComputeStack synths here — each one costs minutes in asset staging,
 * and the worker boundary shapes (default/true/false) are pinned by the
 * rebaselined suites grounded-sam-worker-infra.test.ts and
 * gsam-preview-infra.test.ts instead.
 *
 * What this suite pins:
 *  - Domain_Context spelling equivalence (Req 3.5): the decorated spelling
 *    'HTTPS://d23v4ltibogb5x.cloudfront.net/' and the bare spelling
 *    'd23v4ltibogb5x.cloudfront.net' synthesize deep-equal StorageStack
 *    templates, because both read points normalize through the
 *    Domain_Normalizer (lib/context-helpers.ts).
 *  - The PortalArtifactsBucket CORS origin carries exactly one https://
 *    scheme and no trailing slash for BOTH spellings (Req 3.2, 3.3) — the
 *    2026-09-07 double-scheme corruption ('https://https://…') cannot recur.
 *  - Absent Domain_Context keeps today's wildcard behavior:
 *    AllowedOrigins ['*'] (Req 3.6).
 *  - cdk.json documents the default-ON worker posture:
 *    context.deployGroundedSamWorker === true (Req 2.1).
 *
 * Note on jest synths and cdk.json: new cdk.App() does NOT read cdk.json,
 * so context here is exactly what each test passes — which is why the
 * cdk.json posture is asserted by parsing the file directly.
 */
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { StorageStack } from '../lib/storage-stack';

const BARE_DOMAIN = 'd23v4ltibogb5x.cloudfront.net';
const DECORATED_DOMAIN = 'HTTPS://d23v4ltibogb5x.cloudfront.net/';
const EXPECTED_ORIGIN = `https://${BARE_DOMAIN}`;

/** Synthesize the StorageStack under the given CDK context (cheap: no assets). */
function synthStorageTemplate(context?: Record<string, unknown>): Template {
  const app = new cdk.App(context ? { context } : undefined);
  const stack = new StorageStack(app, 'EdgeCVPortalStorageStack');
  return Template.fromStack(stack);
}

/** The PortalArtifactsBucket's CORS AllowedOrigins from a synthesized template. */
function allowedOriginsOf(template: Template): string[] {
  const buckets = Object.entries(
    template.findResources('AWS::S3::Bucket'),
  ).filter(([logicalId]) => logicalId.startsWith('PortalArtifactsBucket'));
  expect(buckets).toHaveLength(1);
  const [, bucket] = buckets[0] as [string, any];
  const corsRules = bucket.Properties.CorsConfiguration?.CorsRules;
  expect(corsRules).toHaveLength(1);
  return corsRules[0].AllowedOrigins;
}

// Synthesized once per context shape; StorageStack synths are asset-free
// and fast, but beforeAll still gets a generous margin.
let bareTemplate: Template;
let decoratedTemplate: Template;
let noContextTemplate: Template;

beforeAll(() => {
  bareTemplate = synthStorageTemplate({ cloudFrontDomain: BARE_DOMAIN });
  decoratedTemplate = synthStorageTemplate({
    cloudFrontDomain: DECORATED_DOMAIN,
  });
  noContextTemplate = synthStorageTemplate();
}, 120_000);

describe('Domain_Context spelling equivalence (Requirement 3.5)', () => {
  test('decorated (HTTPS://…/) and bare spellings synthesize deep-equal StorageStack templates', () => {
    expect(decoratedTemplate.toJSON()).toEqual(bareTemplate.toJSON());
  });
});

describe('PortalArtifactsBucket CORS origin (Requirements 3.2, 3.3)', () => {
  test(`bare spelling emits AllowedOrigins of exactly ['${EXPECTED_ORIGIN}']`, () => {
    expect(allowedOriginsOf(bareTemplate)).toEqual([EXPECTED_ORIGIN]);
  });

  test(`decorated spelling emits AllowedOrigins of exactly ['${EXPECTED_ORIGIN}'] — one scheme, no trailing slash`, () => {
    expect(allowedOriginsOf(decoratedTemplate)).toEqual([EXPECTED_ORIGIN]);
  });
});

describe('Absent Domain_Context keeps wildcard CORS (Requirement 3.6)', () => {
  test("no-context synth emits AllowedOrigins ['*']", () => {
    expect(allowedOriginsOf(noContextTemplate)).toEqual(['*']);
  });
});

describe('cdk.json documents the default-ON worker posture (Requirement 2.1)', () => {
  test('context.deployGroundedSamWorker === true in cdk.json', () => {
    const cdkJsonPath = path.join(__dirname, '..', 'cdk.json');
    const cdkJson = JSON.parse(fs.readFileSync(cdkJsonPath, 'utf8'));
    expect(cdkJson.context.deployGroundedSamWorker).toBe(true);
  });
});
