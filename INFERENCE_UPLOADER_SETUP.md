# Inference Results S3 Upload Feature

## Overview

The Inference Uploader component automatically uploads inference results (images and metadata) from edge devices to S3 for centralized storage, analysis, and monitoring.

## Architecture

### Components

1. **Greengrass Component**: `aws.edgeml.dda.InferenceUploader`
   - Platform-independent (works on all architectures)
   - Monitors `/aws_dda/inference-results/` directory
   - Uploads images (.jpg, .png) and metadata (.jsonl) to S3
   - Manages local file retention and cleanup

2. **S3 Bucket**: `dda-inference-results-{account-id}`
   - Created in each UseCase Account
   - Lifecycle policies: 30 days → IA, 90 days → delete
   - Encrypted with S3-managed keys
   - Block all public access

3. **IAM Permissions**:
   - Devices: Upload permissions via `DDAPortalComponentAccessPolicy`
   - Portal: Read permissions via `DDAPortalAccessRole`

### Data Flow

```
Edge Device
  └─> /aws_dda/inference-results/{model-id}/
       ├─> {event-id}.jpg (1.1MB)
       └─> {event-id}.jsonl (1.2KB)
            └─> InferenceUploader Component
                 └─> S3: s3://dda-inference-results-{account}/
                          {usecase-id}/{device-id}/{model-id}/YYYY/MM/DD/
                           ├─> {event-id}.jpg
                           └─> {event-id}.jsonl
```

## Setup Instructions

### Step 1: Build and Publish Component (Portal Account)

```bash
cd DefectDetectionApplication
./build-inference-uploader.sh
```

This will:
- Upload artifacts to `s3://dda-component-{region}-{portal-account}/`
- Create component version in Greengrass
- Tag component as `dda-portal:managed=true`

### Step 2: Deploy Infrastructure (UseCase Account)

```bash
cd edge-cv-portal/infrastructure
npm run build
rm -rf cdk.out
cdk deploy EdgeCVPortalStack-UseCaseAccountStack --context usecase_account_id=198226511894
```

This creates:
- S3 bucket: `dda-inference-results-198226511894`
- IAM permissions for devices to upload
- IAM permissions for portal to read

### Step 3: Provision Shared Components

The InferenceUploader component is automatically provisioned to usecase accounts along with LocalServer:

1. **For New Usecases**: Automatically provisioned during onboarding
2. **For Existing Usecases**: Use "Update All Usecases" button in portal

Or via API:
```bash
POST /api/v1/shared-components/provision
{
  "usecase_id": "your-usecase-id"
}
```

### Step 4: Deploy to Devices

Create a deployment in the portal with:

**Required Components**:
- `aws.edgeml.dda.LocalServer.{arch}` (already required)
- `aws.edgeml.dda.InferenceUploader` v1.0.0 (NEW)
- Model component (e.g., `model-cookie-defect-detection-arm64-cpu`)

**Component Configuration** for `aws.edgeml.dda.InferenceUploader`:
```json
{
  "s3Bucket": "dda-inference-results-198226511894",
  "s3Prefix": "{usecase-id}/{device-id}",
  "uploadIntervalSeconds": 300,
  "batchSize": 100,
  "localRetentionDays": 7,
  "uploadImages": true,
  "uploadMetadata": true,
  "inferenceResultsPath": "/aws_dda/inference-results",
  "awsRegion": "us-east-1"
}
```

## Configuration Options

| Parameter | Default | Description |
|-----------|---------|-------------|
| `uploadIntervalSeconds` | 300 | Upload batch interval (5 minutes) |
| `batchSize` | 100 | Max files per batch |
| `localRetentionDays` | 7 | Keep local files for N days after upload |
| `uploadImages` | true | Upload .jpg/.png files |
| `uploadMetadata` | true | Upload .jsonl/.json files |
| `inferenceResultsPath` | `/aws_dda/inference-results` | Local directory to monitor |
| `s3Bucket` | (required) | Target S3 bucket |
| `s3Prefix` | (optional) | S3 key prefix for organization |
| `awsRegion` | `us-east-1` | AWS region |

## S3 Structure

```
s3://dda-inference-results-{account}/
  └─> {usecase-id}/
       └─> {device-id}/
            └─> {model-id}/
                 └─> YYYY/MM/DD/
                      ├─> {event-id}.jpg
                      └─> {event-id}.jsonl
```

Example:
```
s3://dda-inference-results-198226511894/
  └─> cookie-factory/
       └─> dda_edge_server_2/
            └─> cc3ke9hm/
                 └─> 2026/01/19/
                      ├─> cc3ke9hm-a20e6456a4ed44649c3dc48da1a5a8a7.jpg
                      └─> cc3ke9hm-a20e6456a4ed44649c3dc48da1a5a8a7.jsonl
```

## Metadata Format

Each `.jsonl` file contains:
```json
{
  "deviceGroundTruthData": [{
    "source-ref": "/aws_dda/inference-results/...",
    "anomaly-label-detected": 1,
    "anomaly-label-detected-metadata": {
      "class-name": "Anomaly",
      "creation-date": "2026-01-19T18:25:51",
      "confidence": 0.9375,
      "type": "groundtruth/image-classification"
    }
  }],
  "eventMetadata": {
    "eventId": "cc3ke9hm-a20e6456...",
    "modelName": "model-cookie-defect-detection-arm64-cpu",
    "modelVersion": "1",
    "inferenceTime": "2026-01-19T18:25:51"
  }
}
```

## Monitoring

### Component Logs

View logs in CloudWatch:
```
/aws/greengrass/UserComponent/{region}/{device-name}/aws.edgeml.dda.InferenceUploader
```

### Upload Status

The component maintains state in:
```
/aws_dda/.inference_uploader_state.json
```

This tracks uploaded files to avoid duplicates.

### Metrics

Monitor:
- Upload success/failure rates
- Batch sizes
- Local disk usage
- S3 bucket size

## Troubleshooting

### No files uploading

1. Check component is running:
   ```bash
   sudo /aws_dda/greengrass/v2/bin/greengrass-cli component list
   ```

2. Check logs:
   ```bash
   sudo tail -f /aws_dda/greengrass/v2/logs/aws.edgeml.dda.InferenceUploader.log
   ```

3. Verify IAM permissions:
   - `GreengrassV2TokenExchangeRole` has `DDAPortalComponentAccessPolicy` attached
   - Policy includes S3 upload permissions

### Permission denied errors

Ensure the device's IAM role has:
```json
{
  "Effect": "Allow",
  "Action": [
    "s3:PutObject",
    "s3:PutObjectTagging",
    "s3:GetBucketLocation"
  ],
  "Resource": [
    "arn:aws:s3:::dda-inference-results-{account}",
    "arn:aws:s3:::dda-inference-results-{account}/*"
  ]
}
```

### Files not being cleaned up

Check `localRetentionDays` configuration. Set to 0 to disable cleanup.

## Future Enhancements

1. **Portal UI**: Browse and view uploaded inference results
2. **Analytics Dashboard**: Visualize inference metrics over time
3. **Alerts**: Notify on anomaly detection patterns
4. **Batch Processing**: Trigger SageMaker jobs on uploaded data
5. **Compression**: Optional gzip compression before upload
6. **Filtering**: Upload only anomalies or specific confidence ranges

## Related Files

- Component: `DefectDetectionApplication/inference-uploader/`
- Build Script: `DefectDetectionApplication/build-inference-uploader.sh`
- Infrastructure: `edge-cv-portal/infrastructure/lib/usecase-account-stack.ts`
- Backend: `edge-cv-portal/backend/functions/shared_components.py`
