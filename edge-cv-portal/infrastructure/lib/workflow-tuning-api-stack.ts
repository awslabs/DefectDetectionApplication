import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Construct } from 'constructs';
import * as crypto from 'crypto';

export interface WorkflowTuningApiStackProps extends cdk.NestedStackProps {
  /** Rest API id of the existing portal API (ComputeStack/ApiGatewayStack). */
  restApiId: string;
  /** Root resource id of the existing portal API. */
  restApiRootResourceId: string;
  /** Stage the portal API serves on (ApiGatewayStack deployOptions.stageName). */
  stageName: string;
  userPool: cognito.IUserPool;
  /** workflow_tuning.py handler — serves every /workflow-tuning/anomaly/** route. */
  workflowTuningHandler: lambda.Function;
}

/**
 * VLM/LLM Anomaly Tuning API Gateway routes (quality-prompt-tuning, task 5.2).
 *
 * The portal API's own nested stack (ApiGatewayStack) sits at the
 * CloudFormation 500-resource limit, so these routes live in their own
 * nested stack that imports the Rest API by id and attaches
 * `/workflow-tuning` at its root — the same pattern as
 * CameraRegistryApiStack / DdaLabelingApiStack / WorkflowManagerGapsApiStack.
 * A fresh deployment created here re-points the existing stage so the new
 * routes go live; its logical id is salted with the route table so route
 * changes roll a new deployment.
 *
 * Every method is behind the Cognito authorizer; the per-operation
 * permission (`workflow:read` for GETs, `workflow:edit` for mutations,
 * `workflow:save` for apply — Requirements 9.1, 9.2) is enforced in
 * workflow_tuning.py via authorize_workflow_access.
 *
 * Route table (design "Portal backend — workflow_tuning.py Lambda"):
 * - GET    /workflow-tuning/anomaly/workflows                              (overview; Req 1.2, 1.6)
 * - POST   /workflow-tuning/anomaly/sessions                               (create-or-get; Req 3.1, 5.1)
 * - GET    /workflow-tuning/anomaly/sessions/{id}                          (session view)
 * - DELETE /workflow-tuning/anomaly/sessions/{id}                          (Req 10.2, 10.5)
 * - POST   /workflow-tuning/anomaly/sessions/{id}/refresh                  (Req 3.4)
 * - GET    /workflow-tuning/anomaly/sessions/{id}/samples                  (Req 4.4, 4.8)
 * - PUT    /workflow-tuning/anomaly/sessions/{id}/samples/labels           (Req 4.2)
 * - PUT    /workflow-tuning/anomaly/sessions/{id}/synthetic-negatives      (Req 4.5, 4.6)
 * - POST   /workflow-tuning/anomaly/sessions/{id}/candidates               (Req 5.2)
 * - PUT    /workflow-tuning/anomaly/sessions/{id}/candidates/{cid}         (Req 5.2, 5.7)
 * - DELETE /workflow-tuning/anomaly/sessions/{id}/candidates/{cid}         (Req 5.7)
 * - POST   /workflow-tuning/anomaly/sessions/{id}/score-runs               (Req 6.6, 6.10, 6.13)
 * - PUT    /workflow-tuning/anomaly/sessions/{id}/selection                (Req 7.5)
 * - POST   /workflow-tuning/anomaly/sessions/{id}/apply                    (Req 8)
 * - GET    /workflow-tuning/anomaly/candidates/{cid}/preview               (Req 5.3-5.5)
 * - GET    /workflow-tuning/anomaly/score-runs/{rid}                       (Req 6.8, 6.11)
 * - GET    /workflow-tuning/anomaly/score-runs/{rid}/outcomes              (Req 7.2)
 * - POST   /workflow-tuning/anomaly/score-runs/{rid}/cancel                (Req 6.11)
 * - GET    /workflow-tuning/anomaly/score-runs/{rid}/diff/{other}          (Req 7.3)
 *
 * By-id reads sit at the `anomaly` root (`.../score-runs/{rid}`,
 * `.../candidates/{cid}/preview`) exactly as the design's route table
 * spells them, while creation stays session-scoped
 * (`.../sessions/{id}/score-runs`, `.../sessions/{id}/candidates`).
 */
export class WorkflowTuningApiStack extends cdk.NestedStack {
  constructor(scope: Construct, id: string, props: WorkflowTuningApiStackProps) {
    super(scope, id, props);

    const api = apigateway.RestApi.fromRestApiAttributes(this, 'PortalApi', {
      restApiId: props.restApiId,
      rootResourceId: props.restApiRootResourceId,
    });

    // Same authorizer configuration as the ApiGatewayStack's; authorizers
    // are per-Rest-API resources, so this stack attaches its own instance
    // to the imported API (CameraRegistryApiStack pattern).
    const authorizer = new apigateway.CognitoUserPoolsAuthorizer(
      this,
      'WorkflowTuningAuthorizer',
      {
        cognitoUserPools: [props.userPool],
        authorizerName: 'EdgeCVPortalWorkflowTuningAuthorizer',
        identitySource: 'method.request.header.Authorization',
      },
    );

    // The imported root resource does not carry the RestApi construct's
    // defaultCorsPreflightOptions, so mirror them on the /workflow-tuning
    // root created here (it applies to all child resources).
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
    // instead of two (same resource-count economy as the sibling stacks).
    const integration = new apigateway.LambdaIntegration(
      props.workflowTuningHandler,
      { allowTestInvoke: false },
    );

    const methods: apigateway.Method[] = [];
    const addMethods = (resource: apigateway.IResource, httpMethods: string[]) => {
      for (const httpMethod of httpMethods) {
        methods.push(
          resource.addMethod(httpMethod, integration, {
            authorizer,
            authorizationType: apigateway.AuthorizationType.COGNITO,
          }),
        );
      }
    };

    // /workflow-tuning/anomaly — the section root. The CORS preflight
    // options on the section root cover every child resource created below.
    const sectionRoot = api.root.addResource('workflow-tuning', {
      defaultCorsPreflightOptions: corsOptions,
    });
    const anomalyRoot = sectionRoot.addResource('anomaly');

    // GET /workflow-tuning/anomaly/workflows — workflows with Tunable_Nodes,
    // per-node sample counts and sampleExportEnabled (Req 1.2, 1.6).
    addMethods(anomalyRoot.addResource('workflows'), ['GET']);

    // /workflow-tuning/anomaly/sessions — create-or-get a Tuning_Session.
    const sessionsResource = anomalyRoot.addResource('sessions');
    addMethods(sessionsResource, ['POST']);

    // /workflow-tuning/anomaly/sessions/{id} — the session view and delete.
    const sessionResource = sessionsResource.addResource('{id}');
    addMethods(sessionResource, ['GET', 'DELETE']);

    // POST .../sessions/{id}/refresh — additive re-index (Req 3.4).
    addMethods(sessionResource.addResource('refresh'), ['POST']);

    // GET .../sessions/{id}/samples — paged samples with presigned URLs.
    const samplesResource = sessionResource.addResource('samples');
    addMethods(samplesResource, ['GET']);

    // PUT .../sessions/{id}/samples/labels — multi-set Labels (Req 4.2).
    addMethods(samplesResource.addResource('labels'), ['PUT']);

    // PUT .../sessions/{id}/synthetic-negatives — toggle (Req 4.5, 4.6).
    addMethods(sessionResource.addResource('synthetic-negatives'), ['PUT']);

    // .../sessions/{id}/candidates[/{cid}] — Candidate CRUD (Req 5.2, 5.7).
    const sessionCandidatesResource = sessionResource.addResource('candidates');
    addMethods(sessionCandidatesResource, ['POST']);
    addMethods(sessionCandidatesResource.addResource('{cid}'), ['PUT', 'DELETE']);

    // POST .../sessions/{id}/score-runs — start a Score_Run (Req 6.6).
    addMethods(sessionResource.addResource('score-runs'), ['POST']);

    // PUT .../sessions/{id}/selection — the selected Candidate (Req 7.5).
    addMethods(sessionResource.addResource('selection'), ['PUT']);

    // POST .../sessions/{id}/apply — save the new version (Req 8).
    addMethods(sessionResource.addResource('apply'), ['POST']);

    // GET .../candidates/{cid}/preview — exact request text + warnings
    // (Req 5.3-5.5).
    addMethods(
      anomalyRoot.addResource('candidates').addResource('{cid}')
        .addResource('preview'),
      ['GET'],
    );

    // .../score-runs/{rid} — progress and summary (Req 6.8, 6.11).
    const scoreRunsResource = anomalyRoot.addResource('score-runs');
    const scoreRunResource = scoreRunsResource.addResource('{rid}');
    addMethods(scoreRunResource, ['GET']);

    // GET .../score-runs/{rid}/outcomes — Sample_Outcomes (Req 7.2).
    addMethods(scoreRunResource.addResource('outcomes'), ['GET']);

    // POST .../score-runs/{rid}/cancel — cancellation (Req 6.11).
    addMethods(scoreRunResource.addResource('cancel'), ['POST']);

    // GET .../score-runs/{rid}/diff/{other} — differing samples (Req 7.3).
    addMethods(
      scoreRunResource.addResource('diff').addResource('{other}'),
      ['GET'],
    );

    // ------------------------------------------------------------------
    // Deployment re-pointing the existing stage so the routes above go
    // live. The logical id is salted with the route table: any route change
    // creates a new deployment (a deployment snapshots the whole API, so it
    // always includes the ApiGatewayStack routes too).
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

    const deployment = new apigateway.CfnDeployment(this, 'WorkflowTuningDeployment', {
      restApiId: props.restApiId,
      stageName: props.stageName,
      description: 'VLM/LLM Anomaly Tuning routes deployment (quality-prompt-tuning)',
    });
    deployment.overrideLogicalId(`WorkflowTuningDeployment${routeSalt}`);
    // Every resource/method (including the CORS preflight OPTIONS methods)
    // and the authorizer must exist before the deployment snapshot is taken;
    // a construct dependency covers the whole created subtree.
    deployment.node.addDependency(authorizer);
    deployment.node.addDependency(sectionRoot);
  }
}
