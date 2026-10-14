import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Construct } from 'constructs';
import * as crypto from 'crypto';

export interface QuickSetupApiStackProps extends cdk.NestedStackProps {
  /** Rest API id of the existing portal API (ComputeStack/ApiGatewayStack). */
  restApiId: string;
  /** Root resource id of the existing portal API. */
  restApiRootResourceId: string;
  /** Stage the portal API serves on (ApiGatewayStack deployOptions.stageName). */
  stageName: string;
  userPool: cognito.IUserPool;
  /** device_registrations.py — Cognito JWT + manage_devices RBAC routes. */
  deviceRegistrationsHandler: lambda.Function;
  /** quick_setup.py — token-authenticated (AuthorizationType.NONE) routes. */
  quickSetupHandler: lambda.Function;
}

/**
 * Station Quick Setup API Gateway routes (station-quick-setup, task 8.3).
 *
 * The portal API's own nested stack (ApiGatewayStack) sits at the
 * CloudFormation 500-resource limit, so the Quick Setup routes live in their
 * own nested stack that imports the Rest API by id and registers
 * resources/methods against its root — the same pattern as
 * NodeDesignerApiStack, CameraRegistryApiStack, and UserAdminApiStack. A
 * fresh deployment created here re-points the existing stage so the new
 * routes go live; its logical id is salted with the route table so route
 * changes roll a new deployment.
 *
 * Two authentication models (design "Route authentication model"):
 *
 * JWT-authenticated (Cognito user pool authorizer; manage_devices RBAC is
 * enforced inside device_registrations.py):
 * - POST   /device-registrations                    (create + Setup_Command)
 * - GET    /device-registrations                     (listing, Req 6.3)
 * - GET    /device-registrations/thing-groups        (Thing Group listing, Req 1.7)
 * - POST   /device-registrations/{id}/command        (regenerate, Req 2.5)
 * - DELETE /device-registrations/{id}                (delete, Req 6.6/6.9)
 *
 * Token-authenticated (AuthorizationType.NONE — the same mechanism as the
 * existing POST /auth/refresh route; safety comes from in-handler Setup_Token
 * validation, an app-level invalid-token IP limiter, and the method-level
 * throttling applied below):
 * - GET    /quick-setup/bootstrap                    (public bootstrap script)
 * - POST   /quick-setup/bundle                       (Setup_Bundle manifest)
 * - POST   /quick-setup/credentials                  (Provisioning_Credentials)
 * - POST   /quick-setup/status                       (status report)
 *
 * The `/quick-setup/*` methods additionally carry method-level throttling
 * (~10 rps / burst 20) as defense-in-depth against volumetric abuse below
 * the application layer (Req 3.9).
 */
export class QuickSetupApiStack extends cdk.NestedStack {
  constructor(scope: Construct, id: string, props: QuickSetupApiStackProps) {
    super(scope, id, props);

    const api = apigateway.RestApi.fromRestApiAttributes(this, 'PortalApi', {
      restApiId: props.restApiId,
      rootResourceId: props.restApiRootResourceId,
    });

    // Same authorizer configuration as the ApiGatewayStack's; authorizers are
    // per-Rest-API resources, so this stack attaches its own instance to the
    // imported API (NodeDesignerApiStack / UserAdminApiStack pattern).
    const authorizer = new apigateway.CognitoUserPoolsAuthorizer(this, 'QuickSetupAuthorizer', {
      cognitoUserPools: [props.userPool],
      authorizerName: 'EdgeCVPortalQuickSetupAuthorizer',
      identitySource: 'method.request.header.Authorization',
    });

    // The imported API does not carry the RestApi construct's
    // defaultCorsPreflightOptions, so mirror them on the resource roots
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

    // allowTestInvoke: false — one AWS::Lambda::Permission per method instead
    // of two (same resource-count economy as the other nested API stacks).
    const registrationsIntegration = new apigateway.LambdaIntegration(
      props.deviceRegistrationsHandler,
      { allowTestInvoke: false },
    );
    const quickSetupIntegration = new apigateway.LambdaIntegration(
      props.quickSetupHandler,
      { allowTestInvoke: false },
    );

    const methods: apigateway.Method[] = [];

    // JWT-authenticated method (Cognito user pool authorizer).
    const addJwtMethod = (
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

    // Token-authenticated method (no Cognito authorizer; validated in-handler).
    const addPublicMethod = (
      resource: apigateway.IResource,
      httpMethod: string,
      integration: apigateway.LambdaIntegration,
    ) => {
      methods.push(
        resource.addMethod(httpMethod, integration, {
          authorizationType: apigateway.AuthorizationType.NONE,
        }),
      );
    };

    // ------------------------------------------------------------------
    // JWT routes — /device-registrations (device_registrations.py).
    // ------------------------------------------------------------------
    const registrationsResource = api.root.addResource('device-registrations', {
      defaultCorsPreflightOptions: corsOptions,
    });
    addJwtMethod(registrationsResource, 'POST', registrationsIntegration);
    addJwtMethod(registrationsResource, 'GET', registrationsIntegration);

    // Static sibling of {id}; API Gateway prefers static segments for exact
    // matches (same pattern as the other nested stacks).
    // GET /device-registrations/thing-groups — existing IoT Thing Groups (Req 1.7).
    addJwtMethod(
      registrationsResource.addResource('thing-groups'),
      'GET',
      registrationsIntegration,
    );

    // /device-registrations/{id}
    const registrationResource = registrationsResource.addResource('{id}');
    // DELETE /device-registrations/{id} — status-gated deletion (Req 6.6/6.9).
    addJwtMethod(registrationResource, 'DELETE', registrationsIntegration);
    // POST /device-registrations/{id}/command — Setup_Command regeneration (Req 2.5).
    addJwtMethod(
      registrationResource.addResource('command'),
      'POST',
      registrationsIntegration,
    );

    // ------------------------------------------------------------------
    // Token routes — /quick-setup/* (quick_setup.py, AuthorizationType.NONE).
    // ------------------------------------------------------------------
    const quickSetupResource = api.root.addResource('quick-setup', {
      defaultCorsPreflightOptions: corsOptions,
    });
    // GET /quick-setup/bootstrap — public bootstrap script (integrity anchored
    // by the checksum embedded in the Setup_Command).
    addPublicMethod(
      quickSetupResource.addResource('bootstrap'),
      'GET',
      quickSetupIntegration,
    );
    // POST /quick-setup/bundle — Setup_Bundle manifest for a valid token.
    addPublicMethod(
      quickSetupResource.addResource('bundle'),
      'POST',
      quickSetupIntegration,
    );
    // POST /quick-setup/credentials — scoped Provisioning_Credentials exchange.
    addPublicMethod(
      quickSetupResource.addResource('credentials'),
      'POST',
      quickSetupIntegration,
    );
    // POST /quick-setup/status — station-reported provisioning outcome.
    addPublicMethod(
      quickSetupResource.addResource('status'),
      'POST',
      quickSetupIntegration,
    );

    // ------------------------------------------------------------------
    // Deployment re-pointing the existing stage so the routes above go live.
    // The logical id is salted with the route table: any route change creates
    // a new deployment (a deployment snapshots the whole API, so it always
    // includes the ApiGatewayStack routes too).
    //
    // Method-level throttling for the token-authenticated /quick-setup/*
    // methods (~10 rps / burst 20) is attached via the deployment's stage
    // method settings — API Gateway defense-in-depth below the application
    // layer (Req 3.9). The JWT /device-registrations routes inherit the
    // stage's account/default throttling.
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

    // NOTE: The ~10 rps / burst 20 method-level throttling for the
    // /quick-setup/* routes (Req 3.9 defense-in-depth) is configured on the
    // stage OWNER (ApiGatewayStack deployOptions.methodOptions), NOT here: a
    // CfnDeployment that re-points an already-existing stage cannot carry a
    // StageDescription ("StageDescription cannot be specified when stage
    // referenced by StageName already exists"). This deployment therefore only
    // snapshots the new routes and re-points the stage, matching the
    // CameraRegistryApi / UserAdminApi nested-stack pattern.
    const deployment = new apigateway.CfnDeployment(this, 'QuickSetupDeployment', {
      restApiId: props.restApiId,
      stageName: props.stageName,
      description: 'Station Quick Setup routes deployment (station-quick-setup)',
    });
    deployment.overrideLogicalId(`QuickSetupDeployment${routeSalt}`);
    // Every resource/method (including the CORS preflight OPTIONS methods)
    // and the authorizer must exist before the deployment snapshot is taken;
    // a construct dependency covers the whole subtree.
    deployment.node.addDependency(authorizer);
    deployment.node.addDependency(registrationsResource);
    deployment.node.addDependency(quickSetupResource);
  }
}
