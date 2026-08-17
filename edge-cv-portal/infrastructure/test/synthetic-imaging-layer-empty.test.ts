/**
 * Static infrastructure assertions for the synthetic-imaging-layer-empty
 * bugfix (SyntheticImagingLayer deployed empty — runtime `No module named
 * 'PIL'` in dda-synthetic-data-handler).
 *
 * Two clearly separated suites:
 *
 * 1. Bug condition exploration (Property 1) — synthesizes the
 *    SyntheticDataStack and asserts the STAGED imaging layer asset contains
 *    `python/PIL` (Pillow installed from requirements.txt). EXPECTED TO FAIL
 *    on the unfixed stack (the asset is the raw source directory holding
 *    only build.sh + requirements.txt); the same test validates the fix once
 *    it passes. Requirements: 1.1, 1.2, 2.1.
 *
 * 2. Preservation (Property 2) — captures, on the unfixed stack, that the
 *    shared and jwt layer assets are verbatim copies of their source
 *    directories and that the handler keeps exactly three layers and its
 *    full configuration. MUST PASS both before and after the fix.
 *    Requirements: 3.1, 3.2.
 *
 * Conventions follow synthetic-data-s3-permissions.test.ts: synthesize once
 * in beforeAll with a generous timeout (asset staging/bundling is
 * expensive), assert on the raw cloud assembly (staged asset directories +
 * template JSON). The synthesized assembly is a deterministic function of
 * the source tree, so these exhaustive assertions quantify over every layer
 * asset and every handler property.
 */
import * as cdk from 'aws-cdk-lib';
import * as cxapi from 'aws-cdk-lib/cx-api';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as fs from 'fs';
import * as path from 'path';
import { StorageStack } from '../lib/storage-stack';
import { SyntheticDataStack } from '../lib/synthetic-data-stack';

const TRUSTED_USECASE_ACCOUNT = '111111111111';
const HANDLER_FUNCTION_NAME = 'dda-synthetic-data-handler';
const LAYERS_SOURCE_DIR = path.join(__dirname, '../../backend/layers');

let assemblyDir: string;
let template: any;

beforeAll(() => {
  const app = new cdk.App();

  const storage = new StorageStack(app, 'Storage');
  const deps = new cdk.Stack(app, 'Deps');

  new SyntheticDataStack(app, 'SyntheticData', {
    useCasesTable: storage.useCasesTable,
    userRolesTable: storage.userRolesTable,
    auditLogTable: storage.auditLogTable,
    settingsTable: storage.settingsTable,
    trainingJobsTable: storage.trainingJobsTable,
    trustedUseCaseAccountIds: [TRUSTED_USECASE_ACCOUNT],
    userPool: new cognito.UserPool(deps, 'Pool'),
    restApiId: 'testrestapiid',
    restApiRootResourceId: 'testrootresourceid',
    apiStageName: 'prod',
  });

  const assembly: cxapi.CloudAssembly = app.synth();
  assemblyDir = assembly.directory;
  template = assembly.getStackByName('SyntheticData').template;
}, 300_000);

/**
 * Resolve the STAGED asset directory for the AWS::Lambda::LayerVersion whose
 * logical id starts with the given prefix. The layer's Content.S3Key is
 * `<assetHash>.zip`, and the staged (or bundled) asset lives at
 * `<assemblyDir>/asset.<assetHash>`.
 */
function stagedLayerAssetDir(logicalIdPrefix: string): string {
  const layers = Object.entries(
    template.Resources as Record<string, any>
  ).filter(
    ([logicalId, res]) =>
      res.Type === 'AWS::Lambda::LayerVersion' &&
      logicalId.startsWith(logicalIdPrefix)
  );
  expect(layers).toHaveLength(1);
  const s3Key: string = layers[0][1].Properties.Content.S3Key;
  expect(typeof s3Key).toBe('string');
  const assetHash = s3Key.replace(/\.zip$/, '');
  const dir = path.join(assemblyDir, `asset.${assetHash}`);
  expect(fs.existsSync(dir)).toBe(true);
  return dir;
}

/** Recursive relative file listing (sorted), for verbatim-copy comparison. */
function listFilesRecursive(dir: string, base = dir): string[] {
  const entries: string[] = [];
  for (const name of fs.readdirSync(dir)) {
    const full = path.join(dir, name);
    if (fs.statSync(full).isDirectory()) {
      entries.push(...listFilesRecursive(full, base));
    } else {
      entries.push(path.relative(base, full));
    }
  }
  return entries.sort();
}

/** The single AWS::Lambda::Function resource for the synthetic handler. */
function handlerResource(): any {
  const fns = Object.values(template.Resources as Record<string, any>).filter(
    (res: any) =>
      res.Type === 'AWS::Lambda::Function' &&
      res.Properties?.FunctionName === HANDLER_FUNCTION_NAME
  );
  expect(fns).toHaveLength(1);
  return fns[0];
}

// ---------------------------------------------------------------------------
// Property 1: Bug Condition — Synthesized Imaging Layer Asset Contains PIL
//
// isBugCondition(asset) := NOT exists(asset/python/PIL). EXPECTED TO FAIL on
// the unfixed stack: the staged asset is the raw backend/layers/imaging
// directory (only build.sh + requirements.txt, no python/). This test
// encodes the expected behavior and validates the fix.
// ---------------------------------------------------------------------------
describe('Property 1: bug condition exploration — staged imaging layer asset contains python/PIL (Requirements 1.1, 1.2, 2.1)', () => {
  test('SyntheticImagingLayer staged asset contains python/PIL with the Pillow package', () => {
    const assetDir = stagedLayerAssetDir('SyntheticImagingLayer');

    // Counterexample surface: on the unfixed stack the asset directory
    // listing is exactly ['build.sh', 'requirements.txt'] — no python/.
    const pilDir = path.join(assetDir, 'python', 'PIL');
    expect(fs.existsSync(pilDir)).toBe(true);

    // Pillow module content is actually installed (not an empty dir).
    expect(fs.existsSync(path.join(pilDir, 'Image.py'))).toBe(true);
    expect(fs.existsSync(path.join(pilDir, 'ImageDraw.py'))).toBe(true);

    // The native imaging extension shipped with the manylinux wheel is
    // present (the layer must work on the Lambda runtime, not just import).
    const soFiles = fs
      .readdirSync(pilDir)
      .filter((f) => f.startsWith('_imaging') && f.includes('.so'));
    expect(soFiles.length).toBeGreaterThanOrEqual(1);
  });
});

// ---------------------------------------------------------------------------
// Property 2: Preservation — Sibling Layers, Handler Wiring Unchanged
//
// Observed on the UNFIXED stack (observation-first): shared/jwt layer assets
// are verbatim copies of their source directories; the handler has exactly
// three layers and its full configuration. MUST PASS before and after the
// fix.
// ---------------------------------------------------------------------------
describe('Property 2: preservation — sibling layers and handler wiring (Requirements 3.1, 3.2)', () => {
  test('shared layer staged asset is a verbatim copy of backend/layers/shared (Requirement 3.1)', () => {
    const assetDir = stagedLayerAssetDir('SyntheticSharedLayer');
    const sourceDir = path.join(LAYERS_SOURCE_DIR, 'shared');
    expect(listFilesRecursive(assetDir)).toEqual(listFilesRecursive(sourceDir));
  });

  test('jwt layer staged asset is a verbatim copy of backend/layers/jwt (Requirement 3.1)', () => {
    const assetDir = stagedLayerAssetDir('SyntheticJwtLayer');
    const sourceDir = path.join(LAYERS_SOURCE_DIR, 'jwt');
    expect(listFilesRecursive(assetDir)).toEqual(listFilesRecursive(sourceDir));
  });

  test('handler keeps exactly three layers (shared, jwt, imaging) and its configuration (Requirement 3.2)', () => {
    const handler = handlerResource();

    // Exactly three layers, referencing the three LayerVersion resources.
    const layerRefs: any[] = handler.Properties.Layers;
    expect(layerRefs).toHaveLength(3);
    const referenced = layerRefs.map((r) => r.Ref as string);
    expect(
      referenced.some((id) => id.startsWith('SyntheticSharedLayer'))
    ).toBe(true);
    expect(referenced.some((id) => id.startsWith('SyntheticJwtLayer'))).toBe(
      true
    );
    expect(
      referenced.some((id) => id.startsWith('SyntheticImagingLayer'))
    ).toBe(true);

    // Function configuration unchanged.
    expect(handler.Properties.Runtime).toBe('python3.11');
    expect(handler.Properties.Handler).toBe('synthetic_data.handler');
    expect(handler.Properties.MemorySize).toBe(1024);
    expect(handler.Properties.Timeout).toBe(900);

    // Environment variable set unchanged.
    expect(
      Object.keys(handler.Properties.Environment.Variables).sort()
    ).toEqual(
      [
        'AUDIT_LOG_TABLE',
        'PORTAL_ACCOUNT_ID',
        'PROMPT_TEMPLATES_TABLE',
        'SETTINGS_TABLE',
        'SYNTHETIC_DATA_FUNCTION_NAME',
        'SYNTHETIC_SESSIONS_TABLE',
        'TRAINING_JOBS_TABLE',
        'USECASES_TABLE',
        'USER_ROLES_TABLE',
      ].sort()
    );
  });
});
