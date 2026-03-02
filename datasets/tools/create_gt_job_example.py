#!/usr/bin/env python3
"""
Example: Create a SageMaker Ground Truth semantic segmentation labeling job.

This script demonstrates the working configuration for creating a GT labeling
job via the API. Customize the constants at the top for your environment.

Prerequisites:
  - S3 bucket with raw images and CORS configured
  - Input manifest (JSONL with source-ref entries) uploaded to S3
  - Private workforce and workteam created
  - IAM execution role with SageMaker + S3 permissions

Usage:
    python3 create_gt_job_example.py --profile my-profile \
        --bucket my-bucket \
        --prefix datasets/my-dataset \
        --role-arn arn:aws:iam::123456789012:role/MySageMakerRole \
        --workteam-name my-labeling-team \
        --label "Defect Class Name"
"""
import argparse
import boto3
import json
from datetime import datetime, timezone


# Default UI template — must be wrapped in <crowd-form>
UI_TEMPLATE = """\
<script src="https://assets.crowd.aws/crowd-html-elements.js"></script>
<crowd-form>
  <crowd-semantic-segmentation
    name="crowd-semantic-segmentation"
    src="{{ task.input.taskObject | grant_read_access }}"
    header="Draw a mask around defect areas in the image."
    labels="{{ task.input.labels | to_json | escape }}"
  >
    <full-instructions header="Segmentation instructions">
      <ol>
        <li><strong>Read</strong> the task carefully and inspect the image.</li>
        <li><strong>Read</strong> the options and review the examples provided.</li>
        <li><strong>Choose</strong> the appropriate label and paint the object.</li>
      </ol>
    </full-instructions>
    <short-instructions>
      <p>Draw a mask around defect areas. If no defects, submit without drawing.</p>
    </short-instructions>
  </crowd-semantic-segmentation>
</crowd-form>"""


def main():
    parser = argparse.ArgumentParser(description="Create a GT semantic segmentation job")
    parser.add_argument("--profile", required=True, help="AWS CLI profile name")
    parser.add_argument("--region", default="us-east-1", help="AWS region")
    parser.add_argument("--bucket", required=True, help="S3 bucket name")
    parser.add_argument("--prefix", required=True, help="S3 prefix for job artifacts")
    parser.add_argument("--input-manifest", default=None,
                        help="S3 key for input manifest (default: {prefix}/input.manifest)")
    parser.add_argument("--role-arn", required=True, help="SageMaker execution role ARN")
    parser.add_argument("--workteam-name", required=True, help="Private workteam name")
    parser.add_argument("--label", required=True, help="Defect label name (e.g. 'Missing Zip Tie')")
    parser.add_argument("--workers", type=int, default=1,
                        help="Number of workers per image (default: 1)")
    parser.add_argument("--task-timeout", type=int, default=600,
                        help="Task time limit in seconds (default: 600)")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    sm = session.client("sagemaker")
    s3 = session.client("s3")
    account = session.client("sts").get_caller_identity()["Account"]

    prefix = args.prefix.strip("/")
    input_manifest = args.input_manifest or f"{prefix}/input.manifest"

    # Upload label category config
    label_config = {
        "document-version": "2018-11-28",
        "labels": [{"label": args.label}],
        "instructions": {
            "shortInstruction": f"<p>Draw a mask around {args.label} areas.</p>",
            "fullInstruction": f"<p>Carefully outline {args.label} regions in the image.</p>",
        },
    }
    label_config_key = f"{prefix}/label-categories.json"
    s3.put_object(
        Bucket=args.bucket,
        Key=label_config_key,
        Body=json.dumps(label_config),
        ContentType="application/json",
    )
    print(f"Uploaded label config: s3://{args.bucket}/{label_config_key}")

    # Upload UI template
    template_key = f"{prefix}/ui-template.liquid.html"
    s3.put_object(
        Bucket=args.bucket,
        Key=template_key,
        Body=UI_TEMPLATE,
        ContentType="text/html",
    )
    print(f"Uploaded UI template: s3://{args.bucket}/{template_key}")

    # Pre/post annotation Lambda ARNs (us-east-1)
    # For other regions, see:
    # https://docs.aws.amazon.com/sagemaker/latest/dg/API_HumanTaskConfig.html
    lambda_account = "432418664414"  # AWS-managed GT Lambda account for us-east-1
    pre_lambda = f"arn:aws:lambda:{args.region}:{lambda_account}:function:PRE-SemanticSegmentation"
    post_lambda = f"arn:aws:lambda:{args.region}:{lambda_account}:function:ACS-SemanticSegmentation"

    workteam_arn = f"arn:aws:sagemaker:{args.region}:{account}:workteam/private-crowd/{args.workteam_name}"

    # Create job
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    job_name = f"gt-seg-{timestamp}"

    response = sm.create_labeling_job(
        LabelingJobName=job_name,
        LabelAttributeName="seg-label-ref",  # must end with -ref for segmentation
        InputConfig={
            "DataSource": {
                "S3DataSource": {
                    "ManifestS3Uri": f"s3://{args.bucket}/{input_manifest}"
                }
            }
        },
        OutputConfig={
            "S3OutputPath": f"s3://{args.bucket}/{prefix}/gt-output/"
        },
        RoleArn=args.role_arn,
        HumanTaskConfig={
            "WorkteamArn": workteam_arn,
            "UiConfig": {
                "UiTemplateS3Uri": f"s3://{args.bucket}/{template_key}"
            },
            "PreHumanTaskLambdaArn": pre_lambda,
            "TaskTitle": f"Semantic Segmentation — {args.label}",
            "TaskDescription": f"Draw masks around {args.label} in the images",
            "NumberOfHumanWorkersPerDataObject": args.workers,
            "TaskTimeLimitInSeconds": args.task_timeout,
            "TaskAvailabilityLifetimeInSeconds": 864000,
            "MaxConcurrentTaskCount": 1000,
            "AnnotationConsolidationConfig": {
                "AnnotationConsolidationLambdaArn": post_lambda
            },
        },
        LabelCategoryConfigS3Uri=f"s3://{args.bucket}/{label_config_key}",
    )

    # Get portal URL
    workteam = sm.describe_workteam(WorkteamName=args.workteam_name)
    subdomain = workteam["Workteam"]["SubDomain"]

    print(f"\nLabeling job created: {job_name}")
    print(f"ARN: {response['LabelingJobArn']}")
    print(f"\nLabeling portal: https://{subdomain}")
    print(f"\nMonitor:")
    print(f"  aws sagemaker describe-labeling-job --labeling-job-name {job_name} --profile {args.profile}")


if __name__ == "__main__":
    main()
