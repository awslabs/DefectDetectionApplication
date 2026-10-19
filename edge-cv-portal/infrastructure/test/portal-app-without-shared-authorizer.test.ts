/**
 * The portal app WITHOUT the `sharedAuthorizerId` context value.
 *
 * The workflow debug and telemetry stacks attach their routes to the portal
 * API's existing Cognito authorizer, whose id arrives as the CDK context
 * value `sharedAuthorizerId`. deploy-infrastructure.sh resolves and passes
 * it, but deploy-auth.sh, deploy-frontend.sh, deploy_portal_fixes.sh and the
 * package.json cdk scripts run `cdk deploy`/`cdk synth` against bin/app.ts
 * with no such context, and the CDK CLI runs the whole app before selecting
 * a stack. bin/app.ts therefore reads the value once and instantiates the
 * two stacks only when it is present, warning otherwise; a guard that threw
 * inside the stack constructors broke every one of those entry points (steps
 * 2 and 4 of deploy-portal.sh) and no test saw it, because the bats harness
 * stubs npx and the jest tests mirror the app instead of running it.
 *
 * This test runs the REAL app through the CDK CLI (`cdk synth`, which is
 * what every deploy script does before deploying) with the fixture context
 * minus `sharedAuthorizerId`, exactly the reviewer's spot-check, and asserts
 * that the synth exits 0, that every pre-existing stack bin/app.ts
 * instantiates is in the cloud assembly, that the two new stacks are not,
 * and that the one warning names the context key and the deploy command.
 * The app WITH the value (all ten stacks, 0 authorizers in the two) is
 * portal-api-authorizer-count.test.ts; the per-stack prop guard is in the
 * two stack tests.
 *
 * Environment isolation follows the IAM synth gate
 * (test/backend-test/security/preservation/test_preservation_iam_cdk_synth.py):
 * every ambient credential source is stripped so the CLI cannot resolve a
 * caller account and overwrite the fixture CDK_DEFAULT_ACCOUNT, and the
 * shared-config files are redirected to paths that cannot exist rather than
 * unset (unsetting them makes the SDK fall back to ~/.aws). The SSM fallbacks
 * bin/app.ts tries then fail fast and the context values win.
 */
import { spawnSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { SHARED_AUTHORIZER_CONTEXT_KEY, sharedAuthorizerIdIfGiven } from '../lib/context-helpers';

const INFRA_DIR = path.join(__dirname, '..');
const APP_SOURCE = path.join(INFRA_DIR, 'bin', 'app.ts');
/** The stacks bin/app.ts instantiates only with the context value present. */
const CONTEXT_GATED_STACKS = ['WorkflowDebugStack', 'TelemetryStack'];
/** The canonical fixture context of the IAM synth gate, minus `sharedAuthorizerId`. */
const FIXTURE_CONTEXT = [
  '--context', 'portalAccountId=111111111111',
  '--context', 'externalId=fixture-eid',
  '--context', 'trustedUseCaseAccountIds=222222222222,333333333333',
];

let outdir: string;
let status: number | null;
let stderr: string;
let stdout: string;
let synthesizedStacks: string[];

beforeAll(() => {
  outdir = fs.mkdtempSync(path.join(os.tmpdir(), 'portal-app-no-shared-authorizer-'));
  const env: NodeJS.ProcessEnv = { ...process.env };
  for (const key of [
    'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
    'AWS_PROFILE', 'AWS_DEFAULT_PROFILE',
    'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI', 'AWS_CONTAINER_CREDENTIALS_FULL_URI',
    'AWS_WEB_IDENTITY_TOKEN_FILE', 'AWS_ROLE_ARN',
  ]) {
    delete env[key];
  }
  env.CDK_DEFAULT_ACCOUNT = '111111111111';
  env.CDK_DEFAULT_REGION = 'us-east-1';
  env.AWS_SHARED_CREDENTIALS_FILE = path.join(outdir, 'no-such-credentials');
  env.AWS_CONFIG_FILE = path.join(outdir, 'no-such-config');
  env.AWS_EC2_METADATA_DISABLED = 'true';

  // The CLI the deploy scripts run (`npx cdk ...`), resolved from this
  // package so the test needs nothing beyond `npm ci`.
  const cdkBin = require.resolve('aws-cdk/bin/cdk');
  const result = spawnSync(
    process.execPath,
    [cdkBin, 'synth', '--quiet', '--output', outdir, ...FIXTURE_CONTEXT],
    { cwd: INFRA_DIR, env, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 },
  );
  status = result.status;
  stdout = result.stdout ?? '';
  stderr = result.stderr ?? '';

  const manifestPath = path.join(outdir, 'manifest.json');
  synthesizedStacks = [];
  if (fs.existsSync(manifestPath)) {
    const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8')) as {
      artifacts?: Record<string, { type: string }>;
    };
    synthesizedStacks = Object.entries(manifest.artifacts ?? {})
      .filter(([, artifact]) => artifact.type === 'aws:cloudformation:stack')
      .map(([id]) => id)
      .sort();
  }
}, 900_000);

afterAll(() => {
  if (outdir) fs.rmSync(outdir, { recursive: true, force: true });
});

/** Every `new <X>Stack(app, ...)` in bin/app.ts, by class name. */
function stacksInApp(): string[] {
  const source = fs.readFileSync(APP_SOURCE, 'utf8');
  return Array.from(source.matchAll(/new\s+(\w+Stack)\s*\(\s*app\s*,/g), (m) => m[1]);
}

describe('cdk synth of bin/app.ts without sharedAuthorizerId', () => {
  test('exits 0: the pre-existing stacks still synthesize without the new context value', () => {
    if (status !== 0) {
      throw new Error(
        `cdk synth exited ${status} without ${SHARED_AUTHORIZER_CONTEXT_KEY}; this is the path ` +
          'deploy-auth.sh, deploy-frontend.sh and deploy_portal_fixes.sh take.\n--- stderr ---\n' +
          stderr.slice(-4000) + '\n--- stdout ---\n' + stdout.slice(-1000),
      );
    }
    expect(status).toBe(0);
  });

  test('every stack bin/app.ts instantiates unconditionally is in the assembly', () => {
    const inApp = stacksInApp();
    expect(inApp.length).toBeGreaterThan(0);
    for (const gated of CONTEXT_GATED_STACKS) {
      expect(inApp).toContain(gated);
    }
    const expected = inApp
      .filter((stackClass) => !CONTEXT_GATED_STACKS.includes(stackClass))
      .map((stackClass) => `EdgeCVPortal${stackClass}`)
      .sort();
    // Eight today: Auth, Storage, TestRunner, Compute, NodeDesigner,
    // BuildFleet, SyntheticData, Frontend.
    expect(expected.length).toBeGreaterThanOrEqual(8);
    expect(synthesizedStacks).toEqual(expected);
    for (const stackName of expected) {
      expect(fs.existsSync(path.join(outdir, `${stackName}.template.json`))).toBe(true);
    }
  });

  test('the two context-gated stacks are absent, not failed', () => {
    for (const gated of CONTEXT_GATED_STACKS) {
      const stackName = `EdgeCVPortal${gated}`;
      expect(synthesizedStacks).not.toContain(stackName);
      expect(fs.existsSync(path.join(outdir, `${stackName}.template.json`))).toBe(false);
    }
    expect(stderr).not.toMatch(/requires sharedAuthorizerId/);
  });

  test('one warning names the context key, both skipped stacks and the deploy command', () => {
    const warnings = stderr
      .split('\n')
      .filter((line) => line.includes(`CDK context ${SHARED_AUTHORIZER_CONTEXT_KEY} is not set`));
    expect(warnings).toHaveLength(1);
    const [warning] = warnings;
    expect(warning).toContain('EdgeCVPortalWorkflowDebugStack');
    expect(warning).toContain('EdgeCVPortalTelemetryStack');
    expect(warning).toContain('deploy-infrastructure.sh');
    expect(warning).toContain(
      `cdk deploy EdgeCVPortalWorkflowDebugStack EdgeCVPortalTelemetryStack --exclusively -c ${SHARED_AUTHORIZER_CONTEXT_KEY}=<id>`,
    );
    expect(warning).toContain('aws apigateway get-authorizers');
  });
});

describe('sharedAuthorizerIdIfGiven (the bin/app.ts presence check)', () => {
  test('absent and blank values mean "not given"', () => {
    expect(sharedAuthorizerIdIfGiven(undefined)).toBeUndefined();
    expect(sharedAuthorizerIdIfGiven(null)).toBeUndefined();
    expect(sharedAuthorizerIdIfGiven('')).toBeUndefined();
    expect(sharedAuthorizerIdIfGiven('   ')).toBeUndefined();
  });

  test('a given value is trimmed and passed through unvalidated (the stack guard validates it)', () => {
    expect(sharedAuthorizerIdIfGiven('abc123')).toBe('abc123');
    expect(sharedAuthorizerIdIfGiven(' abc123 ')).toBe('abc123');
    expect(sharedAuthorizerIdIfGiven('Fixture-0')).toBe('Fixture-0');
    expect(sharedAuthorizerIdIfGiven(42)).toBe('42');
  });
});
