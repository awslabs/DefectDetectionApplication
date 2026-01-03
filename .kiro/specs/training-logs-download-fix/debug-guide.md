# Debug Guide: Training Logs Download 500 Error

## CloudWatch Logs to Check

### 1. Training Lambda Function Logs
**Log Group**: `/aws/lambda/EdgeCVPortalComputeStack-TrainingHandler-[RANDOM_ID]`

**What to look for**:
- Error messages from the `download_training_logs` function
- Session name validation errors
- Cross-account role assumption failures
- CloudWatch API call errors

**Sample log search queries**:
```
ERROR download_training_logs
ERROR assume_usecase_role
ValidationError
AccessDenied
```

### 2. API Gateway Access Logs
**Log Group**: `/aws/apigateway/EdgeCVPortalAPI`

**What to look for**:
- 500 status codes for `/training/{id}/logs/download` requests
- Request routing issues
- CORS-related errors

### 3. SageMaker Training Job Logs
**Log Group**: `/aws/sagemaker/TrainingJobs`
**Log Stream**: `{training_job_name}`

**What to check**:
- Whether the training job actually has logs
- If the log stream exists for your training job ID `291626fe-802b-46c5-a533-b69d871b9a2c`

## Common Issues and Solutions

### Issue 1: Session Name Too Long
**Error Pattern**: `ValidationError: roleSessionName failed to satisfy constraint: Member must have length less than or equal to 64`

**Solution**: The session name in `assume_usecase_role` is too long. Current format:
```python
f"training-logs-download-{user_id}-{int(datetime.utcnow().timestamp())}"
```

**Fix**: Shorten to:
```python
f"logs-{user_id[:8]}-{int(datetime.utcnow().timestamp())}"
```

### Issue 2: CloudWatch Logs Not Found
**Error Pattern**: `ResourceNotFoundException` or `No logs available yet`

**Causes**:
- Training job hasn't started yet
- Training job failed before generating logs
- Wrong log group or stream name

### Issue 3: Cross-Account Permission Issues
**Error Pattern**: `AccessDenied` when assuming role

**Check**:
- UseCase account role has CloudWatch Logs permissions
- External ID is correct
- Role trust policy allows assumption

## Immediate Debug Steps

1. **Check Lambda logs** for training ID `291626fe-802b-46c5-a533-b69d871b9a2c`:
```bash
aws logs filter-log-events \
  --log-group-name "/aws/lambda/EdgeCVPortalComputeStack-TrainingHandler-*" \
  --filter-pattern "291626fe-802b-46c5-a533-b69d871b9a2c" \
  --start-time $(date -d '1 hour ago' +%s)000
```

2. **Check if training job has logs**:
```bash
aws logs describe-log-streams \
  --log-group-name "/aws/sagemaker/TrainingJobs" \
  --log-stream-name-prefix "cookie-classification-*"
```

3. **Check API Gateway logs** for 500 errors:
```bash
aws logs filter-log-events \
  --log-group-name "/aws/apigateway/EdgeCVPortalAPI" \
  --filter-pattern "500" \
  --start-time $(date -d '1 hour ago' +%s)000
```

## Quick Fix Implementation

The most likely issue is the session name length. Here's the immediate fix needed in `training.py`:

```python
# Current (problematic):
f"training-logs-download-{user_id}-{int(datetime.utcnow().timestamp())}"

# Fixed (under 64 chars):
f"logs-{user_id[:8]}-{int(datetime.utcnow().timestamp())}"
```

Would you like me to implement this fix right away, or would you prefer to check the CloudWatch logs first to confirm the root cause?