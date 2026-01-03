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
}

/**
 * Stack for Ground Truth labeling workflow.
 * Includes monitoring Lambda and EventBridge rules.
 */
export class LabelingWorkflowStack extends cdk.Stack {
  public readonly monitorFunction: lambda.Function;

  constructor(scope: Construct, id: string, props: LabelingWorkflowStackProps) {
    super(scope, id, props);

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

    // Allow assuming cross-account roles
    this.monitorFunction.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['sts:AssumeRole'],
        resources: ['arn:aws:iam::*:role/DDAPortalAccessRole'],
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
