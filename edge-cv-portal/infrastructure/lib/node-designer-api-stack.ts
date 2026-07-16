import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Construct } from 'constructs';
import * as crypto from 'crypto';

export interface NodeDesignerApiStackProps extends cdk.NestedStackProps {
  /** Rest API id of the existing portal API (ComputeStack/ApiGatewayStack). */
  restApiId: string;
  /** Root resource id of the existing portal API. */
  restApiRootResourceId: string;
  /** Stage the portal API serves on (ApiGatewayStack deployOptions.stageName). */
  stageName: string;
  userPool: cognito.IUserPool;
  pluginRecordsHandler: lambda.Function;
  pluginImporterHandler: lambda.Function;
  nodeGeneratorHandler: lambda.Function;
  pluginBuildsHandler: lambda.Function;
  pluginComponentsHandler: lambda.Function;
  pluginSimulatorHandler: lambda.Function;
  customNodeTypesHandler: lambda.Function;
}

/**
 * Node_Designer API Gateway routes (custom-node-designer, task 6.1).
 *
 * The portal API's own nested stack (ApiGatewayStack) sits at ~491 of the
 * CloudFormation 500-resource limit, so the Node_Designer routes live in
 * their own nested stack that imports the Rest API by id and registers
 * resources/methods against it (the documented pattern for spreading one
 * REST API across stacks). A fresh deployment created here re-points the
 * existing stage so the new routes go live; its logical id is salted with
 * the route table so route changes roll a new deployment.
 *
 * Route table (design "New components"):
 * - plugin_records.py:    GET/POST /plugins, GET/PUT/DELETE /plugins/{id},
 *                         GET /plugins/{id}/versions/{v},
 *                         POST .../promote | .../demote | .../review
 * - plugin_importer.py:   POST /plugins/import, GET /plugin-modules,
 *                         POST /plugins/{id}/versions/{v}/select-plugins,
 *                         POST /plugins/{id}/versions/{v}/adjust-revision
 * - node_generator.py:    POST /plugins/generate,
 *                         GET  /plugins/generate/{session},
 *                         POST /plugins/generate/{session}/message
 * - plugin_builds.py:     POST /plugins/{id}/versions/{v}/build, GET .../builds
 * - plugin_components.py: GET /plugins/{id}/versions/{v}/component
 * - plugin_simulator.py:  POST /plugins/{id}/versions/{v}/simulate,
 *                         GET /simulations/{runId}
 * - custom_node_types.py: GET/POST /custom-node-types,
 *                         GET/PUT/DELETE /custom-node-types/{id},
 *                         POST .../deprecate
 */
export class NodeDesignerApiStack extends cdk.NestedStack {
  constructor(scope: Construct, id: string, props: NodeDesignerApiStackProps) {
    super(scope, id, props);

    const api = apigateway.RestApi.fromRestApiAttributes(this, 'PortalApi', {
      restApiId: props.restApiId,
      rootResourceId: props.restApiRootResourceId,
    });

    // Same authorizer configuration as the ApiGatewayStack's; authorizers are
    // per-Rest-API resources, so this stack attaches its own instance to the
    // imported API.
    const authorizer = new apigateway.CognitoUserPoolsAuthorizer(this, 'NodeDesignerAuthorizer', {
      cognitoUserPools: [props.userPool],
      authorizerName: 'EdgeCVPortalNodeDesignerAuthorizer',
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
    // of two (same resource-count economy as the Workflow Manager routes).
    const recordsIntegration = new apigateway.LambdaIntegration(props.pluginRecordsHandler, { allowTestInvoke: false });
    const importerIntegration = new apigateway.LambdaIntegration(props.pluginImporterHandler, { allowTestInvoke: false });
    const generatorIntegration = new apigateway.LambdaIntegration(props.nodeGeneratorHandler, { allowTestInvoke: false });
    const buildsIntegration = new apigateway.LambdaIntegration(props.pluginBuildsHandler, { allowTestInvoke: false });
    const componentsIntegration = new apigateway.LambdaIntegration(props.pluginComponentsHandler, { allowTestInvoke: false });
    const simulatorIntegration = new apigateway.LambdaIntegration(props.pluginSimulatorHandler, { allowTestInvoke: false });
    const nodeTypesIntegration = new apigateway.LambdaIntegration(props.customNodeTypesHandler, { allowTestInvoke: false });

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

    // /plugins — Plugin_Record collection.
    const pluginsResource = api.root.addResource('plugins', {
      defaultCorsPreflightOptions: corsOptions,
    });
    addMethod(pluginsResource, 'GET', recordsIntegration);
    addMethod(pluginsResource, 'POST', recordsIntegration);

    // Static siblings of {id}; API Gateway prefers static segments for exact
    // matches (same pattern as /workflows/node-catalog).
    // /plugins/import — repository import.
    addMethod(pluginsResource.addResource('import'), 'POST', importerIntegration);

    // /plugins/generate — Bedrock scaffold-generation sessions
    // (start/poll: POST starts a turn with 202, GET {session} polls it).
    const generateResource = pluginsResource.addResource('generate');
    addMethod(generateResource, 'POST', generatorIntegration);
    const generateSessionResource = generateResource.addResource('{session}');
    addMethod(generateSessionResource, 'GET', generatorIntegration);
    addMethod(
      generateSessionResource.addResource('message'),
      'POST',
      generatorIntegration,
    );

    // /plugins/{id}
    const pluginResource = pluginsResource.addResource('{id}');
    addMethod(pluginResource, 'GET', recordsIntegration);
    addMethod(pluginResource, 'PUT', recordsIntegration);
    addMethod(pluginResource, 'DELETE', recordsIntegration);

    // /plugins/{id}/versions/{v}
    const versionResource = pluginResource.addResource('versions').addResource('{v}');
    addMethod(versionResource, 'GET', recordsIntegration);

    // Source inspection (10.2) and edited-source submission ahead of a
    // build (1.5, 1.6) — both served by plugin_records.py.
    const sourceResource = versionResource.addResource('source');
    addMethod(sourceResource, 'GET', recordsIntegration);
    addMethod(sourceResource, 'PUT', recordsIntegration);

    // Plugin-set import selection (plugin_importer.py): completes an import
    // awaiting selection (import status pending_selection) by recording the
    // chosen subset of the enumerated plugins and submitting the deferred
    // per-arch builds with the selection as the PLUGIN_TARGETS env override.
    addMethod(versionResource.addResource('select-plugins'), 'POST', importerIntegration);

    // Post-import per-platform revision adjustment (plugin_importer.py):
    // fetches (or reuses) the requested revision's source tree, maps the
    // architecture through arch_revisions, and re-runs the affected
    // platform's build (imported-plugin-revision-adjustment-fix 2.1).
    addMethod(versionResource.addResource('adjust-revision'), 'POST', importerIntegration);

    // Stored Introspection_Report with derived Parameter_Suggestions, or a
    // machine-readable unavailability reason (gst-parameter-prepopulation
    // 1.5, 1.6) — served by plugin_records.py.
    addMethod(versionResource.addResource('gst-properties'), 'GET', recordsIntegration);

    // Lifecycle transitions and security review (plugin_records.py).
    addMethod(versionResource.addResource('promote'), 'POST', recordsIntegration);
    addMethod(versionResource.addResource('demote'), 'POST', recordsIntegration);
    addMethod(versionResource.addResource('review'), 'POST', recordsIntegration);

    // Builds (plugin_builds.py) and the auto-packaged Plugin_Component status
    // (plugin_components.py).
    addMethod(versionResource.addResource('build'), 'POST', buildsIntegration);
    addMethod(versionResource.addResource('builds'), 'GET', buildsIntegration);
    addMethod(versionResource.addResource('component'), 'GET', componentsIntegration);

    // Simulator run start (plugin_simulator.py).
    addMethod(versionResource.addResource('simulate'), 'POST', simulatorIntegration);

    // /plugin-modules — Module_Listing (plugin_importer.py).
    const pluginModulesResource = api.root.addResource('plugin-modules', {
      defaultCorsPreflightOptions: corsOptions,
    });
    addMethod(pluginModulesResource, 'GET', importerIntegration);

    // /simulations/{runId} — simulator status/results.
    const simulationsResource = api.root.addResource('simulations', {
      defaultCorsPreflightOptions: corsOptions,
    });
    addMethod(simulationsResource.addResource('{runId}'), 'GET', simulatorIntegration);

    // /custom-node-types — Custom_Node_Type registration and lifecycle.
    const nodeTypesResource = api.root.addResource('custom-node-types', {
      defaultCorsPreflightOptions: corsOptions,
    });
    addMethod(nodeTypesResource, 'GET', nodeTypesIntegration);
    addMethod(nodeTypesResource, 'POST', nodeTypesIntegration);
    const nodeTypeResource = nodeTypesResource.addResource('{id}');
    addMethod(nodeTypeResource, 'GET', nodeTypesIntegration);
    addMethod(nodeTypeResource, 'PUT', nodeTypesIntegration);
    addMethod(nodeTypeResource, 'DELETE', nodeTypesIntegration);
    addMethod(nodeTypeResource.addResource('deprecate'), 'POST', nodeTypesIntegration);

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

    const deployment = new apigateway.CfnDeployment(this, 'NodeDesignerDeployment', {
      restApiId: props.restApiId,
      stageName: props.stageName,
      description: 'Node_Designer routes deployment (custom-node-designer)',
    });
    deployment.overrideLogicalId(`NodeDesignerDeployment${routeSalt}`);
    // Every resource/method (including the CORS preflight OPTIONS methods)
    // and the authorizer must exist before the deployment snapshot is taken;
    // a construct dependency covers the whole subtree.
    deployment.node.addDependency(authorizer);
    deployment.node.addDependency(pluginsResource);
    deployment.node.addDependency(pluginModulesResource);
    deployment.node.addDependency(simulationsResource);
    deployment.node.addDependency(nodeTypesResource);
  }
}
