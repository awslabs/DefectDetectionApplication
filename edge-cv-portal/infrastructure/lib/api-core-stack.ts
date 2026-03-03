import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import { Construct } from 'constructs';

export interface ApiCoreStackProps extends cdk.NestedStackProps {
  api: apigateway.RestApi;
  authorizer: apigateway.CognitoUserPoolsAuthorizer;
  authHandler: lambda.Function;
  userManagementHandler: lambda.Function;
  useCasesHandler: lambda.Function;
  devicesHandler: lambda.Function;
  deviceLogsHandler: lambda.Function;
  deploymentsHandler: lambda.Function;
  auditLogsHandler: lambda.Function;
}

export class ApiCoreStack extends cdk.NestedStack {
  public readonly usecaseResource: apigateway.Resource;

  constructor(scope: Construct, id: string, props: ApiCoreStackProps) {
    super(scope, id, props);

    const { api, authorizer } = props;

    // Create Lambda integrations
    const authIntegration = new apigateway.LambdaIntegration(props.authHandler);
    const userManagementIntegration = new apigateway.LambdaIntegration(props.userManagementHandler);
    const useCasesIntegration = new apigateway.LambdaIntegration(props.useCasesHandler);
    const devicesIntegration = new apigateway.LambdaIntegration(props.devicesHandler);
    const deviceLogsIntegration = new apigateway.LambdaIntegration(props.deviceLogsHandler);
    const deploymentsIntegration = new apigateway.LambdaIntegration(props.deploymentsHandler);
    const auditLogsIntegration = new apigateway.LambdaIntegration(props.auditLogsHandler);

    // Auth endpoints
    const authResource = api.root.addResource('auth');
    authResource.addResource('me').addMethod('GET', authIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    authResource.addResource('refresh').addMethod('POST', authIntegration, {
      authorizationType: apigateway.AuthorizationType.NONE,
    });

    // User Management endpoints
    const usersResource = api.root.addResource('users');
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
    const usecasesResource = api.root.addResource('usecases');
    usecasesResource.addMethod('GET', useCasesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    usecasesResource.addMethod('POST', useCasesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    this.usecaseResource = usecasesResource.addResource('{id}');
    const verifyRoleResource = usecasesResource.addResource('verify-role');
    verifyRoleResource.addMethod('POST', useCasesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    this.usecaseResource.addMethod('GET', useCasesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    this.usecaseResource.addMethod('PUT', useCasesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
    this.usecaseResource.addMethod('DELETE', useCasesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Devices endpoints
    const devicesResource = api.root.addResource('devices');
    devicesResource.addMethod('GET', devicesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const deviceResource = devicesResource.addResource('{id}');
    deviceResource.addMethod('GET', devicesIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const deviceLogsResource = deviceResource.addResource('logs');
    deviceLogsResource.addMethod('GET', deviceLogsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    const deviceComponentLogsResource = deviceLogsResource.addResource('{component}');
    deviceComponentLogsResource.addMethod('GET', deviceLogsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });

    // Deployments endpoints
    const deploymentsResource = api.root.addResource('deployments');
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

    // Audit Logs endpoints
    const auditLogsResource = api.root.addResource('audit-logs');
    auditLogsResource.addMethod('GET', auditLogsIntegration, {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
    });
  }
}
