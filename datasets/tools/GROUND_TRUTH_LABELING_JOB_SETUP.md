# Setting Up a SageMaker Ground Truth Semantic Segmentation Labeling Job

## Summary

This document captures the findings from setting up a Ground Truth semantic segmentation
labeling job via the API for the DDA pipeline. Multiple issues were encountered and resolved
around UI templates, IAM roles, CORS configuration, bucket policies, and browser extensions.

## Prerequisites

- An S3 bucket containing raw images to label
- A SageMaker private workforce and workteam
- An IAM execution role with SageMaker and S3 permissions
- An input manifest (JSONL) with `source-ref` entries pointing to each image

## Working Configuration

The following configuration was validated end-to-end and successfully labeled 83 images.

### IAM Execution Role

The role must have:
- Trust policy allowing `sagemaker.amazonaws.com` to assume it
- Trust policy allowing the GT Lambda account (`432418664414` for us-east-1) to assume it
- S3 permissions (`GetObject`, `PutObject`, `ListBucket`) on the input/output buckets
- The `AmazonSageMakerGroundTruthExecution` managed policy (recommended)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "sagemaker.amazonaws.com" },
      "Action": "sts:AssumeRole"
    },
    {
      "Effect": "Allow",
      "Principal": { "AWS": "arn:aws:iam::432418664414:root" },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

The account `432418664414` is the AWS-managed account that hosts the built-in Ground Truth
pre-annotation and annotation-consolidation Lambda functions in `us-east-1`. This is not
your account — it is part of the SageMaker Ground Truth service infrastructure and is the
same for all AWS customers in that region.

### S3 CORS Configuration

The bucket containing the source images **must** have CORS configured. Without it, the
labeling portal shows "No CORS Configuration Detected" and images fail to load.

The CORS configuration that works (matches AWS documentation):

```json
[
  {
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["GET", "HEAD", "PUT"],
    "AllowedOrigins": ["*"],
    "ExposeHeaders": ["Access-Control-Allow-Origin"],
    "MaxAgeSeconds": 3000
  }
]
```

Apply with:
```bash
aws s3api put-bucket-cors --bucket YOUR_BUCKET --cors-configuration '{
  "CORSRules": [
    {
      "AllowedHeaders": ["*"],
      "AllowedMethods": ["GET", "HEAD", "PUT"],
      "AllowedOrigins": ["*"],
      "ExposeHeaders": ["Access-Control-Allow-Origin"],
      "MaxAgeSeconds": 3000
    }
  ]
}'
```

### UI Template

Semantic segmentation jobs **require** a custom UI template — there is no built-in
`HumanTaskUiArn` for this task type.

The template **must** be wrapped in `<crowd-form>`. Without it, the submit button throws
a JavaScript error: `Cannot read properties of undefined (reading 'submit')`.

Working template:

```html
<script src="https://assets.crowd.aws/crowd-html-elements.js"></script>
<crowd-form>
  <crowd-semantic-segmentation
    name="crowd-semantic-segmentation"
    src="{{ task.input.taskObject | grant_read_access }}"
    header="Please segment the defects in this image."
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
</crowd-form>
```

Key points:
- `name` must be `"crowd-semantic-segmentation"` (the standard name the crowd element expects)
- `labels` uses `{{ task.input.labels | to_json | escape }}` to pull labels from the label
  category config via the pre-annotation Lambda
- The `<crowd-form>` wrapper is **mandatory** — this is what the crowd JS uses to find the
  submit handler

### Label Category Config

```json
{
  "document-version": "2018-11-28",
  "labels": [
    { "label": "Missing Zip Tie" }
  ],
  "instructions": {
    "shortInstruction": "<p>Draw a mask around defect areas.</p>",
    "fullInstruction": "<p>Carefully outline defect regions in the image.</p>"
  }
}
```

### LabelAttributeName

For semantic segmentation, the `LabelAttributeName` **must** end with `-ref`.
Using a name without `-ref` (e.g., `gt-eval-seg-label`) produces a validation error:

```
The value of the LabelAttributeName field for the Semantic Segmentation task type
must end with '-ref'.
```

### Pre/Post Annotation Lambdas

For `us-east-1`, use the built-in Lambda ARNs:
- Pre-annotation: `arn:aws:lambda:us-east-1:432418664414:function:PRE-SemanticSegmentation`
- Consolidation: `arn:aws:lambda:us-east-1:432418664414:function:ACS-SemanticSegmentation`

These are AWS-managed and the same for all customers in the region.

### Input Manifest Format

One JSON object per line with `source-ref` pointing to each image:

```json
{"source-ref": "s3://my-bucket/images/image-001.jpg"}
{"source-ref": "s3://my-bucket/images/image-002.jpg"}
```

### Working Python Script

See `create_gt_job_example.py` in this directory for a complete working example.

## Issues Encountered and Resolutions

### 1. Submit Button JavaScript Error

**Error**: `Cannot read properties of undefined (reading 'submit')`

**Cause**: The `<crowd-semantic-segmentation>` element was not wrapped in `<crowd-form>`.

**Fix**: Wrap the entire crowd element in `<crowd-form>...</crowd-form>`.

### 2. LabelAttributeName Validation Error

**Error**: `The value of the LabelAttributeName field for the Semantic Segmentation task type must end with '-ref'.`

**Cause**: Used `gt-eval-seg-label` as the label attribute name.

**Fix**: Changed to `gt-eval-seg-ref` (must end with `-ref` for segmentation).

### 3. Role Trust Policy Error

**Error**: `The role ARN isn't valid. Make sure the role exists and that its trust relationship policy allows the action "sts:AssumeRole" for the service principal "sagemaker.amazonaws.com".`

**Cause**: Used a role whose trust policy didn't include `sagemaker.amazonaws.com`.

**Fix**: Used `DDASageMakerExecutionRole` which has the correct trust policy.

### 4. No CORS Configuration Detected

**Error**: Portal shows "No CORS Configuration Detected" and images don't load.

**Cause**: The S3 bucket containing source images had no CORS configuration.

**Fix**: Added CORS configuration to the bucket (see above).

### 5. 403 Forbidden on Image Load

**Error**: Browser shows 403 when the labeling UI tries to load images.

**Cause**: Multiple potential causes encountered:
- Bucket policy with service principal (`sagemaker.amazonaws.com`) combined with
  `RestrictPublicBuckets=true` in S3 Block Public Access settings. The block public
  access feature treats service principal grants as "public" and blocks them.
- Images in a different bucket than the one the GT job was originally configured for.

**Fix**: Removed the bucket policy entirely (the IAM role's inline policy already had
`s3:*` on `arn:aws:s3:::*`). Used the same bucket for images and GT output. The
`grant_read_access` Liquid filter generates presigned URLs using the execution role,
so IAM-level permissions are sufficient — no bucket policy needed.

### 6. Browser Extension Interference

**Error**: `window["__f__mlubw4xi.hbn"]` errors from `Hi-Beam.user.js` and CSP violations
blocking `eval()`.

**Cause**: The "Hi-Beam" Firefox browser extension injects JavaScript into the page,
conflicting with the Ground Truth labeling UI's Content Security Policy.

**Fix**: Disable the extension (`about:addons` → Extensions → toggle off Hi-Beam) or
use a private/incognito window where extensions are disabled by default.

### 7. Tasks Not Appearing in Portal

**Observation**: After creating a job, tasks may take 1-2 minutes to appear in the
labeling portal. The portal may also show no tasks between batches if the worker has
completed a batch and the next one hasn't been queued yet.

**Workaround**: Wait and refresh. Check job status via CLI:
```bash
aws sagemaker describe-labeling-job \
  --labeling-job-name JOB_NAME \
  --query "{Status:LabelingJobStatus,Labeled:LabelCounters.HumanLabeled,Unlabeled:LabelCounters.Unlabeled}" \
  --output table
```

## Private Workforce Setup

### Adding a Worker to the Labeling Portal

The labeling portal uses Amazon Cognito for authentication. To add a new worker:

```bash
# Find the Cognito user pool ID from the workteam
aws sagemaker describe-workteam --workteam-name YOUR_TEAM \
  --query "Workteam.MemberDefinitions[0].CognitoMemberDefinition.UserPool" \
  --output text

# Create the user (sends temporary password via email)
aws cognito-idp admin-create-user \
  --user-pool-id USER_POOL_ID \
  --username "user@example.com" \
  --user-attributes Name=email,Value="user@example.com" Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL

# Add user to the labeling group
aws cognito-idp admin-add-user-to-group \
  --user-pool-id USER_POOL_ID \
  --username "user@example.com" \
  --group-name "sagemaker-groundtruth-user-group"
```

The user will receive a temporary password by email and must set a new password on first login.

### Finding the Portal URL

```bash
aws sagemaker describe-workteam --workteam-name YOUR_TEAM \
  --query "Workteam.SubDomain" --output text
# Returns: xxxxx.labeling.REGION.sagemaker.aws
# Portal URL: https://xxxxx.labeling.REGION.sagemaker.aws
```
