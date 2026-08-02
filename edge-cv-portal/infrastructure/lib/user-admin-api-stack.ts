import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Construct } from 'constructs';
import * as crypto from 'crypto';

export interface UserAdminApiStackProps extends cdk.NestedStackProps {
  /** Rest API id of the existing portal API (ComputeStack/ApiGatewayStack). */
  restApiId: string;
  /** Root resource id of the existing portal API. */
  restApiRootResourceId: string;
  /** Stage the portal API serves on (ApiGatewayStack deployOptions.stageName). */
  stageName: string;
  userPool: cognito.IUserPool;
  userAdminHandler: lambda.Function;
}

/**
 * User_Manager admin API Gateway routes (portal-user-manager, task 5.1).
 *
 * The portal API's own nested stack (ApiGatewayStack) sits at the
 * CloudFormation 500-resource limit, so the /admin/* routes live in their
 * own nested stack that imports the Rest API by id and registers
 * resources/methods against its root — the same pattern as
 * CameraRegistryApiStack and NodeDesignerApiStack. A fresh deployment
 * created here re-points the existing stage so the new routes go live; its
 * logical id is salted with the route table so route changes roll a new
 * deployment.
 *
 * Every method is guarded by the existing JWT authorizer configuration
 * (Cognito user pool authorizer on the Authorization header): requests
 * without a valid User_Pool JWT are rejected before user_admin.py runs
 * (Requirement 1.7). The PortalAdmin role gate (Requirement 1.5) is
 * enforced inside the Lambda.
 *
 * Route table (design "Portal backend — user_admin.py Lambda"):
 * - GET  /admin/users                                (account listing)
 * - POST /admin/users/{username}/password            (password change)
 * - POST /admin/users/{username}/forgot-password     (temporary password)
 * - PUT  /admin/users/{username}/role                (role change)
 * - GET  /admin/edge-sync/devices                    (per-device sync state)
 * - POST /admin/edge-sync/devices/{deviceId}         (stage + sync attempt)
 */
export class UserAdminApiStack extends cdk.NestedStack {
  constructor(scope: Construct, id: string, props: UserAdminApiStackProps) {
    super(scope, id, props);

    const api = apigateway.RestApi.fromRestApiAttributes(this, 'PortalApi', {
      restApiId: props.restApiId,
      rootResourceId: props.restApiRootResourceId,
    });

    // Same authorizer configuration as the ApiGatewayStack's; authorizers
    // are per-Rest-API resources, so this stack attaches its own instance
    // to the imported API (CameraRegistryApiStack pattern).
    const authorizer = new apigateway.CognitoUserPoolsAuthorizer(this, 'UserAdminAuthorizer', {
      cognitoUserPools: [props.userPool],
      authorizerName: 'EdgeCVPortalUserAdminAuthorizer',
      identitySource: 'method.request.header.Authorization',
    });

    // The imported API does not carry the RestApi construct's
    // defaultCorsPreflightOptions, so mirror them on the /admin resource
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

    // allowTestInvoke: false — one AWS::Lambda::Permission per method
    // instead of two (same resource-count economy as the Camera_Registry
    // and Node_Designer routes).
    const userAdminIntegration = new apigateway.LambdaIntegration(
      props.userAdminHandler,
      { allowTestInvoke: false },
    );

    const methods: apigateway.Method[] = [];
    const addMethod = (resource: apigateway.IResource, httpMethod: string) => {
      methods.push(
        resource.addMethod(httpMethod, userAdminIntegration, {
          authorizer,
          authorizationType: apigateway.AuthorizationType.COGNITO,
        }),
      );
    };

    // /admin — root of the User_Manager admin routes.
    const adminResource = api.root.addResource('admin', {
      defaultCorsPreflightOptions: corsOptions,
    });

    // GET /admin/users — full Cognito account listing joined with the
    // edge-credentials table (Requirement 2.1).
    const usersResource = adminResource.addResource('users');
    addMethod(usersResource, 'GET');

    // Per-account actions under /admin/users/{username}.
    const userResource = usersResource.addResource('{username}');

    // POST /admin/users/{username}/password — password change (Req 3.1)
    addMethod(userResource.addResource('password'), 'POST');

    // POST /admin/users/{username}/forgot-password — temporary password
    // by email (Requirement 4.1)
    addMethod(userResource.addResource('forgot-password'), 'POST');

    // PUT /admin/users/{username}/role — role change with the
    // last-PortalAdmin guard (Requirement 5.1)
    addMethod(userResource.addResource('role'), 'PUT');

    // /admin/edge-sync/devices — Account_Sync_Service device panel.
    const edgeSyncResource = adminResource.addResource('edge-sync');
    const syncDevicesResource = edgeSyncResource.addResource('devices');

    // GET /admin/edge-sync/devices — per-device last sync status (Req 7.4)
    addMethod(syncDevicesResource, 'GET');

    // POST /admin/edge-sync/devices/{deviceId} — stage the selected
    // account set and trigger an immediate sync attempt (Requirement 7.1)
    addMethod(syncDevicesResource.addResource('{deviceId}'), 'POST');

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

    const deployment = new apigateway.CfnDeployment(this, 'UserAdminDeployment', {
      restApiId: props.restApiId,
      stageName: props.stageName,
      description: 'User_Manager admin routes deployment (portal-user-manager)',
    });
    deployment.overrideLogicalId(`UserAdminDeployment${routeSalt}`);
    // Every resource/method (including the CORS preflight OPTIONS methods)
    // and the authorizer must exist before the deployment snapshot is
    // taken; a construct dependency covers the whole subtree.
    deployment.node.addDependency(authorizer);
    deployment.node.addDependency(adminResource);
  }
}
