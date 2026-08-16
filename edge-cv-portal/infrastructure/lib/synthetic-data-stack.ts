import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Construct } from 'constructs';
import * as crypto from 'crypto';
import * as path from 'path';

export interface SyntheticDataStackProps extends cdk.StackProps {
  /** Portal tables the handler reads/writes (StorageStack). */
  useCasesTable: dynamodb.Table;
  userRolesTable: dynamodb.Table;
  auditLogTable: dynamodb.Table;
  settingsTable: dynamodb.Table;
  trainingJobsTable: dynamodb.Table;
  /**
   * Trusted UseCase account IDs the handler may assume DDAPortalAccessRole
   * into (cross-account Use_Case data bucket access, same policy shape as
   * the ComputeStack's DatasetsHandler). Same context source and same
   * non-empty synth-time requirement as the ComputeStack.
   */
  trustedUseCaseAccountIds: string[];
  /** Portal Cognito user pool (route authorizer). */
  userPool: cognito.IUserPool;
  /** Rest API id of the existing portal API (ComputeStack.api). */
  restApiId: string;
  /** Root resource id of the existing portal API. */
  restApiRootResourceId: string;
  /** Stage the portal API serves on (ApiGatewayStack deployOptions). */
  apiStageName: string;
}

/**
 * Synthetic defect data generation infrastructure
 * (synthetic-defect-data-generation, task 6.1).
 *
 * Self-contained stack following the NodeDesignerStack / BuildFleetStack
 * precedent: it owns its DynamoDB tables and Lambda, and registers its API
 * routes against the imported portal Rest API (the API's own nested stack
 * sits near the CloudFormation 500-resource limit, so new route families
 * get their own stack). The only shared-file edit for this feature's
 * infrastructure is the single additive instantiation in bin/app.ts.
 *
 * - SyntheticSessionsTable: PK session_id, SK sk ('META' | 'PREVIEW#<id>');
 *   GSI usecase-index (usecase_id / created_at) for session listing.
 * - PromptTemplatesTable: PK usecase_id, SK template_key
 *   ('{object_type}#{defect_type}').
 * - SyntheticDataHandler Lambda (synthetic_data.py): API routing + async
 *   generation worker via self-invocation (InvocationType='Event'), 1024 MB
 *   and a 15 min timeout for the worker path. Layers: shared utilities, JWT,
 *   and the imaging layer bundling Pillow (image decode/diff for the
 *   bbox_from_diff auto-annotation fallback).
 * - Routes: the /synthetic/... route matrix from the design (models, prompt
 *   templates, sessions CRUD, generate, previews/approval, integrate,
 *   retrain).
 */
export class SyntheticDataStack extends cdk.Stack {
  public readonly syntheticSessionsTable: dynamodb.Table;
  public readonly promptTemplatesTable: dynamodb.Table;
  public readonly syntheticDataHandler: lambda.Function;

  constructor(scope: Construct, id: string, props: SyntheticDataStackProps) {
    super(scope, id, props);

    // Same synth-time guard as the ComputeStack: never fall back to a
    // wildcard account for sts:AssumeRole.
    if (!props.trustedUseCaseAccountIds || props.trustedUseCaseAccountIds.length === 0) {
      throw new Error(
        'SyntheticDataStack requires a non-empty trustedUseCaseAccountIds list ' +
          '(pass -c trustedUseCaseAccountIds=<id>,<id>). Refusing to synth an ' +
          'sts:AssumeRole grant on a wildcard account.'
      );
    }

    // Fixed function name so the Lambda's environment can reference itself
    // (async worker self-invocation) and the role can scope
    // lambda:InvokeFunction to exactly this function without a circular
    // reference — same fixed-name pattern as the NodeDesignerStack's
    // simulator state machine.
    const HANDLER_FUNCTION_NAME = 'dda-synthetic-data-handler';
    const handlerFunctionArn =
      `arn:aws:lambda:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}` +
      `:function:${HANDLER_FUNCTION_NAME}`;

    // ------------------------------------------------------------------
    // DynamoDB tables (design "Data Models" — new, additive)
    // ------------------------------------------------------------------

    // SyntheticSessions: one item collection per Generation_Session
    // (sk 'META' + one 'PREVIEW#<id>' item per Preview_Image) so a single
    // Query restores full session state without hitting the 400 KB item
    // limit (Req 10.2).
    this.syntheticSessionsTable = new dynamodb.Table(this, 'SyntheticSessionsTable', {
      tableName: 'dda-portal-synthetic-sessions',
      partitionKey: {
        name: 'session_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'sk',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Session listing per Use_Case ordered by creation time (Req 10.4).
    this.syntheticSessionsTable.addGlobalSecondaryIndex({
      indexName: 'usecase-index',
      partitionKey: {
        name: 'usecase_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'created_at',
        type: dynamodb.AttributeType.NUMBER,
      },
    });

    // PromptTemplates: stored Prompt_Template per Use_Case keyed by
    // '{object_type}#{defect_type}' (Req 2.1, 2.4).
    this.promptTemplatesTable = new dynamodb.Table(this, 'PromptTemplatesTable', {
      tableName: 'dda-portal-prompt-templates',
      partitionKey: {
        name: 'usecase_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'template_key',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ------------------------------------------------------------------
    // Lambda layers: this stack builds its own shared-utils and JWT layer
    // versions from the same assets as the ComputeStack (the
    // NodeDesignerStack and BuildFleetStack do the same for the shared
    // layer), keeping the cross-stack dependency one-directional. The
    // imaging layer is new to this feature and bundles Pillow (built by
    // backend/layers/imaging/build.sh, same convention as the jwt layer).
    // ------------------------------------------------------------------
    const sharedLayer = new lambda.LayerVersion(this, 'SyntheticSharedLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/layers/shared')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11],
      description: 'Shared utilities for the synthetic data Lambda function',
    });

    const jwtLayer = new lambda.LayerVersion(this, 'SyntheticJwtLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/layers/jwt')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11],
      description: 'JWT dependencies for the synthetic data Lambda function',
    });

    const imagingLayer = new lambda.LayerVersion(this, 'SyntheticImagingLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/layers/imaging')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_11],
      description:
        'Pillow for synthetic preview image decode/diff (bbox_from_diff auto-annotation)',
    });

    // ------------------------------------------------------------------
    // Handler role (deliberately narrower than the ComputeStack's
    // createLambdaRole: only the tables and services this feature uses).
    // ------------------------------------------------------------------
    const handlerRole = new iam.Role(this, 'SyntheticDataHandlerRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
    });

    // Own tables: full read/write.
    this.syntheticSessionsTable.grantReadWriteData(handlerRole);
    this.promptTemplatesTable.grantReadWriteData(handlerRole);
    // Portal tables: usecases read (get_usecase), user-roles read
    // (check_user_access), audit write (log_audit_event), settings read.
    // Training-jobs read/write: the retrain endpoint proxies
    // training.py::create_training_job in-process, which writes the
    // training item (and status updates read/write it).
    props.useCasesTable.grantReadData(handlerRole);
    props.userRolesTable.grantReadData(handlerRole);
    props.auditLogTable.grantWriteData(handlerRole);
    props.settingsTable.grantReadData(handlerRole);
    props.trainingJobsTable.grantReadWriteData(handlerRole);

    // Bedrock: invoke_model on the image generation models (the catalog is
    // runtime-filtered, so the grant covers foundation models and inference
    // profiles rather than a fixed model ARN — same shape as the
    // WorkflowGeneratorHandler grant).
    handlerRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['bedrock:InvokeModel'],
      resources: [
        'arn:aws:bedrock:*::foundation-model/*',
        `arn:aws:bedrock:*:${cdk.Aws.ACCOUNT_ID}:inference-profile/*`,
      ],
    }));
    // ListFoundationModels is a list action without resource-level scoping
    // (same as the data-accounts handler's Bedrock model dropdown grant).
    handlerRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['bedrock:ListFoundationModels'],
      resources: ['*'],
    }));

    // STS: cross-account Use_Case data bucket access. Same policy shape as
    // the DatasetsHandler (createLambdaRole): AssumeRole scoped to the fixed
    // DDAPortalAccessRole name in the trusted UseCase account list.
    handlerRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['sts:AssumeRole'],
      resources: props.trustedUseCaseAccountIds.map(
        (id) => `arn:aws:iam::${id}:role/DDAPortalAccessRole`
      ),
    }));

    // Async generation worker: the API handler re-invokes this same Lambda
    // with InvocationType='Event'.
    handlerRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['lambda:InvokeFunction'],
      resources: [handlerFunctionArn],
    }));

    // SageMaker: the retrain endpoint runs training.py in-process, whose
    // same-account path uses the Lambda's own credentials for
    // CreateTrainingJob/DescribeTrainingJob (cross-account setups go through
    // the assumed DDAPortalAccessRole instead). Same statement shape as the
    // ComputeStack's createLambdaRole, trimmed to the training-job actions
    // training.py uses.
    handlerRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'sagemaker:CreateTrainingJob',
        'sagemaker:DescribeTrainingJob',
        'sagemaker:ListTrainingJobs',
        'sagemaker:AddTags',
      ],
      resources: ['arn:aws:sagemaker:*:*:training-job/*'],
    }));
    // PassRole for the SageMaker execution role (DDASageMakerExecutionRole),
    // same scoping and condition as the ComputeStack's createLambdaRole.
    handlerRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['iam:PassRole'],
      resources: ['arn:aws:iam::*:role/DDA*Role'],
      conditions: {
        StringEquals: {
          'iam:PassedToService': 'sagemaker.amazonaws.com',
        },
      },
    }));

    // ------------------------------------------------------------------
    // SyntheticDataHandler Lambda (API routing + async generation worker).
    // ------------------------------------------------------------------
    this.syntheticDataHandler = new lambda.Function(this, 'SyntheticDataHandler', {
      functionName: HANDLER_FUNCTION_NAME,
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'synthetic_data.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../backend/functions')),
      role: handlerRole,
      environment: {
        SYNTHETIC_SESSIONS_TABLE: this.syntheticSessionsTable.tableName,
        PROMPT_TEMPLATES_TABLE: this.promptTemplatesTable.tableName,
        TRAINING_JOBS_TABLE: props.trainingJobsTable.tableName,
        SYNTHETIC_DATA_FUNCTION_NAME: HANDLER_FUNCTION_NAME,
        // Shared portal environment (shared_utils + training.py contract,
        // matching the ComputeStack lambdaEnvironment names).
        USECASES_TABLE: props.useCasesTable.tableName,
        USER_ROLES_TABLE: props.userRolesTable.tableName,
        AUDIT_LOG_TABLE: props.auditLogTable.tableName,
        SETTINGS_TABLE: props.settingsTable.tableName,
        PORTAL_ACCOUNT_ID: cdk.Aws.ACCOUNT_ID,
      },
      layers: [sharedLayer, jwtLayer, imagingLayer],
      // Worker path: up to 20 variations x N source images, one Bedrock
      // image generation call each.
      memorySize: 1024,
      timeout: cdk.Duration.minutes(15),
    });

    // ------------------------------------------------------------------
    // API routes on the shared portal API (same imported-API pattern as
    // NodeDesignerApiStack / BuildFleetStack: authorizers are per-Rest-API
    // resources, so this stack attaches its own instance; a fresh
    // deployment re-points the stage).
    // ------------------------------------------------------------------
    const api = apigateway.RestApi.fromRestApiAttributes(this, 'PortalApi', {
      restApiId: props.restApiId,
      rootResourceId: props.restApiRootResourceId,
    });

    const authorizer = new apigateway.CognitoUserPoolsAuthorizer(this, 'SyntheticDataAuthorizer', {
      cognitoUserPools: [props.userPool],
      authorizerName: 'EdgeCVPortalSyntheticDataAuthorizer',
      identitySource: 'method.request.header.Authorization',
    });

    // The imported API does not carry the RestApi construct's
    // defaultCorsPreflightOptions, so mirror them on the resource root
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

    // allowTestInvoke: false — one AWS::Lambda::Permission per method
    // instead of two (same resource-count economy as the Node_Designer and
    // build-fleet routes).
    const integration = new apigateway.LambdaIntegration(this.syntheticDataHandler, {
      allowTestInvoke: false,
    });

    const methods: apigateway.Method[] = [];
    const addMethod = (resource: apigateway.IResource, httpMethod: string) => {
      methods.push(
        resource.addMethod(httpMethod, integration, {
          authorizer,
          authorizationType: apigateway.AuthorizationType.COGNITO,
        }),
      );
    };

    // Route matrix (design "backend/functions/synthetic_data.py"):
    //   GET    /synthetic/models
    //   GET    /synthetic/prompt-templates
    //   PUT    /synthetic/prompt-templates
    //   POST   /synthetic/sessions
    //   GET    /synthetic/sessions
    //   GET    /synthetic/sessions/{id}
    //   PATCH  /synthetic/sessions/{id}
    //   POST   /synthetic/sessions/{id}/generate
    //   POST   /synthetic/sessions/{id}/previews/approval
    //   POST   /synthetic/sessions/{id}/integrate
    //   POST   /synthetic/sessions/{id}/retrain
    const syntheticResource = api.root.addResource('synthetic', {
      defaultCorsPreflightOptions: corsOptions,
    });

    // /synthetic/models — Model_Catalog filtered by runtime availability.
    addMethod(syntheticResource.addResource('models'), 'GET');

    // /synthetic/prompt-templates — stored/default template read + persist.
    const promptTemplatesResource = syntheticResource.addResource('prompt-templates');
    addMethod(promptTemplatesResource, 'GET');
    addMethod(promptTemplatesResource, 'PUT');

    // /synthetic/sessions — Generation_Session collection.
    const sessionsResource = syntheticResource.addResource('sessions');
    addMethod(sessionsResource, 'POST');
    addMethod(sessionsResource, 'GET');

    // /synthetic/sessions/{id} — session detail + updates.
    const sessionResource = sessionsResource.addResource('{id}');
    addMethod(sessionResource, 'GET');
    addMethod(sessionResource, 'PATCH');

    // Generation, review, integration, retrain sub-actions.
    addMethod(sessionResource.addResource('generate'), 'POST');
    addMethod(
      sessionResource.addResource('previews').addResource('approval'),
      'POST',
    );
    addMethod(sessionResource.addResource('integrate'), 'POST');
    addMethod(sessionResource.addResource('retrain'), 'POST');

    // ------------------------------------------------------------------
    // Deployment re-pointing the existing stage so the routes above go
    // live. The logical id is salted with the route table: any route change
    // creates a new deployment (a deployment snapshots the whole API, so it
    // always includes the other stacks' routes too). Same pattern as
    // NodeDesignerApiStack / BuildFleetStack.
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

    const deployment = new apigateway.CfnDeployment(this, 'SyntheticDataDeployment', {
      restApiId: props.restApiId,
      stageName: props.apiStageName,
      description: 'Synthetic data routes deployment (synthetic-defect-data-generation)',
    });
    deployment.overrideLogicalId(`SyntheticDataDeployment${routeSalt}`);
    // Every resource/method (including the CORS preflight OPTIONS methods)
    // and the authorizer must exist before the deployment snapshot is
    // taken; a construct dependency covers the whole subtree.
    deployment.node.addDependency(authorizer);
    deployment.node.addDependency(syntheticResource);
  }
}
