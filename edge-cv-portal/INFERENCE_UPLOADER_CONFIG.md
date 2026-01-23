# Inference Uploader Configuration

The Inference Uploader is an optional Greengrass component that automatically uploads inference results (images and metadata) from edge devices to S3.

## Overview

**Status:** Optional (Opt-in)

The Inference Uploader is **disabled by default** and must be explicitly enabled in the UseCase configuration. This allows customers to choose their own data sync strategy based on their requirements.

## When to Enable

Enable the Inference Uploader when you need:
- **Automatic data collection** from edge devices
- **Centralized storage** of inference results for analysis
- **Model retraining** using production data
- **Audit trail** of all inferences
- **Quality monitoring** and drift detection

## When to Disable

Disable (or don't enable) when:
- **Privacy concerns** - Don't want to upload images to cloud
- **Bandwidth constraints** - Limited network connectivity
- **Custom sync logic** - Using your own data pipeline
- **Local-only inference** - Results stay on device
- **Cost optimization** - Reduce S3 storage costs

## Configuration Options

### Enable/Disable

Add to UseCase DynamoDB record:

```json
{
  "enable_inference_uploader": true
}
```

### Upload Interval

Control how frequently data is uploaded:

```json
{
  "inference_uploader_interval_seconds": 300
}
```

**Common Intervals:**
- `10` - Immediate (every 10 seconds)
- `60` - Every minute
- `300` - Every 5 minutes (default)
- `900` - Every 15 minutes
- `1800` - Every 30 minutes
- `3600` - Every hour
- `21600` - Every 6 hours
- `86400` - Daily

### S3 Bucket

Specify custom S3 bucket for uploads:

```json
{
  "inference_uploader_s3_bucket": "my-custom-inference-bucket"
}
```

Default: `dda-inference-results-{account_id}`

### Batch Size

Number of files to upload per batch:

```json
{
  "inference_uploader_batch_size": 100
}
```

Default: `100`

### Local Retention

Days to keep files locally before deletion:

```json
{
  "inference_uploader_retention_days": 7
}
```

Default: `7` days

### Upload Options

Control what gets uploaded:

```json
{
  "inference_uploader_upload_images": true,
  "inference_uploader_upload_metadata": true
}
```

Defaults: Both `true`

## Complete Configuration Example

### Immediate Upload (Real-time)

```json
{
  "usecase_id": "manufacturing-line-1",
  "name": "Manufacturing Line 1",
  "enable_inference_uploader": true,
  "inference_uploader_interval_seconds": 10,
  "inference_uploader_s3_bucket": "production-inference-data",
  "inference_uploader_batch_size": 50,
  "inference_uploader_retention_days": 1,
  "inference_uploader_upload_images": true,
  "inference_uploader_upload_metadata": true
}
```

**Use Case:** Critical production line requiring immediate data upload for real-time monitoring.

### Hourly Batch Upload

```json
{
  "usecase_id": "quality-inspection",
  "name": "Quality Inspection",
  "enable_inference_uploader": true,
  "inference_uploader_interval_seconds": 3600,
  "inference_uploader_batch_size": 500,
  "inference_uploader_retention_days": 7,
  "inference_uploader_upload_images": true,
  "inference_uploader_upload_metadata": true
}
```

**Use Case:** Quality inspection with hourly data sync to reduce bandwidth usage.

### Metadata Only (No Images)

```json
{
  "usecase_id": "privacy-sensitive",
  "name": "Privacy Sensitive",
  "enable_inference_uploader": true,
  "inference_uploader_interval_seconds": 300,
  "inference_uploader_upload_images": false,
  "inference_uploader_upload_metadata": true
}
```

**Use Case:** Privacy-sensitive environment where only inference metadata (predictions, confidence) is uploaded, not images.

### Disabled (Default)

```json
{
  "usecase_id": "local-only",
  "name": "Local Only",
  "enable_inference_uploader": false
}
```

**Use Case:** Local-only inference with no cloud uploads. Customer handles data sync separately.

## Updating Configuration

### Via DynamoDB Console

1. Go to DynamoDB → Tables → `edge-cv-portal-usecases`
2. Find your UseCase item
3. Click **Edit**
4. Add/update the configuration fields
5. Save

### Via AWS CLI

```bash
aws dynamodb update-item \
  --table-name edge-cv-portal-usecases \
  --key '{"usecase_id": {"S": "manufacturing-line-1"}}' \
  --update-expression "SET enable_inference_uploader = :enabled, inference_uploader_interval_seconds = :interval" \
  --expression-attribute-values '{
    ":enabled": {"BOOL": true},
    ":interval": {"N": "300"}
  }'
```

### Via Portal API (Future Enhancement)

In a future release, these settings will be configurable through the Portal UI in the UseCase settings page.

## Deployment Behavior

When creating a deployment:

1. **If `enable_inference_uploader = true`:**
   - InferenceUploader component is automatically included
   - Configuration is applied from UseCase settings
   - Logs show: "Auto-included aws.edgeml.dda.InferenceUploader with interval Xs"

2. **If `enable_inference_uploader = false` or not set:**
   - InferenceUploader component is NOT included
   - Logs show: "InferenceUploader not included - disabled in UseCase configuration"
   - No S3 uploads occur

## S3 Structure

Uploaded files are organized by UseCase and device:

```
s3://dda-inference-results-{account_id}/
├── {usecase_id}/
│   ├── {device_id}/
│   │   ├── images/
│   │   │   ├── 2026-01-23/
│   │   │   │   ├── inference_001.jpg
│   │   │   │   ├── inference_002.jpg
│   │   │   │   └── ...
│   │   └── metadata/
│   │       ├── 2026-01-23/
│   │       │   ├── inference_001.json
│   │       │   ├── inference_002.json
│   │       │   └── ...
```

## Monitoring

### CloudWatch Logs

InferenceUploader logs are available in CloudWatch:

```
/aws/greengrass/UserComponent/aws.edgeml.dda.InferenceUploader
```

### Metrics

Monitor upload activity:
- Files uploaded per interval
- Upload failures
- S3 storage usage
- Network bandwidth

### Alerts

Set up CloudWatch alarms for:
- Upload failures exceeding threshold
- S3 storage exceeding budget
- Network bandwidth spikes

## Cost Considerations

### S3 Storage

- **Standard Storage:** ~$0.023/GB/month
- **Intelligent-Tiering:** Automatic cost optimization
- **Lifecycle Policies:** Auto-delete old data

### Data Transfer

- **Same Region:** Free (device to S3 in same region)
- **Cross-Region:** $0.02/GB

### Example Costs

**Scenario:** 100 devices, 1000 inferences/day, 500KB/image

- Daily data: 100 devices × 1000 images × 0.5MB = 50GB/day
- Monthly data: 50GB × 30 = 1,500GB
- Monthly cost: 1,500GB × $0.023 = $34.50

**Optimization:**
- Upload metadata only: ~$0.50/month (99% savings)
- Hourly batching: Reduce API calls
- Lifecycle policies: Delete after 30 days

## Best Practices

1. **Start Disabled:** Enable only when needed
2. **Test Intervals:** Start with longer intervals, optimize based on needs
3. **Monitor Costs:** Set up billing alerts
4. **Lifecycle Policies:** Auto-delete old data
5. **Metadata First:** Consider metadata-only uploads for privacy
6. **Batch Uploads:** Use longer intervals for bandwidth-constrained environments
7. **Regional Buckets:** Use same region as devices to avoid transfer costs

## Troubleshooting

### InferenceUploader Not Running

**Check:**
1. Is `enable_inference_uploader = true` in UseCase?
2. Was deployment created after enabling?
3. Check CloudWatch logs for errors

### No Files Uploaded

**Check:**
1. Are inferences being generated? (Check LocalServer logs)
2. Is S3 bucket accessible? (Check IAM permissions)
3. Is upload interval too long? (Check configuration)

### High S3 Costs

**Solutions:**
1. Increase upload interval (reduce frequency)
2. Enable metadata-only uploads
3. Add S3 lifecycle policies
4. Reduce batch size

## Migration Guide

### Existing Deployments

If you have existing deployments with InferenceUploader:

1. **To Keep InferenceUploader:**
   ```bash
   aws dynamodb update-item \
     --table-name edge-cv-portal-usecases \
     --key '{"usecase_id": {"S": "your-usecase-id"}}' \
     --update-expression "SET enable_inference_uploader = :enabled" \
     --expression-attribute-values '{ ":enabled": {"BOOL": true} }'
   ```

2. **To Remove InferenceUploader:**
   - Set `enable_inference_uploader = false`
   - Create new deployment (InferenceUploader will be removed)

### New Deployments

All new deployments will NOT include InferenceUploader unless explicitly enabled.

## Related Documentation

- [INFERENCE_UPLOADER_SETUP.md](../INFERENCE_UPLOADER_SETUP.md) - Setup instructions
- [TUTORIAL_MULTI_ACCOUNT_WORKFLOW.md](TUTORIAL_MULTI_ACCOUNT_WORKFLOW.md) - Multi-account setup
- [ADMIN_GUIDE.md](ADMIN_GUIDE.md) - Portal administration

## Support

For questions or issues:
1. Check CloudWatch logs
2. Review this documentation
3. Contact your AWS support team
