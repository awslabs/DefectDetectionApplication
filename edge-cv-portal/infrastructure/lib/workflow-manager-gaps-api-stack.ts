import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Construct } from 'constructs';
import * as crypto from 'crypto';

export interface WorkflowManagerGapsApiStackProps extends cdk.NestedStackProps {
  /** Rest API id of the existing portal API (ComputeStack/ApiGatewayStack). */
  restApiId: string;
  /** Root resource id of the existing portal API. */
  restApiRootResourceId: string;
  /** Resource id of /workflows/generate in the ApiGatewayStack. */
  workflowGenerateResourceId: string;
  /** Resource id of /workflows/{id} in the ApiGatewayStack. */
  workflowResourceId: string;
  /** Stage the portal API serves on (ApiGatewayStack deployOptions.stageName). */
  stageName: string;
  userPool: cognito.IUserPool;
  /** workflow_generator.py handler — serves GET /workflows/generate/{job_id}. */
  workflowGeneratorHandler: lambda.Function;
  /** workflows.py handler — serves PATCH /workflows/{id}/name. */
  workflowsHandler: lambda.Function;
}

/**
 * Workflow Manager gaps API Gateway routes (workflow-manager-gaps, task 10.3).
 *
 * The portal API's own nested stack (ApiGatewayStack) sits at the
 * CloudFormation 500-resource limit, so the generation-status and rename
 * routes live in their own nested stack that imports the Rest API and the
 * /workflows/generate and /workflows/{id} resources by id and registers
 * resources/methods against them — the same pattern as
 * CameraRegistryApiStack and DdaLabelingApiStack. A fresh deployment created
 * here re-points the existing stage so the new routes go live; its logical
 * id is salted with the route table so route changes roll a new deployment.
 *
 * Route table (design "Infrastructure", workflow_generator.py / workflows.py):
 * - GET   /workflows/generate/{job_id}  (generation job status; Req 2.1)
 * - PATCH /workflows/{id}/name          (metadata-only rename;  Req 5.1)
 */
export class WorkflowManagerGapsApiStack extends cdk.NestedStack {
  constructor(scope: Construct, id: string, props: WorkflowManagerGapsApiStackProps) {
    super(scope, id, props);

    const api = apigateway.RestApi.fromRestApiAttributes(this, 'PortalApi', {
      restApiId: props.restApiId,
      rootResourceId: props.restApiRootResourceId,
    });

    // Both parents are owned by the ApiGatewayStack — import them by
    // resource id so the new sub-resources attach under the existing paths
    // (the CameraRegistryApiStack /devices/{id} pattern).
    const workflowGenerateResource = apigateway.Resource.fromResourceAttributes(
      this,
      'WorkflowGenerateResource',
      {
        restApi: api,
        resourceId: props.workflowGenerateResourceId,
        path: '/workflows/generate',
      },
    );
    const workflowResource = apigateway.Resource.fromResourceAttributes(
      this,
      'WorkflowResource',
      {
        restApi: api,
        resourceId: props.workflowResourceId,
        path: '/workflows/{id}',
      },
    );

    // Same authorizer configuration as the ApiGatewayStack's; authorizers
    // are per-Rest-API resources, so this stack attaches its own instance
    // to the imported API (CameraRegistryApiStack pattern).
    const authorizer = new apigateway.CognitoUserPoolsAuthorizer(
      this,
      'WorkflowManagerGapsAuthorizer',
      {
        cognitoUserPools: [props.userPool],
        authorizerName: 'EdgeCVPortalWorkflowManagerGapsAuthorizer',
        identitySource: 'method.request.header.Authorization',
      },
    );

    // The imported resources do not carry the RestApi construct's
    // defaultCorsPreflightOptions, so mirror them on the resources created
    // here (adds the OPTIONS preflight methods).
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
    const workflowGeneratorIntegration = new apigateway.LambdaIntegration(
      props.workflowGeneratorHandler,
      { allowTestInvoke: false },
    );
    const workflowsIntegration = new apigateway.LambdaIntegration(
      props.workflowsHandler,
      { allowTestInvoke: false },
    );

    const methods: apigateway.Method[] = [];
    const addMethod = (
      resource: apigateway.IResource,
      httpMethod: string,
      integration: apigateway.LambdaIntegration,
    ) => {
      methods.push(
        resource.addMethod(httpMethod, integration, {
          authorizer,
          authorizationType: apigateway.AuthorizationType.COGNITO,
        }),
      );
    };

    // GET /workflows/generate/{job_id} — Generation_Job status poll (Req 2.1)
    const jobResource = workflowGenerateResource.addResource('{job_id}', {
      defaultCorsPreflightOptions: corsOptions,
    });
    addMethod(jobResource, 'GET', workflowGeneratorIntegration);

    // PATCH /workflows/{id}/name — metadata-only Display_Name rename (Req 5.1)
    const nameResource = workflowResource.addResource('name', {
      defaultCorsPreflightOptions: corsOptions,
    });
    addMethod(nameResource, 'PATCH', workflowsIntegration);

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

    const deployment = new apigateway.CfnDeployment(this, 'WorkflowManagerGapsDeployment', {
      restApiId: props.restApiId,
      stageName: props.stageName,
      description: 'Workflow Manager gaps routes deployment (workflow-manager-gaps)',
    });
    deployment.overrideLogicalId(`WorkflowManagerGapsDeployment${routeSalt}`);
    // Every resource/method (including the CORS preflight OPTIONS methods)
    // and the authorizer must exist before the deployment snapshot is taken;
    // construct dependencies cover both created subtrees.
    deployment.node.addDependency(authorizer);
    deployment.node.addDependency(jobResource);
    deployment.node.addDependency(nameResource);
  }
}
