import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

export class StorageStack extends cdk.Stack {
  public readonly useCasesTable: dynamodb.Table;
  public readonly userRolesTable: dynamodb.Table;
  public readonly devicesTable: dynamodb.Table;
  public readonly auditLogTable: dynamodb.Table;
  public readonly labelingJobsTable: dynamodb.Table;
  public readonly preLabeledDatasetsTable: dynamodb.Table;
  public readonly trainingJobsTable: dynamodb.Table;
  public readonly modelsTable: dynamodb.Table;
  public readonly deploymentsTable: dynamodb.Table;
  public readonly settingsTable: dynamodb.Table;
  public readonly componentsTable: dynamodb.Table;
  public readonly sharedComponentsTable: dynamodb.Table;
  public readonly dataAccountsTable: dynamodb.Table;
  public readonly workflowsTable: dynamodb.Table;
  public readonly workflowVersionsTable: dynamodb.Table;
  public readonly testDatasetsTable: dynamodb.Table;
  public readonly testRunsTable: dynamodb.Table;
  public readonly workflowChatSessionsTable: dynamodb.Table;
  public readonly cameraRegistryTable: dynamodb.Table;
  public readonly deviceRegistrationsTable: dynamodb.Table;
  public readonly labelingTeamsTable: dynamodb.Table;
  public readonly labelingTasksTable: dynamodb.Table;
  public readonly portalArtifactsBucket: s3.Bucket;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // UseCases Table
    this.useCasesTable = new dynamodb.Table(this, 'UseCasesTable', {
      tableName: 'dda-portal-usecases',
      partitionKey: {
        name: 'usecase_id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.useCasesTable.addGlobalSecondaryIndex({
      indexName: 'owner-index',
      partitionKey: {
        name: 'owner',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'created_at',
        type: dynamodb.AttributeType.NUMBER,
      },
    });

    // UserRoles Table
    this.userRolesTable = new dynamodb.Table(this, 'UserRolesTable', {
      tableName: 'dda-portal-user-roles',
      partitionKey: {
        name: 'user_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'usecase_id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.userRolesTable.addGlobalSecondaryIndex({
      indexName: 'usecase-users-index',
      partitionKey: {
        name: 'usecase_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'user_id',
        type: dynamodb.AttributeType.STRING,
      },
    });

    // Devices Table
    this.devicesTable = new dynamodb.Table(this, 'DevicesTable', {
      tableName: 'dda-portal-devices',
      partitionKey: {
        name: 'device_id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.devicesTable.addGlobalSecondaryIndex({
      indexName: 'usecase-devices-index',
      partitionKey: {
        name: 'usecase_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'last_heartbeat',
        type: dynamodb.AttributeType.NUMBER,
      },
    });

    this.devicesTable.addGlobalSecondaryIndex({
      indexName: 'status-index',
      partitionKey: {
        name: 'status',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'last_heartbeat',
        type: dynamodb.AttributeType.NUMBER,
      },
    });

    // AuditLog Table
    this.auditLogTable = new dynamodb.Table(this, 'AuditLogTable', {
      tableName: 'dda-portal-audit-log',
      partitionKey: {
        name: 'event_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'timestamp',
        type: dynamodb.AttributeType.NUMBER,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.auditLogTable.addGlobalSecondaryIndex({
      indexName: 'user-actions-index',
      partitionKey: {
        name: 'user_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'timestamp',
        type: dynamodb.AttributeType.NUMBER,
      },
    });

    this.auditLogTable.addGlobalSecondaryIndex({
      indexName: 'usecase-actions-index',
      partitionKey: {
        name: 'usecase_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'timestamp',
        type: dynamodb.AttributeType.NUMBER,
      },
    });

    // LabelingJobs Table
    this.labelingJobsTable = new dynamodb.Table(this, 'LabelingJobsTable', {
      tableName: 'dda-portal-labeling-jobs',
      partitionKey: {
        name: 'job_id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.labelingJobsTable.addGlobalSecondaryIndex({
      indexName: 'usecase-jobs-index',
      partitionKey: {
        name: 'usecase_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'created_at',
        type: dynamodb.AttributeType.NUMBER,
      },
    });

    this.labelingJobsTable.addGlobalSecondaryIndex({
      indexName: 'status-index',
      partitionKey: {
        name: 'status',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'created_at',
        type: dynamodb.AttributeType.NUMBER,
      },
    });

    // TrainingJobs Table
    this.trainingJobsTable = new dynamodb.Table(this, 'TrainingJobsTable', {
      tableName: 'dda-portal-training-jobs',
      partitionKey: {
        name: 'training_id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.trainingJobsTable.addGlobalSecondaryIndex({
      indexName: 'usecase-training-index',
      partitionKey: {
        name: 'usecase_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'created_at',
        type: dynamodb.AttributeType.NUMBER,
      },
    });

    this.trainingJobsTable.addGlobalSecondaryIndex({
      indexName: 'model-index',
      partitionKey: {
        name: 'model_name',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'model_version',
        type: dynamodb.AttributeType.STRING,
      },
    });

    // Deployment-gate lookup for vLLM model components
    // (vllm-triton-inference Requirements 3.3, 8.6): greengrass_publish.py
    // materializes a top-level component_name attribute on published vLLM
    // records so deployments.py can resolve a model-vllm-* component back
    // to its record's supported_architectures.
    this.trainingJobsTable.addGlobalSecondaryIndex({
      indexName: 'component_name-index',
      partitionKey: {
        name: 'component_name',
        type: dynamodb.AttributeType.STRING,
      },
    });

    // Models Table
    this.modelsTable = new dynamodb.Table(this, 'ModelsTable', {
      tableName: 'dda-portal-models',
      partitionKey: {
        name: 'model_id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.modelsTable.addGlobalSecondaryIndex({
      indexName: 'usecase-models-index',
      partitionKey: {
        name: 'usecase_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'created_at',
        type: dynamodb.AttributeType.NUMBER,
      },
    });

    this.modelsTable.addGlobalSecondaryIndex({
      indexName: 'stage-index',
      partitionKey: {
        name: 'stage',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'usecase_id',
        type: dynamodb.AttributeType.STRING,
      },
    });

    // Deployments Table
    this.deploymentsTable = new dynamodb.Table(this, 'DeploymentsTable', {
      tableName: 'dda-portal-deployments',
      partitionKey: {
        name: 'deployment_id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.deploymentsTable.addGlobalSecondaryIndex({
      indexName: 'usecase-deployments-index',
      partitionKey: {
        name: 'usecase_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'created_at',
        type: dynamodb.AttributeType.NUMBER,
      },
    });

    this.deploymentsTable.addGlobalSecondaryIndex({
      indexName: 'status-index',
      partitionKey: {
        name: 'status',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'created_at',
        type: dynamodb.AttributeType.NUMBER,
      },
    });

    // PreLabeledDatasets Table
    this.preLabeledDatasetsTable = new dynamodb.Table(this, 'PreLabeledDatasetsTable', {
      tableName: 'dda-portal-pre-labeled-datasets',
      partitionKey: {
        name: 'dataset_id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.preLabeledDatasetsTable.addGlobalSecondaryIndex({
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

    // Settings Table
    this.settingsTable = new dynamodb.Table(this, 'SettingsTable', {
      tableName: 'dda-portal-settings',
      partitionKey: {
        name: 'setting_key',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Components Table for Greengrass Component Browser
    this.componentsTable = new dynamodb.Table(this, 'ComponentsTable', {
      tableName: 'dda-portal-components',
      partitionKey: {
        name: 'component_id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.componentsTable.addGlobalSecondaryIndex({
      indexName: 'component-name-index',
      partitionKey: {
        name: 'component_name',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'version',
        type: dynamodb.AttributeType.STRING,
      },
    });

    this.componentsTable.addGlobalSecondaryIndex({
      indexName: 'component-type-index',
      partitionKey: {
        name: 'component_type',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'updated_at',
        type: dynamodb.AttributeType.STRING,
      },
    });

    this.componentsTable.addGlobalSecondaryIndex({
      indexName: 'publisher-index',
      partitionKey: {
        name: 'publisher',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'component_name',
        type: dynamodb.AttributeType.STRING,
      },
    });

    // SharedComponents Table - tracks components shared from portal to usecase accounts
    this.sharedComponentsTable = new dynamodb.Table(this, 'SharedComponentsTable', {
      tableName: 'dda-portal-shared-components',
      partitionKey: {
        name: 'usecase_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'component_name',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // DataAccounts Table - stores registered Data Accounts for cross-account data access
    this.dataAccountsTable = new dynamodb.Table(this, 'DataAccountsTable', {
      tableName: 'dda-portal-data-accounts',
      partitionKey: {
        name: 'data_account_id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.dataAccountsTable.addGlobalSecondaryIndex({
      indexName: 'status-index',
      partitionKey: {
        name: 'status',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'created_at',
        type: dynamodb.AttributeType.NUMBER,
      },
    });

    // Workflows Table - Workflow Manager workflow metadata (Workflow_Store)
    this.workflowsTable = new dynamodb.Table(this, 'WorkflowsTable', {
      tableName: 'dda-portal-workflows',
      partitionKey: {
        name: 'workflow_id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.workflowsTable.addGlobalSecondaryIndex({
      indexName: 'usecase-workflows-index',
      partitionKey: {
        name: 'usecase_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'created_at',
        type: dynamodb.AttributeType.NUMBER,
      },
    });

    // WorkflowVersions Table - immutable per-save workflow versions
    this.workflowVersionsTable = new dynamodb.Table(this, 'WorkflowVersionsTable', {
      tableName: 'dda-portal-workflow-versions',
      partitionKey: {
        name: 'workflow_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'version',
        type: dynamodb.AttributeType.NUMBER,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // TestDatasets Table - canned sample-input datasets for workflow test runs
    this.testDatasetsTable = new dynamodb.Table(this, 'TestDatasetsTable', {
      tableName: 'dda-portal-test-datasets',
      partitionKey: {
        name: 'dataset_id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.testDatasetsTable.addGlobalSecondaryIndex({
      indexName: 'usecase-datasets-index',
      partitionKey: {
        name: 'usecase_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'created_at',
        type: dynamodb.AttributeType.NUMBER,
      },
    });

    // TestRuns Table - Workflow_Test_Runner execution records
    this.testRunsTable = new dynamodb.Table(this, 'TestRunsTable', {
      tableName: 'dda-portal-test-runs',
      partitionKey: {
        name: 'test_run_id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.testRunsTable.addGlobalSecondaryIndex({
      indexName: 'workflow-runs-index',
      partitionKey: {
        name: 'workflow_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'started_at',
        type: dynamodb.AttributeType.NUMBER,
      },
    });

    // WorkflowChatSessions Table - prompt-based generation chat sessions (TTL'd)
    this.workflowChatSessionsTable = new dynamodb.Table(this, 'WorkflowChatSessionsTable', {
      tableName: 'dda-portal-workflow-chat-sessions',
      partitionKey: {
        name: 'session_id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // CameraRegistry Table - per-device Camera_Source inventory synced from
    // edge devices (camera-registry-sync). One partition per device (thing
    // name) with item-type-prefixed sort keys:
    //   CAMERA#{camera_source_id} — camera source entries with sync metadata
    //   META                      — device meta (last_report_at, never_synced)
    //   CONFLICT#{ts}#{uuid}      — conflict events, co-located with the device
    this.cameraRegistryTable = new dynamodb.Table(this, 'CameraRegistryTable', {
      tableName: 'dda-portal-camera-registry',
      partitionKey: {
        name: 'device_id',
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

    // Use_Case-scoped listings and authorization checks (every item carries
    // the usecase_id of its device).
    this.cameraRegistryTable.addGlobalSecondaryIndex({
      indexName: 'usecase-index',
      partitionKey: {
        name: 'usecase_id',
        type: dynamodb.AttributeType.STRING,
      },
    });

    // DeviceRegistrations Table - pending-station registrations for Station
    // Quick Setup (station-quick-setup). One partition per registration
    // (registration_id) holding the device name, Device_Group, Use_Case,
    // Setup_Status, and Setup_Token metadata (hash only). The same table
    // also holds invalid-token rate-limit counters under synthetic
    // `RATELIMIT#{source_ip}` partition keys. The `ttl` time-to-live
    // attribute is set ONLY on RATELIMIT# items so they auto-expire;
    // registration items never carry `ttl` and are retained.
    this.deviceRegistrationsTable = new dynamodb.Table(this, 'DeviceRegistrationsTable', {
      tableName: 'dda-portal-device-registrations',
      partitionKey: {
        name: 'registration_id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Use_Case-scoped listings and device-name uniqueness checks
    // (Requirements 1.1, 1.3): query registrations by Use_Case, keyed by
    // device name.
    this.deviceRegistrationsTable.addGlobalSecondaryIndex({
      indexName: 'usecase-device-index',
      partitionKey: {
        name: 'usecase_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'device_name',
        type: dynamodb.AttributeType.STRING,
      },
    });

    // LabelingTeams Table - DDA labeling teams (dda-data-labeling). Single-table
    // layout: one partition per team (team_id) with item-type-prefixed sort keys:
    //   META             — team metadata (usecase_id, team_name, created_at, created_by)
    //   MEMBER#{user_id} — team member entries (user_id, email, added_at, added_by)
    this.labelingTeamsTable = new dynamodb.Table(this, 'LabelingTeamsTable', {
      tableName: 'dda-portal-labeling-teams',
      partitionKey: {
        name: 'team_id',
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

    // Use_Case-scoped team listings and per-use-case name uniqueness checks
    // (only META items carry usecase_id/created_at, so the index holds META
    // items only).
    this.labelingTeamsTable.addGlobalSecondaryIndex({
      indexName: 'usecase-teams-index',
      partitionKey: {
        name: 'usecase_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'created_at',
        type: dynamodb.AttributeType.NUMBER,
      },
    });

    // LabelingTasks Table - DDA labeling task assignments (dda-data-labeling).
    // One item per (job, image): task_id is 'task-<zero-padded index>';
    // assignee_user_id is a Cognito sub, 'UNASSIGNED', or 'AUTO' (skip-verification).
    // Also holds Prompt_Tuning_Preview run state (llm-autolabel-prompt-tuning):
    // 'PREVIEW#{run_id}' run/sample items and 'PREVIEWLOCK#{usecase_id}'
    // in-flight locks, which carry a 'ttl'. TTL is cleanup only — expiry
    // correctness stays with the explicit 'expires_at' comparisons in the
    // conditional lock claim and in reads (Req 8.8).
    this.labelingTasksTable = new dynamodb.Table(this, 'LabelingTasksTable', {
      tableName: 'dda-portal-labeling-tasks',
      partitionKey: {
        name: 'job_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'task_id',
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Labeler-scoped task lookups: a labeler's assignments across jobs
    // (GET /labeler/jobs) and per-job unsubmitted counts.
    this.labelingTasksTable.addGlobalSecondaryIndex({
      indexName: 'assignee-index',
      partitionKey: {
        name: 'assignee_user_id',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'job_id',
        type: dynamodb.AttributeType.STRING,
      },
    });

    // Portal Artifacts Bucket - stores shared component artifacts (dda-LocalServer)
    // Note: For cross-account Greengrass component access, we use the GDK component bucket
    // (dda-component-{region}-{account}) which is configured with cross-account access
    // via the gdk-component-build-and-publish.sh script.
    // Browser-based test-dataset uploads (Workflow_Test_Runner) go directly
    // to this bucket via presigned multipart URLs, so the bucket needs CORS
    // for the portal origin. The CloudFront domain is passed via the same
    // `cloudFrontDomain` CDK context the compute stack uses; before the
    // first frontend deployment (domain unknown) any origin is allowed —
    // access is still gated by the presigned URLs themselves.
    const corsCloudFrontDomain = this.node.tryGetContext('cloudFrontDomain');
    this.portalArtifactsBucket = new s3.Bucket(this, 'PortalArtifactsBucket', {
      bucketName: `dda-portal-artifacts-${this.account}-${this.region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      versioned: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      cors: [
        {
          allowedOrigins: corsCloudFrontDomain
            ? [`https://${corsCloudFrontDomain}`]
            : ['*'],
          allowedMethods: [
            s3.HttpMethods.GET,
            s3.HttpMethods.PUT,
            s3.HttpMethods.POST,
            s3.HttpMethods.HEAD,
          ],
          allowedHeaders: ['*'],
          // Multipart uploads read each part's ETag from the response.
          exposedHeaders: ['ETag'],
          maxAge: 3600,
        },
      ],
      lifecycleRules: [
        {
          // 'labeling-previews/' holds ephemeral Prompt_Tuning_Preview result
          // payloads (llm-autolabel-prompt-tuning, Req 1.6/3.5) — they are not
          // pipeline Pre_Label artifacts and are referenced by no Labeling_Job,
          // so they are expired a day after a preview run writes them.
          id: 'ExpireLabelingPreviews',
          prefix: 'labeling-previews/',
          enabled: true,
          expiration: cdk.Duration.days(1),
          noncurrentVersionExpiration: cdk.Duration.days(1),
        },
      ],
    });

    // Outputs
    new cdk.CfnOutput(this, 'UseCasesTableName', {
      value: this.useCasesTable.tableName,
      description: 'UseCases DynamoDB Table Name',
    });

    new cdk.CfnOutput(this, 'UserRolesTableName', {
      value: this.userRolesTable.tableName,
      description: 'UserRoles DynamoDB Table Name',
    });

    new cdk.CfnOutput(this, 'DevicesTableName', {
      value: this.devicesTable.tableName,
      description: 'Devices DynamoDB Table Name',
    });

    new cdk.CfnOutput(this, 'AuditLogTableName', {
      value: this.auditLogTable.tableName,
      description: 'AuditLog DynamoDB Table Name',
    });

    new cdk.CfnOutput(this, 'LabelingJobsTableName', {
      value: this.labelingJobsTable.tableName,
      description: 'LabelingJobs DynamoDB Table Name',
    });

    new cdk.CfnOutput(this, 'TrainingJobsTableName', {
      value: this.trainingJobsTable.tableName,
      description: 'TrainingJobs DynamoDB Table Name',
    });

    new cdk.CfnOutput(this, 'ModelsTableName', {
      value: this.modelsTable.tableName,
      description: 'Models DynamoDB Table Name',
    });

    new cdk.CfnOutput(this, 'DeploymentsTableName', {
      value: this.deploymentsTable.tableName,
      description: 'Deployments DynamoDB Table Name',
    });

    new cdk.CfnOutput(this, 'PreLabeledDatasetsTableName', {
      value: this.preLabeledDatasetsTable.tableName,
      description: 'PreLabeledDatasets DynamoDB Table Name',
    });

    new cdk.CfnOutput(this, 'SettingsTableName', {
      value: this.settingsTable.tableName,
      description: 'Settings DynamoDB Table Name',
    });

    new cdk.CfnOutput(this, 'ComponentsTableName', {
      value: this.componentsTable.tableName,
      description: 'Components DynamoDB Table Name',
    });

    new cdk.CfnOutput(this, 'SharedComponentsTableName', {
      value: this.sharedComponentsTable.tableName,
      description: 'SharedComponents DynamoDB Table Name',
    });

    new cdk.CfnOutput(this, 'DataAccountsTableName', {
      value: this.dataAccountsTable.tableName,
      description: 'DataAccounts DynamoDB Table Name',
    });

    new cdk.CfnOutput(this, 'WorkflowsTableName', {
      value: this.workflowsTable.tableName,
      description: 'Workflows DynamoDB Table Name',
    });

    new cdk.CfnOutput(this, 'WorkflowVersionsTableName', {
      value: this.workflowVersionsTable.tableName,
      description: 'WorkflowVersions DynamoDB Table Name',
    });

    new cdk.CfnOutput(this, 'TestDatasetsTableName', {
      value: this.testDatasetsTable.tableName,
      description: 'TestDatasets DynamoDB Table Name',
    });

    new cdk.CfnOutput(this, 'TestRunsTableName', {
      value: this.testRunsTable.tableName,
      description: 'TestRuns DynamoDB Table Name',
    });

    new cdk.CfnOutput(this, 'WorkflowChatSessionsTableName', {
      value: this.workflowChatSessionsTable.tableName,
      description: 'WorkflowChatSessions DynamoDB Table Name',
    });

    new cdk.CfnOutput(this, 'CameraRegistryTableName', {
      value: this.cameraRegistryTable.tableName,
      description: 'CameraRegistry DynamoDB Table Name',
    });

    new cdk.CfnOutput(this, 'DeviceRegistrationsTableName', {
      value: this.deviceRegistrationsTable.tableName,
      description: 'DeviceRegistrations DynamoDB Table Name',
    });

    new cdk.CfnOutput(this, 'LabelingTeamsTableName', {
      value: this.labelingTeamsTable.tableName,
      description: 'LabelingTeams DynamoDB Table Name',
    });

    new cdk.CfnOutput(this, 'LabelingTasksTableName', {
      value: this.labelingTasksTable.tableName,
      description: 'LabelingTasks DynamoDB Table Name',
    });

    new cdk.CfnOutput(this, 'PortalArtifactsBucketName', {
      value: this.portalArtifactsBucket.bucketName,
      description: 'Portal Artifacts S3 Bucket Name for shared components',
    });

    new cdk.CfnOutput(this, 'PortalArtifactsBucketArn', {
      value: this.portalArtifactsBucket.bucketArn,
      description: 'Portal Artifacts S3 Bucket ARN',
    });
  }
}
