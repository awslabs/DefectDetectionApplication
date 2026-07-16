import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Construct } from 'constructs';
import * as crypto from 'crypto';

export interface CameraRegistryApiStackProps extends cdk.NestedStackProps {
  /** Rest API id of the existing portal API (ComputeStack/ApiGatewayStack). */
  restApiId: string;
  /** Root resource id of the existing portal API. */
  restApiRootResourceId: string;
  /** Resource id of /devices/{id} in the ApiGatewayStack. */
  deviceResourceId: string;
  /** Stage the portal API serves on (ApiGatewayStack deployOptions.stageName). */
  stageName: string;
  userPool: cognito.IUserPool;
  cameraRegistryHandler: lambda.Function;
}

/**
 * Camera_Registry API Gateway routes (camera-registry-sync, task 4.1).
 *
 * The portal API's own nested stack (ApiGatewayStack) sits at the
 * CloudFormation 500-resource limit, so the Camera_Registry routes live in
 * their own nested stack that imports the Rest API and the /devices/{id}
 * resource by id and registers resources/methods against them — the same
 * pattern as NodeDesignerApiStack. A fresh deployment created here re-points
 * the existing stage so the new routes go live; its logical id is salted
 * with the route table so route changes roll a new deployment.
 *
 * Route table (design "Camera_Registry API", camera_registry.py):
 * - GET    /devices/{id}/cameras                          (Viewer)
 * - POST   /devices/{id}/cameras                          (Operator)
 * - PUT    /devices/{id}/cameras/{csid}                   (Operator)
 * - DELETE /devices/{id}/cameras/{csid}                   (Operator)
 * - GET    /devices/{id}/cameras/conflicts                (Viewer)
 * - POST   /devices/{id}/cameras/conflicts/{cid}/reapply  (Operator)
 * - POST   /devices/{id}/cameras/refresh                  (Viewer)
 */
export class CameraRegistryApiStack extends cdk.NestedStack {
  constructor(scope: Construct, id: string, props: CameraRegistryApiStackProps) {
    super(scope, id, props);

    const api = apigateway.RestApi.fromRestApiAttributes(this, 'PortalApi', {
      restApiId: props.restApiId,
      rootResourceId: props.restApiRootResourceId,
    });

    // Attach under the existing /devices/{id} resource of the portal API.
    const deviceResource = apigateway.Resource.fromResourceAttributes(this, 'DeviceResource', {
      restApi: api,
      resourceId: props.deviceResourceId,
      path: '/devices/{id}',
    });

    // Same authorizer configuration as the ApiGatewayStack's; authorizers are
    // per-Rest-API resources, so this stack attaches its own instance to the
    // imported API.
    const authorizer = new apigateway.CognitoUserPoolsAuthorizer(this, 'CameraRegistryAuthorizer', {
      cognitoUserPools: [props.userPool],
      authorizerName: 'EdgeCVPortalCameraRegistryAuthorizer',
      identitySource: 'method.request.header.Authorization',
    });

    // The imported resource does not carry the RestApi construct's
    // defaultCorsPreflightOptions, so mirror them on the cameras resource
    // root created here (applies to all child resources).
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

    // allowTestInvoke: false — one AWS::Lambda::Permission per method instead
    // of two (same resource-count economy as the Workflow Manager and
    // Node_Designer routes).
    const cameraRegistryIntegration = new apigateway.LambdaIntegration(
      props.cameraRegistryHandler,
      { allowTestInvoke: false },
    );

    const methods: apigateway.Method[] = [];
    const addMethod = (resource: apigateway.IResource, httpMethod: string) => {
      methods.push(
        resource.addMethod(httpMethod, cameraRegistryIntegration, {
          authorizer,
          authorizationType: apigateway.AuthorizationType.COGNITO,
        }),
      );
    };

    // /devices/{id}/cameras — registry entries + META (GET, Viewer) and
    // portal-created Camera_Source creation (POST, Operator).
    const camerasResource = deviceResource.addResource('cameras', {
      defaultCorsPreflightOptions: corsOptions,
    });
    addMethod(camerasResource, 'GET');
    addMethod(camerasResource, 'POST');

    // Static 'conflicts' and 'refresh' resources are siblings of the {csid}
    // path param; API Gateway prefers static segments for exact matches
    // (same pattern as /devices/{id}/logs/analyze).

    // GET /devices/{id}/cameras/conflicts — conflict events, newest first (Viewer)
    const conflictsResource = camerasResource.addResource('conflicts');
    addMethod(conflictsResource, 'GET');

    // POST /devices/{id}/cameras/conflicts/{cid}/reapply — re-issue the
    // overridden portal version as a new pending change (Operator)
    addMethod(
      conflictsResource.addResource('{cid}').addResource('reapply'),
      'POST',
    );

    // POST /devices/{id}/cameras/refresh — on-demand GetThingShadow pull (Viewer)
    addMethod(camerasResource.addResource('refresh'), 'POST');

    // PUT/DELETE /devices/{id}/cameras/{csid} — update / pending-delete (Operator)
    const cameraResource = camerasResource.addResource('{csid}');
    addMethod(cameraResource, 'PUT');
    addMethod(cameraResource, 'DELETE');

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

    const deployment = new apigateway.CfnDeployment(this, 'CameraRegistryDeployment', {
      restApiId: props.restApiId,
      stageName: props.stageName,
      description: 'Camera_Registry routes deployment (camera-registry-sync)',
    });
    deployment.overrideLogicalId(`CameraRegistryDeployment${routeSalt}`);
    // Every resource/method (including the CORS preflight OPTIONS methods)
    // and the authorizer must exist before the deployment snapshot is taken;
    // a construct dependency covers the whole subtree.
    deployment.node.addDependency(authorizer);
    deployment.node.addDependency(camerasResource);
  }
}
