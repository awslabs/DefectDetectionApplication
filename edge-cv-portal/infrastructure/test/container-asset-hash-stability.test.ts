/**
 * Container image asset hashes must not move when untracked Python bytecode
 * appears in a worker's build context.
 *
 * Why this suite exists: CDK fingerprints the whole build context of a
 * DockerImageAsset. `edge-cv-portal/backend` accumulates hundreds of untracked
 * `__pycache__/*.pyc` files as soon as anyone runs the backend tests, and two of
 * them sitting in `backend/grounded-sam-worker` were enough to change that
 * worker's asset hash. cdk-assets then could not find the tag in ECR and set out
 * to rebuild the image — pulling the Grounding DINO export, its tokenizer and
 * the SAM archive — which stalled two portal deploys for hours and, on the
 * rescue path, temporarily removed the live worker.
 *
 * The guard is behavioural on purpose: it synthesizes the real ComputeStack,
 * drops a stray `.pyc` into the real build context, synthesizes again, and
 * requires the image asset hash to be identical. A source-level check alone
 * would not notice a future asset that forgets the exclude list, so the second
 * test walks every `fromImageAsset` call site instead.
 */
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { ComputeStack } from '../lib/compute-stack';
import { StorageStack } from '../lib/storage-stack';
import { PYTHON_CONTAINER_ASSET_EXCLUDES } from '../lib/container-asset-excludes';

const TRUSTED_USECASE_ACCOUNT = '111111111111';

const GROUNDED_SAM_CONTEXT = path.join(
  __dirname,
  '../../backend/grounded-sam-worker',
);
const SAM_CONTEXT = path.join(__dirname, '../../backend/sam-worker');
const COMPUTE_STACK_SOURCE = path.join(__dirname, '../lib/compute-stack.ts');

/** Synthesize the ComputeStack the way a routine portal deploy does. */
function synthCompute(): Template {
  const app = new cdk.App();
  const storage = new StorageStack(app, 'Storage');
  const deps = new cdk.Stack(app, 'Deps');
  const userPool = new cognito.UserPool(deps, 'Pool');

  const compute = new ComputeStack(app, 'Compute', {
    userPool,
    useCasesTable: storage.useCasesTable,
    userRolesTable: storage.userRolesTable,
    devicesTable: storage.devicesTable,
    auditLogTable: storage.auditLogTable,
    trainingJobsTable: storage.trainingJobsTable,
    labelingJobsTable: storage.labelingJobsTable,
    labelingTeamsTable: storage.labelingTeamsTable,
    labelingTasksTable: storage.labelingTasksTable,
    preLabeledDatasetsTable: storage.preLabeledDatasetsTable,
    modelsTable: storage.modelsTable,
    deploymentsTable: storage.deploymentsTable,
    settingsTable: storage.settingsTable,
    componentsTable: storage.componentsTable,
    sharedComponentsTable: storage.sharedComponentsTable,
    dataAccountsTable: storage.dataAccountsTable,
    workflowsTable: storage.workflowsTable,
    workflowVersionsTable: storage.workflowVersionsTable,
    testDatasetsTable: storage.testDatasetsTable,
    testRunsTable: storage.testRunsTable,
    workflowChatSessionsTable: storage.workflowChatSessionsTable,
    cameraRegistryTable: storage.cameraRegistryTable,
    deviceRegistrationsTable: storage.deviceRegistrationsTable,
    portalArtifactsBucket: storage.portalArtifactsBucket,
    trustedUseCaseAccountIds: [TRUSTED_USECASE_ACCOUNT],
  });

  return Template.fromStack(compute);
}

/**
 * The 64-hex asset hashes carried by every image-package Lambda in the
 * template, keyed by logical id. The hash is embedded in the ECR image URI that
 * `DockerImageCode.fromImageAsset` produces.
 */
function imageAssetHashes(template: Template): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [logicalId, resource] of Object.entries(
    template.findResources('AWS::Lambda::Function'),
  )) {
    const properties = (resource as { Properties?: Record<string, unknown> })
      .Properties;
    if (!properties || properties.PackageType !== 'Image') continue;
    const match = /[0-9a-f]{64}/.exec(JSON.stringify(properties.Code ?? {}));
    if (match) out[logicalId] = match[0];
  }
  return out;
}

/** Absolute path of a stray bytecode file inside a build context. */
function strayPycPath(contextDir: string): string {
  return path.join(contextDir, '__pycache__', 'handler.cpython-310.pyc');
}

describe('a stray .pyc in a build context does not move the image asset hash', () => {
  let cleanHashes: Record<string, string>;
  let pollutedHashes: Record<string, string>;
  // Only remove what this test created: a pre-existing cache directory belongs
  // to whoever ran the backend tests and is not this suite's to delete.
  let createdCacheDir = false;

  beforeAll(() => {
    cleanHashes = imageAssetHashes(synthCompute());

    const cacheDir = path.dirname(strayPycPath(GROUNDED_SAM_CONTEXT));
    createdCacheDir = !fs.existsSync(cacheDir);
    fs.mkdirSync(cacheDir, { recursive: true });
    fs.writeFileSync(
      strayPycPath(GROUNDED_SAM_CONTEXT),
      Buffer.from('\x03\xf3\r\nstray bytecode from a local test run'),
    );

    try {
      pollutedHashes = imageAssetHashes(synthCompute());
    } finally {
      fs.rmSync(strayPycPath(GROUNDED_SAM_CONTEXT), { force: true });
      if (createdCacheDir) fs.rmSync(cacheDir, { recursive: true, force: true });
    }
  }, 600_000);

  test('the default synth publishes at least one image asset to compare', () => {
    expect(Object.keys(cleanHashes).length).toBeGreaterThan(0);
  });

  test('every image asset hash is unchanged by the stray bytecode', () => {
    // Equality here is the whole point: an inequality is a deploy that rebuilds
    // a multi-gigabyte image for a worker whose source never changed.
    expect(pollutedHashes).toEqual(cleanHashes);
  });
});

describe('every image asset excludes Python bytecode from its fingerprint', () => {
  test('the shared exclude list covers bytecode and the test caches that produce it', () => {
    expect(PYTHON_CONTAINER_ASSET_EXCLUDES).toEqual(
      expect.arrayContaining([
        '__pycache__/**',
        '**/__pycache__/**',
        '**/*.pyc',
      ]),
    );
  });

  test('each fromImageAsset call site passes the shared exclude list', () => {
    const source = fs.readFileSync(COMPUTE_STACK_SOURCE, 'utf8');
    const callSites = source.split('fromImageAsset(').slice(1);

    // Both container workers are asset call sites; sam-worker is absent from the
    // default synth, so only a source-level walk can hold it to the same rule.
    expect(callSites.length).toBeGreaterThanOrEqual(2);

    for (const callSite of callSites) {
      const args = callSite.slice(0, 700);
      expect(args).toContain('PYTHON_CONTAINER_ASSET_EXCLUDES');
    }
  });

  test('both worker build contexts exist where the stack expects them', () => {
    expect(fs.existsSync(path.join(GROUNDED_SAM_CONTEXT, 'Dockerfile'))).toBe(
      true,
    );
    expect(fs.existsSync(path.join(SAM_CONTEXT, 'Dockerfile'))).toBe(true);
  });
});
