/**
 * Static infrastructure assertions for the workflow-manager-gaps generator
 * self-invocation wiring in compute-stack.ts (task 10.1).
 *
 * Requirements covered:
 * - 1.2: the submit path Event-invokes the generator's own function as the
 *   background worker, so the generator role needs lambda:InvokeFunction on
 *   itself and the handler needs its own function name in the environment.
 * - 3.6: an abnormally terminated worker must surface as failed via the
 *   status endpoint's reaper rather than being silently re-run — the async
 *   invoke config carries MaximumRetryAttempts: 0.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { ComputeStack } from '../lib/compute-stack';
import { StorageStack } from '../lib/storage-stack';

const TRUSTED_USECASE_ACCOUNT = '111111111111';
const GENERATOR_FUNCTION_NAME = 'dda-portal-workflow-generator';

// Synthesized once: the ComputeStack stages Lambda/layer assets and runs the
// quick-setup bundle packaging script at synth time, which is expensive.
let computeTemplate: Template;

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

  computeTemplate = Template.fromStack(compute);
}, 300_000);

/** The single workflow_generator.handler Lambda function resource. */
function generatorFunction(): [string, any] {
  const matches = Object.entries(
    computeTemplate.findResources('AWS::Lambda::Function')
  ).filter(
    ([, resource]) =>
      (resource as any).Properties.Handler === 'workflow_generator.handler'
  );
  expect(matches).toHaveLength(1);
  return matches[0] as [string, any];
}

describe('generator self-invocation wiring (Requirements 1.2, 3.6)', () => {
  test('generator carries its fixed function name in WORKFLOW_GENERATOR_FUNCTION_NAME and keeps the 270 s timeout', () => {
    const [, fn] = generatorFunction();
    expect(fn.Properties.FunctionName).toBe(GENERATOR_FUNCTION_NAME);
    expect(
      fn.Properties.Environment.Variables.WORKFLOW_GENERATOR_FUNCTION_NAME
    ).toBe(GENERATOR_FUNCTION_NAME);
    // Lambda timeout stays 270 s (Bedrock client timeout headroom).
    expect(fn.Properties.Timeout).toBe(270);
  });

  test('generator role is granted lambda:InvokeFunction on its own function', () => {
    const [logicalId, fn] = generatorFunction();
    const roleRef = fn.Properties.Role['Fn::GetAtt']?.[0];
    expect(roleRef).toBeDefined();

    // Find a policy attached to the generator role carrying the self-invoke
    // statement scoped to the fixed-name function ARN. The generator role's
    // default policy exceeds the inline-policy size limit, so CDK splits it
    // into overflow AWS::IAM::ManagedPolicy resources — search both types.
    const policies = [
      ...Object.values(computeTemplate.findResources('AWS::IAM::Policy')),
      ...Object.values(
        computeTemplate.findResources('AWS::IAM::ManagedPolicy')
      ),
    ] as any[];
    const selfInvokeStatements = policies
      .filter((p) => p.Properties.Roles?.some((r: any) => r.Ref === roleRef))
      .filter((p) => p.Properties.PolicyDocument?.Statement)
      .flatMap((p) => p.Properties.PolicyDocument.Statement as any[])
      .filter((s) => {
        const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
        if (!actions.includes('lambda:InvokeFunction')) return false;
        const resources = Array.isArray(s.Resource) ? s.Resource : [s.Resource];
        return resources.some((r: any) =>
          JSON.stringify(r).includes(`:function:${GENERATOR_FUNCTION_NAME}`)
        );
      });
    expect(selfInvokeStatements.length).toBeGreaterThanOrEqual(1);

    // Sanity: the logical id is stable (no accidental replacement of the
    // construct itself, only the physical name is pinned).
    expect(logicalId).toContain('WorkflowGeneratorHandler');
  });

  test('async invoke config never retries the background worker', () => {
    const configs = Object.values(
      computeTemplate.findResources('AWS::Lambda::EventInvokeConfig')
    ) as any[];
    const [, fn] = generatorFunction();
    void fn;
    const generatorConfigs = configs.filter((c) =>
      JSON.stringify(c.Properties.FunctionName).includes(
        'WorkflowGeneratorHandler'
      )
    );
    expect(generatorConfigs).toHaveLength(1);
    expect(generatorConfigs[0].Properties.MaximumRetryAttempts).toBe(0);
    expect(generatorConfigs[0].Properties.Qualifier).toBe('$LATEST');
  });
});
