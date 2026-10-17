# Defect Detection Application (DDA) - Portal Edition

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Docker](https://img.shields.io/badge/docker-%230db7ed.svg?style=flat&logo=docker&logoColor=white)](https://www.docker.com/)

The Defect Detection Application (DDA) is an edge-deployed computer vision solution for quality assurance in discrete manufacturing environments. Originally developed by the AWS EdgeML service team, DDA is now available as an open-source project under the stewardship of the AWS Manufacturing TFC and Auto/Manufacturing IBU.

## Table of Contents

- [Overview](#overview)
- [Deployment](#deployment)
  - [Step 1: Deploy Portal Infrastructure](#step-1-deploy-portal-infrastructure-portal-account)
  - [Step 2: Build and Deploy Frontend](#step-2-build-and-deploy-frontend)
  - [Step 3: Post-Deployment Setup](#step-3-post-deployment-setup)
  - [Step 4: Build the DDA LocalServer Component (Build Fleet)](#step-4-build-the-dda-localserver-component-build-fleet)
    - [JetPack 7 (JP7) Builds](#45-jetpack-7-jp7-builds)
  - [Step 5: Set Up Accounts](#step-5-set-up-accounts)
  - [Step 6: Create UseCase](#step-6-create-usecase)
  - [Step 7: Set Up Edge Devices (Add Device)](#step-7-set-up-edge-devices-add-device)
    - [JetPack 7 (JP7) Devices](#76-jetpack-7-jp7-devices)
- [Deploy DDA Application to Edge Device](#deploy-dda-application-to-edge-device)
  - [Revise a Deployment](#revise-a-deployment)
- [Devices](#devices)
- [Using the Portal](#using-the-portal)
- [ML Workflow](#ml-workflow)
- [Using ONNX Models](#using-onnx-models)
- [Using LLMs (vLLM)](#using-llms-vllm)
- [Remote Device Access (SSH)](#remote-device-access-ssh)
- [Optional Datasets](#optional-datasets-before-model-training)
- [Inference Results Upload](#inference-results-upload-optional)
- [Edge Device Management](#edge-device-management)
- [Troubleshooting](#troubleshooting)
- [Project Structure](#project-structure)
- [Contributing](#contributing)
- [License](#license)
- [Support](#support)

## Overview

DDA provides real-time defect detection capabilities for manufacturing quality control using computer vision and machine learning. The system runs at the edge using AWS IoT Greengrass, enabling low-latency inference and reducing dependency on cloud connectivity.

### Key Benefits

- **Real-time Processing**: Sub-second inference times for immediate quality feedback
- **Edge Deployment**: Operates independently of cloud connectivity
- **Scalable Architecture**: Supports multiple camera inputs and production lines
- **ML Model Flexibility**: Compatible with various computer vision models
- **Manufacturing Integration**: RESTful APIs for integration with existing systems
- **Portal Management**: Centralized multi-tenant admin portal for managing all deployments

### Portal Features

| Feature | Description |
|---------|-------------|
| **Data Management** | Browse & upload training images to S3 |
| **Data Accounts** | Centralized management of cross-account data access with dropdown selection |
| **Labeling** | Create Ground Truth labeling jobs (classification & segmentation) |
| **Manifest Transformation** | Auto-transform Ground Truth manifests to DDA-compatible format |
| **Training** | SageMaker training with AWS Marketplace algorithm and manifest validation |
| **Compilation** | Compile models for edge (x86-64, ARM64) |
| **Components** | Manage Greengrass components |
| **Builds** | Build and publish the DDA LocalServer component (JP5, JP6, JP7, AMD64, AMD64_NVIDIA) on ephemeral or dedicated build compute |
| **Build Fleet** | Launch, start, stop, and terminate dedicated build servers (PortalAdmin only) |
| **Deployments** | Deploy LocalServer, models, and workflows to edge devices, revise a device's deployment, optional Inference Uploader |
| **Inference Uploader** | Automatically upload inference results from edge devices to S3 |
| **Devices** | Register devices with a one-line setup command (Add Device) and monitor IoT Greengrass devices |
| **Settings** | Portal configuration, Data Accounts, and Build Infrastructure settings (PortalAdmin only) |

## Deployment

### Architecture Overview

DDA supports both single-account and multi-account architectures:

**Single-Account** (Recommended for Getting Started)
- All components in one AWS account
- Simplest setup and management
- Requires running `./deploy-account-role.sh` (select Option 1) to create the `DDASageMakerExecutionRole`

**Multi-Account** (Recommended for Production)
- Separates Portal, UseCase, and Data accounts
- Better security and governance
- Requires cross-account IAM roles (created by `deploy-account-role.sh`)

#### Architecture Components

```
Portal Account          UseCase Account         Data Account (Optional)
┌──────────────┐       ┌──────────────┐        ┌──────────────┐
│ CloudFront   │       │ SageMaker    │        │ S3 Buckets   │
│ API Gateway  │──────▶│ Greengrass   │◀──────▶│ (Training    │
│ Cognito      │ STS   │ IoT Core     │        │  Data)       │
│ DynamoDB     │       │ S3 Buckets   │        │              │
└──────────────┘       └──────────────┘        └──────────────┘
```

**Portal Account** - Central management hub
- DDA Portal web interface, user authentication and RBAC
- Training and compilation job orchestration

**UseCase Account** - ML workflow execution
- SageMaker training jobs, model compilation for edge deployment
- Greengrass component management
- Can be the same as Portal Account for single-account setups

**Data Account** (Optional) - Centralized data storage
- Training datasets and inference results from edge devices

### Prerequisites

- AWS CLI configured
- Node.js 18+, Python 3.11+
- AWS CDK: `npm install -g aws-cdk`
- [AWS Marketplace subscription](https://aws.amazon.com/marketplace/pp/prodview-j72hhmlt6avp6) (in UseCase Account — see below)

> **Important: AWS Marketplace Subscription Required**
> 
> The DDA training algorithm is an AWS Marketplace product. You must subscribe **in the UseCase Account** (the account where SageMaker runs) before creating training jobs.
> 
> 1. Sign in to the **UseCase Account** AWS Console
> 2. Go to [AWS Marketplace - DDA Algorithm](https://aws.amazon.com/marketplace/pp/prodview-j72hhmlt6avp6)
> 3. Click **Continue to Subscribe** → **Accept Terms**
> 4. Wait for the subscription to become active (usually a few minutes)
> 
> For multi-account setups, the subscription must be in the UseCase Account (where SageMaker runs), not the Portal Account or Data Account.

### Step 1: Deploy Portal Infrastructure (Portal Account)

#### 1.1 Configure CloudWatch Logs for API Gateway

```bash
# 1. Create IAM role for API Gateway CloudWatch Logs
aws iam create-role \
  --role-name APIGatewayCloudWatchLogsRole \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "apigateway.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'

# 2. Attach CloudWatch Logs policy
aws iam attach-role-policy \
  --role-name APIGatewayCloudWatchLogsRole \
  --policy-arn arn:aws:iam::aws:policy/CloudWatchLogsFullAccess

# 3. Get the role ARN and set in API Gateway
ROLE_ARN=$(aws iam get-role --role-name APIGatewayCloudWatchLogsRole --query 'Role.Arn' --output text)
aws apigateway update-account \
  --patch-operations op=replace,path=/cloudwatchRoleArn,value=$ROLE_ARN \
  --region us-east-2
```

> **Note**: This is a one-time setup per AWS account.

#### 1.2 Deploy Portal Infrastructure with CDK

```bash
cd edge-cv-portal

# First time only: bootstrap CDK in your account
cd infrastructure
npm install
npx cdk bootstrap
cd ..

# Deploy all stacks
./deploy-infrastructure.sh
```

> **Note**: `cdk bootstrap` must be run from `edge-cv-portal/infrastructure/` where `cdk.json` lives. The `deploy-infrastructure.sh` script handles `npm install` and `cdk deploy` for you after that.

> **Registry enforcement and allowed buckets.** Production deployments run with the Portal_Identity registry enforced: `PORTAL_REGISTRY_ENFORCED=true ./deploy-infrastructure.sh`, after `edge-cv-portal/backfill_portal_registry.py` has been applied and the accounts verified (a fresh install starts with enforcement off, and the script prints a warning whenever the value it resolves for a deploy is not `true`). Independently of that flag, the portal reads and writes S3 only in each use case's **allowed buckets** — its configured S3 bucket, its Data Account bucket, and the **Additional S3 buckets** a UseCaseAdmin allows (Use Cases → Edit → Allowed buckets, or "Allow for this use case" in Data Management). A bucket must be tagged `dda-portal:managed=true` (`aws s3control tag-resource --account-id ACCOUNT_ID --resource-arn arn:aws:s3:::BUCKET --tags Key=dda-portal:managed,Value=true`, plus `aws s3api put-bucket-abac --bucket BUCKET --abac-status Status=Enabled` for a bucket reached through a cross-account role) **before** a use case is created with it or allows it; buckets created through the portal are tagged, ABAC-enabled and allowed automatically. See `edge-cv-portal/ADMIN_GUIDE.md`, "Use case buckets and Additional S3 buckets" and "The enforcement flag".

**What gets deployed:** CloudFront, API Gateway, Cognito, DynamoDB, Lambda functions, S3 buckets, IAM roles.

**Save the CDK outputs:** `PortalApiEndpoint`, `PortalFrontendUrl`, `CognitoUserPoolId`, `CognitoClientId`.

### Step 2: Build and Deploy Frontend

```bash
cd edge-cv-portal
./deploy-frontend.sh
```

This automatically generates `config.json` from CDK stack outputs, builds the React app, uploads to S3, and invalidates CloudFront cache.

### Step 3: Post-Deployment Setup

#### 3.1 Shared Components

The portal shares the following Greengrass components with UseCase accounts:

- **LocalServer** - Core DDA inference component. The backend container packages the **NVIDIA Triton Inference Server**, which loads compiled models and serves inference; the GStreamer pipeline calls into Triton via the `emltriton` plugin. The portal deploy does not create it: build it with Build Fleet in [Step 4](#step-4-build-the-dda-localserver-component-build-fleet) before you create a UseCase, because a variant with no published version is not shared.
- **InferenceUploader** - Optional component for uploading inference results to S3

These are automatically shared when you create a UseCase in the portal. The UseCase creation also provisions the `DDAPortalComponentAccessPolicy` required by edge devices.

#### 3.2 Create Portal Admin User

Self sign-up is off, so you create the first PortalAdmin by hand with the AWS CLI, using credentials for the portal account. Two things make it a working admin:

- **Its global row in the Portal_Identity registry**, the DynamoDB table `dda-portal-user-roles`. This row is what grants PortalAdmin, whether `PORTAL_REGISTRY_ENFORCED` is off (the fresh-install default) or on.
- **The `custom:role=PortalAdmin` attribute.** The portal UI reads it to decide which pages to show; without it the admin gets a Viewer UI with no User Manager and none of the Builds, Build Fleet, or Settings pages that Step 4 uses. With enforcement on it grants nothing. Never grant or change a role by editing `custom:role` (for example with `admin-update-user-attributes`); use the User Manager.

Registry background: [ADMIN_GUIDE.md, "Create Admin User"](edge-cv-portal/ADMIN_GUIDE.md#create-admin-user).

Replace `<REGION>`, `<ADMIN_EMAIL>`, and `<YOUR_SECURE_PASSWORD>`, then run:

```bash
# The region you deployed the portal to (deploy-infrastructure.sh printed it as "Region:")
REGION="<REGION>"
# A mailbox you control: password recovery is email-only
ADMIN_EMAIL="<ADMIN_EMAIL>"

# Get User Pool ID from CDK output (or use the value from config.json)
USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name EdgeCVPortalAuthStack \
  --query 'Stacks[0].Outputs[?OutputKey==`AuthConfig`].OutputValue' \
  --output text --region "$REGION" | python3 -c "import sys,json; print(json.load(sys.stdin)['userPoolId'])")

# 1. Create the account. The username can't be an email address (email is a sign-in alias).
aws cognito-idp admin-create-user \
  --user-pool-id "$USER_POOL_ID" \
  --username admin \
  --user-attributes \
    Name=email,Value="$ADMIN_EMAIL" \
    Name=email_verified,Value=true \
    Name=given_name,Value=Portal \
    Name=family_name,Value=Admin \
    Name=custom:role,Value=PortalAdmin \
  --message-action SUPPRESS \
  --region "$REGION"

# 2. Set a permanent password: 12+ characters with upper and lower case, a digit and a symbol.
#    Keep the single quotes.
aws cognito-idp admin-set-user-password \
  --user-pool-id "$USER_POOL_ID" \
  --username admin \
  --password '<YOUR_SECURE_PASSWORD>' \
  --permanent \
  --region "$REGION"

# 3. Grant PortalAdmin: write the account's global row in the Portal_Identity registry
USER_SUB=$(aws cognito-idp admin-get-user \
  --user-pool-id "$USER_POOL_ID" \
  --username admin \
  --query 'UserAttributes[?Name==`sub`].Value' \
  --output text --region "$REGION")

aws dynamodb put-item \
  --table-name dda-portal-user-roles \
  --region "$REGION" \
  --item "{
    \"user_id\":     {\"S\": \"$USER_SUB\"},
    \"usecase_id\":  {\"S\": \"global\"},
    \"role\":        {\"S\": \"PortalAdmin\"},
    \"status\":      {\"S\": \"enabled\"},
    \"username\":    {\"S\": \"admin\"},
    \"email\":       {\"S\": \"$ADMIN_EMAIL\"},
    \"assigned_by\": {\"S\": \"bootstrap\"},
    \"assigned_at\": {\"N\": \"$(date +%s)\"}
  }"

# 4. Check the row: role should be PortalAdmin and status enabled
aws dynamodb get-item \
  --table-name dda-portal-user-roles \
  --key "{\"user_id\": {\"S\": \"$USER_SUB\"}, \"usecase_id\": {\"S\": \"global\"}}" \
  --region "$REGION"
```

Authorization reads only `user_id` (the account's Cognito `sub`), `usecase_id` (`global`), `role` (exactly `PortalAdmin`), and, with enforcement on, `status` (`enabled`; any other value denies). The other attributes are the ones the User Manager also writes, for display and attribution; `assigned_at` is in epoch seconds.

Sign in at the portal URL that `./deploy-frontend.sh` printed in Step 2 (the `FrontendUrl` output of `EdgeCVPortalFrontendStack`) with username `admin` and the password you set. Open the user menu (top right) and choose **User Manager**: the **Accounts** table lists the pool's accounts, including `admin`. Create every other account there with **Create user**, which writes both the Cognito account and its registry row.

### Step 4: Build the DDA LocalServer Component (Build Fleet)

The DDA application runs on each edge device as the LocalServer Greengrass component, `aws.edgeml.dda.LocalServer.<variant>`, with one variant per device architecture. Build it from the portal's **Builds** page: the portal provisions build compute, builds the component from a GitHub repository, and publishes it to AWS IoT Greengrass in the portal account. This is the LocalServer component that [3.1 Shared Components](#31-shared-components) shares with UseCase accounts, so build every variant your devices need before you create the UseCase in [Step 6](#step-6-create-usecase).

#### 4.1 One-Time Setup

Step 1 already deployed the build fleet (`EdgeCVPortalBuildFleetStack`): the build job API and dispatcher, a dedicated build VPC, and the `dda-build-role` instance profile. Build instances have no SSH key and no inbound rules; the portal runs builds on them through AWS Systems Manager. The default **Ephemeral compute** mode needs no further setup.

| Task | Page | Roles |
|------|------|-------|
| Submit, monitor, cancel, and retry builds | **Builds** (`/builds`) | DataScientist, UseCaseAdmin, PortalAdmin |
| Launch, start, stop, and terminate dedicated build servers | **Build Fleet** (`/admin/fleet`) | PortalAdmin |
| Change build defaults | **Settings** → **Build Infrastructure** | PortalAdmin |

Optional PortalAdmin setup:

- **Settings** → **Build Infrastructure**: **ARM64 instance type** (default `m6g.4xlarge`), **x86_64 instance type** (default `m6i.4xlarge`), **Volume size (GB)** (default 200), **Max runtime (hours)** (default 4), **Ephemeral runners** (Spot instances, off by default), **Default Ubuntu flavor**, and optional **Per-target volume sizes** and **Runtime budgets (per target and mode)**. A build keeps the settings that were in effect when it was submitted.
- **Build Fleet** → **Launch server** (dedicated builds): enter a **Server name**; pick the **CPU architecture** (**ARM64** for JP5, JP6, and JP7, **x86_64** for AMD64 and AMD64_NVIDIA) and the **Ubuntu flavor** (**Standard** or **Ubuntu Pro**); for ARM64, also pick the **Ubuntu version** (**Ubuntu 22.04 (default)** for JP5/JP6, **Ubuntu 24.04** for JP7). The build environment installs automatically, and the server shows `running` when it is ready. **Start** works on a stopped server; **Stop** and **Terminate** work only while no build runs on the server. **Terminate** asks you to type the server name and permanently destroys the instance and its build workspace. Stop dedicated servers you are not using to save cost.
- Subscribe to the `dda-portal-build-alerts` SNS topic for orphaned-runner notifications.

#### 4.2 Start a Build

On **Builds**, fill in **Submit build**:

1. **Build targets**: select one or more. Each target becomes its own build job; the jobs run one after another in the order you selected them.

   | Target | Component | Devices (Target Architecture) |
   |--------|-----------|-------------------------------|
   | JP5 | `aws.edgeml.dda.LocalServer.arm64JP5` | Jetson, JetPack 5 (L4T R35, Ubuntu 20.04): `arm64_jp5` |
   | JP6 | `aws.edgeml.dda.LocalServer.arm64JP6` | Jetson, JetPack 6 (L4T R36, Ubuntu 22.04): `arm64_jp6` |
   | JP7 | `aws.edgeml.dda.LocalServer.arm64JP7` | Jetson Thor, JetPack 7 (L4T R38, Ubuntu 24.04): `arm64_jp7` |
   | AMD64 | `aws.edgeml.dda.LocalServer.amd64` | x86_64 CPU: `x86_64` |
   | AMD64_NVIDIA | `aws.edgeml.dda.LocalServer.amd64Nvidia` | x86_64 with an NVIDIA GPU: `x86_64_nvidia` |

2. **Repository**: pre-filled with the configured repository (`https://github.com/awslabs/DefectDetectionApplication` by default). To build a fork, enter `https://github.com/<owner>/<repo>`; only HTTPS GitHub repositories are accepted.
3. **Branch or ref** (optional): pick a discovered branch, or type any branch, tag, or 40-character commit SHA. Leave it empty to build the repository's default branch. Branch discovery works only for public repositories; if it fails, you can still type a ref.
4. **Execution mode**: **Ephemeral compute** (default) launches a build instance for the job and terminates it when the job ends. **Dedicated build server** runs on a running fleet server that you select; its CPU architecture must match every selected target. This option is unavailable until the fleet has a server.
5. Click **Submit build request**. The page confirms how many build jobs were created.

Portal builds always use the default build options: the JetPack targets compile the GPU ONNX Runtime from source, and JP6 and JP7 include vLLM. The portal has no option for a vLLM-free (`VLLM_ENABLE=0`) or CPU-only ONNX Runtime image.

> **Not a Build Fleet target:** the generic ARM64 CPU component, `aws.edgeml.dda.LocalServer.arm64` (non-Jetson arm64 devices such as AWS Graviton, Target Architecture `arm64_cpu`), cannot be built from the portal.

#### 4.3 Monitor a Build

The **Builds** table lists the build jobs of the last 90 days, newest first, and refreshes every 15 seconds. It shows **Build job**, **Status**, **Target**, **Mode**, **Requested by**, **Submitted**, **Started**, **Ended**, and, for succeeded jobs, **Published version**.

| Status | Meaning |
|--------|---------|
| `queued` | Waiting to start, for example behind the previous target of the same request or a build already running on the dedicated server |
| `provisioning` | The ephemeral build instance is starting (ephemeral mode only) |
| `building` | The build is running |
| `publishing` | The component is being published to Greengrass |
| `succeeded` | Published; see **Published version** |
| `failed` | The build or the publish failed, or the job exceeded its runtime limit |
| `interrupted` | The build compute was lost before the build finished (for example, a reclaimed Spot instance) |
| `cancelled` | Cancelled by a user |

Click a **Build job** to open its detail page:

- **Job information**: target, execution mode, requester, **Repository**, **Source ref**, **Resolved commit**, and the submitted, started, and ended times.
- **Build log**: the build output (CloudWatch Logs group `/dda/portal-builds`), refreshed every 30 seconds while the job runs. Output can take a few minutes to appear after the job starts.
- **Execution diagnostics**, when the portal retained them: classification, command status and response code, queue-wait, provisioning, and execution times, the timeout budget, the last heartbeat and progress, runner disk space, and stdout/stderr excerpts.
- **Published artifacts** (succeeded jobs): **Component**, **Published version**, and **Pushed images**.
- A **Build failed** alert with the error. When the publish step failed, it lists what was and was not published.

Actions:

- **Cancel** (table) or **Cancel build** (detail page) is available while a job is `queued`, `building`, or `publishing`. A queued job is cancelled at once. A running build is cancelled only if its build process is confirmed stopped within 5 minutes; otherwise the job keeps its status. A job cannot be cancelled while it is `provisioning`.
- **Retry** (table) or **Retry build** (detail page) is available for `interrupted` jobs and reruns the same target and execution mode. For a `failed` job, submit a new build.
- A job that runs past its runtime limit fails: **Max runtime (hours)** (default 4), or the runtime budget set for its target and mode.

#### 4.4 Use the Published Component

Each successful build publishes the next patch version of its component (the first build publishes `1.0.0`) to AWS IoT Greengrass in the portal account and region, tagged `dda-portal:managed=true`. The container images go to the `dda/flask-app` and `dda/react-webapp` ECR repositories in the same account and are listed under **Pushed images**.

Every build of a target publishes to the same component, whatever its repository or ref. A build of a fork or a test branch therefore becomes that component's latest version, which Create Deployment adds by default and offers through **Update to latest**, and which the next copy to UseCase accounts takes. Build only refs that are meant for rollout.

How the new version becomes deployable:

- **Single-account setup** (the UseCase uses the portal account): the version appears on **Create Deployment** right away, on the **Recommended** and **Portal Components** tabs.
- **Multi-account setup**: the portal copies LocalServer components into each UseCase account. Creating a UseCase ([Step 6](#step-6-create-usecase)) copies the latest version of each published variant. After a later build, a UseCaseAdmin or PortalAdmin opens **Use Cases** → **Actions** → **Re-provision Shared Components** for the UseCase. A PortalAdmin can also click **Update All Usecases** in the **Shared Components Status** panel when it lists UseCases that need an update. The copy covers the `arm64JP5`, `arm64JP6`, `amd64`, and generic `arm64` variants. `arm64JP7` and `amd64Nvidia` are not copied yet, so in a multi-account setup those builds do not appear on Create Deployment.

To move a device to the new version, revise its deployment and click **Update to latest** on the LocalServer row (see [Revise a Deployment](#revise-a-deployment)).

#### 4.5 JetPack 7 (JP7) Builds

- Ephemeral JP7 builds run on an Ubuntu 24.04 arm64 build instance, selected automatically.
- Dedicated JP7 builds need an **ARM64** server launched with **Ubuntu 24.04**. The portal rejects a JP7 request for a server on another Ubuntu release.
- No prebuilt GPU ONNX Runtime or vLLM wheels exist for Jetson Thor, so the JP7 image compiles both from source (vLLM `v0.11.2`). If JP7 builds fail on the runtime limit, a PortalAdmin can raise **Max runtime (hours)** or add a JP7 entry under **Runtime budgets (per target and mode)** in **Settings** → **Build Infrastructure**, then submit the build again.

### Step 5: Set Up Accounts

#### 5.1 UseCase Account (Required)

```bash
cd edge-cv-portal
./deploy-account-role.sh
```

- **Option 1**: Single-account setup (creates `DDASageMakerExecutionRole`)
- **Option 2**: Multi-account UseCase account (creates `DDAPortalAccessRole` + `DDASageMakerExecutionRole`)

Save the generated `usecase-account-*-config.txt` file — you'll upload it when creating a UseCase.

#### 5.2 Data Account (Optional, for multi-account)

If your training data lives in a separate AWS account:

```bash
cd edge-cv-portal
./deploy-account-role.sh
# Select option 3: Data Account
```

Save the generated `data-account-*-config.txt` file, then register it in the portal:

1. Go to **Settings** → **Data Accounts** → **Add Data Account**
2. Upload the `data-account-*-config.txt` file to auto-fill the fields
3. Enter a name and click **Register**

This only needs to be done once per data account. All future UseCases can select it from a dropdown.

### Step 6: Create UseCase

1. Go to **Use Cases** → **Onboard New Use Case**
2. Choose setup type (Single Account or Multi-Account)
3. For multi-account: upload `usecase-account-*-config.txt` to auto-fill role details
4. Configure data account (select from registered accounts or enter manually)
5. Select the use case's S3 bucket: an existing bucket must already be tagged `dda-portal:managed=true` (and ABAC-enabled when it is reached through a cross-account role), or a PortalAdmin creates one with **Create new bucket**, which tags and allows it automatically
6. Click **Create Use Case**

This provisions Greengrass components, creates `DDAPortalComponentAccessPolicy`, and sets up S3 buckets. The portal checks each configured bucket's tags through the use case's role at creation and answers `400 bucket_not_eligible` with a remedy for an untagged, unreadable or already-configured bucket (see `edge-cv-portal/ADMIN_GUIDE.md`, "Onboarding a New Customer").

### Step 7: Set Up Edge Devices (Add Device)

Register each edge device with **Add Device** on the **Devices** page. The portal generates a one-line setup command that contains a single-use setup token. Running the command on the device installs AWS IoT Greengrass and the DDA host dependencies, provisions the device in the UseCase account, and registers it with the portal. The device needs no repository checkout, AWS login, IAM user, or access keys. Create the UseCase first ([Step 6](#step-6-create-usecase)): every device is registered under a UseCase.

#### 7.1 Device Prerequisites

- Ubuntu 20.04, 22.04, or 24.04 on x86_64 or aarch64. That covers Jetson devices on JetPack 5 (Ubuntu 20.04), JetPack 6 (Ubuntu 22.04), or JetPack 7 (Ubuntu 24.04), x86_64 machines with or without an NVIDIA GPU, and generic arm64 machines. JetPack 4 (Ubuntu 18.04) is not supported.
- `sudo` access and the `curl` (or `wget`), `sha256sum`, `tar`, and `lsb_release` commands.
- At least 2 GB free on `/` for the installer. The LocalServer container images deployed later are larger than 2 GB, so leave room for them.
- Outbound internet access to the portal API and AWS. The installer downloads packages and the Greengrass software.

The installer checks these first. If any are missing, it lists every unmet prerequisite and exits without changing the system.

x86_64 machines with an NVIDIA GPU also need the NVIDIA driver and the NVIDIA Container Toolkit, configured for Docker, before you deploy `aws.edgeml.dda.LocalServer.amd64Nvidia`. The setup does not install them, and it registers the NVIDIA container runtime only on Jetson.

To try the portal without edge hardware, launch an Ubuntu x86_64 EC2 instance with 30 GB or more of disk and run the setup command on it. A Graviton (arm64) instance is detected as `arm64_cpu` and needs the generic ARM64 LocalServer, which Build Fleet does not build ([4.2](#42-start-a-build)).

#### 7.2 Register the Device

Registering a device takes the Operator, UseCaseAdmin, or PortalAdmin role.

1. Go to **Devices**, select the UseCase in **Use Case**, and click **Add Device**.
2. In **Register Edge Device**, fill in:
   - **Device name**: the IoT Thing name for the device, 1–128 letters, digits, colons (`:`), underscores (`_`), or hyphens (`-`). The name must not already exist in the UseCase account or in another registration.
   - **Device group**: an existing IoT thing group, or a new group name (same characters). The device joins this group during setup. If the group already has a group deployment, the device receives that deployment once it is set up ([Thing Group Targets](#thing-group-targets)).
3. Click **Register device**. The **Setup command for `<device name>`** dialog shows the command and when its token expires. Click **Copy command**.

The command has this form. Always copy the real command from the portal, because it contains the token and the installer checksum:

```bash
curl -fsSL https://<portal-api>/quick-setup/bootstrap -o /tmp/dda-qs.sh && echo "<sha256>  /tmp/dda-qs.sh" | sha256sum -c - && sudo bash /tmp/dda-qs.sh --endpoint https://<portal-api>/quick-setup --token <setup-token>
```

The registration appears in the **Device Registrations** panel with **Status** `Pending` and its **Token Expires** time.

#### 7.3 Run the Setup Command on the Device

Paste the command into a shell on the device, as a user that can run `sudo`, and keep the shell session open until the command finishes. The command:

1. Downloads the installer and verifies its SHA-256 checksum before running it.
2. Checks the prerequisites.
3. Downloads the setup bundle and verifies its checksum.
4. Exchanges the token for short-lived AWS credentials limited to the provisioning steps for this device. The token is used up here. The credentials stay in memory and are cleared when the run ends.
5. Provisions the device. It installs Java, Python 3.11, the AWS CLI, GStreamer, and Docker; on Jetson it also registers the NVIDIA container runtime. It installs Greengrass Core v2 under `/aws_dda/greengrass/v2`, creates the IoT thing in the device group, sets up the `GreengrassV2TokenExchangeRole` permissions, and tags the core device `dda-portal:managed=true` so the portal lists it.
6. Checks that the device is in the device group and that the `greengrass` service is active.
7. Detects the device's DDA Target Architecture (`arm64_jp5`, `arm64_jp6`, `arm64_jp7`, `x86_64`, `x86_64_nvidia`, or `arm64_cpu`) and reports the result to the portal.

The output shows each step and ends with `✅ Station Quick Setup completed successfully`. The full log is in `/var/log/dda-quick-setup-<timestamp>.log` (in `/tmp` if `/var/log` is not writable).

A run that is interrupted after the token exchange (step 4), for example by Ctrl-C, a closed SSH session, or a reboot, reports no result to the portal, so the registration stays `In progress` ([7.4 Setup Token Rules](#74-setup-token-rules)). The same happens when the output warns that the setup status could not be reported to the Edge CV Portal.

If the output warns that `DDAPortalComponentAccessPolicy` could not be verified or attached, make sure that policy is attached to `GreengrassV2TokenExchangeRole` in the UseCase account. Without it, deployments can fail with S3 or ECR access errors (see [Edge Device Issues](#edge-device-issues)).

For details of the setup flow, see [station_install/quick_setup/README.md](station_install/quick_setup/README.md).

#### 7.4 Setup Token Rules

- A token is valid for 90 minutes from when the command is generated.
- Run the command with at least 15 minutes of validity left. The credential exchange refuses a token closer to expiry.
- A token works once. It is consumed when the device exchanges it for credentials, so the same command cannot be run a second time.
- Until it has run, the command works for whoever runs it first. Share it only with the person setting up the device, and click **Delete** or **Regenerate** if it may have reached anyone else.
- **Regenerate**, in the **Device Registrations** panel, issues a new command for a registration that is not completed and invalidates the previous token. An `Expired` or `Failed` registration returns to `Pending`. An `In progress` registration keeps that status, and its new command is refused at the credential exchange, so **Regenerate** does not restart it.
- **Delete** removes a registration that is not completed and invalidates its token.
- Neither **Regenerate** nor **Delete** stops a run that has already exchanged its token: the run's credentials stay valid until they expire (at most 1 hour) but can no longer report its result, so do not use either while the command is still running.
- An invalid or expired token stops the installer with "Generate a new setup command from the Edge CV Portal and run it again." Repeated invalid attempts from the same IP address are rate-limited.

#### 7.5 Verify the Device

- **Devices** → **Refresh**: the registration **Status** goes from `Pending` to `In progress` to `Completed` (or `Failed`).
- The device appears in the **Devices** table under its device name. Its **Status** shows `Healthy` once Greengrass connects. It has no DDA components until you deploy them.
- Open the device. **Overview** shows the **Target Architecture (DDA)** that the setup recorded, for example `arm64_jp6`. If it shows **Not recorded**, click **Edit** and set it: vLLM and node-plugin deployments are rejected without it, and Create Deployment can only filter LocalServer variants for a device whose architecture is recorded.
- On the device, `sudo systemctl status greengrass` shows the service running. More commands are in [Edge Device Management](#edge-device-management).
- If the registration **Status** is `Failed`, read the log on the device and fix the cause. Then click **Regenerate** and run the new command.

#### 7.6 JetPack 7 (JP7) Devices

- Jetson Thor devices on JetPack 7 (L4T R38, Ubuntu 24.04) are recorded as `arm64_jp7` and need `aws.edgeml.dda.LocalServer.arm64JP7`. Create Deployment does not offer them the JP5 or JP6 LocalServer, and it does not offer the JP7 LocalServer to JP5 or JP6 devices.
- GPU access needs the NVIDIA container runtime registered with Docker. The setup registers it with `nvidia-ctk` or in `/etc/docker/daemon.json`. If the output warns that neither `nvidia-ctk` nor `nvidia-container-runtime` was found, GPU containers will not start: install the NVIDIA Container Toolkit on the device before you deploy.
- DLR-only (Neo-compiled) models are not supported on JP7. Use ONNX models instead ([Using ONNX Models](#using-onnx-models)).
- The JP7 LocalServer includes vLLM ([Using LLMs (vLLM)](#using-llms-vllm)).

## Deploy DDA Application to Edge Device

Every device needs the LocalServer variant that matches its **Target Architecture (DDA)** (see the table in [4.2 Start a Build](#42-start-a-build)) before models or workflows can run on it. All deployments are created and revised in the portal.

### Create the First Deployment

1. Go to **Deployments** → **Create Deployment**.
2. **Use Case**: select the UseCase the device is registered in. **Deployment Name** is optional; without one, the portal names the deployment `portal-deployment-<timestamp>`.
3. **Deployment Target**: keep **Specific Devices** and select the device in **Target Devices**. Select one device per deployment (see [One Deployment per Device](#one-deployment-per-device)). If the device's group already has a deployment, the device already runs it: revise the group deployment instead ([Thing Group Targets](#thing-group-targets)). The portal does not warn you when a device-level deployment overlaps a group deployment.
4. **Components**: the **Recommended** tab lists the LocalServer variant that matches the device's recorded architecture. Click **Add**. Add exactly one LocalServer variant: two variants on one device conflict. If the device has no recorded Target Architecture, Create Deployment cannot filter the JetPack variants, so set it first ([7.5 Verify the Device](#75-verify-the-device)).
5. Optional: under **Advanced Options**, set **Auto Rollback** (on by default) and **Component Update Timeout** (60 seconds by default).
6. Click **Create Deployment**. The deployment's detail page opens.
7. Verify on **Devices** → device → **Components**: `aws.edgeml.dda.LocalServer.<variant>` reaches Lifecycle State **Running**.

If the device already has a deployment, the form switches to **Update Deployment**. See [Revise a Deployment](#revise-a-deployment).

### One Deployment per Device

A Greengrass deployment targets one device or one thing group, and each target has one current deployment. A new deployment for the same target replaces the previous one instead of running alongside it. The portal treats the new deployment as a revision: it keeps the deployment name, titles the form **Update Deployment**, and shows the **Revising existing deployment** banner ("A device can only have one active deployment …"). The **Deployments** list shows each target's current deployment.

A deployment's component list is the complete list for its target. Greengrass removes components that are installed on the device but left out of the new deployment. For a device target, the portal lists them in a **Components will be removed** dialog before it submits; **Continue Deployment** goes ahead. So every deployment and revision must include LocalServer and every model, workflow, and plugin component the device should keep. The dialog does not list `aws.greengrass.SecureTunneling`, the Remote Access component, so enable Remote Access again if a deployment leaves it out.

There is one exception. A component you remove that a remaining component still requires stays installed as a dependency: model components depend on their LocalServer variant, and workflow components depend on their models. In that case the portal stops with **Deployment not submitted: de-selected component(s) will remain installed on the device**. Acknowledge the named component and submit again, or also remove the components that require it.

Other portal actions also revise a device's deployment. On a device that has its own deployment, enabling or disabling Remote Access ([Remote Device Access (SSH)](#remote-device-access-ssh)) revises that deployment: it adds or removes `aws.greengrass.SecureTunneling` and keeps the other components. Devices managed through a thing group behave differently ([Thing Group Targets](#thing-group-targets)).

### Revise a Deployment

1. Open the deployment (**Deployments** → deployment ID, or **Devices** → device → **Deployments** tab) and click **Revise Deployment**. On **Create Deployment**, selecting the same single device has the same effect, but only before you add any component: the form loads the current components only while none are selected, and the **Deployment Target** field is below **Components**. For a thing group, use **Revise Deployment** ([Thing Group Targets](#thing-group-targets)).
2. The form loads the target's current deployment: its name and its components. `aws.greengrass.Nucleus`, `aws.greengrass.LogManager`, and `aws.greengrass.ShadowManager` are not listed because the portal adds them again ([Deployment Options](#deployment-options)).
3. Change the list. You can **Add** components, **Remove** components, or update versions. A component with a newer version shows an **update available** badge; **Update to latest** (one component) or **Update all to latest** changes the version in place. This is how a new LocalServer build reaches a device.
4. Click **Update Deployment**, and confirm the **Components will be removed** dialog if it appears.
5. The portal creates a new deployment for the same target, which replaces the old one. With **Auto Rollback** on, a failed revision is rolled back automatically.

Revision needs exactly one selected device, or a thing group. With several devices selected, the portal does not load an existing deployment.

### Add Models or Workflows Later

To add models, workflows, or plugins to a device that already runs LocalServer, revise its deployment: open it, click **Revise Deployment**, add the components (the **Recommended** tab lists the model components for the device's architecture), and click **Update Deployment**. LocalServer and the other components stay because they are pre-loaded.

Some shortcuts open Create Deployment with components already selected: **Deploy Selected** on the **Components** page, **Deploy Component** on a component's page, **Deploy this workflow** after packaging a workflow, and **Clone Deployment**. These select only those components and do not load the device's current deployment. Add LocalServer and everything else the device should keep (the **Components will be removed** dialog shows what would be removed), or start from **Revise Deployment** instead. If the device has Remote Access enabled, a shortcut deployment that leaves out `aws.greengrass.SecureTunneling` removes it from the device, and the dialog does not list it. Enable Remote Access again on the device's **Remote Access** tab afterwards.

A packaged workflow is offered only to devices whose recorded architecture is among the architectures it was packaged for.

### Thing Group Targets

- **Deployment Target** → **Thing Group**, then enter the **Thing Group Name**, for example the device group chosen in Add Device. Every device in the group gets the group's components, and devices added to the group later receive the group's deployment too. The group has one current deployment of its own. To revise it, open the group deployment and click **Revise Deployment**, which sets the group name in one step and pre-loads the group's component list. What you submit replaces that list on every member. Group deployments get no **Components will be removed** dialog, so check the deployment name and component list before you submit any group deployment, including the first.
- A device that also has its own device-level deployment runs both deployments at once, and its components can conflict. When you target a group, the portal lists members that have their own deployment (**Devices in this group have individual deployments**) and asks you to confirm with **Deploy anyway**. It recommends cancelling those individual deployments first (**Cancel Deployment**, available while a deployment is `ACTIVE`). Greengrass cancels a deployment only for devices that have not received it yet, so a device that already applied its own deployment keeps those components.
- So use one approach per device, and decide before its first deployment: deploy to the device, or to its group, not both. Devices registered with Add Device are already members of their device group.
- Remote Access reads and writes only device-level deployments. On a device that gets its components from a group deployment, enabling Remote Access creates a separate device-level deployment that contains only `aws.greengrass.SecureTunneling`, and disabling it afterwards leaves the component installed.
- For a group, the portal cannot check member architectures. Keep one architecture per group and add the LocalServer variant for it.

### Deployment Options

- **Auto-included components**: when a deployment contains LocalServer or a model component, the portal adds `aws.greengrass.Nucleus` (pinned to the Nucleus version the device runs, when it can be read), `aws.greengrass.LogManager` (uploads logs to CloudWatch every 5 minutes), `aws.greengrass.ShadowManager` (syncs the camera registry, camera bindings, and model status shadows with IoT Core), and `aws.edgeml.dda.InferenceUploader` when the UseCase enables it.
- **Advanced Options**: **Auto Rollback** sets the Greengrass failure handling to `ROLLBACK` (off: `DO_NOTHING`). **Component Update Timeout** sets `timeoutInSeconds` in the Greengrass component update policy.

### Monitoring Deployments

- **Deployments**: select the UseCase. The table shows **Deployment ID**, **Name**, **Target**, **Status**, **Latest**, and **Created**. You can filter by status: `ACTIVE`, `COMPLETED`, `FAILED`, `CANCELED`, or `INACTIVE`.
- Deployment detail: **Status**, **Target**, **Components**, and **Latest for Target**, with **Auto-refresh** (every 5 seconds) while the deployment is `ACTIVE`. The tabs are **Overview** (IDs, revision ID, and per-device failure details), **Device Status** (each device's status, such as `QUEUED`, `IN_PROGRESS`, `SUCCEEDED`, `FAILED`, `REJECTED`, `TIMED_OUT`, or `CANCELED`, with the reason), **Components**, **Deployment Policies**, and **AI Diagnostics**.
- Actions: **Revise Deployment**; **Clone Deployment**, which opens Create Deployment with the same components (except the `aws.greengrass.*` ones) and no target; and **Cancel Deployment** while the deployment is `ACTIVE`.
- On the device page, the **Components** tab lists the installed components with their lifecycle state, and the **Deployments** tab lists the device's effective deployments.

## Devices

### Device Management (via Portal)

- **Devices** page shows all registered devices with status, platform, architecture, last seen
- Click a device for: Overview, Components, Deployments, Cameras, Logs, Results, AI Diagnostics, Remote Access
- Logs are sent to CloudWatch and viewable in the portal
- **Results** browses the inference-results captures uploaded from the device
- **AI Diagnostics** analyzes the device's logs with AI (**Analyze Logs with AI**) and lists detected issues

## Using the Portal

### Simplified Workflow

```
1. Labeling     → Create labeling job → Complete in Ground Truth UI
2. Transform    → Click "Transform Manifest" (auto-fills URIs)
3. Training     → Select job (shows ✓ Transformed) → Start training
4. Compilation  → Select target architecture (ARM64/x86)
5. Deployment   → Revise the device's deployment → Add the model (keep LocalServer) → Optional: Inference Uploader
```

### Data Management

- Upload and organize training images to S3
- Browse datasets with image counts
- Register pre-labeled datasets for quick testing

### Labeling

- Create Ground Truth labeling jobs (classification & segmentation)
- Status auto-syncs from SageMaker
- Supports pre-drawn masks for segmentation guidance

> **Important**: Labeling work is completed in the Ground Truth UI, not the DDA Portal.

### Manifest Transformation

Ground Truth creates manifests with job-specific attribute names. The portal auto-transforms them to DDA format during training job creation:

- `{job-name}` → `anomaly-label`
- `{job-name}-metadata` → `anomaly-label-metadata`
- `{job-name}-ref` → `anomaly-mask-ref` (segmentation)

### Training

- Select Ground Truth job or pre-labeled dataset
- Auto-detect and transform manifests
- Configure model type (Classification, Segmentation, Robust variants)
- Instance types: ml.g4dn.2xlarge (GPU), ml.p3.2xlarge (GPU), ml.m5.xlarge (CPU)
- Optional auto-compile after training

### Compilation

- Compile trained models for edge deployment
- Targets: x86_64 CPU, ARM64 CPU, Jetson (JetPack 5/6/7)
- Compiled models are packaged as Greengrass components

## ML Workflow

### Path A: Ground Truth Labeling (Recommended)

1. **Create Labeling Job** → Upload images, configure task type
2. **Complete Labeling** → Workers label in Ground Truth UI
3. **Create Training Job** → Transform manifest → Start training
4. **Compile Model** → Select target architecture
5. **Deploy to Device** → Via portal Deployments page

### Path B: Pre-Labeled Datasets (Quick Start)

1. **Register Dataset** → Data Management → Pre-Labeled Datasets
2. **Create Training Job** → Select dataset → Start training
3. **Compile Model** → Select target architecture
4. **Deploy to Device** → Via portal Deployments page

## Using ONNX Models

DDA supports **pluggable inference runtimes** so a model can run on the ONNX
Runtime engine (or native PyTorch) alongside the default SageMaker Neo / DLR
path — selected **per model**. This lets you bring your own ONNX model (e.g. a
YOLO object detector) and migrate off Neo/DLR one model at a time, with no
change to existing DLR models. See
[docs/multi-runtime-inference.md](docs/multi-runtime-inference.md) for the design.

### Runtime selection

Each model package's `manifest.json` declares which engine loads it via a
`runtime` field:

| `runtime` | Engine | Artifact |
|-----------|--------|----------|
| `dlr` (default when absent) | SageMaker Neo / DLR — full backward compatibility | Neo `compiled.*` + bundled `libdlr.so` |
| `onnx` | ONNX Runtime | `model.onnx` |
| `pytorch` | Native PyTorch (TorchScript) | `model.pt` |

GPU acceleration for ONNX (CUDA + TensorRT execution providers) is available on
**JetPack 5, 6, and 7**; the CPU images (x86_64, arm64 CPU) run ONNX on CPU. GPU requires a
LocalServer backend image built with the ONNX GPU runtime (see
[Build requirement for GPU](#build-requirement-for-gpu) below).

> **Note**: `dlr` (Neo-compiled) models are not supported on **JetPack 7** —
> migrate those models to `runtime: onnx`. See
> [JetPack 7 (JP7) Devices](#76-jetpack-7-jp7-devices).

### Object detection (YOLO)

ONNX object-detection models add a `task` field and a `detection` block. The
raw YOLO output tensor (e.g. `[1, 84, 8400]`) is decoded on-device (threshold →
class-wise NMS → boxes scaled to the source image). Detections are returned as
bounding boxes drawn onto the output overlay image plus a `detections` block in
the capture metadata.

```json
{
  "runtime": "onnx",
  "runtime_artifact": "model.onnx",
  "task": "object_detection",
  "detection": {
    "num_classes": 80,
    "score_threshold": 0.25,
    "iou_threshold": 0.45,
    "network_input": 640,
    "class_names": ["person", "bicycle", "..."]
  },
  "model_graph": {
    "model_graph_type": "single_stage_model_graph",
    "stages": [{
      "type": "yolo_object_detection",
      "threshold": 0.25,
      "image_width": 640,
      "image_height": 640,
      "image_range_scale": true,
      "normalize": false
    }]
  }
}
```

Classification/anomaly ONNX models omit `task` (they default to the anomaly
output contract) and use their normal stage type.

### Object detection (RF-DETR / DETR-family)

Object detection is **architecture-pluggable** via a `detection.layout`
selector, so DETR-family models work alongside YOLO. RF-DETR differs from YOLO
in output shape and decode:

| | YOLO (v5/v8) | RF-DETR (DETR-family) |
|---|---|---|
| ONNX outputs | 1 tensor `[1, 4+C, N]` | 2 tensors: boxes `[1, Q, 4]` + logits `[1, Q, C]` |
| Box encoding | xywh (center), network-pixel scale | cxcywh, normalized 0..1 |
| Scoring | max class score per anchor | per-query sigmoid (or softmax) |
| Filtering | score threshold + **NMS** | **NMS-free** top-k over query×class |

The on-device decoder (`rf_detr_object_detection`) identifies the boxes vs
logits tensors **by shape** (tolerating exporter output-order differences), does
sigmoid/softmax scoring, a flattened top-k, applies `score_threshold`, converts
cxcywh→xyxy, and scales normalized boxes back to the source image. It returns
the same detection result contract as YOLO, so overlay rendering and metadata
are unchanged.

```json
{
  "runtime": "onnx",
  "runtime_artifact": "model.onnx",
  "task": "object_detection",
  "detection": {
    "layout": "rf_detr",
    "num_classes": 90,
    "score_threshold": 0.5,
    "top_k": 300,
    "network_input": 560,
    "class_names": ["person", "bicycle", "..."]
  },
  "model_graph": {
    "model_graph_type": "single_stage_model_graph",
    "stages": [{
      "type": "rf_detr_object_detection",
      "threshold": 0.5,
      "image_width": 560,
      "image_height": 560,
      "image_range_scale": true,
      "normalize": false
    }]
  }
}
```

Additional `detection` keys for RF-DETR: `layout: "rf_detr"`, `top_k` (default
300), `use_softmax` (default false → sigmoid), and `background_class` (optional
index to drop when using softmax). NMS-only keys (`iou_threshold`) are ignored.

> Note: the RF-DETR decoder is validated against synthetic two-tensor outputs.
> A real RF-DETR ONNX export/device run is still pending; exact output tensor
> names and `network_input` vary by exporter (order is handled by shape-based
> tensor ID).

### Import via the portal (Smart Import)

1. Put your `model.onnx` in the UseCase account S3 — or use the **ONNX** compile
   target (Compilation page) to export a trained model to `model.onnx`.
2. Portal → **Smart Import (BYOM)**:
   - **Model Type**: Object Detection (or Classification)
   - **Runtime / export format**: **ONNX Runtime**
   - **Detection architecture** (object detection only): **YOLO** or **RF-DETR**
   - Input image size (e.g. `640`), number of classes, optional class names
   - **Convert & Import** — this writes a device-correct manifest
     (`runtime: onnx`, and for detection the `task`/`detection` fields including
     `layout`) and packages `model.onnx`.
3. Compile targets / publish as usual, then revise the device's deployment to
   add the model component ([Add Models or Workflows Later](#add-models-or-workflows-later)).

### Build requirement for GPU

On JetPack 5, 6, and 7, GPU (CUDA/TensorRT) needs a LocalServer image with the
GPU ONNX Runtime, which is compiled from source. Build Fleet builds of the JP5,
JP6, and JP7 targets always include it
([Step 4](#step-4-build-the-dda-localserver-component-build-fleet)).

Build Fleet does not offer a CPU-only ONNX Runtime image for the JetPack
targets; the manual build below is the path for that variant, and it publishes
the next version of the same LocalServer component
([4.4](#44-use-the-published-component)):

```bash
ONNXRUNTIME_GPU=0 ./gdk-component-build-and-publish.sh aarch64 6   # JP6; use 5 or 7 for JP5 or JP7
```

`build-custom.sh` defaults `ONNXRUNTIME_GPU=1` for JP5/JP6/JP7 (the source build adds
~1–2 h and is memory-heavy). For a fast CPU-only image, set `ONNXRUNTIME_GPU=0`.
The CPU images (x86_64, arm64 CPU) are CPU-only regardless. The engine auto-selects TensorRT → CUDA → CPU
providers at load, so the same package runs on either a GPU or CPU image.

## Using LLMs (vLLM)

DDA can serve **large language models** on-device alongside the vision stack
using a companion [vLLM](https://github.com/vllm-project/vllm) runtime. LLMs
are registered from a **Hugging Face model ID** (no labeling, training, or
compilation), packaged and published as a Greengrass model component, and
served through device text-generation APIs and the `llm_inference` workflow
node.

**Hardware requirement**: vLLM runs on **JetPack 6 and JetPack 7 devices**
(`arm64_jp6`, e.g. AGX Orin; `arm64_jp7`, e.g. Jetson Thor). The JP6
LocalServer image installs `vllm==0.10.2+cu126` from the Jetson AI Lab index
**by default**; the JP7 image builds vLLM **`v0.11.2` from source by
default** (`install_vllm_gpu.sh`, Thor `sm_110`, against
`torch==2.9.0+cu130`) since no prebuilt Thor/cu130 wheel exists — see
[JetPack 7 (JP7) Devices](#76-jetpack-7-jp7-devices).
The deployment architecture gate rejects vLLM components targeted at JP5 or
CPU-only devices. The full stage-by-stage validation procedure lives in
[test/on-hardware/jp6_vllm_validation.md](test/on-hardware/jp6_vllm_validation.md).

### Build requirement

Build Fleet's JP6 and JP7 builds include vLLM; there is nothing extra to enable
([Step 4](#step-4-build-the-dda-localserver-component-build-fleet)).

The JP7 build compiles vLLM from source (no prebuilt Thor/cu130 wheel exists).
If JP7 builds fail on the runtime limit, see
[JetPack 7 (JP7) Builds](#45-jetpack-7-jp7-builds).

Build Fleet does not offer a vLLM-free image, so that variant needs a manual
Docker build of the LocalServer backend image.
To build an explicitly vLLM-free JP6 or JP7 image, pass
`--build-arg VLLM_ENABLE=0`; the image then carries no vLLM install (on JP7
the torch/vLLM layers are skipped entirely), the app's capability probe runs
the pre-feature (vision-only) startup sequence, and the text-generation
routes are absent.

### Example: install the smoke-test model (`facebook/opt-125m`)

`facebook/opt-125m` is a tiny (~250 MB) model that exercises the full
register → publish → deploy → generate pipeline in minutes. Output quality is
poor — it's a plumbing smoke test, not a usable assistant.

1. **Register** — Portal → **Models** page → **Register LLM**:
   - **Model Name**: `opt125m-smoke`, **Model Version**: `1.0`
   - **Model Source**: Hugging Face model ID `facebook/opt-125m`
   - **Engine settings**: `gpu_memory_utilization` = `0.3`,
     `max_model_len` = `2048` (leave `dtype=auto`, `tensor_parallel_size=1`,
     `enforce_eager` at their defaults)
   - The model appears in the list with the **`LLM (vLLM)`** badge and is
     immediately publish-eligible.

   Alternatively, seed it from the CLI (also registers the larger
   `Qwen/Qwen2.5-7B-Instruct` validation model; idempotent):

   ```bash
   ./test/on-hardware/register_vllm_models.py \
     --portal-api "$PORTAL_API" --token "$TOKEN" --usecase-id "$USECASE_ID"
   ```

2. **Package + publish** — using the `training_id` returned at registration:

   ```bash
   curl -s -X POST "$PORTAL_API/api/v1/training/$TRAINING_ID/package" \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"auto_triggered": true}'
   ```

   This packages for the `jetson-xavier-jp6` target and publishes the
   Greengrass component `model-vllm-opt125m-smoke` version `1.0.0`.

3. **Deploy** — Portal → **Deployments**: revise the JP6 device's deployment to
   add the `model-vllm-opt125m-smoke` component (see
   [Add Models or Workflows Later](#add-models-or-workflows-later)). The device downloads the weights
   from Hugging Face (internet access required) and loads the model without
   restarting LocalServer.

4. **Verify** — wait for READY, then generate:

   ```bash
   # serving state: "loading" then "ready"
   curl -s http://<device>:5000/text-generation/models

   # non-streaming generation
   curl -s -X POST "http://<device>:5000/text-generation/opt125m-smoke/generate" \
     -H "Content-Type: application/json" \
     -d '{"prompt": "The three primary colors are", "max_tokens": 64}'

   # token-by-token SSE streaming
   curl -sN -X POST "http://<device>:5000/text-generation/opt125m-smoke/generate-stream" \
     -H "Content-Type: application/json" \
     -d '{"prompt": "Count from one to ten:", "max_tokens": 64}'
   ```

The low `gpu_memory_utilization=0.3` leaves GPU headroom so vision models keep
serving alongside the LLM. For a realistic workload on a 64 GB Orin, use
`Qwen/Qwen2.5-7B-Instruct` with `gpu_memory_utilization=0.55` and
`max_model_len=8192`. If loading fails with CUDA OOM, lower
`gpu_memory_utilization` and/or `max_model_len`. Full triage tables per stage:
[test/on-hardware/jp6_vllm_validation.md](test/on-hardware/jp6_vllm_validation.md).

## Remote Device Access (SSH)

You can open an SSH session to an edge device from the portal (Device page →
**Remote Access** tab) using **AWS IoT Secure Tunneling** — no inbound ports,
public IP, or VPN required.

### How it works

1. **Enable** on the Remote Access tab adds the AWS-managed
   `aws.greengrass.SecureTunneling` component to the device's own
   device-level deployment and keeps its other components. On a device that
   gets its components from a thing group deployment, it creates a separate
   device-level deployment that holds only SecureTunneling (see
   [Thing Group Targets](#thing-group-targets)). Set the **SSH login user**
   (`ubuntu` on EC2, `ggc_user` for the Greengrass default, or your device
   user).
2. **Open SSH session** creates a short-lived tunnel and returns a **source
   access token**.
3. Locally, run the AWS IoT local proxy with that token and SSH to `localhost`:
   ```bash
   export AWSIOT_TUNNEL_ACCESS_TOKEN=<token>
   localproxy -r <region> -s 5555
   ssh -p 5555 <ssh-user>@localhost
   ```

Full steps (incl. local-proxy install and on-device log inspection) are in
[docs/connect-to-device.md](docs/connect-to-device.md).

### Security model (important)

Secure Tunneling reaches the device over an **outbound** WebSocket to AWS IoT —
there is **no inbound port** on the device. Because nothing listens for inbound
connections, **a security group / IP allowlist does not apply** and would give a
false sense of control. Access is instead gated by:

- **IAM** — only principals allowed to call `iotsecuretunneling:OpenTunnel`
  (the portal's Devices Lambda role, exercised by authenticated portal users)
  can open a tunnel.
- **Short-lived tunnel tokens** — each session token expires automatically
  (default 60 minutes).
- The device must have an SSH server and a valid login user; disable the
  component when not in use.

> IP-based allowlisting (a security group) only applies if a device is an EC2
> instance you SSH to **directly** by IP — a different path than Secure
> Tunneling. A future portal enhancement (see
> [docs/device-web-connect-spec.md](docs/device-web-connect-spec.md)) may add a
> browser terminal.

## Optional Datasets (Before Model Training)

Pre-labeled datasets let you train models without creating labeling jobs. Register them in **Data Management** → **Pre-Labeled Datasets**.

For detailed instructions on Cookie and Alien sample datasets, see [datasets/README.md](./datasets/README.md).

## Inference Results Upload (Optional)

The **Inference Uploader** component enables edge devices to automatically upload inference results to S3.

- Published once to the portal account with `./build-inference-uploader.sh`. This is a manual step: neither the portal deploy nor Build Fleet builds the component. The portal then shares it with UseCase accounts along with LocalServer.
- Enable it per UseCase (`enable_inference_uploader`, see [edge-cv-portal/INFERENCE_UPLOADER_CONFIG.md](edge-cv-portal/INFERENCE_UPLOADER_CONFIG.md)). The portal then adds it to deployments that contain LocalServer or a model.
- Configurable upload interval (10s to daily)
- Results organized as: `s3://bucket/{usecase-id}/{device-id}/{model-id}/YYYY/MM/DD/`

See [INFERENCE_UPLOADER_SETUP.md](INFERENCE_UPLOADER_SETUP.md) for detailed configuration.

## Edge Device Management

Devices are provisioned from the portal with **Add Device** (see [Step 7](#step-7-set-up-edge-devices-add-device)). The commands below inspect and troubleshoot a provisioned device and need a shell on it: a local console, SSH, or [Remote Device Access](#remote-device-access-ssh). A device set up with Add Device has no AWS CLI credentials configured, so run the `aws` commands from a workstation with credentials for the UseCase account.

### Directory Structure on Device

```
/aws_dda/
├── greengrass/v2/              # Greengrass installation
│   ├── config/
│   ├── logs/
│   └── packages/
├── dda_triton/
│   └── triton_model_repo/      # NVIDIA Triton model repository (compiled models)
├── image-capture/              # Captured images from camera
├── inference-results/          # Inference results
├── em_agent/                   # Edge Manager Agent
└── check-cloudwatch-logging.sh # Diagnostics script
```

### Common Device Operations

```bash
# Check Greengrass status
sudo systemctl status greengrass

# Restart Greengrass
sudo systemctl restart greengrass

# View Greengrass logs
tail -f /aws_dda/greengrass/v2/logs/aws.greengrass.Nucleus.log

# View LocalServer logs
tail -f /aws_dda/greengrass/v2/logs/aws.edgeml.dda.LocalServer.log

# Check deployments
aws greengrassv2 list-effective-deployments \
  --core-device-thing-name your-device-name \
  --region us-east-1
```

### Testing Inference (Without Camera - Folder Mode)

Copy test images to the device and run inference directly:

```bash
# Copy images to device
scp -i "device-key.pem" -r ./test-images/* ubuntu@device-ip:/aws_dda/image-capture/<folder-name>

# Test classification
curl -X POST http://localhost:5000/api/v1/inference \
  -F "image=@/aws_dda/image-capture/test-image.jpg" \
  -F "model_name=model-cookie-class"

# Test segmentation
curl -X POST http://localhost:5000/api/v1/inference \
  -F "image=@/aws_dda/image-capture/test-image.jpg" \
  -F "model_name=model-cookie-segmentation"
```

**Batch test all images:**

```bash
for image in /aws_dda/image-capture/*.jpg; do
  echo "Testing: $(basename $image)"
  curl -s -X POST http://localhost:5000/api/v1/inference \
    -F "image=@$image" \
    -F "model_name=model-cookie-class" | python3 -m json.tool
done
```

### Device Health Monitoring

```bash
# Check disk space
df -h /aws_dda/

# Check memory
free -h

# CloudWatch diagnostics
/aws_dda/check-cloudwatch-logging.sh us-east-1

# Search logs for errors
grep -i "error\|failed\|exception" /aws_dda/greengrass/v2/logs/*.log
```

Connectivity shows as the device **Status** on **Devices** ([7.5](#75-verify-the-device)).

## Troubleshooting

### Portal Deployment Issues

**CDK Bootstrap Error** (`SSM parameter /cdk-bootstrap/hnb659fds/version not found`):
```bash
cdk bootstrap aws://YOUR_ACCOUNT_ID/YOUR_REGION
```

**CloudWatch Logs Error** (`CloudWatch Logs role ARN must be set`):
Follow the API Gateway CloudWatch setup in Step 1.1.

**Frontend Not Accessible** (S3 "NoSuchKey" errors):
```bash
./deploy-frontend.sh  # Re-deploy frontend
```

### Edge Device Issues

**Device Provisioning Fails** (`DDAPortalComponentAccessPolicy does not exist`):
Create a UseCase in the portal first — it provisions this policy.

**Greengrass Won't Start:**
```bash
tail -f /aws_dda/greengrass/v2/logs/aws.greengrass.Nucleus.log
sudo systemctl restart greengrass
```

**S3 Access Denied** (`S3_HEAD_OBJECT_ACCESS_DENIED`):
Verify device role has `DDAPortalComponentAccessPolicy` attached:
```bash
aws iam list-attached-role-policies --role-name GreengrassV2TokenExchangeRole
```

**Deployment Fails — Can't Pull Docker Image from ECR** (`GET_ECR_CREDENTIAL_ERROR` / `Failed to get auth token for docker login`):

```
EcrException: User: .../GreengrassV2TokenExchangeRole is not authorized to perform:
  ecr:GetAuthorizationToken on resource: * because no identity-based policy allows the action
```

Docker-based components (e.g. `aws.edgeml.dda.LocalServer`) pull their image from
ECR, so the device token-exchange role needs ECR permissions. These are included
in the latest `DDAPortalComponentAccessPolicy` (and re-applied as the
`ECRComponentAccess` inline policy by `setup_station.sh`). If you provisioned the
device before this was added:

```bash
# Refresh the managed policy (re-run in the UseCase account)
./edge-cv-portal/deploy-account-role.sh        # updates DDAPortalComponentAccessPolicy

# OR add the permissions directly to the device role:
aws iam put-role-policy --role-name GreengrassV2TokenExchangeRole \
  --policy-name ECRComponentAccess \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      { "Sid": "AllowEcrAuthToken", "Effect": "Allow",
        "Action": ["ecr:GetAuthorizationToken"], "Resource": "*" },
      { "Sid": "AllowEcrImagePull", "Effect": "Allow",
        "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
        "Resource": "arn:aws:ecr:*:<ACCOUNT_ID>:repository/dda/*" }
    ]
  }'
```
`ecr:GetAuthorizationToken` does not support resource scoping and must use `"*"`.
No device restart is needed — the role change takes effect on the next deployment: revise the device's deployment ([Revise a Deployment](#revise-a-deployment)) and click **Update Deployment**.

**Deployment Fails** (`COMPONENT_VERSION_REQUIREMENTS_NOT_MET`):
- Verify component exists in your account
- Check architecture matches (arm64 vs x86_64)

**Deployment Fails — Component Store Full** (`DISK_SPACE_CRITICAL` / `SizeLimitException`):

```
SizeLimitException: Component store size limit reached:
  12545441763 bytes existing, 3561542 bytes needed, 10000000000 bytes maximum allowed total
```

The Greengrass component store has hit its size cap (default 10 GB). This is
almost always caused by **old LocalServer versions piling up** — pre-ECR builds
embedded the full multi-GB Docker image tar in the artifact zip, and Greengrass
keeps an archived (`packages/artifacts/`) and unarchived
(`packages/artifacts-unarchived/`) copy of every version. A couple of those fill
the store. (The new deployment "bytes needed" is often tiny — e.g. ~3.5 MB for an
ECR-based component — which itself tells you the store is full of stale data.)

1. Find which versions are on disk and which one is currently deployed:
```bash
sudo du -sh /aws_dda/greengrass/v2/packages/artifacts/aws.edgeml.dda.LocalServer.*/* | sort -h
sudo du -sh /aws_dda/greengrass/v2/packages/artifacts-unarchived/aws.edgeml.dda.LocalServer.*/* | sort -h
# Currently-running version:
grep -i "Resolved component" /aws_dda/greengrass/v2/logs/greengrass.log | tail -3
```

2. Remove the OLD version directories from both `artifacts/` and
   `artifacts-unarchived/`. **Never delete the version that is currently
   deployed/running** (replace `<OLD_VERSION>` accordingly):
```bash
sudo systemctl stop greengrass
sudo rm -rf /aws_dda/greengrass/v2/packages/artifacts/aws.edgeml.dda.LocalServer.arm64/<OLD_VERSION>
sudo rm -rf /aws_dda/greengrass/v2/packages/artifacts-unarchived/aws.edgeml.dda.LocalServer.arm64/<OLD_VERSION>
sudo systemctl start greengrass
```
   Then revise the device's deployment and click **Update Deployment** ([Revise a Deployment](#revise-a-deployment)).

   Alternatively, raise the store cap via the nucleus configuration (then let
   Greengrass garbage-collect unreferenced versions):
```json
"aws.greengrass.Nucleus": {
  "configurationUpdate": { "merge": "{\"componentStoreMaxSizeBytes\":\"30000000000\"}" }
}
```

3. **Prevent recurrence:** publish the LocalServer component through the ECR/S3
   path so artifacts stay small (a few MB instead of multiple GB). Build Fleet
   builds do this automatically when the packaged artifact exceeds 2 GB. Build
   the device's variant with Build Fleet
   ([Step 4](#step-4-build-the-dda-localserver-component-build-fleet)) and move
   the device to the new version: revise its deployment and click
   **Update to latest** on the LocalServer row
   ([4.4 Use the Published Component](#44-use-the-published-component)). Build
   Fleet does not build the generic ARM64 variant; for it, the manual path is
   `./gdk-component-build-and-publish.sh aarch64 cpu`, which applies the same
   2 GB rule. ECR-based components also require the
   device token-exchange role to have `ecr:GetAuthorizationToken`,
   `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`, and `s3:GetObject` in the
   device's region.

**Inference Endpoint Not Responding:**
```bash
ps aux | grep LocalServer
tail -f /aws_dda/greengrass/v2/logs/aws.edgeml.dda.LocalServer.log
netstat -tlnp | grep 5000
```

**Docker Permission Errors:**
```bash
sudo usermod -aG docker ggc_user
sudo systemctl restart greengrass
```

**Frontend Not Accessible on Device** (port 3000):
```bash
docker ps | grep frontend
# Use SSH tunnel: ssh -i "key.pem" -L 3000:localhost:3000 ubuntu@device-ip
```

**GStreamer Pipeline Issues:**
```bash
export GST_DEBUG=3
v4l2-ctl --list-devices
gst-launch-1.0 v4l2src device=/dev/video0 ! autovideosink
```

### Model Loading (Triton Server)

The DDA backend packages the **NVIDIA Triton Inference Server** at
`/opt/tritonserver`. Compiled models are placed in the Triton model repository at
`/aws_dda/dda_triton/triton_model_repo/`. Before deploying a model (or when
debugging a pipeline crash), verify the model loads cleanly in Triton.

```bash
# Test Triton server directly to verify model loading.
cd /opt/tritonserver/bin
./tritonserver --model-repository /aws_dda/dda_triton/triton_model_repo/

# Expected output should show models in READY status:
# +-------------------------------------------+---------+--------+
# | Model                                     | Version | Status |
# +-------------------------------------------+---------+--------+
# | base_model-bd-dda-classification-arm64    | 1       | READY  |
# | marshal_model-bd-dda-classification-arm64 | 1       | READY  |
# | model-bd-dda-classification-arm64         | 1       | READY  |
# +-------------------------------------------+---------+--------+
```

If a model shows `UNAVAILABLE` instead of `READY`, fix the underlying model
error (often a Python syntax error in the model's `model.py`) before deploying
or running the pipeline — a model that fails to load in Triton will crash the
GStreamer inference pipeline (commonly with a `SIGSEGV`).

### GStreamer / Triton Inference Pipeline

DDA uses GStreamer pipelines for video processing and ML inference. The custom
`emltriton` (Triton inference) and `emlcapture` (result capture) plugins live in
`/usr/lib/panoramagst/`. Troubleshoot pipeline issues as follows:

```bash
# Install GStreamer tools if not available
sudo apt update
sudo apt install gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good

# Set GStreamer plugin path for DDA custom plugins
export GST_PLUGIN_PATH=/usr/lib/panoramagst/

# Enable GStreamer debug logging
export GST_DEBUG=3  # or GST_DEBUG=4 for more verbose output

# Confirm the DDA custom plugins are present and loadable
gst-inspect-1.0 | grep -E "emltriton|emlcapture"
gst-inspect-1.0 emltriton
gst-inspect-1.0 emlcapture

# Test the full DDA inference pipeline with a sample image
gst-launch-1.0 filesrc blocksize=-1 location="/aws_dda/bd-classification/test-anomaly-1.jpg" ! \
  jpegdec idct-method=2 ! \
  videoconvert ! \
  videoflip method=automatic ! \
  capsfilter caps=video/x-raw,format=RGB ! \
  emltriton model-repo=/aws_dda/dda_triton/triton_model_repo \
    server-path=/opt/tritonserver \
    model=model-bd-dda-classification-arm64 \
    metadata='{"sagemaker_edge_core_capture_data_disk_path": "/aws_dda/inference-results/test", "capture_id": "test-pipeline"}' \
    correlation-id=test-pipeline ! \
  jpegenc idct-method=2 quality=100 ! \
  emlcapture buffer-message-id=file-target_/aws_dda/inference-results/test-jpg \
    interval=0 \
    meta=triton_inference_output_overlay:file-target_/aws_dda/inference-results/test-overlay.jpg
```

**Debugging pipeline crashes (SIGSEGV)** — work from the simplest case up:

```bash
# 1. Verify the model loads in Triton first (see Model Loading above).
cd /opt/tritonserver/bin
./tritonserver --model-repository /aws_dda/dda_triton/triton_model_repo/

# 2. Test a decode-only pipeline (no inference) to rule out the input image.
export GST_PLUGIN_PATH=/usr/lib/panoramagst/
gst-launch-1.0 filesrc location="/aws_dda/cookies/test-anomaly-3.jpg" ! \
  jpegdec ! videoconvert ! jpegenc ! filesink location="/tmp/test-output.jpg"

# 3. Run the full pipeline INSIDE the backend container (recommended), where the
#    Triton libs and plugin paths are already configured.
docker ps | grep backend
docker exec -it <backend-container-name> bash
# Inside the container:
export GST_PLUGIN_PATH=/usr/lib/panoramagst/
gst-launch-1.0 filesrc blocksize=-1 location="/aws_dda/cookies/test-anomaly-3.jpg" ! \
  emexifextract ! jpegdec idct-method=2 ! videoconvert ! videoflip method=automatic ! \
  capsfilter caps=video/x-raw,format=RGB ! \
  emltriton model-repo=/aws_dda/dda_triton/triton_model_repo \
    server-path=/opt/tritonserver model=model-rajat-segmentation \
    metadata='{"capture_id": "test-pipeline"}' correlation-id=test-pipeline ! \
  jpegenc idct-method=2 quality=100 ! \
  emlcapture buffer-message-id=file-target_/aws_dda/inference-results/test-jpg interval=0
```

**Common GStreamer / Triton issues:**
- **Segmentation fault**: usually a model that failed to load in Triton or a corrupted model file — verify the model is `READY` in Triton first.
- **Plugin not found**: ensure `GST_PLUGIN_PATH` includes `/usr/lib/panoramagst/`.
- **Model loading errors**: confirm the Triton server is running and the `model-repo`/`model` paths are correct.
- **Model syntax errors**: fix Python syntax errors in the model's `model.py` (these surface as `UNAVAILABLE` in Triton).
- **Permission errors**: check file permissions on input images and output directories.
- **Memory issues**: monitor system resources during pipeline execution.

### CloudWatch LogManager Issues

The portal auto-includes `aws.greengrass.LogManager` in deployments to upload device logs to CloudWatch. Logs are organized as:

```
CloudWatch Log Groups:
  /aws/greengrass/GreengrassSystemComponent/{region}/System     # Greengrass system logs
  /aws/greengrass/UserComponent/{region}/{component-name}       # Per-component logs

Log Streams (per device):
  /YYYY/MM/DD/thing/{thing-name}
```

**"Components detected but no logs yet"** in the portal:
- LogManager uploads every 5 minutes — wait for the first upload cycle
- Verify LogManager is installed: `ls /aws_dda/greengrass/v2/logs/aws.greengrass.LogManager.log`
- If no LogManager log file exists, revise the device's deployment (**Revise Deployment** → **Update Deployment**); LogManager is auto-included

**LogManager installed but no CloudWatch log groups:**
```bash
# Check LogManager config has uploadToCloudWatch enabled
grep -A 80 "aws.greengrass.LogManager" /aws_dda/greengrass/v2/config/effectiveConfig.yaml

# Check LogManager logs for errors
tail -30 /aws_dda/greengrass/v2/logs/aws.greengrass.LogManager.log

# Verify device role has CloudWatch Logs permissions
aws iam list-attached-role-policies --role-name GreengrassV2TokenExchangeRole
```

**LogManager running but portal still shows no logs:**
- The portal searches for log streams matching `/thing/{device-id}` — verify your device's thing name matches
- Check CloudWatch directly: `aws logs describe-log-groups --log-group-name-prefix "/aws/greengrass" --region us-east-1`
- Revise the device's deployment (**Revise Deployment** → **Update Deployment**) to ensure LogManager config includes all component names

### Training Issues

**"Caller is not subscribed to the marketplace offering"**: Subscribe to the DDA algorithm in the **UseCase Account** (not Portal Account):
1. Sign in to the UseCase Account console
2. Go to [AWS Marketplace - DDA Algorithm](https://aws.amazon.com/marketplace/pp/prodview-j72hhmlt6avp6)
3. Click **Continue to Subscribe** → **Accept Terms**
4. Wait a few minutes for activation, then retry

**"Manifest validation failed"**: Transform the manifest first using the portal's Transform Manifest button.

**"MaxRuntimeExceeded"**: Increase Max Runtime (recommend 14400-21600s for production datasets).

**Labeling job creation fails** (`UI template can't be accessed`):
```bash
cd edge-cv-portal
./deploy-account-role.sh  # Select Option 1 to update trust policy
```

### Build Issues

**Build Fleet build failed or interrupted:** open the build job on **Builds**.
Its detail page shows the status, the **Build log** (CloudWatch Logs group
`/dda/portal-builds`), the **Execution diagnostics** when the portal retained
them, and a **Build failed** or **Build interrupted** alert
([4.3 Monitor a Build](#43-monitor-a-build)). **Retry build** reruns an
`interrupted` job; for a `failed` job, fix the cause and submit a new build.

The rest of this section applies to manual builds with
`gdk-component-build-and-publish.sh` in a local working tree, for example for a
variant Build Fleet does not offer. Build Fleet runs each job on its own
ephemeral instance, or one job at a time on a dedicated server.

**Parallel builds/publishes clobber each other (wrong image published / staging
corruption).** Do **not** run two component builds or publishes at the same time
from the **same working tree**. The build tags a single shared local image
`flask-app:latest`, pushes to the shared `dda/flask-app` ECR repo (image tag
`<variant>-<version>`), and writes to shared staging dirs (`greengrass-build/`,
`custom-build/`, `.gdk/`). Running two at once races:

- A second build can overwrite `flask-app:latest` in between the first build
  finishing and its `docker tag flask-app:latest <ecr>:<variant>-<version>` step, so the
  **wrong image gets pushed** for that version.
- Concurrent runs overwrite each other's `greengrass-build/`/`custom-build/`
  staging and generated `gdk-config.json`.

Guidance:
- **`gdk-component-build-and-publish.sh`: run sequentially**
  — finish JP6, then JP5. It hardcodes `flask-app:latest` and the shared staging
  dirs, so parallel runs are unsafe even in separate checkouts.
- **Only** the raw backend image build can be parallelized, and only when each
  runs in a **separate working tree** (e.g. `git worktree add`) **and** uses a
  **distinct image name** so it doesn't touch `flask-app:latest`, e.g.:
  ```bash
  # in a second worktree, before building:
  sed -i 's/^    image: flask-app$/    image: flask-app-jp5/' src/docker-compose.yaml
  # copy the gitignored edgemlsdk staging into the worktree first:
  cp -r <main-tree>/src/backend/edgemlsdk src/backend/edgemlsdk
  ```
  Even then, the **publish** step still must be serialized: it pushes the local
  `flask-app:latest` image.

## Project Structure

```
defect-detection-application/
├── edge-cv-portal/              # Portal application
│   ├── infrastructure/          # AWS CDK stacks
│   ├── backend/                 # Lambda functions (Python)
│   │   ├── functions/           # Lambda handlers
│   │   └── layers/shared/       # Shared utilities
│   ├── frontend/                # React app (TypeScript)
│   │   ├── src/pages/           # Page components
│   │   ├── src/components/      # Shared components
│   │   └── src/services/        # API services
│   ├── deploy-account-role.sh   # Account setup
│   ├── launch-arm64-build-server.sh  # Build server launcher
│   └── deploy-frontend.sh       # Frontend deployment
├── src/                         # DDA edge application
│   ├── backend/                 # Python Flask backend
│   ├── frontend/                # React web interface
│   └── edgemlsdk/               # ML inference SDK
├── station_install/             # Edge device installation
│   ├── setup_station.sh         # Device provisioning script
│   ├── launch-edge-device.sh    # EC2 edge device launcher
│   └── edge-device-iam-policy.json
├── datasets/                    # Sample datasets
├── build-custom.sh              # Custom build logic
├── gdk-component-build-and-publish.sh
├── publish-ecr-only.sh          # Publish already-built images (no rebuild)
└── setup-build-server.sh
```

## Documentation

| Document | Description |
|----------|-------------|
| [edge-cv-portal/ADMIN_GUIDE.md](edge-cv-portal/ADMIN_GUIDE.md) | Portal administration guide |
| [INFERENCE_UPLOADER_SETUP.md](INFERENCE_UPLOADER_SETUP.md) | Inference Uploader setup |
| [datasets/README.md](datasets/README.md) | Sample datasets guide |

## Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

This project adheres to the [Amazon Open Source Code of Conduct](CODE_OF_CONDUCT.md).

## License

This project is licensed under the Apache License 2.0. See the [LICENSE](LICENSE) file for details.

## Support

- **GitHub Issues**: Report bugs and feature requests via [GitHub Issues](https://github.com/aws-samples/defect-detection-application/issues)
- **Discussions**: Join the community discussions for questions and support
