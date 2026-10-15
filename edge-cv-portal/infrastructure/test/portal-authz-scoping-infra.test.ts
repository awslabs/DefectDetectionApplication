/**
 * Shared-components provisioning route and invoke (portal-authorization-and-
 * iam-scoping, design I3; task 3.1; Requirements 2.1, 5.1, 5.2).
 *
 *   - POST /shared-components/provision carries the Cognito authorizer like
 *     every sibling method;
 *   - the UseCases function reaches the SharedComponents function with a
 *     direct invoke: lambda:InvokeFunction on its role and
 *     SHARED_COMPONENTS_FUNCTION_NAME in its environment;
 *   - TRUSTED_USECASE_ACCOUNT_IDS on the UseCases function equals the
 *     deploy-time trusted list, joined;
 *   - the HTTP hop's support is gone: no role carries execute-api:Invoke and
 *     the API_GATEWAY_ID environment patch (UpdateLambdaEnv function and the
 *     LambdaEnvUpdater custom resource) is not synthesized.
 *
 * The S3 data-plane grant (design I5 "Design D5-B"; task 5.4; Requirements
 * 7.1-7.4, 7.8), for the 'tag' and 'tag-objects-only' synths of the compute,
 * synthetic-data and node-designer stacks:
 *
 *   - no S3 object statement on the objects of every bucket (or one prefix of
 *     every bucket) and no s3:ListBucket / s3:GetBucketLocation on every
 *     bucket goes without the dda-portal:managed tag condition;
 *   - every data-plane role carries the unconditioned tag-discovery
 *     statement with exactly {s3:GetBucketTagging, s3:GetBucketABAC}, the
 *     only unconditioned statement on every bucket besides the ABAC control
 *     plane of the UseCases and DataManagement roles;
 *   - the node-designer DeleteObject stays under plugins/; Captures and
 *     WorkflowTuning reach the inference-results bucket by name;
 *   - the UseCases role keeps exactly the sibling's three statements, the
 *     DataManagement role gains s3:PutBucketABAC, no other role holds any of
 *     the five tag / ABAC control-plane actions;
 *   - DATA_PLANE_SCOPING reaches the handlers' environment.
 *
 * Conventions follow usecase-bucket-config-iam.test.ts: synthesize
 * StorageStack + ComputeStack once in beforeAll, read the parent template and
 * the ApiGateway nested stack's template, assert on raw CloudFormation
 * properties.
 */
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { ComputeStack } from '../lib/compute-stack';
import {
  DATA_PLANE_SCOPING,
  DataPlaneScoping,
  PORTAL_MANAGED_TAG_CONDITION,
} from '../lib/context-helpers';
import { NodeDesignerStack } from '../lib/node-designer-stack';
import { StorageStack } from '../lib/storage-stack';
import { SyntheticDataStack } from '../lib/synthetic-data-stack';

// Fixture account ids, assembled so no identifier-shaped literal lands in the
// source.
const TRUSTED_ACCOUNTS = ['1'.repeat(12), '2'.repeat(12)];

let computeTemplate: Template;
let apiTemplate: Template;

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
    trustedUseCaseAccountIds: TRUSTED_ACCOUNTS,
  });
  computeTemplate = Template.fromStack(compute);
  apiTemplate = Template.fromStack(
    compute.node.findChild('ApiGateway') as cdk.NestedStack
  );
}, 300_000);

function functionByHandler(template: Template, handler: string): [string, any] {
  const matches = Object.entries(
    template.findResources('AWS::Lambda::Function')
  ).filter(([, fn]: [string, any]) => fn.Properties?.Handler === handler);
  expect(matches).toHaveLength(1);
  return matches[0] as [string, any];
}

function roleLogicalId(prefix: string): string {
  const matches = Object.keys(
    computeTemplate.findResources('AWS::IAM::Role')
  ).filter((logicalId) => logicalId.startsWith(prefix));
  expect(matches).toHaveLength(1);
  return matches[0];
}

/** Every policy statement in a template (policies, managed policies, inline role policies). */
function allStatements(template: Template): Array<{ carrier: string; statement: any }> {
  const out: Array<{ carrier: string; statement: any }> = [];
  for (const type of ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy']) {
    for (const [id, res] of Object.entries(template.findResources(type)) as [string, any][]) {
      for (const statement of res.Properties?.PolicyDocument?.Statement ?? []) {
        out.push({ carrier: id, statement });
      }
    }
  }
  for (const [id, res] of Object.entries(template.findResources('AWS::IAM::Role')) as [string, any][]) {
    for (const policy of res.Properties?.Policies ?? []) {
      for (const statement of policy.PolicyDocument?.Statement ?? []) {
        out.push({ carrier: id, statement });
      }
    }
  }
  return out;
}

/** Statements attached to a role (by Roles reference). */
function roleStatements(roleId: string): any[] {
  return [
    ...Object.values(computeTemplate.findResources('AWS::IAM::Policy')),
    ...Object.values(computeTemplate.findResources('AWS::IAM::ManagedPolicy')),
  ]
    .filter((p: any) => p.Properties.Roles?.some((r: any) => r.Ref === roleId))
    .flatMap((p: any) => p.Properties.PolicyDocument?.Statement ?? []);
}

function asList(value: any): any[] {
  return Array.isArray(value) ? value : [value];
}

describe('POST /shared-components/provision', () => {
  test('is Cognito-authorized like every sibling method', () => {
    const resources = apiTemplate.findResources('AWS::ApiGateway::Resource');
    const sharedComponents = Object.entries(resources).find(
      ([, r]: [string, any]) => r.Properties.PathPart === 'shared-components'
    );
    expect(sharedComponents).toBeDefined();
    const provision = Object.entries(resources).filter(
      ([, r]: [string, any]) =>
        r.Properties.PathPart === 'provision' &&
        r.Properties.ParentId?.Ref === sharedComponents![0]
    );
    expect(provision).toHaveLength(1);
    const methods = Object.values(apiTemplate.findResources('AWS::ApiGateway::Method'))
      .map((m: any) => m.Properties)
      .filter((p: any) => p.ResourceId?.Ref === provision[0][0] && p.HttpMethod === 'POST');
    expect(methods).toHaveLength(1);
    expect(methods[0].AuthorizationType).toBe('COGNITO_USER_POOLS');
    expect(methods[0].AuthorizerId).toBeDefined();
  });

  test('every non-preflight /shared-components method carries the authorizer', () => {
    const resources = apiTemplate.findResources('AWS::ApiGateway::Resource');
    const ids = new Set<string>();
    const root = Object.entries(resources).find(
      ([, r]: [string, any]) => r.Properties.PathPart === 'shared-components'
    )![0];
    ids.add(root);
    for (const [id, r] of Object.entries(resources) as [string, any][]) {
      if (r.Properties.ParentId?.Ref === root) ids.add(id);
    }
    const methods = Object.values(apiTemplate.findResources('AWS::ApiGateway::Method'))
      .map((m: any) => m.Properties)
      .filter((p: any) => ids.has(p.ResourceId?.Ref) && p.HttpMethod !== 'OPTIONS');
    expect(methods.length).toBeGreaterThanOrEqual(5);
    for (const method of methods) {
      expect(method.AuthorizationType).toBe('COGNITO_USER_POOLS');
    }
  });
});

describe('UseCases -> SharedComponents direct invoke', () => {
  test('the UseCases role may invoke the SharedComponents function', () => {
    const [sharedComponentsId] = functionByHandler(computeTemplate, 'shared_components.handler');
    const invokes = roleStatements(roleLogicalId('UseCasesRole')).filter(
      (s) =>
        s.Effect === 'Allow' &&
        asList(s.Action).includes('lambda:InvokeFunction') &&
        asList(s.Resource).some(
          (r: any) => JSON.stringify(r) === JSON.stringify({ 'Fn::GetAtt': [sharedComponentsId, 'Arn'] })
        )
    );
    expect(invokes).toHaveLength(1);
  });

  test('the UseCases function names the SharedComponents function and the trusted list', () => {
    const [sharedComponentsId] = functionByHandler(computeTemplate, 'shared_components.handler');
    const [, useCases] = functionByHandler(computeTemplate, 'usecases.handler');
    const variables = useCases.Properties.Environment.Variables;
    expect(variables.SHARED_COMPONENTS_FUNCTION_NAME).toEqual({ Ref: sharedComponentsId });
    expect(variables.TRUSTED_USECASE_ACCOUNT_IDS).toBe(TRUSTED_ACCOUNTS.join(','));
    expect(variables.API_GATEWAY_ID).toBeUndefined();
    expect(variables.PORTAL_API_URL).toBeUndefined();
  });

  test('every handler of the compute stack carries the trusted list', () => {
    const functions = Object.values(computeTemplate.findResources('AWS::Lambda::Function')) as any[];
    const withPortalEnvironment = functions.filter(
      (fn) => fn.Properties?.Environment?.Variables?.PORTAL_ACCOUNT_ID !== undefined
    );
    expect(withPortalEnvironment.length).toBeGreaterThan(10);
    for (const fn of withPortalEnvironment) {
      expect(fn.Properties.Environment.Variables.TRUSTED_USECASE_ACCOUNT_IDS).toBe(
        TRUSTED_ACCOUNTS.join(',')
      );
    }
  });
});

describe('the HTTP hop support is retired', () => {
  test('no role carries execute-api:Invoke', () => {
    const offenders = [...allStatements(computeTemplate), ...allStatements(apiTemplate)]
      .filter(({ statement }) =>
        asList(statement.Action).some(
          (a: any) => typeof a === 'string' && a.toLowerCase().startsWith('execute-api:')
        )
      )
      .map(({ carrier }) => carrier);
    expect(offenders).toEqual([]);
  });

  test('the API_GATEWAY_ID environment patch is not synthesized', () => {
    const ids = Object.keys(computeTemplate.toJSON().Resources);
    expect(ids.filter((id) => id.startsWith('UpdateLambdaEnv') || id.startsWith('LambdaEnvUpdater'))).toEqual([]);
    expect(computeTemplate.findResources('AWS::CloudFormation::CustomResource')).not.toHaveProperty(
      'LambdaEnvUpdater'
    );
  });
});

// ---------------------------------------------------------------------------
// The S3 data-plane grant (task 5.4)
// ---------------------------------------------------------------------------

const ANY_BUCKET = 'arn:aws:s3:::*';
const FIXTURE_ALLOWLIST = ['fixture-data-a', 'fixture-data-b'];
const FIXTURE_ALLOWLIST_ARNS = FIXTURE_ALLOWLIST.map((name) => `arn:aws:s3:::${name}`);
const S3_OBJECT_ACTIONS = [
  's3:GetObject', 's3:PutObject', 's3:DeleteObject', 's3:GetObjectVersion', 's3:PutObjectTagging',
];
const TAG_DISCOVERY_ACTIONS = ['s3:GetBucketABAC', 's3:GetBucketTagging'];
const ABAC_CONTROL_PLANE_ACTIONS = [
  's3:CreateBucket', 's3:TagResource', 's3:UntagResource', 's3:ListTagsForResource', 's3:PutBucketABAC',
];
const CREATED_BY_CONDITION = {
  StringEquals: { 'aws:RequestTag/dda-portal:created-by': 'portal' },
};
const TAG_CONDITION_JSON = JSON.stringify(PORTAL_MANAGED_TAG_CONDITION);

type ScopingTemplates = { Compute: Template; SyntheticData: Template; NodeDesigner: Template };

/** One app with the three data-plane stacks synthesized under `scoping`. */
function synthesizeScoping(scoping: DataPlaneScoping): ScopingTemplates {
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
    trustedUseCaseAccountIds: TRUSTED_ACCOUNTS,
    dataBucketAllowlist: FIXTURE_ALLOWLIST,
    dataPlaneScoping: scoping,
  });
  const syntheticData = new SyntheticDataStack(app, 'SyntheticData', {
    useCasesTable: storage.useCasesTable,
    userRolesTable: storage.userRolesTable,
    auditLogTable: storage.auditLogTable,
    settingsTable: storage.settingsTable,
    trainingJobsTable: storage.trainingJobsTable,
    trustedUseCaseAccountIds: TRUSTED_ACCOUNTS,
    dataBucketAllowlist: FIXTURE_ALLOWLIST,
    dataPlaneScoping: scoping,
    userPool,
    restApiId: 'testrestapi',
    restApiRootResourceId: 'testrootresource',
    apiStageName: 'v1',
  });
  const nodeDesigner = new NodeDesignerStack(app, 'NodeDesigner', {
    portalArtifactsBucket: new s3.Bucket(deps, 'Artifacts'),
    useCasesTable: storage.useCasesTable,
    userRolesTable: storage.userRolesTable,
    auditLogTable: storage.auditLogTable,
    settingsTable: storage.settingsTable,
    workflowsTable: storage.workflowsTable,
    workflowVersionsTable: storage.workflowVersionsTable,
    testDatasetsTable: storage.testDatasetsTable,
    trustedUseCaseAccountIds: TRUSTED_ACCOUNTS,
    dataBucketAllowlist: FIXTURE_ALLOWLIST,
    dataPlaneScoping: scoping,
    userPool,
    restApiId: 'testrestapi',
    restApiRootResourceId: 'testrootresource',
    apiStageName: 'v1',
  });
  return {
    Compute: Template.fromStack(compute),
    SyntheticData: Template.fromStack(syntheticData),
    NodeDesigner: Template.fromStack(nodeDesigner),
  };
}

/** A resource as text: Fn::Join on '' concatenated, a Ref as `${<name>}`. */
function resourceText(resource: any): string {
  if (typeof resource === 'string') return resource;
  if (resource && Array.isArray(resource['Fn::Join']) && resource['Fn::Join'][0] === '') {
    return resource['Fn::Join'][1].map(resourceText).join('');
  }
  if (resource && typeof resource.Ref === 'string') return '${' + resource.Ref + '}';
  return JSON.stringify(resource);
}

const actionsOf = (statement: any): string[] =>
  asList(statement.Action).filter((action: any) => typeof action === 'string');
const resourcesOf = (statement: any): string[] => asList(statement.Resource).map(resourceText);
const conditionJson = (statement: any): string | undefined =>
  statement.Condition === undefined ? undefined : JSON.stringify(statement.Condition);

/** Every role of a template with all its Allow statements (inline, policies, managed policies). */
function allowStatementsByRole(template: Template): Map<string, any[]> {
  const byRole = new Map<string, any[]>();
  for (const [id, role] of Object.entries(template.findResources('AWS::IAM::Role')) as [string, any][]) {
    byRole.set(id, (role.Properties?.Policies ?? []).flatMap((p: any) => p.PolicyDocument?.Statement ?? []));
  }
  for (const type of ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy']) {
    for (const policy of Object.values(template.findResources(type)) as any[]) {
      for (const ref of policy.Properties?.Roles ?? []) {
        byRole.get(ref?.Ref)?.push(...(policy.Properties.PolicyDocument?.Statement ?? []));
      }
    }
  }
  for (const [id, statements] of byRole) {
    byRole.set(id, statements.filter((s) => s.Effect === 'Allow'));
  }
  return byRole;
}

/** The single role whose logical id starts with `prefix`. */
function roleIdWithPrefix(byRole: Map<string, any[]>, prefix: string): string {
  const matches = [...byRole.keys()].filter((id) => id.startsWith(prefix));
  expect(matches).toHaveLength(1);
  return matches[0];
}

/** Data-plane roles: the roles holding the object statement on every bucket's objects. */
function dataPlaneRoleIds(byRole: Map<string, any[]>): string[] {
  return [...byRole.entries()]
    .filter(([, statements]) =>
      statements.some((s) => actionsOf(s).includes('s3:GetObject') && resourcesOf(s).includes(`${ANY_BUCKET}/*`)))
    .map(([id]) => id);
}

describe.each(['tag', 'tag-objects-only'] as DataPlaneScoping[])(
  "Data_Plane_Scoping '%s': the S3 data-plane grant (task 5.4)",
  (scoping) => {
    let templates: ScopingTemplates;
    let byStack: Record<string, Map<string, any[]>>;

    beforeAll(() => {
      templates = synthesizeScoping(scoping);
      byStack = Object.fromEntries(
        Object.entries(templates).map(([name, template]) => [name, allowStatementsByRole(template)]),
      );
    }, 900_000);

    test('no S3 object statement on every bucket\'s objects goes without the tag condition', () => {
      const offenders: string[] = [];
      let conditioned = 0;
      for (const [stack, byRole] of Object.entries(byStack)) {
        for (const [roleId, statements] of byRole) {
          for (const s of statements) {
            if (!actionsOf(s).some((a) => S3_OBJECT_ACTIONS.includes(a))) continue;
            if (!resourcesOf(s).some((r) => r.startsWith(`${ANY_BUCKET}/`))) continue;
            if (conditionJson(s) === TAG_CONDITION_JSON) conditioned += 1;
            else offenders.push(`${stack}/${roleId}: ${JSON.stringify(s)}`);
          }
        }
      }
      expect(offenders).toEqual([]);
      expect(conditioned).toBeGreaterThan(40);
    });

    test('no s3:ListBucket or s3:GetBucketLocation on every bucket goes without the tag condition', () => {
      const onEveryBucket: Array<{ where: string; statement: any }> = [];
      for (const [stack, byRole] of Object.entries(byStack)) {
        for (const [roleId, statements] of byRole) {
          for (const s of statements) {
            const listing = actionsOf(s).some((a) => a === 's3:ListBucket' || a === 's3:GetBucketLocation');
            if (listing && resourcesOf(s).includes(ANY_BUCKET)) {
              onEveryBucket.push({ where: `${stack}/${roleId}`, statement: s });
            }
          }
        }
      }
      expect(
        onEveryBucket.filter(({ statement }) => conditionJson(statement) !== TAG_CONDITION_JSON).map(({ where }) => where),
      ).toEqual([]);
      if (scoping === 'tag') {
        expect(onEveryBucket.length).toBeGreaterThan(40);
      } else {
        // The bucket-level statement names the allowlist instead.
        expect(onEveryBucket).toEqual([]);
      }
    });

    test('every data-plane role carries the bucket-level statement of this scoping', () => {
      for (const [stack, byRole] of Object.entries(byStack)) {
        const roles = dataPlaneRoleIds(byRole);
        expect(roles.length).toBeGreaterThan(stack === 'Compute' ? 40 : 0);
        for (const roleId of roles) {
          const bucketLevel = byRole.get(roleId)!.filter(
            (s) => [...actionsOf(s)].sort().join(',') === 's3:GetBucketLocation,s3:ListBucket',
          );
          expect({ roleId, count: bucketLevel.length }).toEqual({ roleId, count: 1 });
          if (scoping === 'tag') {
            expect(resourcesOf(bucketLevel[0])).toEqual([ANY_BUCKET]);
            expect(conditionJson(bucketLevel[0])).toBe(TAG_CONDITION_JSON);
          } else {
            expect(resourcesOf(bucketLevel[0])).toEqual(FIXTURE_ALLOWLIST_ARNS);
            expect(bucketLevel[0].Condition).toBeUndefined();
          }
        }
      }
    });

    test('every data-plane role carries tag discovery with exactly its two actions, the only unconditioned grant on every bucket besides the ABAC control plane', () => {
      // Read per action: when CDK splits an oversized role policy it renders
      // same-resource, same-condition statements as one statement, so on the
      // UseCases and DataManagement roles the tag-discovery actions share a
      // statement with their unconditioned ABAC control-plane grants.
      let standalone = 0;
      for (const [stack, byRole] of Object.entries(byStack)) {
        for (const roleId of dataPlaneRoleIds(byRole)) {
          const unconditionedOnEveryBucket = byRole
            .get(roleId)!
            .filter((s) => s.Condition === undefined && resourcesOf(s).includes(ANY_BUCKET));
          for (const s of unconditionedOnEveryBucket) {
            expect({ roleId, resources: resourcesOf(s) }).toEqual({ roleId, resources: [ANY_BUCKET] });
          }
          const actions = unconditionedOnEveryBucket.flatMap(actionsOf).sort();
          const controlPlane = roleId.startsWith('UseCasesRole')
            ? ['s3:CreateBucket', 's3:PutBucketABAC']
            : roleId.startsWith('DataManagementRole')
              ? ['s3:PutBucketABAC']
              : [];
          expect({ stack, roleId, actions }).toEqual({
            stack, roleId, actions: [...TAG_DISCOVERY_ACTIONS, ...controlPlane].sort(),
          });
          standalone += unconditionedOnEveryBucket.filter(
            (s) => [...actionsOf(s)].sort().join(',') === TAG_DISCOVERY_ACTIONS.join(','),
          ).length;
        }
      }
      // Every other data-plane role carries the statement on its own.
      expect(standalone).toBeGreaterThan(40);
    });

    test('the node-designer DeleteObject stays under plugins/', () => {
      const byRole = byStack.NodeDesigner;
      const roleId = roleIdWithPrefix(byRole, 'PluginComponentsRole');
      const deletes = byRole.get(roleId)!.filter((s) => actionsOf(s).includes('s3:DeleteObject'));
      expect(deletes).toHaveLength(1);
      expect(actionsOf(deletes[0])).toEqual(['s3:DeleteObject']);
      expect(resourcesOf(deletes[0])).toEqual([`${ANY_BUCKET}/plugins/*`]);
      expect(conditionJson(deletes[0])).toBe(TAG_CONDITION_JSON);
      // Nothing on the role reaches every bucket unconditioned except tag discovery.
      expect(
        byRole.get(roleId)!
          .filter((s) => s.Condition === undefined && resourcesOf(s).some((r) => r.startsWith(ANY_BUCKET)))
          .map((s) => [...actionsOf(s)].sort().join(',')),
      ).toEqual([TAG_DISCOVERY_ACTIONS.join(',')]);
    });

    test('Captures and WorkflowTuning reach the inference-results bucket by name', () => {
      const byRole = byStack.Compute;
      const resultsBucket = 'arn:aws:s3:::dda-inference-results-${AWS::AccountId}';
      const statementsOf = (prefix: string) => byRole.get(roleIdWithPrefix(byRole, prefix))!;
      const has = (statements: any[], actions: string[], resource: string) =>
        statements.some(
          (s) =>
            s.Condition === undefined &&
            [...actionsOf(s)].sort().join(',') === [...actions].sort().join(',') &&
            JSON.stringify(resourcesOf(s)) === JSON.stringify([resource]),
        );
      const captures = statementsOf('CapturesRole');
      expect(has(captures, ['s3:ListBucket'], resultsBucket)).toBe(true);
      expect(has(captures, ['s3:GetObject'], `${resultsBucket}/*`)).toBe(true);
      const tuning = statementsOf('WorkflowTuningRole');
      expect(has(tuning, ['s3:ListBucket'], resultsBucket)).toBe(true);
      expect(has(tuning, ['s3:GetObject', 's3:PutObject', 's3:DeleteObject'],
        `${resultsBucket}/workflow-tuning/*`)).toBe(true);
      // The prefix grant on the tagged buckets keeps the tag condition.
      const prefixGrant = tuning.filter((s) => resourcesOf(s).includes(`${ANY_BUCKET}/workflow-tuning/*`));
      expect(prefixGrant).toHaveLength(1);
      expect(conditionJson(prefixGrant[0])).toBe(TAG_CONDITION_JSON);
    });

    /**
     * The ABAC control-plane grants of a role, one per action: the action,
     * its resources and its condition. The factory's own s3:CreateBucket on
     * the portal artifacts bucket is not a grant on arbitrary buckets and is
     * left out.
     */
    function controlPlaneGrants(statements: any[]): Array<{ action: string; resources: string[]; condition?: string }> {
      const grants: Array<{ action: string; resources: string[]; condition?: string }> = [];
      for (const s of statements) {
        for (const action of actionsOf(s)) {
          if (!ABAC_CONTROL_PLANE_ACTIONS.includes(action)) continue;
          if (action === 's3:CreateBucket' && !resourcesOf(s).includes(ANY_BUCKET)) continue;
          grants.push({ action, resources: resourcesOf(s), condition: conditionJson(s) });
        }
      }
      return grants.sort((a, b) => a.action.localeCompare(b.action));
    }

    test('the UseCases role keeps exactly the sibling\'s three ABAC control-plane grants, TagResource with its landed condition', () => {
      const byRole = byStack.Compute;
      const statements = byRole.get(roleIdWithPrefix(byRole, 'UseCasesRole'))!;
      expect(controlPlaneGrants(statements)).toEqual([
        { action: 's3:CreateBucket', resources: [ANY_BUCKET], condition: undefined },
        { action: 's3:PutBucketABAC', resources: [ANY_BUCKET], condition: undefined },
        { action: 's3:TagResource', resources: [ANY_BUCKET], condition: JSON.stringify(CREATED_BY_CONDITION) },
      ]);
      const all = statements.flatMap(actionsOf);
      expect(all).not.toContain('s3:UntagResource');
      expect(all).not.toContain('s3:ListTagsForResource');
    });

    test('the DataManagement role carries s3:PutBucketABAC and no other ABAC control-plane grant', () => {
      const byRole = byStack.Compute;
      const statements = byRole.get(roleIdWithPrefix(byRole, 'DataManagementRole'))!;
      expect(controlPlaneGrants(statements)).toEqual([
        { action: 's3:PutBucketABAC', resources: [ANY_BUCKET], condition: undefined },
      ]);
    });

    test('exactly UseCases and DataManagement carry s3:PutBucketABAC; no other role holds any of the five', () => {
      const holders: Record<string, string[]> = {};
      for (const [stack, byRole] of Object.entries(byStack)) {
        for (const [roleId, statements] of byRole) {
          for (const s of statements) {
            for (const action of actionsOf(s)) {
              if (!ABAC_CONTROL_PLANE_ACTIONS.includes(action)) continue;
              // The factory grants s3:CreateBucket on the portal artifacts bucket only.
              if (action === 's3:CreateBucket' && !resourcesOf(s).includes(ANY_BUCKET)) continue;
              const key = `${stack}/${roleId.replace(/[0-9A-F]{8}$/, '')}`;
              holders[key] = [...(holders[key] ?? []), action].sort();
            }
          }
        }
      }
      expect(holders).toEqual({
        'Compute/UseCasesRole': ['s3:CreateBucket', 's3:PutBucketABAC', 's3:TagResource'],
        'Compute/DataManagementRole': ['s3:PutBucketABAC'],
      });
    });

    test('DATA_PLANE_SCOPING on the UseCases and DataManagement functions is the synthesized scoping', () => {
      for (const handler of ['usecases.handler', 'data_management.handler']) {
        const [, fn] = functionByHandler(templates.Compute, handler);
        expect(fn.Properties.Environment.Variables.DATA_PLANE_SCOPING).toBe(scoping);
      }
    });
  },
);

// The DataManagement role's s3:PutBucketABAC carries the repository's
// documented-exception marker for the IAM audit (the same marker as the
// UseCases role's identical statement), so it must stay exactly this narrow:
// one action, every bucket, no condition. Widening it fails here.
describe('The documented wildcard exception on the DataManagement role (task 5.4)', () => {
  test('compute-stack.ts declares it as one statement: exactly s3:PutBucketABAC on arn:aws:s3:::*, no condition', () => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'lib', 'compute-stack.ts'), 'utf8');
    expect(source.split('dataManagementRole.addToPolicy(').length - 1).toBe(1);
    const start = source.indexOf('dataManagementRole.addToPolicy(new iam.PolicyStatement({');
    expect(start).toBeGreaterThan(-1);
    const block = source.slice(start, source.indexOf('}));', start));
    expect(block).toContain('nosec: iam-resource-wildcard');
    expect(block.match(/actions:\s*\[[^\]]*\]/g)).toEqual(["actions: ['s3:PutBucketABAC']"]);
    expect(block.match(/resources:\s*\[[^\]]*\]/g)).toEqual(["resources: ['arn:aws:s3:::*']"]);
    expect(block).not.toMatch(/conditions\s*:|notActions|notResources|effect:\s*iam\.Effect\.DENY/);
  });

  test('the synthesized DataManagement role grants s3:PutBucketABAC once, on arn:aws:s3:::*, unconditioned, and nothing else unconditioned on every bucket beside tag discovery', () => {
    // Read per action: CDK renders the role's unconditioned statements on
    // arn:aws:s3:::* as one statement when it splits the role policy.
    const byRole = allowStatementsByRole(computeTemplate);
    const statements = byRole.get(roleIdWithPrefix(byRole, 'DataManagementRole'))!;
    const grants = statements.flatMap((s) =>
      actionsOf(s).filter((a) => a === 's3:PutBucketABAC').map(() => s));
    expect(grants).toHaveLength(1);
    expect(resourcesOf(grants[0])).toEqual([ANY_BUCKET]);
    expect(grants[0].Condition).toBeUndefined();
    const unconditionedOnEveryBucket = statements.filter(
      (s) => s.Condition === undefined && resourcesOf(s).some((r) => r.startsWith(ANY_BUCKET)));
    for (const s of unconditionedOnEveryBucket) {
      expect(resourcesOf(s)).toEqual([ANY_BUCKET]);
    }
    expect(unconditionedOnEveryBucket.flatMap(actionsOf).sort()).toEqual(
      [...TAG_DISCOVERY_ACTIONS, 's3:PutBucketABAC'].sort());
  });
});

describe('DATA_PLANE_SCOPING reaches every compute handler (default synth)', () => {
  test('the UseCases and DataManagement functions carry the exported constant', () => {
    for (const handler of ['usecases.handler', 'data_management.handler']) {
      const [, fn] = functionByHandler(computeTemplate, handler);
      expect(fn.Properties.Environment.Variables.DATA_PLANE_SCOPING).toBe(DATA_PLANE_SCOPING);
    }
  });

  test('every handler with the shared environment carries it', () => {
    const functions = Object.values(computeTemplate.findResources('AWS::Lambda::Function')) as any[];
    const shared = functions.filter((fn) => fn.Properties?.Environment?.Variables?.PORTAL_ACCOUNT_ID !== undefined);
    expect(shared.length).toBeGreaterThan(10);
    for (const fn of shared) {
      expect(fn.Properties.Environment.Variables.DATA_PLANE_SCOPING).toBe(DATA_PLANE_SCOPING);
    }
  });
});
