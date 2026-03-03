import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import * as path from 'path';

export interface ApiModelStackProps extends cdk.NestedStackProps {
  api: apigateway.RestApi;
  authorizer: apigateway.CognitoUserPoolsAuthorizer;
  trainingHandler: lambda.Function;
  compilationHandler: lambda.Function;
  packagingHandler: lambda.Function;
  greengrassPublishHandler: lambda.Function;
  modelsHandler: lambda.Function;
  modelImportHandler: lambda.Function;
  modelConverterHandler: lambda.Function;
  componentsHandler: lambda.Function;
  sharedComponentsHandler: lambda.Function;
  lambdaEnvironment: { [key: string]: string };
  createLambdaRole: (name: string) => iam.Role;
  sharedLayer: lambda.LayerVersion;
}

export class ApiModelStack extends cdk.NestedStack {
  constructor(scope: Construct, id: string, props: ApiModelStackProps) {
    super(scope, id, props);

    const { api, authorizer } = props;

    // Create Lambda integrations
    const trainingIntegration = new apigateway.LambdaIntegration(props.trainingHandler);
    const compilationIntegration = new apigateway.LambdaIntegration(props.compilationHandler);
    const packagingIntegration = new apigateway.LambdaIntegration(props.packagingHandler);
    const greengrassPublishIntegration = new apigateway.LambdaIntegration(props.greengrassPublishHandler);
    const modelsIntegration = new apigateway.LambdaIntegration(props.modelsHandler);
    const modelImportIntegration = new apigateway.LambdaIntegration(props.modelImportHandler);
    const modelConverterIntegration = new apigateway.LambdaIntegration(props.modelConverterHandler);
    const componentsIntegration = new apigateway.LambdaIntegration(props.componentsHandler);
    const sharedComponentsIntegration = new apigateway.LambdaIntegration(props.sharedComponentsHandler);

    // Training endpoints
    const trainingResource = api.root.addResource('training');
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
    const modelsResource = api.root.addResource('models');
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
    const componentsResource = api.root.addResource('components');
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
    const sharedComponentsResource = api.root.addResource('shared-components');
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
  }
}
