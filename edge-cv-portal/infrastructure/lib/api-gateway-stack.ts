import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import * as path from 'path';

export interface ApiGatewayStackProps extends cdk.NestedStackProps {
  userPool: cognito.UserPool;
  // All Lambda handlers
  authHandler: lambda.Function;
  userManagementHandler: lambda.Function;
  useCasesHandler: lambda.Function;
  devicesHandler: lambda.Function;
  deviceLogsHandler: lambda.Function;
  deviceLogsAnalyzerHandler: lambda.Function;
  deploymentsHandler: lambda.Function;
  dataManagementHandler: lambda.Function;
  datasetsHandler: lambda.Function;
  capturesHandler: lambda.Function;
  preLabeledDatasetsHandler: lambda.Function;
  labelingHandler: lambda.Function;
  trainingHandler: lambda.Function;
  compilationHandler: lambda.Function;
  packagingHandler: lambda.Function;
  greengrassPublishHandler: lambda.Function;
  modelsHandler: lambda.Function;
  modelImportHandler: lambda.Function;
  modelConverterHandler: lambda.Function;
  componentsHandler: lambda.Function;
  sharedComponentsHandler: lambda.Function;
  dataAccountsHandler: lambda.Function;
  auditLogsHandler: lambda.Function;
  workflowsHandler: lambda.Function;
  workflowValidationHandler: lambda.Function;
  workflowPackagingHandler: lambda.Function;
  workflowGeneratorHandler: lambda.Function;
  workflowTestingHandler: lambda.Function;
  lambdaEnvironment: { [key: string]: string };
  createLambdaRole: (name: string) => iam.Role;
  sharedLayer: lambda.LayerVersion;
}

export class ApiGatewayStack extends cdk.NestedStack {
  public readonly api: apigateway.RestApi;
  public readonly apiUrl: string;
  /**
   * Resource id of /devices/{id}. The Camera_Registry routes
   * (camera-registry-sync) attach under this resource from their own nested
   * stack (CameraRegistryApiStack) because this stack sits at the
   * CloudFormation 500-resource limit.
   */
  public readonly deviceResourceId: string;
  /**
   * Resource id of /labeling/{id}. The DDA labeling /stop and /review*
   * routes (dda-data-labeling) attach under this resource from their own
   * nested stack (DdaLabelingApiStack) for the same 500-resource-limit
   * reason.
   */
  public readonly labelingJobResourceId: string;

  /**
   * Resource id of /workflows/generate. The workflow-manager-gaps
   * generation status route (GET /workflows/generate/{job_id}) attaches
   * under this resource from its own nested stack
   * (WorkflowManagerGapsApiStack) for the same 500-resource-limit reason.
   */
  public readonly workflowGenerateResourceId: string;

  /**
   * Resource id of /workflows/{id}. The workflow-manager-gaps rename route
   * (PATCH /workflows/{id}/name) attaches under this resource from its own
   * nested stack (WorkflowManagerGapsApiStack) for the same
   * 500-resource-limit reason.
   */
  public readonly workflowResourceId: string;

  constructor(scope: Construct, id: string, props: ApiGatewayStackProps) {
    super(scope, id, props);

    // API Gateway
    this.api = new apigateway.RestApi(this, 'EdgeCVPortalAPI', {
      restApiName: 'Edge CV Portal API',
      description: 'API for Edge CV Admin Portal',
      deployOptions: {
        stageName: 'v1',
        tracingEnabled: true,
        // Logging disabled - requires CloudWatch Logs role to be set up in account
        // To enable: Set up CloudWatch Logs role for API Gateway first
        // loggingLevel: apigateway.MethodLoggingLevel.INFO,
        // dataTraceEnabled: true,
        metricsEnabled: true,
        // Per-method throttling for the token-authenticated Station Quick Setup
        // routes (station-quick-setup Req 3.9, ~10 rps / burst 20). These
        // methods live in the QuickSetupApi nested stack, but their stage-level
        // throttle must be set here on the stage OWNER: a CfnDeployment that
        // re-points an already-existing stage cannot carry a StageDescription
        // ("StageDescription cannot be specified when stage referenced by
        // StageName already exists"). Keys are literal "{resourcePath}/{httpMethod}"
        // strings, so no cross-stack construct reference is needed.
        methodOptions: {
          '/quick-setup/bootstrap/GET': { throttlingRateLimit: 10, throttlingBurstLimit: 20 },
          '/quick-setup/bundle/POST': { throttlingRateLimit: 10, throttlingBurstLimit: 20 },
          '/quick-setup/credentials/POST': { throttlingRateLimit: 10, throttlingBurstLimit: 20 },
          '/quick-setup/status/POST': { throttlingRateLimit: 10, throttlingBurstLimit: 20 },
        },
      },
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: apigateway.Cors.ALL_METHODS,
        allowHeaders: [
          'Content-Type',
          'X-Amz-Date',
          'Authorization',
          'X-Api-Key',
          'X-Amz-Security-Token',
        ],
      },
    });

    // Gateway-generated error responses (401 expired/missing token, 403,
    // 5xx) do not pass through the Lambda handlers and therefore carry no
    // CORS headers by default — browsers then surface an opaque
    // "NetworkError" instead of the real status. Attach CORS headers to
    // the gateway responses so the frontend can show meaningful errors
    // (e.g. session expired) and trigger re-authentication.
    this.api.addGatewayResponse('Default4xxWithCors', {
      type: apigateway.ResponseType.DEFAULT_4XX,
      responseHeaders: {
        'Access-Control-Allow-Origin': "'*'",
        'Access-Control-Allow-Headers':
          "'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token'",
      },
    });
    this.api.addGatewayResponse('Default5xxWithCors', {
      type: apigateway.ResponseType.DEFAULT_5XX,
      responseHeaders: {
        'Access-Control-Allow-Origin': "'*'",
        'Access-Control-Allow-Headers':
          "'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token'",
      },
    });

    // Cognito Authorizer
    const authorizer = new apigateway.CognitoUserPoolsAuthorizer(this, 'CognitoAuthorizer', {
      cognitoUserPools: [props.userPool],
      authorizerName: 'EdgeCVPortalAuthorizer',
      identitySource: 'method.request.header.Authorization',
    });

    // ------------------------------------------------------------------
    // Lambda integrations.
    //
    // CDK's default LambdaIntegration adds one AWS::Lambda::Permission per
    // API method (two with console test-invoke enabled), which pushed this
    // nested stack past the CloudFormation 500-resource limit (~205
    // permissions) when the vllm-triton-inference routes were added.
    //
    // Instead, each handler is integrated through an ARN-imported reference
    // (skipPermissions: true) so CDK generates NO per-method permissions,
    // and a single API-wide permission is granted per function with a
    // wildcard SourceArn:
    //   arn:aws:execute-api:{region}:{account}:{apiId}/*/*/*
    // That ARN is a strict superset of the per-method grants (the stage
    // wildcard also covers the console test-invoke stage), so invocation
    // behavior is unchanged while the permission count drops from ~2 per
    // method to exactly 1 per Lambda function.
    // ------------------------------------------------------------------
    const lambdaIntegration = (
      name: string,
      handler: lambda.IFunction,
    ): apigateway.LambdaIntegration => {
      new lambda.CfnPermission(this, `${name}ApiInvokePermission`, {
        action: 'lambda:InvokeFunction',
        functionName: handler.functionArn,
        principal: 'apigateway.amazonaws.com',
        sourceArn: this.api.arnForExecuteApi(), // {apiId}/*/*/*
      });
      // Re-import the function by ARN so LambdaIntegration/Method.bind skips
      // its automatic per-method AWS::Lambda::Permission resources — the
      // single API-wide permission above already covers every method.
      // sameEnvironment: false disables permission creation on the import
      // (canCreatePermissions), and skipPermissions: true marks that as
      // intentional (suppresses the UnclearLambdaEnvironment warning).
      const importedHandler = lambda.Function.fromFunctionAttributes(this, `${name}IntegrationTarget`, {
        functionArn: handler.functionArn,
        sameEnvironment: false,
        skipPermissions: true,
      });
      return new apigateway.LambdaIntegration(importedHandler);
    };

    const authIntegration = lambdaIntegration('Auth', props.authHandler);
    const userManagementIntegration = lambdaIntegration('UserManagement', props.userManagementHandler);
    const useCasesIntegration = lambdaIntegration('UseCases', props.useCasesHandler);
    const devicesIntegration = lambdaIntegration('Devices', props.devicesHandler);
    const deviceLogsIntegration = lambdaIntegration('DeviceLogs', props.deviceLogsHandler);
    const deviceLogsAnalyzerIntegration = lambdaIntegration('DeviceLogsAnalyzer', props.deviceLogsAnalyzerHandler);
    const deploymentsIntegration = lambdaIntegration('Deployments', props.deploymentsHandler);
    const dataManagementIntegration = lambdaIntegration('DataManagement', props.dataManagementHandler);
    const datasetsIntegration = lambdaIntegration('Datasets', props.datasetsHandler);
    const capturesIntegration = lambdaIntegration('Captures', props.capturesHandler);
    const preLabeledDatasetsIntegration = lambdaIntegration('PreLabeledDatasets', props.preLabeledDatasetsHandler);
    const labelingIntegration = lambdaIntegration('Labeling', props.labelingHandler);
    const trainingIntegration = lambdaIntegration('Training', props.trainingHandler);
    const compilationIntegration = lambdaIntegration('Compilation', props.compilationHandler);
    const packagingIntegration = lambdaIntegration('Packaging', props.packagingHandler);
    const greengrassPublishIntegration = lambdaIntegration('GreengrassPublish', props.greengrassPublishHandler);
    const modelsIntegration = lambdaIntegration('Models', props.modelsHandler);
    const modelImportIntegration = lambdaIntegration('ModelImport', props.modelImportHandler);
    const modelConverterIntegration = lambdaIntegration('ModelConverter', props.modelConverterHandler);
    const componentsIntegration = lambdaIntegration('Components', props.componentsHandler);
    const sharedComponentsIntegration = lambdaIntegration('SharedComponents', props.sharedComponentsHandler);
    const dataAccountsIntegration = lambdaIntegration('DataAccounts', props.dataAccountsHandler);
    const auditLogsIntegration = lambdaIntegration('AuditLogs', props.auditLogsHandler);
    const workflowsIntegration = lambdaIntegration('Workflows', props.workflowsHandler);
    const workflowValidationIntegration = lambdaIntegration('WorkflowValidation', props.workflowValidationHandler);
    const workflowPackagingIntegration = lambdaIntegration('WorkflowPackaging', props.workflowPackagingHandler);
    const workflowGeneratorIntegration = lambdaIntegration('WorkflowGenerator', props.workflowGeneratorHandler);
    const workflowTestingIntegration = lambdaIntegration('WorkflowTesting', props.workflowTestingHandler);

    // Auth endpoints
    const authResource = this.api.root.addResource('auth');
    authResource.addResource('me').addMethod('GET', authIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    authResource.addResource('refresh').addMethod('POST', authIntegration, {
      authorizationType: apigateway.AuthorizationType.NONE,
    });

    // User Management endpoints
    const usersResource = this.api.root.addResource('users');
    usersResource.addMethod('GET', userManagementIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const assignRoleResource = usersResource.addResource('assign-role');
    assignRoleResource.addMethod('POST', userManagementIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const userResource = usersResource.addResource('{user_id}');
    const userPermissionsResource = userResource.addResource('permissions');
    userPermissionsResource.addMethod('GET', userManagementIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const userRolesResource = userResource.addResource('roles');
    const userRoleResource = userRolesResource.addResource('{usecase_id}');
    userRoleResource.addMethod('DELETE', userManagementIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const meResource = usersResource.addResource('me');
    const myUsecasesResource = meResource.addResource('usecases');
    myUsecasesResource.addMethod('GET', userManagementIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // UseCases endpoints
    const usecasesResource = this.api.root.addResource('usecases');
    usecasesResource.addMethod('GET', useCasesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    usecasesResource.addMethod('POST', useCasesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const usecaseResource = usecasesResource.addResource('{id}');
    const verifyRoleResource = usecasesResource.addResource('verify-role');
    verifyRoleResource.addMethod('POST', useCasesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    usecaseResource.addMethod('GET', useCasesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    usecaseResource.addMethod('PUT', useCasesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    usecaseResource.addMethod('DELETE', useCasesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // S3 buckets in the current (portal) account - used by Onboard New Use Case
    const s3BucketsResource = this.api.root.addResource('s3-buckets');
    s3BucketsResource.addMethod('GET', useCasesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    s3BucketsResource.addMethod('POST', useCasesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Data Management endpoints
    const usecaseDataResource = usecaseResource.addResource('data');
    
    const dataBucketsResource = usecaseDataResource.addResource('buckets');
    dataBucketsResource.addMethod('GET', dataManagementIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    dataBucketsResource.addMethod('POST', dataManagementIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const dataFoldersResource = usecaseDataResource.addResource('folders');
    dataFoldersResource.addMethod('GET', dataManagementIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    dataFoldersResource.addMethod('POST', dataManagementIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const dataUploadUrlResource = usecaseDataResource.addResource('upload-url');
    dataUploadUrlResource.addMethod('POST', dataManagementIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const dataBatchUploadResource = usecaseDataResource.addResource('batch-upload-urls');
    dataBatchUploadResource.addMethod('POST', dataManagementIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const dataConfigureResource = usecaseDataResource.addResource('configure');
    dataConfigureResource.addMethod('POST', dataManagementIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Datasets endpoints
    const datasetsResource = this.api.root.addResource('datasets');
    datasetsResource.addMethod('GET', datasetsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const datasetsCountResource = datasetsResource.addResource('count');
    datasetsCountResource.addMethod('POST', datasetsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const preLabeledResource = datasetsResource.addResource('pre-labeled');
    preLabeledResource.addMethod('GET', preLabeledDatasetsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    preLabeledResource.addMethod('POST', preLabeledDatasetsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const preLabeledItemResource = preLabeledResource.addResource('{id}');
    preLabeledItemResource.addMethod('GET', preLabeledDatasetsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    preLabeledItemResource.addMethod('DELETE', preLabeledDatasetsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const validateManifestResource = datasetsResource.addResource('validate-manifest');
    validateManifestResource.addMethod('POST', preLabeledDatasetsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const previewResource = datasetsResource.addResource('preview');
    previewResource.addMethod('GET', datasetsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Captures endpoint (inference-results Results_Viewer)
    // GET /captures?usecase_id&prefix&device_id&limit -> list_captures
    const capturesResource = this.api.root.addResource('captures');
    capturesResource.addMethod('GET', capturesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Workteams endpoint
    const workteamsResource = this.api.root.addResource('workteams');
    workteamsResource.addMethod('GET', labelingIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Labeling endpoints
    const labelingResource = this.api.root.addResource('labeling');
    labelingResource.addMethod('GET', labelingIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    labelingResource.addMethod('POST', labelingIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const labelingJobResource = labelingResource.addResource('{id}');
    this.labelingJobResourceId = labelingJobResource.resourceId;
    labelingJobResource.addMethod('GET', labelingIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const labelingManifestResource = labelingJobResource.addResource('manifest');
    labelingManifestResource.addMethod('GET', labelingIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const labelingWorkteamsResource = labelingResource.addResource('workteams');
    labelingWorkteamsResource.addMethod('GET', labelingIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Devices endpoints
    const devicesResource = this.api.root.addResource('devices');
    devicesResource.addMethod('GET', devicesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const deviceResource = devicesResource.addResource('{id}');
    this.deviceResourceId = deviceResource.resourceId;
    deviceResource.addMethod('GET', devicesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    // PUT /devices/{id} — portal-recorded device attributes: the
    // UseCaseAdmin-set test_device flag and the recorded
    // target_architecture (custom-node-designer task 10.5).
    deviceResource.addMethod('PUT', devicesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // SSH tunnel (AWS IoT Secure Tunneling) — enable/disable + status + open.
    const sshTunnelResource = deviceResource.addResource('ssh-tunnel');
    sshTunnelResource.addMethod('POST', devicesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    sshTunnelResource.addMethod('GET', devicesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    const sshTunnelOpenResource = sshTunnelResource.addResource('open');
    sshTunnelOpenResource.addMethod('POST', devicesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const deviceLogsResource = deviceResource.addResource('logs');
    deviceLogsResource.addMethod('GET', deviceLogsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Static 'analyze' resource — POST /devices/{id}/logs/analyze. Defined as a
    // sibling of the {component} path param; API Gateway prefers the static
    // segment for exact matches, so this won't shadow GET /logs/{component}.
    const deviceLogsAnalyzeResource = deviceLogsResource.addResource('analyze');
    deviceLogsAnalyzeResource.addMethod('POST', deviceLogsAnalyzerIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const deviceComponentLogsResource = deviceLogsResource.addResource('{component}');
    deviceComponentLogsResource.addMethod('GET', deviceLogsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Camera_Registry endpoints (camera-registry-sync) live in their own
    // nested stack (CameraRegistryApiStack) — this nested stack sits at the
    // CloudFormation 500-resource limit, the same reason the Node_Designer
    // routes moved to NodeDesignerApiStack. The device resource id is
    // exported below so the camera routes can attach under /devices/{id}.

    // Deployments endpoints
    const deploymentsResource = this.api.root.addResource('deployments');
    deploymentsResource.addMethod('GET', deploymentsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    deploymentsResource.addMethod('POST', deploymentsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const deploymentResource = deploymentsResource.addResource('{id}');
    deploymentResource.addMethod('GET', deploymentsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    deploymentResource.addMethod('DELETE', deploymentsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Training endpoints
    const trainingResource = this.api.root.addResource('training');
    trainingResource.addMethod('GET', trainingIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    trainingResource.addMethod('POST', trainingIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const trainingTransformManifestResource = trainingResource.addResource('transform-manifest');
    trainingTransformManifestResource.addMethod('POST', trainingIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const trainingJobResource = trainingResource.addResource('{id}');
    trainingJobResource.addMethod('GET', trainingIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const trainingLogsResource = trainingJobResource.addResource('logs');
    trainingLogsResource.addMethod('GET', trainingIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const trainingLogsDownloadResource = trainingLogsResource.addResource('download');
    trainingLogsDownloadResource.addMethod('GET', trainingIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const compileResource = trainingJobResource.addResource('compile');
    compileResource.addMethod('POST', compilationIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    compileResource.addMethod('GET', compilationIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const packageResource = trainingJobResource.addResource('package');
    packageResource.addMethod('POST', packagingIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const publishResource = trainingJobResource.addResource('publish');
    publishResource.addMethod('POST', greengrassPublishIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Models endpoints
    const modelsResource = this.api.root.addResource('models');
    modelsResource.addMethod('GET', modelsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const modelFormatSpecResource = modelsResource.addResource('format-spec');
    modelFormatSpecResource.addMethod('GET', modelImportIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const modelValidateResource = modelsResource.addResource('validate');
    modelValidateResource.addMethod('POST', modelImportIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const modelImportResource = modelsResource.addResource('import');
    modelImportResource.addMethod('POST', modelImportIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // vLLM model registration (vllm-triton-inference)
    const modelVllmResource = modelsResource.addResource('vllm');
    modelVllmResource.addMethod('POST', modelImportIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const modelVllmEngineSpecResource = modelVllmResource.addResource('engine-spec');
    modelVllmEngineSpecResource.addMethod('GET', modelImportIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Post-import engine-configuration editing (vllm-sizing-and-packaging-errors)
    const modelVllmTrainingResource = modelVllmResource.addResource('{training_id}');
    const modelVllmEngineConfigResource =
      modelVllmTrainingResource.addResource('engine-configuration');
    modelVllmEngineConfigResource.addMethod('PUT', modelImportIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const modelConvertResource = modelsResource.addResource('convert');
    modelConvertResource.addMethod('POST', modelConverterIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const modelInspectResource = modelsResource.addResource('inspect');
    modelInspectResource.addMethod('POST', modelConverterIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const modelTypesResource = modelsResource.addResource('types');
    modelTypesResource.addMethod('GET', modelConverterIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const modelResource = modelsResource.addResource('{id}');
    modelResource.addMethod('GET', modelsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    modelResource.addMethod('DELETE', modelsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const modelStageResource = modelResource.addResource('stage');
    modelStageResource.addMethod('PUT', modelsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Components endpoints
    const componentsResource = this.api.root.addResource('components');
    componentsResource.addMethod('GET', componentsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    componentsResource.addMethod('POST', componentsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const componentResource = componentsResource.addResource('{id}');
    componentResource.addMethod('GET', componentsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    componentResource.addMethod('DELETE', componentsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const componentVersionsResource = componentResource.addResource('versions');
    componentVersionsResource.addMethod('GET', componentsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const discoverResource = componentsResource.addResource('discover');
    discoverResource.addMethod('POST', componentsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Component Configuration endpoints
    const componentConfigurationHandler = new lambda.Function(this, 'ComponentConfigurationHandler', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'component_configuration.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: props.createLambdaRole('ComponentConfiguration'),
      environment: props.lambdaEnvironment,
      layers: [props.sharedLayer],
      timeout: cdk.Duration.seconds(60),
    });

    const componentConfigurationIntegration = lambdaIntegration('ComponentConfiguration', componentConfigurationHandler);

    const schemaResource = componentsResource.addResource('schema');
    schemaResource.addMethod('GET', componentConfigurationIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const configureResource = componentsResource.addResource('configure');
    configureResource.addMethod('POST', componentConfigurationIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Shared Components endpoints
    const sharedComponentsResource = this.api.root.addResource('shared-components');
    sharedComponentsResource.addMethod('GET', sharedComponentsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const availableSharedResource = sharedComponentsResource.addResource('available');
    availableSharedResource.addMethod('GET', sharedComponentsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const provisionSharedResource = sharedComponentsResource.addResource('provision');
    provisionSharedResource.addMethod('POST', sharedComponentsIntegration);

    const statusSharedResource = sharedComponentsResource.addResource('status');
    statusSharedResource.addMethod('GET', sharedComponentsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const updateAllSharedResource = sharedComponentsResource.addResource('update-all');
    updateAllSharedResource.addMethod('POST', sharedComponentsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Data Accounts endpoints
    const dataAccountsResource = this.api.root.addResource('data-accounts');
    dataAccountsResource.addMethod('GET', dataAccountsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    dataAccountsResource.addMethod('POST', dataAccountsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const dataAccountIdResource = dataAccountsResource.addResource('{id}');
    dataAccountIdResource.addMethod('GET', dataAccountsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    dataAccountIdResource.addMethod('PUT', dataAccountsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    dataAccountIdResource.addMethod('DELETE', dataAccountsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const testConnectionResource = dataAccountIdResource.addResource('test');
    testConnectionResource.addMethod('POST', dataAccountsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Invokable Bedrock model options for the settings-page model dropdown;
    // served on the reserved 'bedrock-configuration' id:
    // GET /data-accounts/bedrock-configuration/models
    const dataAccountModelsResource = dataAccountIdResource.addResource('models');
    dataAccountModelsResource.addMethod('GET', dataAccountsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Per-model output token limits (llm-model-token-and-image-sizing
    // Requirements 4.1, 4.3). Like /models above, these ride the reserved
    // 'bedrock-configuration' data-account id:
    //   GET/PUT /data-accounts/bedrock-configuration/token-limits
    // Authorization beyond the Cognito authorizer is the PortalAdmin
    // 'bedrock-config:write' gate enforced inside
    // data_accounts.handle_bedrock_configuration, which dispatches this path
    // ahead of the bare GET/PUT, so the write gate and its denied-attempt
    // audit entry are inherited unchanged.
    const dataAccountTokenLimitsResource = dataAccountIdResource.addResource('token-limits');
    dataAccountTokenLimitsResource.addMethod('GET', dataAccountsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    dataAccountTokenLimitsResource.addMethod('PUT', dataAccountsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Workflow Manager endpoints
    const workflowsResource = this.api.root.addResource('workflows');
    workflowsResource.addMethod('GET', workflowsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    workflowsResource.addMethod('POST', workflowsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Static 'node-catalog' resource — sibling of the {id} path param; API
    // Gateway prefers static segments for exact matches (same pattern as
    // /devices/{id}/logs/analyze above).
    const nodeCatalogResource = workflowsResource.addResource('node-catalog');
    nodeCatalogResource.addMethod('GET', workflowValidationIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Prompt-based workflow generation (Bedrock chat sessions)
    const workflowGenerateResource = workflowsResource.addResource('generate');
    this.workflowGenerateResourceId = workflowGenerateResource.resourceId;
    workflowGenerateResource.addMethod('POST', workflowGeneratorIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const workflowResource = workflowsResource.addResource('{id}');
    this.workflowResourceId = workflowResource.resourceId;
    workflowResource.addMethod('GET', workflowsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    workflowResource.addMethod('PUT', workflowsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    workflowResource.addMethod('DELETE', workflowsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const workflowDuplicateResource = workflowResource.addResource('duplicate');
    workflowDuplicateResource.addMethod('POST', workflowsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const workflowVersionsResource = workflowResource.addResource('versions');
    workflowVersionsResource.addMethod('GET', workflowsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const workflowValidateResource = workflowResource.addResource('validate');
    workflowValidateResource.addMethod('POST', workflowValidationIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const workflowPackageResource = workflowResource.addResource('package');
    workflowPackageResource.addMethod('POST', workflowPackagingIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const workflowTestRunsResource = workflowResource.addResource('test-runs');
    workflowTestRunsResource.addMethod('POST', workflowTestingIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    workflowTestRunsResource.addMethod('GET', workflowTestingIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Test datasets (canned sample inputs for workflow test runs)
    const testDatasetsResource = this.api.root.addResource('test-datasets');
    testDatasetsResource.addMethod('GET', workflowTestingIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    testDatasetsResource.addMethod('POST', workflowTestingIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const testDatasetResource = testDatasetsResource.addResource('{id}');
    testDatasetResource.addMethod('GET', workflowTestingIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    testDatasetResource.addMethod('DELETE', workflowTestingIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Test runs (status and per-node results)
    const testRunsResource = this.api.root.addResource('test-runs');
    const testRunResource = testRunsResource.addResource('{id}');
    testRunResource.addMethod('GET', workflowTestingIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Custom node code assist (custom-node-code-assist): prompt-to-code
    // generation for custom Python nodes. Reuses the WorkflowGeneratorHandler
    // bundle — the handler dispatches on resource == '/code-assist' — so no
    // new Lambda or compute-stack change is needed.
    const codeAssistResource = this.api.root.addResource('code-assist');
    codeAssistResource.addMethod('POST', workflowGeneratorIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Audit Logs endpoints
    const auditLogsResource = this.api.root.addResource('audit-logs');
    auditLogsResource.addMethod('GET', auditLogsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    this.apiUrl = this.api.url;

    // Outputs
    new cdk.CfnOutput(this, 'ApiUrl', {
      value: this.api.url,
      description: 'API Gateway URL',
      // Don't export from nested stack - parent will export it
    });

    new cdk.CfnOutput(this, 'ApiId', {
      value: this.api.restApiId,
      description: 'API Gateway ID',
      // Don't export from nested stack - parent will export it
    });
  }
}
