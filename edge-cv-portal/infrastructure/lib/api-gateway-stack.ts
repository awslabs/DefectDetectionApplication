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
  lambdaEnvironment: { [key: string]: string };
  createLambdaRole: (name: string) => iam.Role;
  sharedLayer: lambda.LayerVersion;
}

export class ApiGatewayStack extends cdk.NestedStack {
  public readonly api: apigateway.RestApi;
  public readonly apiUrl: string;

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

    // Cognito Authorizer
    const authorizer = new apigateway.CognitoUserPoolsAuthorizer(this, 'CognitoAuthorizer', {
      cognitoUserPools: [props.userPool],
      authorizerName: 'EdgeCVPortalAuthorizer',
      identitySource: 'method.request.header.Authorization',
    });

    // Create Lambda integrations
    const authIntegration = new apigateway.LambdaIntegration(props.authHandler);
    const userManagementIntegration = new apigateway.LambdaIntegration(props.userManagementHandler);
    const useCasesIntegration = new apigateway.LambdaIntegration(props.useCasesHandler);
    const devicesIntegration = new apigateway.LambdaIntegration(props.devicesHandler);
    const deviceLogsIntegration = new apigateway.LambdaIntegration(props.deviceLogsHandler);
    const deviceLogsAnalyzerIntegration = new apigateway.LambdaIntegration(props.deviceLogsAnalyzerHandler);
    const deploymentsIntegration = new apigateway.LambdaIntegration(props.deploymentsHandler);
    const dataManagementIntegration = new apigateway.LambdaIntegration(props.dataManagementHandler);
    const datasetsIntegration = new apigateway.LambdaIntegration(props.datasetsHandler);
    const capturesIntegration = new apigateway.LambdaIntegration(props.capturesHandler);
    const preLabeledDatasetsIntegration = new apigateway.LambdaIntegration(props.preLabeledDatasetsHandler);
    const labelingIntegration = new apigateway.LambdaIntegration(props.labelingHandler);
    const trainingIntegration = new apigateway.LambdaIntegration(props.trainingHandler);
    const compilationIntegration = new apigateway.LambdaIntegration(props.compilationHandler);
    const packagingIntegration = new apigateway.LambdaIntegration(props.packagingHandler);
    const greengrassPublishIntegration = new apigateway.LambdaIntegration(props.greengrassPublishHandler);
    const modelsIntegration = new apigateway.LambdaIntegration(props.modelsHandler);
    const modelImportIntegration = new apigateway.LambdaIntegration(props.modelImportHandler);
    const modelConverterIntegration = new apigateway.LambdaIntegration(props.modelConverterHandler);
    const componentsIntegration = new apigateway.LambdaIntegration(props.componentsHandler);
    const sharedComponentsIntegration = new apigateway.LambdaIntegration(props.sharedComponentsHandler);
    const dataAccountsIntegration = new apigateway.LambdaIntegration(props.dataAccountsHandler);
    const auditLogsIntegration = new apigateway.LambdaIntegration(props.auditLogsHandler);

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
    deviceResource.addMethod('GET', devicesIntegration, {
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

    const componentConfigurationIntegration = new apigateway.LambdaIntegration(componentConfigurationHandler);

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
