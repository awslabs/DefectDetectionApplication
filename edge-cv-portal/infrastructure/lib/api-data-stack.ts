import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import { Construct } from 'constructs';

export interface ApiDataStackProps extends cdk.NestedStackProps {
  api: apigateway.RestApi;
  authorizer: apigateway.CognitoUserPoolsAuthorizer;
  usecaseResource: apigateway.Resource;
  dataManagementHandler: lambda.Function;
  datasetsHandler: lambda.Function;
  preLabeledDatasetsHandler: lambda.Function;
  labelingHandler: lambda.Function;
  dataAccountsHandler: lambda.Function;
}

export class ApiDataStack extends cdk.NestedStack {
  constructor(scope: Construct, id: string, props: ApiDataStackProps) {
    super(scope, id, props);

    const { api, authorizer, usecaseResource } = props;

    // Create Lambda integrations
    const dataManagementIntegration = new apigateway.LambdaIntegration(props.dataManagementHandler);
    const datasetsIntegration = new apigateway.LambdaIntegration(props.datasetsHandler);
    const preLabeledDatasetsIntegration = new apigateway.LambdaIntegration(props.preLabeledDatasetsHandler);
    const labelingIntegration = new apigateway.LambdaIntegration(props.labelingHandler);
    const dataAccountsIntegration = new apigateway.LambdaIntegration(props.dataAccountsHandler);

    // Data Management endpoints (under usecases/{usecase_id}/data)
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
    const datasetsResource = api.root.addResource('datasets');
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

    // Workteams endpoint
    const workteamsResource = api.root.addResource('workteams');
    workteamsResource.addMethod('GET', labelingIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Labeling endpoints
    const labelingResource = api.root.addResource('labeling');
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

    // Data Accounts endpoints
    const dataAccountsResource = api.root.addResource('data-accounts');
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
  }
}
