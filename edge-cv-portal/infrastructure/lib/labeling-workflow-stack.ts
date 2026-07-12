import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export interface LabelingWorkflowStackProps extends cdk.StackProps {
  labelingJobsTable: dynamodb.Table;
  useCasesTable: dynamodb.Table;
  sharedLayer: lambda.LayerVersion;
  /**
   * Trusted UseCase account IDs the labeling monitor is allowed to assume
   * `DDAPortalAccessRole` into. Sourced from CDK context
   * (`-c trustedUseCaseAccountIds=111111111111,222222222222`) or a
   * deployment-time SSM parameter (`/dda-portal/trusted-usecase-account-ids`).
   * Must be non-empty — an empty list is a synth-time error (I5); the design
   * DOES NOT fall back to a wildcard account.
   */
  trustedUseCaseAccountIds: string[];
}

/**
 * Stack for Ground Truth labeling workflow.
 * Includes monitoring Lambda and EventBridge rules.
 */
export class LabelingWorkflowStack extends cdk.Stack {
  public readonly monitorFunction: lambda.Function;

  constructor(scope: Construct, id: string, props: LabelingWorkflowStackProps) {
    super(scope, id, props);

    // Validate the trusted UseCase account list at synth time. An empty list
    // would otherwise produce an empty sts:AssumeRole resource list; the
    // design requires an explicit failure rather than any fallback to a
    // wildcard account (I5).
    if (!props.trustedUseCaseAccountIds || props.trustedUseCaseAccountIds.length === 0) {
      throw new Error(
        'LabelingWorkflowStack requires a non-empty trustedUseCaseAccountIds list ' +
          '(pass -c trustedUseCaseAccountIds=<id>,<id> or the SSM parameter ' +
          '/dda-portal/trusted-usecase-account-ids). Refusing to synth an ' +
          'sts:AssumeRole grant on a wildcard account.'
      );
    }

    // Labeling Monitor Lambda Function
    this.monitorFunction = new lambda.Function(this, 'LabelingMonitorFunction', {
      functionName: 'EdgeCVPortal-LabelingMonitor',
      runtime: lambda.Runtime.PYTHON_3_9,
      handler: 'labeling_monitor.handler',
      code: lambda.Code.fromAsset('backend/functions'),
      layers: [props.sharedLayer],
      timeout: cdk.Duration.minutes(5),
      environment: {
        LABELING_JOBS_TABLE: props.labelingJobsTable.tableName,
        USECASES_TABLE: props.useCasesTable.tableName,
      },
    });

    // Grant permissions
    props.labelingJobsTable.grantReadWriteData(this.monitorFunction);
    props.useCasesTable.grantReadData(this.monitorFunction);

    // Allow assuming cross-account roles. The role name DDAPortalAccessRole
    // stays fixed; the account portion is bounded to the trusted UseCase
    // account list at synth time (no arn:aws:iam::*:role/ wildcard) (I5).
    this.monitorFunction.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['sts:AssumeRole'],
        resources: props.trustedUseCaseAccountIds.map(
          (id) => `arn:aws:iam::${id}:role/DDAPortalAccessRole`
        ),
      })
    );

    // EventBridge Rule: Schedule to monitor all InProgress jobs every 5 minutes
    const scheduleRule = new events.Rule(this, 'LabelingMonitorSchedule', {
      ruleName: 'EdgeCVPortal-LabelingMonitorSchedule',
      description: 'Monitor Ground Truth labeling jobs every 5 minutes',
      schedule: events.Schedule.rate(cdk.Duration.minutes(5)),
    });

    scheduleRule.addTarget(new targets.LambdaFunction(this.monitorFunction));

    // EventBridge Rule: SageMaker Labeling Job State Changes
    const stateChangeRule = new events.Rule(this, 'LabelingJobStateChange', {
      ruleName: 'EdgeCVPortal-LabelingJobStateChange',
      description: 'Capture SageMaker Ground Truth job state changes',
      eventPattern: {
        source: ['aws.sagemaker'],
        detailType: ['SageMaker Labeling Job State Change'],
      },
    });

    stateChangeRule.addTarget(new targets.LambdaFunction(this.monitorFunction));

    // Outputs
    new cdk.CfnOutput(this, 'MonitorFunctionArn', {
      value: this.monitorFunction.functionArn,
      description: 'ARN of the labeling monitor Lambda function',
    });
  }
}
