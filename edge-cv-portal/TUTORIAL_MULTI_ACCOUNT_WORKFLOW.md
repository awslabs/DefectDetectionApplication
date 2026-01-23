# Tutorial: Multi-Account ML Workflow with DDA Portal

A complete step-by-step guide to label data, train a model, and deploy to edge devices using the DDA Portal with a multi-account architecture.

## Overview

This tutorial walks through the complete workflow using:
- **Portal Account** (`164152369890`) - Hosts the DDA Portal
- **UseCase Account** (`198226511894`) - Runs SageMaker training, Greengrass devices, stores outputs
- **Data Account** (`814373574263`) - Stores training images (input data)

**S3 Bucket Strategy:**
- `dda-alien-bucket` (Data Account) - Training images and datasets
- `dda-alien-output` (UseCase Account) - SageMaker outputs, models, manifests

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         WORKFLOW OVERVIEW                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  1. SETUP (One-time)                                                        │
│     ├── Deploy Portal Account infrastructure                                │
│     ├── Deploy UseCase Account role                                         │
│     ├── Deploy Data Account role                                            │
│     └── Create UseCase in Portal                                            │
│                                                                              │
│  2. DATA PREPARATION                                                        │
│     ├── Upload images to Data Account S3                                    │
│     └── Tag bucket for portal discovery                                     │
│                                                                              │
│  3. LABELING                                                                │
│     ├── Create labeling job in Portal                                       │
│     ├── Label images using Ground Truth UI                                  │
│     └── Wait for job completion                                             │
│                                                                              │
│  4. TRAINING                                                                │
│     ├── Create training job using labeled dataset                           │
│     ├── Monitor training progress                                           │
│     └── Review model metrics                                                │
│                                                                              │
│  5. COMPILATION & PACKAGING                                                 │
│     ├── Compile model for target architecture (ARM64/x86)                   │
│     ├── Package as Greengrass component                                     │
│     └── Publish to Greengrass                                               │
│                                                                              │
│  6. DEPLOYMENT                                                              │
│     ├── Create deployment targeting edge device                             │
│     └── Monitor deployment status                                           │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Prerequisites

- AWS CLI configured with credentials for each account
- Node.js 18+, Python 3.11+
- AWS CDK: `npm install -g aws-cdk`
- Access to all three AWS accounts

---

## Part 1: One-Time Setup

### Step 1.1: Deploy Portal Account Infrastructure

In the **Portal Account** (`164152369890`):

```bash
cd edge-cv-portal

# Build shared Lambda layer
cd backend/layers/shared && ./build.sh && cd ../../..

# Deploy infrastructure
cd infrastructure
npm install
npm run build
rm -rf cdk.out
cdk bootstrap  # Only needed once
cdk deploy --all --require-approval never
```

Save the outputs:
- `ApiUrl` - e.g., `https://abc123.execute-api.us-east-1.amazonaws.com/v1`
- `UserPoolId` - e.g., `us-east-1_jBJ4LzuQ8`
- `UserPoolClientId` - e.g., `1abc2def3ghi4jkl5mno`
- `DistributionDomainName` - e.g., `d3qeryypza4i9i.cloudfront.net`

### Step 1.2: Configure and Deploy Frontend

```bash
cd ../frontend
npm install

# Create config with your CDK outputs
cat > public/config.json << 'EOF'
{
  "apiUrl": "YOUR_API_URL",
  "userPoolId": "YOUR_USER_POOL_ID",
  "userPoolClientId": "YOUR_CLIENT_ID",
  "region": "us-east-1"
}
EOF

npm run build
cd ..
./deploy-frontend.sh
```

### Step 1.3: Create Admin User

```bash
USER_POOL_ID="YOUR_USER_POOL_ID"

# Create user
aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username admin \
  --user-attributes Name=email,Value=admin@company.com \
  --temporary-password TempPass123!

# Set permanent password
aws cognito-idp admin-set-user-password \
  --user-pool-id $USER_POOL_ID \
  --username admin \
  --password YourSecurePassword123! \
  --permanent

# Get user sub for role assignment
USER_SUB=$(aws cognito-idp admin-get-user \
  --user-pool-id $USER_POOL_ID \
  --username admin \
  --query 'UserAttributes[?Name==`sub`].Value' --output text)

# Assign PortalAdmin role
aws dynamodb put-item \
  --table-name edge-cv-portal-user-roles \
  --item "{
    \"user_id\": {\"S\": \"$USER_SUB\"},
    \"usecase_id\": {\"S\": \"global\"},
    \"role\": {\"S\": \"PortalAdmin\"},
    \"assigned_at\": {\"N\": \"$(date +%s)000\"},
    \"assigned_by\": {\"S\": \"system\"}
  }"
```

### Step 1.4: Deploy UseCase Account Role

Switch to **UseCase Account** (`198226511894`) credentials:

```bash
cd edge-cv-portal
./deploy-account-role.sh
```

Select option **1 (UseCase Account)** and enter:
- Portal Account ID: `164152369890`

Save the outputs:
- `RoleArn`: `arn:aws:iam::198226511894:role/DDAPortalAccessRole`
- `ExternalId`: `7B1EA7C8-A279-4F44-9732-E1C912F01272` (example)
- `SageMakerExecutionRoleArn`: `arn:aws:iam::198226511894:role/DDASageMakerExecutionRole`

### Step 1.5: Deploy Data Account Role

Switch to **Data Account** (`814373574263`) credentials:

```bash
cd edge-cv-portal
./deploy-account-role.sh
```

Select option **2 (Data Account)** and enter:
- Portal Account ID: `164152369890`

The script will:
- Generate a secure External ID
- Deploy the IAM role via CloudFormation
- Create a config file: `data-account-814373574263-config.txt`

Save the outputs from the config file:
- `Data Account ID`: `814373574263`
- `Portal Access Role ARN`: `arn:aws:iam::814373574263:role/DDAPortalDataAccessRole`
- `External ID`: (auto-generated UUID)

### Step 1.6: Register Data Account in Portal

1. Open the Portal URL (CloudFront domain from Step 1.1)
2. Login with admin credentials
3. Go to **Settings** → **Data Accounts** tab
4. Click **Add Data Account**

**Option A: Upload Config File (Recommended)**
- Click "Upload Configuration File"
- Select `data-account-814373574263-config.txt` from Step 1.5
- Form fields will auto-fill

**Option B: Manual Entry**
- Data Account ID: `814373574263`
- Name: `Production Data Account`
- Description: `Centralized training data storage`
- Role ARN: `arn:aws:iam::814373574263:role/DDAPortalDataAccessRole`
- External ID: (from config file)
- Region: `us-east-1`

5. Click **Register**

The portal will test the connection and save the Data Account credentials.

### Step 1.7: Subscribe to AWS Marketplace Algorithm

In the **UseCase Account** (`198226511894`):

1. Go to [Computer Vision Defect Detection Model](https://aws.amazon.com/marketplace/pp/prodview-j72hhmlt6avp6)
2. Click **Continue to Subscribe** → **Accept Offer**
3. Wait for activation (~2 minutes)

Verify:
```bash
aws sagemaker list-algorithms --name-contains "computer-vision-defect-detection"
```

### Step 1.8: Create UseCase in Portal

1. Open the Portal URL (CloudFront domain from Step 1.1)
2. Login with admin credentials
3. Go to **Use Cases** → **Create Use Case**
4. Fill in the wizard:

**Basic Information:**
- Name: `Manufacturing Line 1`
- Description: `Defect detection for production line`

**UseCase Account Configuration:**

Upload the `usecase-account-198226511894-config.txt` file from Step 1.4, or enter manually:
- AWS Account ID: `198226511894`
- Cross-Account Role ARN: `arn:aws:iam::198226511894:role/DDAPortalAccessRole`
- External ID: `7B1EA7C8-A279-4F44-9732-E1C912F01272`
- SageMaker Execution Role ARN: `arn:aws:iam::198226511894:role/DDASageMakerExecutionRole`

**S3 Storage Configuration:**

Select: **Separate Data Account** (recommended for enterprise)

**Data Account Credentials:**

**Option A: Select from Dropdown (Recommended)**
- Select: `Production Data Account` (registered in Step 1.6)
- Account ID, Role ARN, and External ID will auto-fill

**Option B: Upload Config File**
- Upload `data-account-814373574263-config.txt`

**Option C: Manual Entry**
- Data Account ID: `814373574263`
- Data Account Role ARN: `arn:aws:iam::814373574263:role/DDAPortalDataAccessRole`
- Data Account External ID: (from config file)

**Data Bucket Configuration:**
- Data S3 Bucket: `dda-alien-bucket` (in Data Account - for training images)
- Data S3 Prefix: `datasets/`

**SageMaker Assets Storage:**
- Select: **Store in UseCase Account bucket** (separate outputs)
- UseCase S3 Bucket: `dda-alien-output` (in UseCase Account - for model outputs)
- UseCase S3 Prefix: `sagemaker/`

5. Click **Create UseCase**

The portal will automatically:
- Configure CORS on the Data Account bucket (`dda-alien-bucket`)
- Update bucket policy for SageMaker to read from Data Account
- Update bucket policy for SageMaker to write to UseCase Account
- Provision shared Greengrass components

---

## Part 2: Data Preparation

### Step 2.1: Create S3 Buckets

**In Data Account** (`814373574263`):
```bash
# Create bucket for training data (input)
aws s3 mb s3://dda-alien-bucket --region us-east-1

# Tag bucket for portal discovery
aws s3api put-bucket-tagging \
  --bucket dda-alien-bucket \
  --tagging 'TagSet=[{Key=dda-portal:managed,Value=true}]'
```

**In UseCase Account** (`619071348270`):
```bash
# Create bucket for SageMaker outputs
aws s3 mb s3://dda-alien-output --region us-east-1

# Create folder structure
aws s3api put-object --bucket dda-alien-output --key sagemaker/
aws s3api put-object --bucket dda-alien-output --key models/
aws s3api put-object --bucket dda-alien-output --key manifests/
```

### Step 2.2: Get Sample Dataset

Clone the Lookout for Vision sample repository:

```bash
git clone https://github.com/aws-samples/amazon-lookout-for-vision.git
cd amazon-lookout-for-vision
```

The alien dataset contains:
- `normal/` - Images of normal toy aliens
- `anomaly/` - Images of defective toy aliens

### Step 2.3: Upload Images to Data Account

Switch to **Data Account** (`814373574263`) credentials:

```bash
# Upload images to Data Account bucket
aws s3 sync ./datasets/alien/normal/ s3://dda-alien-bucket/datasets/alien/normal/
aws s3 sync ./datasets/alien/anomaly/ s3://dda-alien-bucket/datasets/alien/anomaly/
```

### Step 2.4: Verify Data Upload

```bash
# Check normal images
aws s3 ls s3://dda-alien-bucket/datasets/alien/normal/ --summarize

# Check anomaly images
aws s3 ls s3://dda-alien-bucket/datasets/alien/anomaly/ --summarize
```

---

## Part 3: Labeling

### Step 3.1: Create Ground Truth Workteam (One-time)

In the **UseCase Account** (`198226511894`):

1. Go to AWS Console → SageMaker → Ground Truth → Labeling workforces
2. Click **Private** tab → **Create private team**
3. Add team members (email addresses)
4. Save the **Workteam ARN**

### Step 3.2: Create Labeling Job in Portal

1. In the Portal, go to **Labeling** → **Create Labeling Job**
2. Fill in:

**Job Configuration:**
- Job Name: `alien-defect-labeling`
- Task Type: `Bounding Box`

**Dataset:**
- Select the UseCase: `Manufacturing Line 1`
- Browse to: `datasets/alien/` (shows images from Data Account)
- The portal will list images from both `normal/` and `anomaly/` folders

**Labels:**
- Add label: `defect`
- Add label: `normal`

**Workforce:**
- Select your private workteam

3. Click **Create Job**

### Step 3.3: Label Images

1. Team members receive email with labeling portal link
2. Open the Ground Truth labeling UI
3. For each image:
   - Draw bounding box around defects (if any)
   - Select appropriate label (`defect` or `normal`)
4. Submit annotations

### Step 3.4: Monitor Labeling Progress

In the Portal:
1. Go to **Labeling** → Click on your job
2. View progress: labeled count, remaining, completion percentage
3. Wait for job to complete (status: `Completed`)

The output manifest will be saved to:
`s3://dda-alien-output/sagemaker/labeling/alien-defect-labeling/output/output.manifest`

---

## Part 4: Training

### Step 4.1: Create Training Job

1. In the Portal, go to **Training** → **Create Training Job**
2. Fill in:

**Model Configuration:**
- Model Name: `alien-defect-detector`
- Model Version: `1.0.0`
- Model Type: `Object Detection`

**Dataset:**
- Select: **Use Labeling Job Output**
- Select job: `alien-defect-labeling`
- Or manually enter manifest S3 URI

**Training Configuration:**
- Instance Type: `ml.p3.2xlarge` (GPU recommended)
- Max Runtime: `3600` seconds (1 hour)

**Auto-Compilation (Optional):**
- Enable: ✅ Auto-compile after training
- Targets: `arm64-cpu` (for ARM devices)

3. Click **Start Training**

### Step 4.2: Monitor Training

1. Go to **Training** → Click on your job
2. View:
   - Status: `InProgress` → `Completed`
   - Training logs (real-time)
   - Metrics: loss, accuracy, mAP

Training typically takes 15-30 minutes depending on dataset size.

### Step 4.3: Review Model Metrics

After training completes:
1. View final metrics in the Training Detail page
2. Check model artifacts in S3:
   `s3://dda-alien-output/sagemaker/training/alien-defect-detector-1.0.0/output/model.tar.gz`

---

## Part 5: Compilation & Packaging

### Step 5.1: Compile Model (If not auto-compiled)

1. Go to **Training** → Select your completed job
2. Click **Compile** tab
3. Select target:
   - `arm64-cpu` - For ARM64 devices (Raspberry Pi, Jetson, Graviton)
   - `x86_64-cpu` - For x86 devices
   - `arm64-gpu` - For ARM64 with GPU
4. Click **Start Compilation**

Compilation takes 5-15 minutes.

### Step 5.2: Package as Greengrass Component

After compilation completes:
1. Click **Package** tab
2. Click **Start Packaging**

This creates a Greengrass-compatible component package.

### Step 5.3: Publish to Greengrass

1. Click **Publish** tab
2. Enter:
   - Component Name: `com.example.alien-defect-detector`
   - Component Version: `1.0.0`
   - Friendly Name: `Alien Defect Detector`
3. Click **Publish**

The component is now available in the UseCase Account's Greengrass registry.

---

## Part 6: Deployment

### Step 6.1: Setup Edge Device (One-time)

On your edge device (ARM64 or x86):

```bash
# Clone the repo
git clone <your-dda-repo>
cd DefectDetectionApplication/station_install

# Run setup script (requires sudo)
sudo ./setup_station.sh us-east-1 dda_edge_server_1
```

This script:
- Installs Greengrass Core v2
- Creates IoT Thing and certificates
- Tags device for portal discovery
- Attaches cross-account S3 access policy

### Step 6.2: Verify Device in Portal

1. Go to **Devices** in the Portal
2. Select your UseCase
3. Your device should appear with status `HEALTHY`

### Step 6.3: Create Deployment

1. Go to **Deployments** → **Create Deployment**
2. Fill in:

**Target:**
- Select device: `dda_edge_server_1`

**Components:**
- Add: `com.example.alien-defect-detector` (version `1.0.0`)
- Add: `aws.edgeml.dda.LocalServer` (auto-included)

3. Click **Create Deployment**

### Step 6.4: Monitor Deployment

1. Go to **Deployments** → Click on your deployment
2. View status: `IN_PROGRESS` → `SUCCEEDED`
3. Check device components:
   - Go to **Devices** → Select device → **Components** tab
   - Verify `com.example.alien-defect-detector` is `RUNNING`

### Step 6.5: View Device Logs

1. Go to **Devices** → Select device → **Logs** tab
2. Select component: `com.example.alien-defect-detector`
3. View real-time logs from the edge device

---

## Part 7: Testing the Model

### Step 7.1: Access Inference Endpoint

The DDA LocalServer component exposes a REST API on the device:

```bash
# SSH to device
ssh ubuntu@<device-ip>

# Test inference
curl -X POST http://localhost:5000/predict \
  -F "image=@test_image.jpg"
```

### Step 7.2: View Results

Response includes:
- Detected defects with bounding boxes
- Confidence scores
- Classification (normal/anomaly)

---

## Troubleshooting

### Labeling Job Can't Find Images

**Cause**: Data Account role or bucket not configured correctly.

**Fix**:
1. Verify UseCase has correct `data_account_role_arn` in DynamoDB
2. Check bucket is tagged with `dda-portal:managed=true`
3. Verify External ID matches between DynamoDB and IAM role

### Training Fails with "Access Denied"

**Cause**: SageMaker can't access Data Account bucket or UseCase Account output bucket.

**Fix**: The bucket policies should be auto-configured. Check:

**Data Account bucket** (read access):
```bash
aws s3api get-bucket-policy --bucket dda-alien-bucket --query Policy --output text | jq .
```

Should include a statement allowing `DDASageMakerExecutionRole` from UseCase Account to read.

**UseCase Account bucket** (write access):
```bash
aws s3api get-bucket-policy --bucket dda-alien-output --query Policy --output text | jq .
```

Should allow `DDASageMakerExecutionRole` to write outputs.

### Deployment Fails with "S3 Access Denied"

**Cause**: Device can't download component artifacts.

**Fix**:
1. Verify `DDAPortalComponentAccessPolicy` is attached to `GreengrassV2TokenExchangeRole`
2. Check Portal's component bucket policy allows UseCase Account

### Device Not Showing in Portal

**Cause**: Device not tagged or not set up via `setup_station.sh`.

**Fix**:
```bash
# Tag the Greengrass Core Device
aws greengrassv2 tag-resource \
  --resource-arn arn:aws:greengrass:us-east-1:198226511894:coreDevices:dda_edge_server_1 \
  --tags "dda-portal:managed=true"
```

---

## Summary

You've completed the full workflow:

1. ✅ Set up multi-account architecture (Portal, UseCase, Data)
2. ✅ Registered Data Account in Portal Settings
3. ✅ Uploaded training images to Data Account
4. ✅ Created and completed labeling job
5. ✅ Trained defect detection model
6. ✅ Compiled and packaged for edge deployment
7. ✅ Deployed to Greengrass edge device
8. ✅ Tested inference on device

The model is now running on your edge device, ready to detect defects in real-time!

**Key Features Used:**
- **Data Account Registration**: Centralized credential management in Settings
- **Dropdown Selection**: Quick UseCase setup using registered Data Accounts
- **Auto-Configuration**: Automatic bucket policy and CORS setup
- **Multi-Account Security**: Separate accounts for data, training, and deployment

---

## Next Steps

- **Add more training data**: Upload additional images and retrain
- **Fine-tune model**: Adjust hyperparameters for better accuracy
- **Scale deployment**: Deploy to multiple devices using thing groups
- **Monitor production**: Use device logs to track inference performance
- **Update models**: Create new versions and deploy updates

---

## Related Documentation

- [DATA_ACCOUNTS_DEPLOYMENT.md](DATA_ACCOUNTS_DEPLOYMENT.md) - Data Account setup and registration
- [ADMIN_GUIDE.md](ADMIN_GUIDE.md) - Portal administration
- [SHARED_COMPONENTS.md](SHARED_COMPONENTS.md) - Greengrass component management
- [BYOM_GUIDE.md](BYOM_GUIDE.md) - Bring Your Own Model
- [CHANGELOG.md](CHANGELOG.md) - Version history and new features
