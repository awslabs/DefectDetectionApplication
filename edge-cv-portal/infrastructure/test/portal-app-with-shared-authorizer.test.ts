/**
 * The portal app WITH the `sharedAuthorizerId` context value: the path
 * deploy-infrastructure.sh takes (it resolves the id from the deployed API),
 * and the fixture of the IAM synth gate
 * (test/backend-test/security/preservation/test_preservation_iam_cdk_synth.py).
 *
 * The CDK CLI runs the whole app before selecting a stack, so one stack
 * constructor throwing fails `cdk synth` and `cdk deploy` of EVERY stack.
 * That happened when the authorization branch and the telemetry branch were
 * merged: lib/telemetry-stack.ts probed lib/context-helpers for a
 * `dataPlaneGrant` export and, once the authorization helper existed with a
 * contract of its own, handed its shape to iam.PolicyStatement
 * (`TypeError: Spread syntax requires ...iterable`). Neither branch saw it
 * alone, `tsc` cannot see it (the probe went through `as unknown`), and the
 * IAM synth gate skipped instead of failing. The stack now wires a grant only
 * through its explicit `dataPlaneGrant` prop, which bin/app.ts does not pass.
 *
 * This test runs the REAL app through the CDK CLI with the gate's fixture
 * context and asserts that the synth exits 0, that every stack bin/app.ts
 * instantiates (the two context-gated ones included) is in the cloud
 * assembly, and that the telemetry stack synthesizes with the data-plane
 * grant absent. The app WITHOUT the value is
 * portal-app-without-shared-authorizer.test.ts; the authorizer count of the
 * two gated stacks is portal-api-authorizer-count.test.ts.
 *
 * Environment isolation follows the IAM synth gate: every ambient credential
 * source is stripped so the CLI cannot resolve a caller account and overwrite
 * the fixture CDK_DEFAULT_ACCOUNT, and the shared-config files are redirected
 * to paths that cannot exist rather than unset.
 */
import { spawnSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { SHARED_AUTHORIZER_CONTEXT_KEY } from '../lib/context-helpers';

const INFRA_DIR = path.join(__dirname, '..');
const APP_SOURCE = path.join(INFRA_DIR, 'bin', 'app.ts');
const TELEMETRY_STACK = 'EdgeCVPortalTelemetryStack';
/** The canonical fixture context of the IAM synth gate. */
const FIXTURE_CONTEXT = [
  '--context', 'portalAccountId=111111111111',
  '--context', 'externalId=fixture-eid',
  '--context', 'trustedUseCaseAccountIds=222222222222,333333333333',
  '--context', 'portalRegistryEnforced=false',
  '--context', `${SHARED_AUTHORIZER_CONTEXT_KEY}=fixture0`,
];

let outdir: string;
let status: number | null;
let stderr: string;
let stdout: string;
let synthesizedStacks: string[];

beforeAll(() => {
  outdir = fs.mkdtempSync(path.join(os.tmpdir(), 'portal-app-with-shared-authorizer-'));
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

function telemetryTemplate(): any {
  return JSON.parse(fs.readFileSync(path.join(outdir, `${TELEMETRY_STACK}.template.json`), 'utf8'));
}

describe(`cdk synth of bin/app.ts with ${SHARED_AUTHORIZER_CONTEXT_KEY}`, () => {
  test('exits 0: the whole app, the two context-gated stacks included, synthesizes', () => {
    if (status !== 0) {
      throw new Error(
        `cdk synth exited ${status} with ${SHARED_AUTHORIZER_CONTEXT_KEY}; this is the path ` +
          'deploy-infrastructure.sh and the IAM synth gate take, and one failing stack constructor ' +
          'fails every stack (the telemetry stack once threw here by consuming the ' +
          'lib/context-helpers dataPlaneGrant export).\n--- stderr ---\n' +
          stderr.slice(-4000) + '\n--- stdout ---\n' + stdout.slice(-1000),
      );
    }
    expect(status).toBe(0);
    expect(stderr).not.toContain(`CDK context ${SHARED_AUTHORIZER_CONTEXT_KEY} is not set`);
  });

  test('every stack bin/app.ts instantiates is in the assembly', () => {
    const inApp = stacksInApp();
    expect(inApp).toContain('WorkflowDebugStack');
    expect(inApp).toContain('TelemetryStack');
    const expected = inApp.map((stackClass) => `EdgeCVPortal${stackClass}`).sort();
    // Ten today: Auth, Storage, TestRunner, Compute, NodeDesigner, BuildFleet,
    // SyntheticData, WorkflowDebug, Telemetry, Frontend.
    expect(expected.length).toBeGreaterThanOrEqual(10);
    expect(synthesizedStacks).toEqual(expected);
    for (const stackName of expected) {
      expect(fs.existsSync(path.join(outdir, `${stackName}.template.json`))).toBe(true);
    }
  });

  test('the telemetry stack synthesizes with the data-plane grant absent', () => {
    const template = telemetryTemplate();
    const resources = Object.values(template.Resources ?? {}) as any[];
    const functions = resources.filter((r) => r.Type === 'AWS::Lambda::Function');
    expect(functions.length).toBeGreaterThanOrEqual(4);
    for (const fn of functions) {
      expect(fn.Properties.Environment.Variables.TELEMETRY_DATA_PLANE_GRANT).toBe('absent');
    }
    expect(template.Outputs.TelemetryDataPlaneGrant.Value).toBe('absent');
    for (const resource of resources) {
      const documents: any[] = [];
      if (resource.Type === 'AWS::IAM::Policy' || resource.Type === 'AWS::IAM::ManagedPolicy') {
        documents.push(resource.Properties.PolicyDocument);
      }
      if (resource.Type === 'AWS::IAM::Role') {
        for (const policy of resource.Properties.Policies ?? []) documents.push(policy.PolicyDocument);
      }
      for (const document of documents) {
        for (const statement of document?.Statement ?? []) {
          expect(String(statement.Sid ?? '')).not.toContain('DataPlane');
        }
      }
    }
  });
});
