# Manifest Validation & Model Selection Feature

## Overview

Added two key features to improve training workflow:
1. **Model Selection Dropdown** - Prepare for BYOM (Bring Your Own Model) support
2. **Manifest Validation** - Automatically validate manifests for AWS Marketplace model compatibility

## Changes Made

### Frontend (`CreateTraining.tsx`)

**New State:**
```typescript
const [modelSource, setModelSource] = useState<SelectProps.Option>({
  label: 'AWS Marketplace - Computer Vision Defect Detection',
  value: 'marketplace',
});
```

**New UI Elements:**
1. **Model Source Dropdown:**
   - AWS Marketplace - Computer Vision Defect Detection (active)
   - Bring Your Own Model (BYOM) - Coming soon (disabled)

2. **Manifest Requirements Alert:**
   - Shows when marketplace model is selected
   - Lists required attributes: `source-ref`, `anomaly-label`, `anomaly-label-metadata`
   - Directs users to Manifest Transformer tool if needed

**Updated API Call:**
```typescript
await apiService.createTrainingJob({
  usecase_id: useCaseId.value as string,
  model_source: modelSource.value as string,  // NEW
  model_name: modelName.trim(),
  // ... rest of parameters
});
```

### Backend (`training.py`)

**New Function: `validate_marketplace_manifest()`**

Validates manifest format before training:

**Checks:**
1. **S3 URI format** - Valid s3:// URI
2. **File exists** - Manifest file is accessible
3. **JSON Lines format** - Valid JSONL structure
4. **Required attributes:**
   - `source-ref` (string) - Image S3 URI
   - `anomaly-label` (number) - Label value (0 or 1)
   - `anomaly-label-metadata` (object) - Label metadata
   - For segmentation: `anomaly-mask-ref`, `anomaly-mask-ref-metadata`

**Detection:**
- Automatically detects Ground Truth manifests that need transformation
- Provides helpful error messages with suggestions

**Validation Flow:**
```python
if model_source == 'marketplace':
    validation_result = validate_marketplace_manifest(
        dataset_manifest_s3,
        usecase,
        model_type
    )
    if not validation_result['valid']:
        return create_response(400, {
            'error': 'Manifest validation failed',
            'details': validation_result['errors'],
            'suggestion': 'Use the Manifest Transformer tool...'
        })
```

**Error Response Example:**
```json
{
  "error": "Manifest validation failed",
  "details": [
    "Missing required attributes: anomaly-label, anomaly-label-metadata. This appears to be a Ground Truth manifest that needs transformation."
  ],
  "suggestion": "Use the Manifest Transformer tool to convert your Ground Truth manifest to DDA-compatible format",
  "detected_attributes": ["source-ref", "my-job", "my-job-metadata"]
}
```

## User Workflow

### Happy Path (DDA-Compatible Manifest)

1. User selects **AWS Marketplace** model
2. Sees alert about manifest requirements
3. Provides manifest with correct attributes
4. Training starts successfully

### Transformation Required Path

1. User selects **AWS Marketplace** model
2. Provides Ground Truth manifest (wrong attribute names)
3. Gets validation error with helpful message
4. Goes to **Labeling** → **Manifest Transformer**
5. Transforms manifest
6. Returns to training with transformed manifest URI
7. Training starts successfully

### Future BYOM Path

1. User selects **Bring Your Own Model**
2. No manifest validation (custom format allowed)
3. Provides custom model configuration
4. Training starts with custom model

## Benefits

1. **Early Error Detection** - Catches manifest format issues before training starts
2. **Clear Guidance** - Tells users exactly what's wrong and how to fix it
3. **Cost Savings** - Prevents wasted training time/cost on invalid manifests
4. **Better UX** - Proactive validation vs cryptic SageMaker errors
5. **Future-Proof** - Ready for BYOM support

## Deployment

```bash
# Backend
cd edge-cv-portal/infrastructure
npm run build
rm -rf cdk.out
cdk deploy EdgeCVPortalStack-ComputeStack

# Frontend
cd ../
./deploy-frontend.sh
```

## Testing

### Test Valid Manifest
```json
{"source-ref": "s3://bucket/image.jpg", "anomaly-label": 1, "anomaly-label-metadata": {"class-name": "defect"}}
```

### Test Invalid Manifest (Ground Truth format)
```json
{"source-ref": "s3://bucket/image.jpg", "my-job": 1, "my-job-metadata": {"class-name": "defect"}}
```

Expected: Validation error with transformation suggestion

### Test Missing Attributes
```json
{"source-ref": "s3://bucket/image.jpg"}
```

Expected: Error listing missing attributes

## Related Features

- **Manifest Transformer** (`labeling.py` + `ManifestTransformer.tsx`) - Converts Ground Truth manifests
- **Model Selection** - Foundation for future BYOM support
- **Training Creation** - Enhanced with validation and better error messages

## Future Enhancements

1. **BYOM Support** - Enable custom model training
2. **Batch Validation** - Validate multiple manifest entries
3. **Auto-Fix** - Automatically transform manifests during training creation
4. **Validation Cache** - Cache validation results to avoid repeated checks
