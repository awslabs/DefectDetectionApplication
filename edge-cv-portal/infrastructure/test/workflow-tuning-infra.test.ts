/**
 * Static infrastructure assertions for VLM/LLM Anomaly Tuning
 * (spec: quality-prompt-tuning, task 5.3 — "CDK assertions: table + TTL;
 * Lambda grants (Bedrock, self-invoke policy, shadow, S3 prefix); device
 * role statement iff export enabled; lifecycle rules; routes with the
 * authorizer").
 *
 * What is asserted here is what CDK actually declares:
 *
 * - StorageStack: the `dda-portal-workflow-tuning` single table with
 *   `pk`/`sk` and TTL on `ttl` (Requirement 10.1; outcome items expire,
 *   Req 10.3).
 * - ComputeStack: the `workflow_tuning.py` Lambda's timeout and its four
 *   grants — the Tuning_Session table, Bedrock Converse (Req 6.3), the
 *   standalone self-invoke policy (chunked Score_Run steps, Req 6.8/6.12)
 *   and the named-shadow + `workflow-tuning/*` object access (Req 6.9,
 *   9.4).
 * - ComputeStack: the deployments handler's S3 lifecycle grant, which is
 *   the infrastructure half of Requirement 2.9 (see below).
 * - WorkflowTuningApiStack: every designed `/workflow-tuning/anomaly/**`
 *   route behind the Cognito authorizer, with CORS preflights and a
 *   deployment re-pointing the stage (Req 9.1, 9.2).
 *
 * Two of task 5.3's bullets are deliberately NOT CloudFormation:
 *
 * - The **device role statement iff export enabled** (Req 2.8) is an
 *   inline policy on the Use_Case account's
 *   `GreengrassV2TokenExchangeRole`, written by `deployments.py` when a
 *   deployment delivers the export configuration. That role is created by
 *   `station_install/setup_station.sh`, not by these stacks.
 * - The **Sample_Store lifecycle rules** (Req 2.9) are applied to the
 *   Use_Case's inference results bucket, which no portal stack owns and
 *   which may live in another account, so they are put at delivery time
 *   too. What CDK contributes is the deployments handler's permission to
 *   put them, asserted below.
 *
 * Both are asserted as behaviour by the Portal-side property test
 * `edge-cv-portal/backend/tests/test_property_tuning_deployment_config.py`
 * (Property 17, deployment half).
 */
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { ComputeStack } from '../lib/compute-stack';
import { StorageStack } from '../lib/storage-stack';

const TRUSTED_USECASE_ACCOUNT = '111111111111';
const TUNING_TABLE = 'dda-portal-workflow-tuning';
const TUNING_HANDLER = 'workflow_tuning.handler';

/** The 19 designed routes (design "Portal backend — workflow_tuning.py"). */
const EXPECTED_ROUTES = [
  'DELETE /workflow-tuning/anomaly/sessions/{id}',
  'DELETE /workflow-tuning/anomaly/sessions/{id}/candidates/{cid}',
  'GET /workflow-tuning/anomaly/candidates/{cid}/preview',
  'GET /workflow-tuning/anomaly/score-runs/{rid}',
  'GET /workflow-tuning/anomaly/score-runs/{rid}/diff/{other}',
  'GET /workflow-tuning/anomaly/score-runs/{rid}/outcomes',
  'GET /workflow-tuning/anomaly/sessions/{id}',
  'GET /workflow-tuning/anomaly/sessions/{id}/samples',
  'GET /workflow-tuning/anomaly/workflows',
  'POST /workflow-tuning/anomaly/score-runs/{rid}/cancel',
  'POST /workflow-tuning/anomaly/sessions',
  'POST /workflow-tuning/anomaly/sessions/{id}/apply',
  'POST /workflow-tuning/anomaly/sessions/{id}/candidates',
  'POST /workflow-tuning/anomaly/sessions/{id}/refresh',
  'POST /workflow-tuning/anomaly/sessions/{id}/score-runs',
  'PUT /workflow-tuning/anomaly/sessions/{id}/candidates/{cid}',
  'PUT /workflow-tuning/anomaly/sessions/{id}/samples/labels',
  'PUT /workflow-tuning/anomaly/sessions/{id}/selection',
  'PUT /workflow-tuning/anomaly/sessions/{id}/synthetic-negatives',
];

// Synthesized once: the ComputeStack stages Lambda/layer assets and runs
// the quick-setup bundle packaging script at synth time, which is expensive.
let storageTemplate: Template;
let computeTemplate: Template;
let tuningApiTemplate: Template;

beforeAll(() => {
  const app = new cdk.App();

  const storage = new StorageStack(app, 'Storage');
  const deps = new cdk.Stack(app, 'Deps');

  const compute = new ComputeStack(app, 'Compute', {
    userPool: new cognito.UserPool(deps, 'Pool'),
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

  storageTemplate = Template.fromStack(storage);
  computeTemplate = Template.fromStack(compute);
  tuningApiTemplate = Template.fromStack(
    compute.node.findChild('WorkflowTuningApi') as cdk.NestedStack
  );
}, 300_000);

/** The single Lambda function in the compute template with `handler`. */
function lambdaByHandler(handler: string): [string, any] {
  const matches = Object.entries(
    computeTemplate.findResources('AWS::Lambda::Function')
  ).filter(([, resource]) => (resource as any).Properties.Handler === handler);
  expect(matches).toHaveLength(1);
  return matches[0] as [string, any];
}

/**
 * Every IAM statement attached to the role of `fn`. A role carrying many
 * grants overflows the inline-policy size limit and CDK splits it into
 * managed policies, so both resource types are searched.
 */
function statementsForRole(fn: any): any[] {
  const roleRef = fn.Properties.Role['Fn::GetAtt'][0];
  const policies = [
    ...Object.values(computeTemplate.findResources('AWS::IAM::Policy')),
    ...Object.values(computeTemplate.findResources('AWS::IAM::ManagedPolicy')),
  ] as any[];
  return policies
    .filter((p) => (p.Properties.Roles ?? []).some((r: any) => r.Ref === roleRef))
    .filter((p) => p.Properties.PolicyDocument?.Statement)
    .flatMap((p) => p.Properties.PolicyDocument.Statement as any[]);
}

const asArray = (value: any): any[] =>
  Array.isArray(value) ? value : [value];

const resourceStrings = (statement: any): string[] =>
  asArray(statement.Resource).map((r) => JSON.stringify(r));

/** Statements of `fn`'s role that allow `action`. */
function statementsAllowing(fn: any, action: string): any[] {
  return statementsForRole(fn).filter(
    (s) => s.Effect === 'Allow' && asArray(s.Action).includes(action)
  );
}

describe('Tuning_Session store (Requirements 10.1, 10.3)', () => {
  test('dda-portal-workflow-tuning is a pk/sk table with TTL on ttl', () => {
    storageTemplate.hasResourceProperties(
      'AWS::DynamoDB::Table',
      Match.objectLike({
        TableName: TUNING_TABLE,
        KeySchema: [
          { AttributeName: 'pk', KeyType: 'HASH' },
          { AttributeName: 'sk', KeyType: 'RANGE' },
        ],
        TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
        BillingMode: 'PAY_PER_REQUEST',
      })
    );
  });

  test('both key attributes are strings and no secondary index exists', () => {
    const [, table] = Object.entries(
      storageTemplate.findResources('AWS::DynamoDB::Table')
    ).find(
      ([, resource]) => (resource as any).Properties.TableName === TUNING_TABLE
    ) as [string, any];

    expect(table.Properties.AttributeDefinitions).toEqual(
      expect.arrayContaining([
        { AttributeName: 'pk', AttributeType: 'S' },
        { AttributeName: 'sk', AttributeType: 'S' },
      ])
    );
    // Every access path is a query on one partition (a session's items or a
    // run's outcomes), so an index would only add cost and drift.
    expect(table.Properties.GlobalSecondaryIndexes).toBeUndefined();
    expect(table.Properties.LocalSecondaryIndexes).toBeUndefined();
    // Tuning state survives Portal redeployments (Req 10.1).
    expect(table.DeletionPolicy).toBe('Retain');
  });
});

describe('workflow_tuning.py Lambda grants', () => {
  test('timeout covers the chunked Score_Run steps (Req 6.8)', () => {
    const [, fn] = lambdaByHandler(TUNING_HANDLER);
    expect(fn.Properties.Timeout).toBe(900);
    expect(fn.Properties.Runtime).toBe('python3.11');
  });

  test('the Tuning_Session table is reachable and named in the environment', () => {
    const [, fn] = lambdaByHandler(TUNING_HANDLER);
    expect(fn.Properties.Environment.Variables.WORKFLOW_TUNING_TABLE).toBe(
      TUNING_TABLE
    );

    const writes = statementsAllowing(fn, 'dynamodb:PutItem').filter((s) =>
      resourceStrings(s).some((r) => r.includes(`table/${TUNING_TABLE}`))
    );
    expect(writes.length).toBeGreaterThanOrEqual(1);
    const actions = writes.flatMap((s) => asArray(s.Action));
    for (const action of [
      'dynamodb:GetItem',
      'dynamodb:Query',
      'dynamodb:UpdateItem',
      'dynamodb:DeleteItem',
      'dynamodb:BatchWriteItem',
      'dynamodb:ConditionCheckItem',
    ]) {
      expect(actions).toContain(action);
    }
  });

  test('no other handler may write the Tuning_Session table', () => {
    const [, tuningFn] = lambdaByHandler(TUNING_HANDLER);
    const tuningRoleRef = tuningFn.Properties.Role['Fn::GetAtt'][0];

    const policies = [
      ...Object.values(computeTemplate.findResources('AWS::IAM::Policy')),
      ...Object.values(
        computeTemplate.findResources('AWS::IAM::ManagedPolicy')
      ),
    ] as any[];
    for (const policy of policies) {
      const statements = policy.Properties.PolicyDocument?.Statement ?? [];
      const touchesTuningTable = statements.some((s: any) =>
        resourceStrings(s).some((r) => r.includes(`table/${TUNING_TABLE}`))
      );
      if (!touchesTuningTable) continue;
      const roles = (policy.Properties.Roles ?? []).map((r: any) => r.Ref);
      expect(roles).toEqual([tuningRoleRef]);
    }
  });

  test('Bedrock_Scorer replay grant has the existing foundation-model/inference-profile shape (Req 6.3)', () => {
    const [, fn] = lambdaByHandler(TUNING_HANDLER);
    const invokeModel = statementsAllowing(fn, 'bedrock:InvokeModel');
    expect(invokeModel.length).toBeGreaterThanOrEqual(1);

    const actions = invokeModel.flatMap((s) => asArray(s.Action));
    expect(actions).toContain('bedrock:InvokeModelWithResponseStream');

    const resources = invokeModel.flatMap(resourceStrings);
    expect(resources.some((r) => r.includes('foundation-model/*'))).toBe(true);
    expect(resources.some((r) => r.includes('inference-profile/*'))).toBe(true);
  });

  test('a standalone policy lets the function invoke itself (Req 6.8, 6.12)', () => {
    const [logicalId, fn] = lambdaByHandler(TUNING_HANDLER);
    const roleRef = fn.Properties.Role['Fn::GetAtt'][0];

    // grantInvoke(self) would close a CloudFormation cycle
    // (policy -> function -> role -> policy), so the grant must live in a
    // standalone AWS::IAM::Policy attached to the same role.
    const selfInvokePolicies = Object.entries(
      computeTemplate.findResources('AWS::IAM::Policy')
    ).filter(([, resource]) => {
      const props = (resource as any).Properties;
      const roles = (props.Roles ?? []).map((r: any) => r.Ref);
      if (!roles.includes(roleRef)) return false;
      return (props.PolicyDocument?.Statement ?? []).some(
        (s: any) =>
          asArray(s.Action).includes('lambda:InvokeFunction') &&
          resourceStrings(s).some((r) => r.includes(logicalId))
      );
    });
    expect(selfInvokePolicies).toHaveLength(1);
    expect(selfInvokePolicies[0][0]).toContain('WorkflowTuningSelfInvokePolicy');
    expect(logicalId).toContain('WorkflowTuningHandler');
  });

  test('Device_Score_Job delivery may read and write thing shadows (Req 6.9)', () => {
    const [, fn] = lambdaByHandler(TUNING_HANDLER);
    const shadow = statementsAllowing(fn, 'iot:UpdateThingShadow');
    expect(shadow.length).toBeGreaterThanOrEqual(1);

    expect(shadow.flatMap((s) => asArray(s.Action))).toContain(
      'iot:GetThingShadow'
    );
    // IAM scopes shadow actions to the thing resource; named shadows
    // (dda-workflow-tuning) share it. No statement granting a shadow write
    // may be unscoped — the role reaches thing ARNs and nothing else.
    for (const statement of shadow) {
      for (const resource of resourceStrings(statement)) {
        expect(resource).toContain('arn:aws:iot:');
      }
    }
    expect(
      shadow.flatMap(resourceStrings).some((r) => r.includes(':thing/'))
    ).toBe(true);
  });

  test('Sample_Store object access is confined to workflow-tuning/ (Req 9.4)', () => {
    const [, fn] = lambdaByHandler(TUNING_HANDLER);
    const deletes = statementsAllowing(fn, 's3:DeleteObject');
    expect(deletes.length).toBeGreaterThanOrEqual(1);

    // A deleted session removes its run objects (Req 10.5) and nothing
    // else: every DeleteObject the tuning role holds is scoped to the
    // tuning prefix.
    for (const statement of deletes) {
      for (const resource of resourceStrings(statement)) {
        expect(resource).toContain('workflow-tuning/');
      }
    }
    const actions = deletes.flatMap((s) => asArray(s.Action));
    expect(actions).toContain('s3:GetObject');
    expect(actions).toContain('s3:PutObject');
    // No bucket-level or control-plane action rides along.
    expect(actions).not.toContain('s3:PutBucketPolicy');
    expect(actions).not.toContain('s3:DeleteBucket');
  });
});

describe('Sample_Store lifecycle: the infrastructure half (Requirement 2.9)', () => {
  test('the deployments handler may put lifecycle rules on the inference-results buckets', () => {
    const [, fn] = lambdaByHandler('deployments.handler');
    const lifecycle = statementsAllowing(fn, 's3:PutLifecycleConfiguration');
    expect(lifecycle.length).toBeGreaterThanOrEqual(1);

    expect(lifecycle.flatMap((s) => asArray(s.Action))).toContain(
      's3:GetLifecycleConfiguration'
    );
    // Scoped to the inference-results bucket family — the Sample_Store's
    // bucket. The rules themselves (samples at the Use_Case retention,
    // jobs/sessions at 30 days) are applied by deployments.py at delivery
    // time, since the bucket is not CDK-managed and may be cross-account.
    for (const statement of lifecycle) {
      for (const resource of resourceStrings(statement)) {
        expect(resource).toContain('dda-inference-results-');
      }
    }
  });

  test('no portal stack declares lifecycle rules on a workflow-tuning prefix', () => {
    // Guard against a future rule landing in CDK on a bucket the portal
    // owns: the Sample_Store lives in the Use_Case account's bucket, so a
    // CDK rule here would expire the wrong objects (or none).
    for (const bucket of Object.values(
      storageTemplate.findResources('AWS::S3::Bucket')
    ) as any[]) {
      const rules = bucket.Properties.LifecycleConfiguration?.Rules ?? [];
      for (const rule of rules) {
        expect(JSON.stringify(rule)).not.toContain('workflow-tuning');
      }
    }
  });
});

describe('/workflow-tuning/anomaly/** routes (Requirements 9.1, 9.2)', () => {
  /** `HTTPMETHOD /full/path` for every non-OPTIONS method in the stack. */
  function routes(): string[] {
    const resources = tuningApiTemplate.findResources(
      'AWS::ApiGateway::Resource'
    );
    const pathOf = (resourceId: string): string => {
      const props = (resources[resourceId] as any).Properties;
      const parentRef = props.ParentId?.Ref;
      // A ParentId that is not a resource in this template is the imported
      // API root (passed in as a nested-stack parameter).
      const parentPath =
        parentRef && resources[parentRef] ? pathOf(parentRef) : '';
      return `${parentPath}/${props.PathPart}`;
    };
    return Object.values(
      tuningApiTemplate.findResources('AWS::ApiGateway::Method')
    )
      .map((m: any) => m.Properties)
      .filter((props) => props.HttpMethod !== 'OPTIONS')
      .map((props) => `${props.HttpMethod} ${pathOf(props.ResourceId.Ref)}`)
      .sort();
  }

  test('exactly the designed route table is registered', () => {
    expect(routes()).toEqual(EXPECTED_ROUTES);
  });

  test('every route sits behind the Cognito authorizer on the tuning handler', () => {
    tuningApiTemplate.hasResourceProperties('AWS::ApiGateway::Authorizer', {
      Type: 'COGNITO_USER_POOLS',
      Name: 'EdgeCVPortalWorkflowTuningAuthorizer',
      IdentitySource: 'method.request.header.Authorization',
    });
    expect(
      Object.keys(tuningApiTemplate.findResources('AWS::ApiGateway::Authorizer'))
    ).toHaveLength(1);

    const methods = Object.values(
      tuningApiTemplate.findResources('AWS::ApiGateway::Method')
    )
      .map((m: any) => m.Properties)
      .filter((props) => props.HttpMethod !== 'OPTIONS');
    expect(methods).toHaveLength(EXPECTED_ROUTES.length);
    for (const props of methods) {
      expect(props.AuthorizationType).toBe('COGNITO_USER_POOLS');
      expect(props.AuthorizerId).toBeDefined();
      // Lambda proxy integration into the WorkflowTuningHandler, imported
      // into the nested stack as a parameter reference.
      expect(props.Integration.Type).toBe('AWS_PROXY');
    }
  });

  test('CORS preflights exist and one deployment re-points the stage', () => {
    const options = Object.values(
      tuningApiTemplate.findResources('AWS::ApiGateway::Method')
    )
      .map((m: any) => m.Properties)
      .filter((props) => props.HttpMethod === 'OPTIONS');
    expect(options.length).toBeGreaterThanOrEqual(EXPECTED_ROUTES.length);
    for (const props of options) {
      expect(props.AuthorizationType).toBe('NONE');
    }

    const deployments = tuningApiTemplate.findResources(
      'AWS::ApiGateway::Deployment'
    );
    expect(Object.keys(deployments)).toHaveLength(1);
    // Salted with the route table so a route change rolls a new deployment.
    expect(Object.keys(deployments)[0]).toMatch(
      /^WorkflowTuningDeployment[0-9a-f]{16}$/
    );
  });
});
