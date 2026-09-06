/**
 * Static infrastructure assertions for the dda-imaging-layer-empty bugfix
 * (compute-stack `ImagingLayer` staged as a raw copy of
 * backend/layers/imaging — layer content depends on whether build.sh was
 * run on the deploying host; on an unbuilt host the layer deploys EMPTY and
 * the DDA labeling functions fail at runtime with "No module named 'PIL'").
 *
 * Property 1 — bug condition exploration (this suite): synthesizes the
 * ComputeStack and SyntheticDataStack into ONE cloud assembly and asserts
 * the STAGED compute `ImagingLayer` asset is synth-time bundling output:
 * `python/` only at the asset root (no staged build tooling), a populated
 * Pillow install under `python/PIL`, and the identical bundled asset as the
 * synthetic stack's `SyntheticImagingLayer` (equal `Content.S3Key`).
 * EXPECTED TO FAIL on the unfixed stack; the same test validates the fix
 * once it passes.
 *
 * HOST-STATE CAVEAT (why the assertions are structural): build.sh HAS been
 * run on this host, so the raw source dir currently contains a python/PIL
 * tree and a naive "asset lacks PIL" check would pass on unfixed code and
 * prove nothing. Raw-copy markers (build.sh / requirements.txt staged as
 * layer content) and cross-stack asset identity are host-state independent:
 * they fail on the unfixed stack no matter what the host contains.
 *
 * **Validates: Requirements 1.1, 1.2, 1.4, 2.1, 2.3**
 *
 * Property 2 — preservation (task 2 of the spec): attach sites and
 * configuration of the three DDA labeling functions, the `ImagingLayer`
 * metadata pinned by llm-model-token-and-image-sizing-infra.test.ts, and
 * verbatim `SharedLayer` staging — captured from the UNFIXED stack
 * (observation-first). MUST PASS both before AND after the fix (the fix
 * only changes how the ImagingLayer ASSET is produced).
 *
 * **Validates: Requirements 3.1, 3.2**
 *
 * Conventions: stack construction mirrors the beforeAll of
 * llm-model-token-and-image-sizing-infra.test.ts; staged-asset resolution
 * via `Content.S3Key` -> `<assemblyDir>/asset.<hash>` mirrors
 * synthetic-imaging-layer-empty.test.ts. Synthesize once in beforeAll with
 * a generous timeout (ComputeStack synth stages many Lambda/layer assets
 * and runs the quick-setup packaging script). The synthesized assembly is a
 * deterministic function of the source tree, so these exhaustive synth-level
 * assertions quantify over every possible synthesis (scoped PBT approach).
 */
import * as cdk from 'aws-cdk-lib';
import * as cxapi from 'aws-cdk-lib/cx-api';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as fs from 'fs';
import * as path from 'path';
import { ComputeStack } from '../lib/compute-stack';
import { StorageStack } from '../lib/storage-stack';
import { SyntheticDataStack } from '../lib/synthetic-data-stack';

const TRUSTED_USECASE_ACCOUNT = '111111111111';

let assemblyDir: string;
let computeTemplate: any;
let syntheticTemplate: any;

beforeAll(() => {
  const app = new cdk.App();

  const storage = new StorageStack(app, 'Storage');
  const deps = new cdk.Stack(app, 'Deps');
  const userPool = new cognito.UserPool(deps, 'Pool');

  new ComputeStack(app, 'Compute', {
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

  // The SyntheticDataStack bundles the same backend/layers/imaging directory
  // as its own (already-fixed) SyntheticImagingLayer — synthesized in the
  // SAME app so the cross-stack asset-identity assertion compares staged
  // assets of one assembly.
  new SyntheticDataStack(app, 'SyntheticData', {
    useCasesTable: storage.useCasesTable,
    userRolesTable: storage.userRolesTable,
    auditLogTable: storage.auditLogTable,
    settingsTable: storage.settingsTable,
    trainingJobsTable: storage.trainingJobsTable,
    // The stack throws at synth time on an empty list — always pass one.
    trustedUseCaseAccountIds: [TRUSTED_USECASE_ACCOUNT],
    userPool,
    restApiId: 'testrestapiid',
    restApiRootResourceId: 'testrootresourceid',
    apiStageName: 'prod',
  });

  const assembly: cxapi.CloudAssembly = app.synth();
  assemblyDir = assembly.directory;
  computeTemplate = assembly.getStackByName('Compute').template;
  syntheticTemplate = assembly.getStackByName('SyntheticData').template;
}, 300_000);

/**
 * The `Content.S3Key` (`<assetHash>.zip`) of the single
 * AWS::Lambda::LayerVersion whose logical id starts with the given prefix.
 */
function layerVersionS3Key(template: any, logicalIdPrefix: string): string {
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
  return s3Key;
}

/** The staged (or bundled) asset directory for an asset `Content.S3Key`. */
function stagedAssetDir(s3Key: string): string {
  const dir = path.join(assemblyDir, `asset.${s3Key.replace(/\.zip$/, '')}`);
  expect(fs.existsSync(dir)).toBe(true);
  return dir;
}

// --- Property 2 helpers (preservation) -------------------------------------

/**
 * The single `[logicalId, resource]` entry of the given resource type whose
 * logical id starts with the given construct-id prefix (CDK logical ids are
 * `<constructId><8-char hash>`; every prefix used here is unique in the
 * compute template, which the toHaveLength(1) guards).
 */
function singleResource(
  template: any,
  type: string,
  logicalIdPrefix: string
): [string, any] {
  const matches = Object.entries(
    template.Resources as Record<string, any>
  ).filter(
    ([logicalId, res]) =>
      res.Type === type && logicalId.startsWith(logicalIdPrefix)
  );
  expect(matches).toHaveLength(1);
  return matches[0];
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

// ---------------------------------------------------------------------------
// Property 1: Bug Condition — Synthesized Compute ImagingLayer Asset Is
// Bundled Pillow
//
// isBugCondition(stagedAsset) := contains build.sh OR requirements.txt at the
// asset root (raw-copy markers: build tooling staged as layer content), OR
// python/PIL absent (unbuilt host => empty layer). Corollary on this branch
// (synthetic layer already bundled): compute Content.S3Key diverges from the
// synthetic Content.S3Key.
//
// EXPECTED TO FAIL on the unfixed stack. This test encodes the expected
// behavior and validates the fix.
//
// **Validates: Requirements 1.1, 1.2, 1.4, 2.1, 2.3**
// ---------------------------------------------------------------------------
describe('Property 1: bug condition exploration — staged compute ImagingLayer asset is bundled Pillow (Requirements 1.1, 1.2, 1.4, 2.1, 2.3)', () => {
  test('staged ImagingLayer asset is bundling output (python/ only, populated PIL) identical to the synthetic bundled asset', () => {
    const computeS3Key = layerVersionS3Key(computeTemplate, 'ImagingLayer');
    const syntheticS3Key = layerVersionS3Key(
      syntheticTemplate,
      'SyntheticImagingLayer'
    );
    const assetDir = stagedAssetDir(computeS3Key);

    // Gather every observation BEFORE asserting, then compare one structural
    // object: a single failing run documents the complete counterexample
    // (asset root listing, PIL state, both S3Keys) in one jest diff.
    const rootEntries = fs.readdirSync(assetDir).sort();
    const pilDir = path.join(assetDir, 'python', 'PIL');
    const pilPresent = fs.existsSync(pilDir);

    const observed = {
      // Counterexample surface (unfixed, this host):
      // ['build.sh', 'python', 'requirements.txt'] — the raw source
      // directory staged verbatim, build tooling included. On a fresh host:
      // ['build.sh', 'requirements.txt'] — the deployed-empty incident.
      assetRootEntries: rootEntries,
      // Pillow module content actually installed (not an empty dir), with
      // the native manylinux extension the Lambda runtime needs. Passes by
      // host accident on unfixed code here (build.sh was run) — the
      // structural assertions above/below are the host-independent ones.
      pil: {
        packageDirPresent: pilPresent,
        imagePy: pilPresent && fs.existsSync(path.join(pilDir, 'Image.py')),
        imageDrawPy:
          pilPresent && fs.existsSync(path.join(pilDir, 'ImageDraw.py')),
        nativeImagingExtension:
          pilPresent &&
          fs
            .readdirSync(pilDir)
            .some((f) => f.startsWith('_imaging') && f.includes('.so')),
      },
      // Counterexample surface (unfixed): compute key != synthetic key (raw
      // source fingerprint vs bundled asset hash).
      contentS3Keys: {
        compute: computeS3Key,
        synthetic: syntheticS3Key,
      },
    };

    expect(observed).toEqual({
      // Bundling output is python/ only — no staged build tooling.
      assetRootEntries: ['python'],
      pil: {
        packageDirPresent: true,
        imagePy: true,
        imageDrawPy: true,
        nativeImagingExtension: true,
      },
      // Both stacks bundle the same source dir with byte-identical bundling
      // options => one shared bundled asset, equal Content.S3Keys (the
      // pinned cross-stack test's equality, restored by construction).
      contentS3Keys: {
        compute: syntheticS3Key,
        synthetic: syntheticS3Key,
      },
    });
  });
});

// ---------------------------------------------------------------------------
// Property 2: Preservation — Attach Sites, Layer Metadata, Sibling Assets
// Unchanged
//
// Observed on the UNFIXED stack (observation-first) and encoded verbatim:
// the fix only changes how the ImagingLayer ASSET is produced, so everything
// below MUST PASS both before and after the fix.
//
// **Validates: Requirements 3.1, 3.2**
// ---------------------------------------------------------------------------
describe('Property 2: preservation — attach sites, layer metadata, sibling assets (Requirements 3.1, 3.2)', () => {
  const DDA_LABELING_FUNCTION_PREFIXES = [
    'DdaLabelingWorker',
    'DdaLabelingHandler',
    'DdaAutolabelWorker',
  ] as const;

  function ddaFunction(prefix: string): any {
    return singleResource(computeTemplate, 'AWS::Lambda::Function', prefix)[1];
  }

  test('the three DDA labeling functions each attach exactly [SharedLayer, ImagingLayer] — the same single ImagingLayer LayerVersion (Requirement 3.1)', () => {
    const [sharedLayerId] = singleResource(
      computeTemplate,
      'AWS::Lambda::LayerVersion',
      'SharedLayer'
    );
    const [imagingLayerId] = singleResource(
      computeTemplate,
      'AWS::Lambda::LayerVersion',
      'ImagingLayer'
    );

    for (const prefix of DDA_LABELING_FUNCTION_PREFIXES) {
      const layerRefs = (ddaFunction(prefix).Properties.Layers as any[]).map(
        (r) => r.Ref as string
      );
      // Exactly two layers, SharedLayer first, the stack's single
      // ImagingLayer second — the identical Ref across all three functions
      // (one shared Pillow build; llm-model-token-and-image-sizing Req 6.6).
      expect({ [prefix]: layerRefs }).toEqual({
        [prefix]: [sharedLayerId, imagingLayerId],
      });
    }
  });

  test('function configuration unchanged: handlers, runtime, timeouts, memory, LLM sizing env wiring (Requirement 3.1)', () => {
    const observed = DDA_LABELING_FUNCTION_PREFIXES.map((prefix) => {
      const props = ddaFunction(prefix).Properties;
      const envKeys = Object.keys(props.Environment.Variables);
      return {
        function: prefix,
        handler: props.Handler,
        runtime: props.Runtime,
        timeout: props.Timeout,
        memorySize: props.MemorySize,
        // Environment wiring pinned where the spec calls it out: the LLM
        // sizing bootstraps live on the handler + autolabel worker but NOT
        // on the labeling worker.
        llmEnv: {
          imageLimits: envKeys.includes('LLM_MODEL_IMAGE_LIMITS'),
          tokenLimits: envKeys.includes('LLM_MODEL_TOKEN_LIMITS'),
        },
      };
    });

    expect(observed).toEqual([
      {
        function: 'DdaLabelingWorker',
        handler: 'dda_labeling_worker.handler',
        runtime: 'python3.11',
        timeout: 900,
        memorySize: 2048,
        llmEnv: { imageLimits: false, tokenLimits: false },
      },
      {
        function: 'DdaLabelingHandler',
        handler: 'dda_labeling.handler',
        runtime: 'python3.11',
        timeout: 900,
        memorySize: 2048,
        llmEnv: { imageLimits: true, tokenLimits: true },
      },
      {
        function: 'DdaAutolabelWorker',
        handler: 'dda_autolabel_worker.handler',
        runtime: 'python3.11',
        timeout: 300,
        memorySize: 2048,
        llmEnv: { imageLimits: true, tokenLimits: true },
      },
    ]);
  });

  test('ImagingLayer metadata unchanged: pinned description and compatible runtimes (Requirement 3.2)', () => {
    const [, imagingLayer] = singleResource(
      computeTemplate,
      'AWS::Lambda::LayerVersion',
      'ImagingLayer'
    );
    // Byte-exact strings pinned by the currently-passing test in
    // llm-model-token-and-image-sizing-infra.test.ts — the fix must not
    // change them (layer metadata is not part of the code asset).
    expect(imagingLayer.Properties.Description).toBe(
      'Pillow imaging layer for DDA labeling mask rendering (built by ' +
        'backend/layers/imaging/build.sh)'
    );
    expect(imagingLayer.Properties.CompatibleRuntimes).toEqual(['python3.11']);
  });

  test('SharedLayer staged asset is a verbatim copy of backend/layers/shared (Requirement 3.2)', () => {
    const sharedS3Key = layerVersionS3Key(computeTemplate, 'SharedLayer');
    const assetDir = stagedAssetDir(sharedS3Key);
    const sourceDir = path.join(__dirname, '../../backend/layers/shared');
    expect(listFilesRecursive(assetDir)).toEqual(listFilesRecursive(sourceDir));
  });
});
