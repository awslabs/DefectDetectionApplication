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

    // Portal Artifacts Bucket - stores shared component artifacts (dda-LocalServer)
    // Cross-account access is handled via IAM policies in usecase accounts
    this.portalArtifactsBucket = new s3.Bucket(this, 'PortalArtifactsBucket', {
      bucketName: `dda-portal-artifacts-${this.account}-${this.region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      versioned: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    
    // Note: Cross-account access for Greengrass devices is granted via:
    // 1. IAM policy created in usecase accounts (DDAPortalSharedComponentsAccess)
    // 2. The policy grants s3:GetObject on this bucket's shared-components/* prefix
    // 3. This is done during usecase onboarding in shared_components.py

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
