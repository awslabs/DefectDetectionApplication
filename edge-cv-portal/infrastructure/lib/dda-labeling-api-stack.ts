import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Construct } from 'constructs';
import * as crypto from 'crypto';

export interface DdaLabelingApiStackProps extends cdk.NestedStackProps {
  /** Rest API id of the existing portal API (ComputeStack/ApiGatewayStack). */
  restApiId: string;
  /** Root resource id of the existing portal API. */
  restApiRootResourceId: string;
  /** Resource id of /labeling/{id} in the ApiGatewayStack. */
  labelingJobResourceId: string;
  /** Stage the portal API serves on (ApiGatewayStack deployOptions.stageName). */
  stageName: string;
  userPool: cognito.IUserPool;
  /** dda_labeling.py — teams, labeler task APIs, admin review. */
  ddaLabelingHandler: lambda.Function;
  /** labeling.py — owns POST /labeling/{id}/stop (task 9.1). */
  labelingHandler: lambda.Function;
}

/**
 * DDA Data Labeling API Gateway routes (dda-data-labeling, task 14.2).
 *
 * The portal API's own nested stack (ApiGatewayStack) sits at the
 * CloudFormation 500-resource limit, so the DDA labeling routes live in
 * their own nested stack that imports the Rest API by id and registers
 * resources/methods against its root — the same pattern as
 * UserAdminApiStack. The /labeling/{id} resource is owned by the
 * ApiGatewayStack, so the /stop and /review* sub-resources attach under it
 * via the imported resource id (the CameraRegistryApiStack /devices/{id}
 * pattern). A fresh deployment created here re-points the existing stage so
 * the new routes go live; its logical id is salted with the route table so
 * route changes roll a new deployment.
 *
 * Every method is guarded by the Cognito user pool authorizer (JWT on the
 * Authorization header); RBAC (MANAGE_LABELING_TEAMS, LABELING_TASKS_SELF,
 * MANAGE_LABELING_JOBS — Requirements 2.5, 3.7, 11.4) is enforced inside
 * the handlers via @rbac_check.
 *
 * Route table (design "2. API surface"):
 * - GET    /labeling-teams                                (list teams, dda_labeling.py)
 * - POST   /labeling-teams                                (create team)
 * - DELETE /labeling-teams/{teamId}                       (delete team)
 * - POST   /labeling-teams/{teamId}/members               (add member)
 * - DELETE /labeling-teams/{teamId}/members/{userId}      (remove member)
 * - GET    /labeler/jobs                                  (caller's jobs)
 * - GET    /labeler/jobs/{jobId}/next                     (next presentable task)
 * - POST   /labeler/tasks/{taskId}/submit                 (persist annotation)
 * - POST   /labeler/tasks/{taskId}/presentation-failure   (withhold task)
 * - GET    /labeler/tasks/{taskId}/image-url              (fresh presigned URL)
 * - POST   /labeling/{id}/stop                            (stop DDA job, labeling.py)
 * - GET    /labeling/{id}/review                          (auto-label results)
 * - POST   /labeling/{id}/review/decisions                (batch accept/reject)
 * - POST   /labeling/{id}/review/finalize                 (finalize + manifest)
 * - POST   /labeling-preview/runs                         (start a Preview_Run)
 * - GET    /labeling-preview/runs/{runId}                 (Preview_Run status)
 */
export class DdaLabelingApiStack extends cdk.NestedStack {
  constructor(scope: Construct, id: string, props: DdaLabelingApiStackProps) {
    super(scope, id, props);

    const api = apigateway.RestApi.fromRestApiAttributes(this, 'PortalApi', {
      restApiId: props.restApiId,
      rootResourceId: props.restApiRootResourceId,
    });

    // /labeling/{id} is owned by the ApiGatewayStack — import it by resource
    // id so /stop and /review* attach under the existing path
    // (CameraRegistryApiStack pattern for /devices/{id}).
    const labelingJobResource = apigateway.Resource.fromResourceAttributes(
      this,
      'LabelingJobResource',
      {
        restApi: api,
        resourceId: props.labelingJobResourceId,
        path: '/labeling/{id}',
      },
    );

    // Same authorizer configuration as the ApiGatewayStack's; authorizers
    // are per-Rest-API resources, so this stack attaches its own instance
    // to the imported API (UserAdminApiStack pattern).
    const authorizer = new apigateway.CognitoUserPoolsAuthorizer(this, 'DdaLabelingAuthorizer', {
      cognitoUserPools: [props.userPool],
      authorizerName: 'EdgeCVPortalDdaLabelingAuthorizer',
      identitySource: 'method.request.header.Authorization',
    });

    // The imported API does not carry the RestApi construct's
    // defaultCorsPreflightOptions, so mirror them on each resource root
    // created here (applies to all child resources).
    const corsOptions: apigateway.CorsOptions = {
      allowOrigins: apigateway.Cors.ALL_ORIGINS,
      allowMethods: apigateway.Cors.ALL_METHODS,
      allowHeaders: [
        'Content-Type',
        'X-Amz-Date',
        'Authorization',
        'X-Api-Key',
        'X-Amz-Security-Token',
      ],
    };

    // allowTestInvoke: false — one AWS::Lambda::Permission per method
    // instead of two (same resource-count economy as the other imported-API
    // nested stacks).
    const ddaLabelingIntegration = new apigateway.LambdaIntegration(
      props.ddaLabelingHandler,
      { allowTestInvoke: false },
    );
    const labelingIntegration = new apigateway.LambdaIntegration(
      props.labelingHandler,
      { allowTestInvoke: false },
    );

    const methods: apigateway.Method[] = [];
    const addMethod = (
      resource: apigateway.IResource,
      httpMethod: string,
      integration: apigateway.LambdaIntegration = ddaLabelingIntegration,
    ) => {
      methods.push(
        resource.addMethod(httpMethod, integration, {
          authorizer,
          authorizationType: apigateway.AuthorizationType.COGNITO,
        }),
      );
    };

    // ------------------------------------------------------------------
    // Team management (permission MANAGE_LABELING_TEAMS — Req 3.7)
    // ------------------------------------------------------------------
    const teamsResource = api.root.addResource('labeling-teams', {
      defaultCorsPreflightOptions: corsOptions,
    });
    // GET /labeling-teams?usecase_id= — teams with member identities (req 3.8)
    addMethod(teamsResource, 'GET');
    // POST /labeling-teams — create team (req 3.1, 3.2)
    addMethod(teamsResource, 'POST');

    const teamResource = teamsResource.addResource('{teamId}');
    // DELETE /labeling-teams/{teamId} — rejected while an InProgress job
    // references the team
    addMethod(teamResource, 'DELETE');

    const membersResource = teamResource.addResource('members');
    // POST /labeling-teams/{teamId}/members — add member (req 3.3–3.5)
    addMethod(membersResource, 'POST');
    // DELETE /labeling-teams/{teamId}/members/{userId} — remove member +
    // reassignment (req 5.3, 5.4)
    addMethod(membersResource.addResource('{userId}'), 'DELETE');

    // ------------------------------------------------------------------
    // Labeler APIs (permission LABELING_TASKS_SELF — Req 2.5)
    // ------------------------------------------------------------------
    const labelerResource = api.root.addResource('labeler', {
      defaultCorsPreflightOptions: corsOptions,
    });

    // GET /labeler/jobs — jobs where the caller holds >=1 unsubmitted task
    const labelerJobsResource = labelerResource.addResource('jobs');
    addMethod(labelerJobsResource, 'GET');

    // GET /labeler/jobs/{jobId}/next — next presentable unsubmitted task
    addMethod(labelerJobsResource.addResource('{jobId}').addResource('next'), 'GET');

    const labelerTaskResource = labelerResource
      .addResource('tasks')
      .addResource('{taskId}');
    // POST /labeler/tasks/{taskId}/submit — persist annotation (req 7.7–7.9)
    addMethod(labelerTaskResource.addResource('submit'), 'POST');
    // POST /labeler/tasks/{taskId}/presentation-failure — withhold task (req 7.12)
    addMethod(labelerTaskResource.addResource('presentation-failure'), 'POST');
    // GET /labeler/tasks/{taskId}/image-url — fresh presigned URL (req 12.7)
    addMethod(labelerTaskResource.addResource('image-url'), 'GET');

    // ------------------------------------------------------------------
    // Job lifecycle & review under the imported /labeling/{id}
    // ------------------------------------------------------------------
    // POST /labeling/{id}/stop — labeling.py owns the stop route
    // (MANAGE_LABELING_JOBS, Req 11.4)
    const stopResource = labelingJobResource.addResource('stop', {
      defaultCorsPreflightOptions: corsOptions,
    });
    addMethod(stopResource, 'POST', labelingIntegration);

    // GET /labeling/{id}/review — paginated auto-label results (req 9.5)
    const reviewResource = labelingJobResource.addResource('review', {
      defaultCorsPreflightOptions: corsOptions,
    });
    addMethod(reviewResource, 'GET');
    // POST /labeling/{id}/review/decisions — batch accept/reject (req 9.6)
    addMethod(reviewResource.addResource('decisions'), 'POST');
    // POST /labeling/{id}/review/finalize — all-decided + >=1 accepted, then
    // manifest generation (req 9.7–9.9)
    addMethod(reviewResource.addResource('finalize'), 'POST');

    // ------------------------------------------------------------------
    // Prompt Tuning Preview (llm-autolabel-prompt-tuning, task 7.2).
    // A Preview_Run is asynchronous: POST starts it and returns 202 with a
    // run id, the wizard short-polls the status route. Both routes land on
    // DdaLabelingHandler and enforce MANAGE_LABELING_JOBS via @rbac_check
    // inside the handler (Requirements 1.3, 8.1).
    // ------------------------------------------------------------------
    const previewResource = api.root.addResource('labeling-preview', {
      defaultCorsPreflightOptions: corsOptions,
    });
    const previewRunsResource = previewResource.addResource('runs');
    // POST /labeling-preview/runs — validate, claim the in-flight lock,
    // create the run and async self-invoke the executor
    addMethod(previewRunsResource, 'POST');
    // GET /labeling-preview/runs/{runId} — run status + per-sample state
    addMethod(previewRunsResource.addResource('{runId}'), 'GET');

    // ------------------------------------------------------------------
    // Deployment re-pointing the existing stage so the routes above go
    // live. The logical id is salted with the route table: any route
    // change creates a new deployment (a deployment snapshots the whole
    // API, so it always includes the ApiGatewayStack routes too).
    // ------------------------------------------------------------------
    const routeSalt = crypto
      .createHash('sha256')
      .update(
        methods
          .map((m) => `${m.httpMethod} ${m.resource.path}`)
          .sort()
          .join('\n'),
      )
      .digest('hex')
      .slice(0, 16);

    const deployment = new apigateway.CfnDeployment(this, 'DdaLabelingDeployment', {
      restApiId: props.restApiId,
      stageName: props.stageName,
      description: 'DDA data labeling routes deployment (dda-data-labeling)',
    });
    deployment.overrideLogicalId(`DdaLabelingDeployment${routeSalt}`);
    // Every resource/method (including the CORS preflight OPTIONS methods)
    // and the authorizer must exist before the deployment snapshot is
    // taken; construct dependencies cover each resource subtree created
    // here (the /labeling/{id} parent is imported, so its two subtrees are
    // added individually).
    deployment.node.addDependency(authorizer);
    deployment.node.addDependency(teamsResource);
    deployment.node.addDependency(labelerResource);
    deployment.node.addDependency(stopResource);
    deployment.node.addDependency(reviewResource);
    deployment.node.addDependency(previewResource);
  }
}
