# Defect Detection Application (DDA) - Portal Edition

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Docker](https://img.shields.io/badge/docker-%230db7ed.svg?style=flat&logo=docker&logoColor=white)](https://www.docker.com/)

The Defect Detection Application (DDA) is an edge-deployed computer vision solution for quality assurance in discrete manufacturing environments. Originally developed by the AWS EdgeML service team, DDA is now available as an open-source project under the stewardship of the AWS Manufacturing TFC and Auto/Manufacturing IBU.

> **New to DDA?** Watch the [overview video](https://youtu.be/lTLSIMHLy7Y), then follow the [video tutorial series](#video-tutorials).

## Table of Contents

- [Overview](#overview)
- [Video Tutorials](#video-tutorials)
- [User Guide](#user-guide)
- [Deployment](#deployment)
  - [IAM Permissions for Installation](#iam-permissions-for-installation)
  - [Step 1: Deploy Portal Infrastructure](#step-1-deploy-portal-infrastructure-portal-account)
  - [Step 2: Redeploy the Frontend (Optional)](#step-2-redeploy-the-frontend-optional)
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

## Video Tutorials

The DDA video tutorial series on YouTube has an overview video and one video per module. Where this README covers the same topic, the third column links to that section; the last column links to the matching pages of the [User Guide](#user-guide).

| Module | Video | Related README section | User guide |
|--------|-------|------------------------|------------|
| Overview | [Overview](https://youtu.be/lTLSIMHLy7Y) | [Overview](#overview) | [1. Getting started](docs/user-guide/01-getting-started.md) |
| Module 0 | [Portal Setup](https://youtu.be/HtH_4zT23Fo) | [Step 1: Deploy Portal Infrastructure](#step-1-deploy-portal-infrastructure-portal-account) | [2. Use Cases](docs/user-guide/02-use-cases.md), [19. Settings](docs/user-guide/19-settings.md) |
| Module 1 | [Device Setup](https://youtu.be/PFdFycCQXA0) | [Step 7: Set Up Edge Devices (Add Device)](#step-7-set-up-edge-devices-add-device) | [15. Devices](docs/user-guide/15-devices.md) |
| Module 2 | [Labeling](https://youtu.be/pxSoAIOLp18) | [Labeling](#labeling) | [4. Labeling](docs/user-guide/04-labeling.md) |
| Module 3 | [Workflows](https://youtu.be/vpL62wIT218) | - | [8. Workflows](docs/user-guide/08-workflows.md) |
| Module 4 | [Custom Node Designer](https://youtu.be/si5gOzzPhn0) | - | [10. Node Designer](docs/user-guide/10-node-designer.md), [17. Plugin Review](docs/user-guide/17-plugin-review.md) |
| Module 5 | [Synthetic Data](https://youtu.be/n-egAmfZiJw) | - | [5. Synthetic Data](docs/user-guide/05-synthetic-data.md) |
| Module 6 | [Build Servers](https://youtu.be/ZNJBk94Ucjo) | [Step 4: Build the DDA LocalServer Component (Build Fleet)](#step-4-build-the-dda-localserver-component-build-fleet) | [12. Builds](docs/user-guide/12-builds.md), [18. Build Fleet](docs/user-guide/18-build-fleet.md) |
| Module 7 | [Deployments](https://youtu.be/FTYq8aql-CU) | [Deploy DDA Application to Edge Device](#deploy-dda-application-to-edge-device) | [13. Deployments](docs/user-guide/13-deployments.md) |
| Module 8 | [Devices](https://youtu.be/r8nz33LQPoc) | [Devices](#devices) | [15. Devices](docs/user-guide/15-devices.md) |
| Module 9 | [Training Models](https://youtu.be/azKIaPPlhZM) | [Training](#training) | [6. Training](docs/user-guide/06-training.md), [7. Models](docs/user-guide/07-models.md) |
| Module 10 | [Prompt Tuning](https://youtu.be/ZnxGwMxCB1M) | - | [9. Workflow Tuning](docs/user-guide/09-workflow-tuning.md) |
| Module 11 | [User Access](https://youtu.be/_HDKtKvft3c) | [3.2 Create Portal Admin User](#32-create-portal-admin-user) | [20. User Manager](docs/user-guide/20-user-manager.md) |
| Module 12 | [Edge Software](https://youtu.be/pInKqoADn2c) | [Edge Device Management](#edge-device-management) | [22. Device web UI](docs/user-guide/22-device-web-ui.md), [23. Quality Station HMI](docs/user-guide/23-station-hmi.md) |
| Module 13 | [Dashboards](https://youtu.be/wTwUSNUQbac) | - | [14. Workflow dashboards](docs/user-guide/14-workflow-dashboards.md) |

## User Guide

The [User Guide](docs/user-guide/README.md) has one page per section of the system, with screenshots and an explanation of every field and value, written around the blue-plates example. Start with the [guide index](docs/user-guide/README.md) for the end-to-end path and the role table.

| Portal | | Edge |
|--------|--|------|
| [1. Getting started](docs/user-guide/01-getting-started.md) | [13. Deployments](docs/user-guide/13-deployments.md) | [22. Device web UI](docs/user-guide/22-device-web-ui.md) |
| [2. Use Cases](docs/user-guide/02-use-cases.md) | [14. Workflow dashboards](docs/user-guide/14-workflow-dashboards.md) | [23. Quality Station HMI](docs/user-guide/23-station-hmi.md) |
| [3. Data Management](docs/user-guide/03-data-management.md) | [15. Devices](docs/user-guide/15-devices.md) | |
| [4. Labeling](docs/user-guide/04-labeling.md) | [16. Debug log](docs/user-guide/16-debug-log.md) | |
| [5. Synthetic Data](docs/user-guide/05-synthetic-data.md) | [17. Plugin Review](docs/user-guide/17-plugin-review.md) | |
| [6. Training](docs/user-guide/06-training.md) | [18. Build Fleet](docs/user-guide/18-build-fleet.md) | |
| [7. Models](docs/user-guide/07-models.md) | [19. Settings](docs/user-guide/19-settings.md) | |
| [8. Workflows](docs/user-guide/08-workflows.md) | [20. User Manager](docs/user-guide/20-user-manager.md) | |
| [9. Workflow Tuning](docs/user-guide/09-workflow-tuning.md) | [21. Audit Logs](docs/user-guide/21-audit-logs.md) | |
| [10. Node Designer](docs/user-guide/10-node-designer.md) | | |
| [11. Components](docs/user-guide/11-components.md) | | |
| [12. Builds](docs/user-guide/12-builds.md) | | |

## Deployment

### Architecture Overview

DDA supports both single-account and multi-account architectures:

**Single-Account** (Recommended for Getting Started)
- All components in one AWS account
- Simplest setup and management
- Deployed end to end by one command, `./deploy-portal.sh single-account`, which also creates the `DDASageMakerExecutionRole` (it runs `./deploy-account-role.sh single-account` as its first step)

**Multi-Account** (Recommended for Production)
- Separates Portal, UseCase, and Data accounts
- Better security and governance
- Requires cross-account IAM roles, created by running `./deploy-portal.sh usecase` in the UseCase account and `./deploy-portal.sh data` in the Data account (see [Step 5](#step-5-set-up-accounts))

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

- AWS CLI configured, with a region: a default region (`aws configure`) or `AWS_REGION` (`deploy-portal.sh` also takes `--region <region>`)
- Credentials for an IAM role with the DDA installer permissions. An administrator user is not needed: see [IAM Permissions for Installation](#iam-permissions-for-installation)
- Node.js 18+, Python 3.11+
- AWS CDK: `npm install -g aws-cdk`
- Docker, with its daemon running and reachable by your user (see [Docker](#docker) below)
- [AWS Marketplace subscription](https://aws.amazon.com/marketplace/pp/prodview-j72hhmlt6avp6) (in UseCase Account — see below)

> `deploy-portal.sh` (Step 1.2) checks the AWS CLI, your credentials, the region, Node.js, CDK and Docker before it changes anything, and stops when the account/region is not bootstrapped (or its bootstrap version is below 21), naming the administrator setup `iam/setup-account.sh` (see [IAM Permissions for Installation](#iam-permissions-for-installation)). With `--allow-admin-bootstrap` it runs a plain `cdk bootstrap` instead, which gives the CloudFormation execution role `AdministratorAccess`. It takes the region from `--region`, else `AWS_REGION` / `AWS_DEFAULT_REGION`, else the CLI's default region, and every step deploys to that region. For the `single-account` topology it also creates the Greengrass component artifact bucket if it is absent (see [Component artifact bucket](#component-artifact-bucket)) and stops if a previous failed deploy left a retained resource behind that would collide on create. It does not check the Marketplace subscription or the API Gateway CloudWatch Logs role from Step 1.1.

#### Docker

Docker is needed at **synthesis** time, not only at deploy time: the compute stack stages a Lambda layer asset (`ImagingLayer`) and builds a Lambda container image by shelling out to `docker`. Without it the run fails while synthesizing the CDK app — during the `auth` step, before anything is deployed — with:

```
«FailedToBundleAsset» Failed to bundle asset EdgeCVPortalComputeStack/ImagingLayer/Code/Stage
Error: spawnSync docker ENOENT
```

On Ubuntu, install from Docker's official repository (it carries the buildx plugin that CDK image assets use). On Debian, use `debian` in place of `ubuntu` in both URLs and `$VERSION_CODENAME` as the codename:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

On RHEL/Fedora/Amazon Linux: `sudo dnf install -y docker` (older Amazon Linux 2: `yum`). Avoid the snap build of Docker — its confinement blocks bind mounts from arbitrary paths, and CDK bundling bind-mounts the asset staging directory into the container.

Then enable the daemon and grant your user access:

```bash
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Group membership only applies to a **new** login session, so start a fresh shell (or run `newgrp docker`) and confirm with `docker info` before deploying. A `permission denied` on `/var/run/docker.sock` means the group change has not taken effect in your current shell; that is a different failure from Docker being missing entirely.

If you build with another container runtime through CDK's `CDK_DOCKER` variable (for example `CDK_DOCKER=finch`), `deploy-portal.sh` checks that command instead of `docker`.

`./deploy-portal.sh --install-docker` performs the install above automatically (from the Ubuntu or the Debian repository, whichever matches the host). It needs root or `sudo` and a recognized distribution, and it is opt-in by design — the default is to stop with instructions rather than install system packages on your host unasked.

#### Component artifact bucket

The compute stack configures, but never creates, the Greengrass component artifact bucket `dda-component-<region>-<account-id>`: it applies a CORS configuration and a staging lifecycle rule to it for the static image pin upload flow. That bucket normally comes into existence as a side effect of publishing a Greengrass component with GDK, so an account that has never published one does not have it, and the deploy fails partway through `EdgeCVPortalComputeStack` with `The specified bucket does not exist`.

`deploy-portal.sh` creates the bucket when it is absent and leaves it untouched when your account already owns it. It stops, creating nothing, when the name belongs to another account or when it cannot tell (the check is `aws s3api head-bucket` with `--expected-bucket-owner <account-id>`). The bucket is deliberately **not** managed by CDK: GDK owns it, and in an established account it holds live component artifacts, so putting it under CloudFormation's delete lifecycle would risk the component artifact store. To create it by hand instead:

```bash
# every region except us-east-1 requires the LocationConstraint
aws s3api create-bucket --bucket "dda-component-<region>-<account-id>" \
  --region "<region>" --create-bucket-configuration "LocationConstraint=<region>"

# us-east-1 must NOT pass --create-bucket-configuration
aws s3api create-bucket --bucket "dda-component-us-east-1-<account-id>" --region us-east-1
```

#### Retrying a failed first deploy

A failed *initial* create of a stack leaves it in `ROLLBACK_COMPLETE`, which CloudFormation will not update; the next `cdk deploy` deletes that stack and creates it again. Resources whose removal policy is `RETAIN` are kept by the rollback and are no longer part of the stack, and `EdgeCVPortalComputeStack` has three with fixed names: the ECR repository `dda-detector-export` and the DynamoDB tables `dda-portal-edge-credentials` and `dda-portal-account-sync`. The next attempt then fails at changeset validation with `already exists`, and because each attempt re-creates them before failing further in, every rollback leaves them behind again.

`deploy-portal.sh` detects this before synthesis (when the compute stack does not exist yet, or is in `ROLLBACK_COMPLETE`, `ROLLBACK_FAILED` or `REVIEW_IN_PROGRESS`) and prints the cleanup commands for the ones it finds, but does not run them; deleting AWS resources is left to you:

```bash
aws ecr delete-repository --repository-name dda-detector-export --region "<region>"
# add --force if it holds images; that deletes those images
aws dynamodb delete-table --table-name dda-portal-edge-credentials --region "<region>"
aws dynamodb delete-table --table-name dda-portal-account-sync --region "<region>"
# deleting a table deletes its items; after a failed first deploy both are empty
```

The rollback also keeps the stack's KMS key for those two tables (description `DDA Portal: encryption at rest for dda-portal-edge-credentials and dda-portal-account-sync...`). It has no fixed name, so it does not block the retry; once the tables it encrypted are deleted you can schedule its deletion with `aws kms schedule-key-deletion`.

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

### IAM Permissions for Installation

Installing DDA does not need an administrator user. An administrator does a one-time setup in each account (create the policies, bootstrap CDK with them, create an installer role); the person installing DDA then runs every step with the installer role. The policy documents are in [`edge-cv-portal/iam/`](edge-cv-portal/iam/).

#### Roles involved

| Role | Created by | Purpose |
|------|------------|---------|
| Installer role (for example `DDAInstaller`) | An administrator, once per account | Runs the deploy scripts and the CLI commands in this README. Its direct calls are listed in [What each step needs](#what-each-step-needs) |
| CDK deploy, file-publishing and image-publishing roles (`cdk-hnb659fds-<name>-role-<account>-<region>`) | `cdk bootstrap` | The CDK CLI assumes them: the deploy role starts the CloudFormation deployments, the publishing roles upload the Lambda code and the container image |
| CloudFormation execution role (`cdk-hnb659fds-cfn-exec-role-<account>-<region>`) | `cdk bootstrap` | Creates, updates and deletes the DDA resources. A plain `cdk bootstrap` attaches `AdministratorAccess` to it, so bootstrap with the DDA execution policies instead (below) |

The installer role does not use the CDK lookup role: the DDA CDK apps make no context lookups. `cdk deploy` and `cdk diff` print that the current credentials could not be used to assume the lookup role and then proceed with the installer's own permissions; that warning is expected. When a deploy fails, the CDK CLI also tries to read CloudTrail for the cause, which the installer policy does not allow: read the failed resource's reason in the stack events instead.

#### Policy files

`iam/setup-account.sh` fills in `ACCOUNT_ID` and `REGION`; if you create a policy by hand, replace them first. The ARNs use the `aws` partition.

| Account | File in `edge-cv-portal/iam/` | Policy name | Attach to |
|---------|-------------------------------|-------------|-----------|
| Portal (also single-account) | `portal-installer-policy.json` | `dda-installer-portal` | Installer role |
| Portal (also single-account) | `portal-cfn-execution-iam-policy.json`, `portal-cfn-execution-app-policy.json`, `portal-cfn-execution-platform-policy.json`, `portal-cfn-execution-network-policy.json` | `dda-cfn-exec-portal-iam`, `dda-cfn-exec-portal-app`, `dda-cfn-exec-portal-platform`, `dda-cfn-exec-portal-network` | CloudFormation execution role, through `cdk bootstrap` |
| UseCase | `usecase-installer-policy.json` | `dda-installer-usecase` | Installer role |
| UseCase | `usecase-cfn-execution-policy.json` | `dda-cfn-exec-usecase` | CloudFormation execution role, through `cdk bootstrap` |
| Data | `data-installer-policy.json` | `dda-installer-data` | Installer role |
| Data | `data-cfn-execution-policy.json` | `dda-cfn-exec-data` | CloudFormation execution role, through `cdk bootstrap` |
| Portal (also single-account) | `portal-permissions-boundary-policy.json` | `dda-portal-permissions-boundary` | Permissions boundary of the CloudFormation execution role (through `cdk bootstrap --custom-permissions-boundary`) and of every role the portal app creates |
| Portal (also single-account) | `sagemaker-execution-boundary-policy.json` | `dda-sagemaker-execution-boundary` | Permissions boundary of `DDASageMakerExecutionRole`: the installer can create that role and write its inline policies only with it |
| Any | `cdk-bootstrap-policy.json` | `dda-cdk-bootstrap` | Optional: a separate bootstrap role, if an administrator does not run `cdk bootstrap` (not the installer role) |
| UseCase (the Portal Account in single-account) | AWS managed policy `AWSMarketplaceManageSubscriptions` | - | Installer role, for the Marketplace subscription only |

The Portal execution policy is split into four files because IAM limits a managed policy to 6,144 characters.

The Portal execution policies manage KMS keys and EC2 network resources (VPCs, subnets, route tables, internet gateways, security groups, VPC endpoints) only when they carry the tag `dda-portal:managed=true`, the tag the portal already uses for its buckets and core devices. The portal stacks create every key and network resource with that tag, and the execution role can create one only with the tag in the create request. It can add EC2 tags only while creating a resource or to a resource that already has the tag, so it cannot take over another network resource by tagging it. KMS is weaker: to create a key with tags, KMS requires `kms:TagResource` on every key in the account and Region, so the execution role can still add `dda-portal:managed=true` to another key and then manage it. On an account deployed before these conditions, `iam/setup-account.sh` first adds the tag to the KMS keys and network resources of the `EdgeCVPortal*` stacks, as the administrator; a deploy that changes an untagged one is refused. IAM Access Analyzer reports `apigateway:TagResource` and `apigateway:UntagResource` in `portal-cfn-execution-app-policy.json` as unknown actions. Keep them: API Gateway checks `apigateway:TagResource` when CloudFormation creates the stage of the MCP server's HTTP API, and the deploy fails without it.

**Permissions boundary (Portal Account).** `iam/setup-account.sh portal` bootstraps CDK with `dda-portal-permissions-boundary` as the permissions boundary of the CloudFormation execution role. That boundary lets a role be created, or given a boundary, only with the same boundary, so every role the portal app creates carries it: the deploy scripts read the boundary from the `CDKToolkit` stack and pass `-c ddaPermissionsBoundary=dda-portal-permissions-boundary`, and `bin/app.ts` puts it on every role. Within it a role can use only the AWS services the portal uses. It can change only the portal's own roles, policies and instance profile, and pass only the portal's roles. It cannot create IAM users or access keys or remove a boundary. The one exception is the devices' `GreengrassV2TokenExchangeRole`, which Quick Setup or the Greengrass installer creates without a boundary. A role inside the boundary can still use those services on any resource in the account; its own policies decide which. To upgrade an install, run `iam/setup-account.sh portal` again, then deploy: CloudFormation adds the boundary to every role. `DDA_PERMISSIONS_BOUNDARY=<policy name>` (or `none`) overrides the name the deploy scripts read. The UseCase and Data accounts do not use a boundary yet.

#### One-time setup (administrator)

Run [`edge-cv-portal/iam/setup-account.sh`](edge-cv-portal/iam/setup-account.sh) once in each account and region, as an administrator, with that account's kind: `portal` (also a single-account install), `usecase` or `data`.

```bash
cd edge-cv-portal
# The user or role that assumes the installer role, for example an IAM Identity Center role
./iam/setup-account.sh portal --installer-principal "<principal-arn>" --region "<region>"
```

The script:

1. Fills the account and region into the policy files. In the Portal Account it first tags the KMS keys and network resources of the `EdgeCVPortal*` stacks `dda-portal:managed=true`, which the execution policies require.
2. Creates the policies, or adds a new default version when a document changed. That covers the execution policies, the installer policy and, in the Portal Account, the `DDASageMakerExecutionRole` permissions boundary. It also puts that boundary on a `DDASageMakerExecutionRole` left by an earlier install.
3. Bootstraps CDK with the execution policies instead of `AdministratorAccess`.
4. Creates the `DDAInstaller` role, trusting `--installer-principal`, and attaches the installer policy. An existing role keeps its trust policy.

Re-running it is safe. `--dry-run` prints the calls that change anything instead of making them, and `--help` lists the other options. For a second region in the same account, pass `--name-suffix` (for example `--name-suffix -eu-west-1`) so that region gets its own policies, then attach the second installer policy to the same installer role.

For the Marketplace subscription (the UseCase Account, or the Portal Account in single-account), attach the AWS managed policy `AWSMarketplaceManageSubscriptions` to the installer role, subscribe, then detach it.

With IAM Identity Center, use a permission set with the same customer managed policies instead of the `DDAInstaller` role.

- **Bootstrap before the first deploy.** A plain `cdk bootstrap` would give the execution role `AdministratorAccess`, so `deploy-portal.sh`, `infrastructure/deploy-auth.sh` and `deploy-account-role.sh` no longer run one. On an account or region that is not bootstrapped (or is below version 21), they stop and name `iam/setup-account.sh`. To bootstrap with `AdministratorAccess` anyway, run `deploy-portal.sh` with `--allow-admin-bootstrap`, or set `DDA_ALLOW_ADMIN_BOOTSTRAP=1` for the other two. A later `cdk bootstrap` keeps the execution policies unless you pass new ones.
- **Already bootstrapped with `AdministratorAccess`?** Run `iam/setup-account.sh`: its bootstrap replaces it.
- **The execution role is shared.** Every CDK app that deploys to the same account and region with the default bootstrap qualifier uses it, so after the setup those apps get only the DDA policies. Give DDA its own accounts, or add those apps' policies with more `--cloudformation-execution-policies` options.
- **Data bucket names.** If you give `deploy-portal.sh data` data bucket names (`--data-buckets`), the Data stack sets each bucket's policy. Before you run `iam/setup-account.sh data`, replace `DATA_BUCKET_NAME` in `data-cfn-execution-policy.json` with those buckets, one ARN each. Otherwise leave it: it matches no bucket, and the portal sets the bucket policies when you onboard each use case. The stack replaces a bucket's whole policy, so it drops statements already on the bucket, and a later update of the Data stack drops the ones the portal added.
- **Bootstrapping without an administrator.** `cdk-bootstrap-policy.json` lists what `cdk bootstrap` needs, for a separate bootstrap role. Treat that role as privileged: it can write the inline policies of the `cdk-hnb659fds-*` roles, which the installer assumes. It attaches to the bootstrap roles only `ReadOnlyAccess`, `AWSCloudFormationReadOnlyAccess` (both used by the bootstrap template) and policies named `dda-cfn-exec-*`, so a plain `cdk bootstrap` rolls back with it; delete the `CDKToolkit` stack and run `iam/setup-account.sh`. Only an administrator creates the `dda-cfn-exec-*` policies: neither the installer policies nor the execution policies allow it.

#### What each step needs

Every `cdk deploy` goes through the CDK roles. The table lists the calls each step makes with the installer role itself; the installer policy files allow these calls, except where a row says otherwise, and no others.

| Step | Account | Calls made with the installer role |
|------|---------|------------------------------------|
| [1.1](#11-configure-cloudwatch-logs-for-api-gateway) API Gateway logging role | Portal | `iam:CreateRole` and `iam:GetRole` on `APIGatewayCloudWatchLogsRole`, `iam:AttachRolePolicy` (only `AmazonAPIGatewayPushToCloudWatchLogs`), `iam:PassRole` to API Gateway; `apigateway:GET` and `apigateway:PATCH` on the account settings |
| [1.2](#12-deploy-the-portal-with-one-command) preflight checks | Portal | `ssm:GetParameter` on `/cdk-bootstrap/hnb659fds/version`, `/dda-portal/trusted-usecase-account-ids` and `/dda-portal/detector-export-image`; `ssm:PutParameter` on the trusted list; `cloudformation:DescribeStacks` and `cloudformation:GetTemplate` on the `EdgeCVPortal*` stacks and `CDKToolkit`; `s3:ListBucket` and `s3:CreateBucket` on `dda-component-<region>-<account>`; `ecr:DescribeRepositories` and `dynamodb:DescribeTable` on the three retained resources |
| 1.2 `account-role` step | Portal | `iam:GetRole` on `DDASageMakerExecutionRole` and `iam:GetPolicy` on its boundary; `iam:CreateRole` and `iam:PutRolePolicy` on `DDASageMakerExecutionRole` only while it carries the `dda-sagemaker-execution-boundary` permissions boundary (after a region change, an administrator updates its trust policy); `iam:CreatePolicy` and `iam:GetPolicy` on `DDAPortalComponentAccessPolicy`; for `Greengrass_ServiceRole`: `iam:CreateRole`, `iam:GetRole`, `iam:AttachRolePolicy` (only `AWSGreengrassResourceAccessRolePolicy`) and `iam:PassRole` to Greengrass; `greengrass:GetServiceRoleForAccount` and `greengrass:AssociateServiceRoleToAccount` |
| 1.2 `auth`, `infrastructure` and `frontend` steps, [Step 2](#step-2-redeploy-the-frontend-optional) | Portal | `sts:AssumeRole` on the CDK deploy and publishing roles; `cloudformation:DescribeStackResources` on the `EdgeCVPortal*` stacks (the CDK CLI reads the failed resources when a deploy fails); `lambda:ListFunctions` (reads the deployed `PORTAL_REGISTRY_ENFORCED` value); `apigateway:GET` on the API's authorizers; `s3:ListBucket`, `s3:PutObject` and `s3:DeleteObject` on `dda-portal-frontend-<account>`; `cloudfront:CreateInvalidation` |
| [3.2](#32-create-portal-admin-user) first PortalAdmin | Portal | `cognito-idp:AdminCreateUser`, `cognito-idp:AdminSetUserPassword` and `cognito-idp:AdminGetUser` on the user pool; `dynamodb:PutItem` and `dynamodb:GetItem` on `dda-portal-user-roles`. `backfill_portal_registry.py` also uses `cognito-idp:ListUsers` |
| [4.1](#41-one-time-setup) build alerts (optional) | Portal | `sns:Subscribe` on `dda-portal-build-alerts` |
| [5.1](#51-usecase-account-required) `deploy-portal.sh usecase` | UseCase | `sts:AssumeRole` on the CDK deploy and file-publishing roles; `ssm:GetParameter` on the bootstrap version; the `Greengrass_ServiceRole` calls listed above |
| 5.1 trust the UseCase Account | Portal | The trusted-list `ssm:GetParameter` and `ssm:PutParameter`, `cloudformation:GetTemplate`, then the 1.2 deploy |
| [5.2](#52-data-account-optional-for-multi-account) `deploy-portal.sh data` | Data | `sts:AssumeRole` on the CDK deploy and file-publishing roles; `ssm:GetParameter` on the bootstrap version |
| Marketplace subscription | UseCase | `AWSMarketplaceManageSubscriptions`, in the AWS Console |
| Tag an existing bucket for a use case ([1.2](#12-deploy-the-portal-with-one-command) note) | Bucket owner | `s3:TagResource` and `s3:PutBucketAbac` on that bucket. Not in the files, because the bucket names are yours |
| Steps 4, 6 and 7, deployments | - | Nothing: the portal's own roles do this work, and devices use the setup token |

#### What the execution policies allow

CloudFormation can create, change and delete only the resource types the DDA stacks use, and only under DDA's names where a resource has one:

- **IAM:** roles named `EdgeCVPortal*` (the names CloudFormation generates for the portal stacks) and each stack's fixed-name roles (`DDAStationProvisioningRole` and `dda-build-role` in the Portal Account, the six `DDA*` roles of the UseCase stack, the two of the Data stack); managed policies named `EdgeCVPortal*` or `DDAPortalComponentAccessPolicy`; the `dda-build-role` instance profile. It attaches only `AWSLambdaBasicExecutionRole`, `AmazonSSMManagedInstanceCore`, `AmazonEventBridgeFullAccess` and its own `EdgeCVPortal*` policies, passes roles only to the services that use them, and creates only the ECS and API Gateway service-linked roles (on a new account, the first deploy creates them).
- **Read-only checks:** the `/cdk-bootstrap/hnb659fds/version` parameter, which CloudFormation resolves with this role, and the checks CloudFormation runs before a deployment (IAM account summary, Lambda account settings, service quotas, the CDK staging bucket's configuration).
- **Named resources:** `dda-portal-*` tables, queues, topics, Firehose streams, schedules and `/dda-portal/*` parameters; `dda-portal-*` and `EdgeCVPortal*` alarms; `dda-portal-*` and `edgecv-portal-telemetry-*` buckets; `EdgeCVPortal*` and `dda-*` Lambda functions; `dda-*` ECR repositories, ECS clusters, CodeBuild projects and state machines; `dda_*` IoT rules; `dda-*` and `EdgeCVPortal*` EventBridge rules; `/dda/*`, `/dda-portal/*` and `/aws/kinesisfirehose/dda-portal-*` log groups; `EdgeCVPortal*` nested stacks.
- **Without a DDA name to scope by** (limited to the deploy region where the service is regional): API Gateway APIs, the Cognito user pool, the CloudFront distribution, KMS keys, Lambda layers and event source mappings, ECS task definitions, CloudWatch Logs deliveries and resource policies, and the VPC networking.
- **Not allowed:** reading or writing table items or bucket objects (other than the CDK assets it deploys), launching instances, and changing any other named resource.

These policies keep CloudFormation to what DDA deploys, but they are not a privilege boundary: anyone who can use the installer role can deploy a template that creates an `EdgeCVPortal*` role with any inline policy. Grant the installer role only to the people who install and update DDA. A hard boundary would need an IAM permissions boundary on every role DDA creates, which the DDA stacks do not set today.

The files match the stacks in this version of the repository. If a later version needs an action they do not list, the deploy stops with a stack event that says the `cdk-hnb659fds-cfn-exec-role` is not authorized to perform that action: add it to the matching `dda-cfn-exec-*` policy (`aws iam create-policy-version --set-as-default`) and deploy again. `teardown-portal.sh` and the maintenance scripts in `edge-cv-portal/scripts/` are not covered.

### Step 1: Deploy Portal Infrastructure (Portal Account)

> **Video:** [Module 0: Portal Setup](https://youtu.be/HtH_4zT23Fo) · **User guide:** [1. Getting started](docs/user-guide/01-getting-started.md)

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

# 2. Attach the AWS managed policy API Gateway uses to write logs
aws iam attach-role-policy \
  --role-name APIGatewayCloudWatchLogsRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs

# 3. Get the role ARN and set it in API Gateway, in the region you deploy the portal to
ROLE_ARN=$(aws iam get-role --role-name APIGatewayCloudWatchLogsRole --query 'Role.Arn' --output text)
aws apigateway update-account \
  --patch-operations op=replace,path=/cloudwatchRoleArn,value=$ROLE_ARN \
  --region <REGION>
```

> **Note**: The role is created once per AWS account; the API Gateway setting is per region. If `aws apigateway get-account --region <REGION>` already shows a `cloudwatchRoleArn`, skip this step.

#### 1.2 Deploy the Portal with One Command

Run the orchestrator from the `edge-cv-portal` directory with the Portal Account's credentials. It deploys the whole portal end to end:

```bash
cd edge-cv-portal
./deploy-portal.sh single-account
```

`deploy-portal.sh` checks the prerequisites (AWS CLI, credentials, region, Node.js, CDK, Docker) and for resources a failed first deploy left behind, resolves the trusted UseCase account list (see below), ensures the Greengrass component artifact bucket exists, bootstraps CDK in the account/region if needed, prints the resolved account, region and log path, and then runs the existing deployment scripts in this order:

1. `./deploy-account-role.sh single-account` — IAM roles and policies (`DDASageMakerExecutionRole`, `DDAPortalComponentAccessPolicy`, `Greengrass_ServiceRole`)
2. `infrastructure/deploy-auth.sh` — the Cognito `EdgeCVPortalAuthStack`
3. `./deploy-infrastructure.sh` — the main CDK stacks (`cdk deploy --all`). On a new install a second `cdk deploy` follows for the workflow debug and telemetry stacks, which attach to the authorizer of the API that the first one creates
4. `./deploy-frontend.sh` — generates `config.json` from the stack outputs, builds and publishes the React frontend, and redeploys the compute stack with the CloudFront domain

Each step's output is shown on the terminal and written to `edge-cv-portal/deploy-portal-<UTC-timestamp>.log` (override with `--log-file <path>`). If a step fails, the command stops, prints `Deployment failed at step: <name>` and the log path, and rolls nothing back; fix the cause and re-run the same command. When every step succeeds it prints a **Deployment Summary** with the Portal URL (the CloudFront domain), the created role ARNs and each step's outcome.

**Trusted UseCase accounts.** The portal's Lambda roles may assume `DDAPortalAccessRole` only in the accounts on the portal's trusted UseCase account list; the same list decides whose `DDASageMakerExecutionRole` may pull the detector-export image and which accounts may send messages to the portal's device queues. A single-account setup needs nothing here: on a first deploy (no list configured, no portal deployed yet) `deploy-portal.sh` uses the Portal Account ID and stores it in the SSM parameter `/dda-portal/trusted-usecase-account-ids`, never overwriting an existing parameter. Every run resolves the list once, before any step, from the first of: the CDK context value `trustedUseCaseAccountIds` (in `infrastructure/cdk.json`, `infrastructure/cdk.context.json` or `~/.cdk.json`), `TRUSTED_USECASE_ACCOUNT_IDS`, the SSM parameter, and the list the deployed portal stacks grant; on a deployed portal it never falls back to the Portal Account ID. It prints the list and its source before the first step, with a `⚠️  <stack>: removes <ids>` line when the deploy drops an account the deployed stacks trust and a `⚠️  <stack>: adds <ids> (the portal may then assume their DDAPortalAccessRole)` line when it trusts a new one, writes both to the log and shows the list in the Deployment Summary (`Trusted accounts:`). When the list cannot be determined (an SSM or CloudFormation read error, a value that is not 12-digit account IDs, or deployed stacks that grant different lists), it stops before any step and prints the commands that set the list. The SSM parameter is not part of any stack, and it is where a multi-account list is kept: to trust a UseCase Account, see [5.1 UseCase Account](#51-usecase-account-required). Anyone who can write the parameter decides, at the next deploy, which accounts' `DDAPortalAccessRole` the portal may assume, so restrict `ssm:PutParameter` on it to the administrators who deploy the portal.

Useful options (`./deploy-portal.sh --help` lists them all):

- `--profile <name>` / `--region <region>` — AWS CLI profile and region for all steps (env: `AWS_PROFILE`, `AWS_REGION`)
- `--sso` — enable SSO on the auth stack; export `SSO_METADATA_URL` (and optionally `SSO_PROVIDER_NAME`, `COGNITO_DOMAIN_PREFIX`) first, see [edge-cv-portal/infrastructure/SSO_SETUP.md](edge-cv-portal/infrastructure/SSO_SETUP.md)
- `--install-docker` — install Docker automatically when it is missing, instead of stopping with instructions. Needs root or `sudo` and a recognized distribution (env: `PORTAL_DEPLOY_INSTALL_DOCKER`)
- `--non-interactive` — never prompt (for CI). The `single-account` topology needs no further inputs:

  ```bash
  ./deploy-portal.sh single-account --non-interactive
  ```

**Manual path (advanced).** The four scripts above remain runnable on their own, in the same order, from `edge-cv-portal/` (`deploy-auth.sh` from `edge-cv-portal/infrastructure/`). Use them to repeat a single step, e.g. `./deploy-frontend.sh` after a frontend change. When you skip the orchestrator on an account/region that is not bootstrapped yet, have an administrator run `iam/setup-account.sh` first (see [IAM Permissions for Installation](#iam-permissions-for-installation)), or, for `AdministratorAccess` on the execution role, bootstrap it from `edge-cv-portal/`: `cdk bootstrap aws://<ACCOUNT_ID>/<REGION>`. There is no `cdk.json` in that directory, so the CLI bootstraps that environment without synthesizing the portal app; run from `infrastructure/`, it would synthesize the app first, which fails until `npm ci` has run there and the trusted UseCase account list is set. On an account that has never published a Greengrass component, also create the component artifact bucket first (see [Component artifact bucket](#component-artifact-bucket)): `deploy-infrastructure.sh` does not create it. `deploy-auth.sh` and `deploy-infrastructure.sh` synthesize the whole portal app, so set the list before you run them: the SSM parameter `/dda-portal/trusted-usecase-account-ids` (a first `./deploy-portal.sh single-account` creates it; or `aws ssm put-parameter --name /dda-portal/trusted-usecase-account-ids --type String --value <ACCOUNT_ID> --region <REGION>`), `TRUSTED_USECASE_ACCOUNT_IDS` (that run only), or the CDK context value `trustedUseCaseAccountIds`; without one they stop with `ComputeStack requires a non-empty trustedUseCaseAccountIds list`. `deploy-frontend.sh` resolves the list the same way as `deploy-portal.sh` and, when none is configured, uses the list the deployed portal stacks grant. `deploy-infrastructure.sh` has no such fallback: it deploys the list the CDK app reads (the CDK context value, `TRUSTED_USECASE_ACCOUNT_IDS`, then the SSM parameter) and prints it first, with the same `⚠️` lines for each account the deploy removes or adds. `deploy-infrastructure.sh` handles the npm install and `cdk deploy` for you. Run on their own, the scripts use `AWS_REGION` (or `AWS_DEFAULT_REGION`) when set, otherwise the CLI's default region; `deploy-auth.sh` also takes `--region`.

> **Registry enforcement and allowed buckets.** Production deployments run with the Portal_Identity registry enforced: `PORTAL_REGISTRY_ENFORCED=true ./deploy-infrastructure.sh`, after `edge-cv-portal/backfill_portal_registry.py` has been applied and the accounts verified (a fresh install starts with enforcement off, and the script prints a warning whenever the value it resolves for a deploy is not `true`). Independently of that flag, the portal reads and writes S3 only in each use case's **allowed buckets** — its configured S3 bucket, its Data Account bucket, and the **Additional S3 buckets** a UseCaseAdmin allows (Use Cases → Edit → Allowed buckets, or "Allow for this use case" in Data Management). A bucket must be tagged `dda-portal:managed=true` (`aws s3control tag-resource --account-id ACCOUNT_ID --resource-arn arn:aws:s3:::BUCKET --tags Key=dda-portal:managed,Value=true`, plus `aws s3api put-bucket-abac --bucket BUCKET --abac-status Status=Enabled` for a bucket reached through a cross-account role) **before** a use case is created with it or allows it; buckets created through the portal are tagged, ABAC-enabled and allowed automatically. See `edge-cv-portal/ADMIN_GUIDE.md`, "Use case buckets and Additional S3 buckets" and "The enforcement flag".

**What gets deployed:** CloudFront, API Gateway, Cognito, DynamoDB, Lambda functions, S3 buckets, IAM roles.

**Save the CDK outputs:** `ApiUrl` (`EdgeCVPortalComputeStack`), `DistributionDomainName` (`EdgeCVPortalFrontendStack`), and `UserPoolId`, `UserPoolClientId`, `AuthConfig` (`EdgeCVPortalAuthStack`). The Deployment Summary prints the Portal URL (`https://` + `DistributionDomainName`), and the stack outputs printed during each `cdk deploy` are recorded in the deploy log, `edge-cv-portal/deploy-portal-<UTC-timestamp>.log`.

### Step 2: Redeploy the Frontend (Optional)

`deploy-portal.sh` already built and published the frontend as its last step. To redeploy only the frontend later (for example after changing it), run:

```bash
cd edge-cv-portal
./deploy-frontend.sh
```

This automatically generates `config.json` from CDK stack outputs, builds the React app, uploads to S3, and invalidates CloudFront cache.

It then redeploys the compute stack with the trusted UseCase account list from the CDK context, `TRUSTED_USECASE_ACCOUNT_IDS` or the SSM parameter `/dda-portal/trusted-usecase-account-ids`, else the list the deployed portal stacks grant (see [Trusted UseCase accounts](#12-deploy-the-portal-with-one-command)). It never substitutes the Portal Account ID, and when the list cannot be determined it stops before it builds, uploads or deploys anything.

### Step 3: Post-Deployment Setup

#### 3.1 Shared Components

The portal shares the following Greengrass components with UseCase accounts:

- **LocalServer** - Core DDA inference component. The backend container packages the **NVIDIA Triton Inference Server**, which loads compiled models and serves inference; the GStreamer pipeline calls into Triton via the `emltriton` plugin. The portal deploy does not create it: build it with Build Fleet in [Step 4](#step-4-build-the-dda-localserver-component-build-fleet) before you create a UseCase, because a variant with no published version is not shared.
- **InferenceUploader** - Optional component for uploading inference results to S3

These are automatically shared when you create a UseCase in the portal. The UseCase creation also provisions the `DDAPortalComponentAccessPolicy` required by edge devices.

#### 3.2 Create Portal Admin User

> **Video:** [Module 11: User Access](https://youtu.be/_HDKtKvft3c) · **User guide:** [20. User Manager](docs/user-guide/20-user-manager.md)

Self sign-up is off, so you create the first PortalAdmin by hand with the AWS CLI, using credentials for the portal account. Two things make it a working admin:

- **Its global row in the Portal_Identity registry**, the DynamoDB table `dda-portal-user-roles`. This row is what grants PortalAdmin, whether `PORTAL_REGISTRY_ENFORCED` is off (the fresh-install default) or on.
- **The `custom:role=PortalAdmin` attribute.** The portal UI reads it to decide which pages to show; without it the admin gets a Viewer UI with no User Manager and none of the Builds, Build Fleet, or Settings pages that Step 4 uses. With enforcement on it grants nothing. Never grant or change a role by editing `custom:role` (for example with `admin-update-user-attributes`); use the User Manager.

Registry background: [ADMIN_GUIDE.md, "Portal Users and Privilege (Portal_Identity Registry)"](edge-cv-portal/ADMIN_GUIDE.md#portal-users-and-privilege-portal_identity-registry).

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
#    Keep the single quotes, and pick a password without a single quote (') in it,
#    because a single quote would end the quoted string.
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

> **Video:** [Module 6: Build Servers](https://youtu.be/ZNJBk94Ucjo) · **User guide:** [12. Builds](docs/user-guide/12-builds.md), [18. Build Fleet](docs/user-guide/18-build-fleet.md)

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
   | JP7 | `aws.edgeml.dda.LocalServer.arm64JP7` | Jetson Thor, JetPack 7 (L4T R38 or R39, Ubuntu 24.04): `arm64_jp7` |
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

- **Single-account setup**: nothing to do — `./deploy-portal.sh single-account` (Step 1.2) already created the `DDASageMakerExecutionRole`, and its first deploy put the Portal Account ID on the trusted UseCase account list.
- **Multi-account setup**: run the orchestrator with the `usecase` topology **in the UseCase Account** (with that account's credentials). It runs only the account-role step, prompts for the Portal Account ID and External ID, and creates `DDAPortalAccessRole` + `DDASageMakerExecutionRole`:

  ```bash
  cd edge-cv-portal
  ./deploy-portal.sh usecase
  ```

  Non-interactive (CI):

  ```bash
  ./deploy-portal.sh usecase --non-interactive \
      --portal-account-id 123456789012 --external-id abc-123
  ```

The Deployment Summary prints the cross-account role ARN and the External ID. Save the generated `usecase-account-*-config.txt` file (written to `edge-cv-portal/`) — you'll upload it when creating a UseCase.

**Trust the UseCase Account in the Portal Account (multi-account, before Step 6).** The portal can assume `DDAPortalAccessRole` only in accounts on its trusted list (Step 1.2), so add the UseCase Account ID to the list and redeploy the portal, with the Portal Account's credentials:

```bash
# In the Portal Account, in the portal's region
# 1. The list the portal trusts now:
aws ssm get-parameter --name /dda-portal/trusted-usecase-account-ids \
    --query Parameter.Value --output text --region <region>
#    ParameterNotFound? Then it is the list the deployed portal stacks grant:
aws cloudformation get-template --stack-name EdgeCVPortalComputeStack --region <region> \
    --query TemplateBody --output json | grep -oE ':iam::[0-9]{12}:role/DDAPortalAccessRole"' | cut -d: -f4 | sort -u
# 2. Store it with the UseCase Account ID added:
aws ssm put-parameter --name /dda-portal/trusted-usecase-account-ids --type String --overwrite \
    --value "<current-ids>,<usecase-account-id>" --region <region>
# 3. Redeploy the portal with it:
cd edge-cv-portal
./deploy-portal.sh single-account --region <region>
```

- The value is a comma-separated list of 12-digit account IDs. Keep the IDs already on it; the Portal Account ID needs to stay only if use cases also run in the Portal Account. Removing an account is the same edit. Before its first step the deploy prints `⚠️  <stack>: adds <id> (the portal may then assume their DDAPortalAccessRole)` for each account it adds and `⚠️  <stack>: removes <id>` for each one it drops; `./deploy-infrastructure.sh`, `./deploy-frontend.sh` and `infrastructure/deploy_portal_fixes.sh` print the same lines, and a `cdk deploy` you run yourself prints neither.
- Deploy the UseCase Account stack (above) before you redeploy: the list also names each account's `DDASageMakerExecutionRole` in the portal's detector-export repository policy.
- Keep a multi-account list in the SSM parameter. `export TRUSTED_USECASE_ACCOUNT_IDS=<ids>` overrides it for one run only: a later run without the variable, including `./deploy-infrastructure.sh` on its own or a `cdk deploy` you run in `infrastructure/`, deploys the parameter's list again and drops every account only the variable added. With no parameter, only `./deploy-portal.sh single-account`, `./deploy-frontend.sh` and `infrastructure/deploy_portal_fixes.sh` fall back to the list the deployed stacks grant; `deploy-auth.sh`, `deploy-infrastructure.sh` and `cdk` stop with `ComputeStack requires a non-empty trustedUseCaseAccountIds list`. A `trustedUseCaseAccountIds` CDK context value in `infrastructure/cdk.json`, `infrastructure/cdk.context.json` or `~/.cdk.json` overrides both.
- Restrict `ssm:PutParameter` on `/dda-portal/trusted-usecase-account-ids` to the administrators who deploy the portal: whoever can write it decides, at the next deploy, which accounts' `DDAPortalAccessRole` the portal may assume.
- Until the redeploy has finished, the onboarding wizard's **Verify Role** fails with `AccessDenied: ` followed by STS's message that the portal's role is not authorized to perform `sts:AssumeRole` on the UseCase Account's `DDAPortalAccessRole`, and creating the use case answers `400 bucket_not_eligible`, shown as "The portal could not assume the role configured for this bucket". A wrong External ID gives the same errors.

> `./deploy-account-role.sh` can still be run directly (menu option 1 = single-account, 2 = UseCase account, 3 = Data account); it is the script the orchestrator runs for you.

#### 5.2 Data Account (Optional, for multi-account)

If your training data lives in a separate AWS account, run the `data` topology **in the Data Account**:

```bash
cd edge-cv-portal
./deploy-portal.sh data
```

For CI add `--non-interactive --portal-account-id <id> --external-id <id>`, and optionally `--data-buckets <bucket1,bucket2>`.

Save the generated `data-account-*-config.txt` file, then register it in the portal:

1. Go to **Settings** → **Data Accounts** → **Add Data Account**
2. Upload the `data-account-*-config.txt` file to auto-fill the fields
3. Enter a name and click **Register**

This only needs to be done once per data account. All future UseCases can select it from a dropdown.

### Step 6: Create UseCase

> **User guide:** [2. Use Cases](docs/user-guide/02-use-cases.md)

1. Go to **Use Cases** → **Onboard New Use Case**
2. Choose setup type (Single Account or Multi-Account)
3. For multi-account: upload `usecase-account-*-config.txt` to auto-fill role details
4. Configure data account (select from registered accounts or enter manually)
5. Select the use case's S3 bucket: an existing bucket must already be tagged `dda-portal:managed=true` (and ABAC-enabled when it is reached through a cross-account role), or a PortalAdmin creates one with **Create new bucket**, which tags and allows it automatically
6. Click **Create Use Case**

This provisions Greengrass components, creates `DDAPortalComponentAccessPolicy`, and sets up S3 buckets. The portal checks each configured bucket's tags through the use case's role at creation and answers `400 bucket_not_eligible` with a remedy for an untagged, unreadable or already-configured bucket (see `edge-cv-portal/ADMIN_GUIDE.md`, "Onboarding a New Customer").

### Step 7: Set Up Edge Devices (Add Device)

> **Video:** [Module 1: Device Setup](https://youtu.be/PFdFycCQXA0) · **User guide:** [15. Devices](docs/user-guide/15-devices.md)

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

- Jetson Thor devices on JetPack 7 (L4T R38, or R39 from JetPack 7.2; Ubuntu 24.04) are recorded as `arm64_jp7` and need `aws.edgeml.dda.LocalServer.arm64JP7`. Create Deployment does not offer them the JP5 or JP6 LocalServer, and it does not offer the JP7 LocalServer to JP5 or JP6 devices.
- An Orin on JetPack 7.2 also reports L4T R39, but setup leaves its architecture unrecorded, because the JP7 build targets Thor's GPU.
- GPU access needs the NVIDIA container runtime registered with Docker. The setup registers it with `nvidia-ctk` or in `/etc/docker/daemon.json`. If the output warns that neither `nvidia-ctk` nor `nvidia-container-runtime` was found, GPU containers will not start: install the NVIDIA Container Toolkit on the device before you deploy.
- DLR-only (Neo-compiled) models are not supported on JP7. Use ONNX models instead ([Using ONNX Models](#using-onnx-models)).
- The JP7 LocalServer includes vLLM ([Using LLMs (vLLM)](#using-llms-vllm)).

## Deploy DDA Application to Edge Device

> **Video:** [Module 7: Deployments](https://youtu.be/FTYq8aql-CU) · **User guide:** [13. Deployments](docs/user-guide/13-deployments.md)

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

> **Video:** [Module 8: Devices](https://youtu.be/r8nz33LQPoc) · **User guide:** [15. Devices](docs/user-guide/15-devices.md)

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

> **User guide:** [3. Data Management](docs/user-guide/03-data-management.md)

- Upload and organize training images to S3
- Browse datasets with image counts
- Register pre-labeled datasets for quick testing

### Labeling

> **Video:** [Module 2: Labeling](https://youtu.be/pxSoAIOLp18) · **User guide:** [4. Labeling](docs/user-guide/04-labeling.md)

- Create Ground Truth labeling jobs (classification & segmentation)
- Status auto-syncs from SageMaker
- Supports pre-drawn masks for segmentation guidance

> **Important**: Labeling work is completed in the Ground Truth UI, not the DDA Portal.

### Manifest Transformation

> **User guide:** [4.5 Monitoring a job](docs/user-guide/04-labeling.md#45-monitoring-a-job)

Ground Truth creates manifests with job-specific attribute names. The portal auto-transforms them to DDA format during training job creation:

- `{job-name}` → `anomaly-label`
- `{job-name}-metadata` → `anomaly-label-metadata`
- `{job-name}-ref` → `anomaly-mask-ref` (segmentation)

### Training

> **Video:** [Module 9: Training Models](https://youtu.be/azKIaPPlhZM) · **User guide:** [6. Training](docs/user-guide/06-training.md), [7. Models](docs/user-guide/07-models.md)

- Select Ground Truth job or pre-labeled dataset
- Auto-detect and transform manifests
- Configure model type (Classification, Segmentation, Robust variants)
- Instance types: ml.g4dn.2xlarge (GPU), ml.p3.2xlarge (GPU), ml.m5.xlarge (CPU)
- Optional auto-compile after training

### Compilation

> **User guide:** [7. Models](docs/user-guide/07-models.md)

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

> **User guide:** [7.2 Smart Import](docs/user-guide/07-models.md#72-smart-import-recommended-for-your-own-models)

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

> **User guide:** [7.3 Manual import and LLM registration](docs/user-guide/07-models.md#73-manual-import-and-llm-registration)

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

> **User guide:** [15.8 Remote Access and Debug](docs/user-guide/15-devices.md#158-remote-access-and-debug)

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

> **Video:** [Module 12: Edge Software](https://youtu.be/pInKqoADn2c) · **User guide:** [22. Device web UI](docs/user-guide/22-device-web-ui.md), [23. Quality Station HMI](docs/user-guide/23-station-hmi.md)

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

**`deploy-portal.sh` stops with `Deployment failed at step: <name>`**: the failed step's full output is in the log file the command printed (`edge-cv-portal/deploy-portal-<UTC-timestamp>.log`). Nothing is rolled back; fix the cause and re-run the same `./deploy-portal.sh` command — it re-runs every step, and the steps that already succeeded are safe to repeat.

**CDK Bootstrap Error** (`SSM parameter /cdk-bootstrap/hnb659fds/version not found`) — the account/region is not bootstrapped. Have an administrator run `edge-cv-portal/iam/setup-account.sh` (it bootstraps with the DDA execution policies), or run `./deploy-portal.sh --allow-admin-bootstrap`. For a plain bootstrap by hand, which gives the execution role `AdministratorAccess`, run it from `edge-cv-portal/`, which has no `cdk.json`:
```bash
cd edge-cv-portal
cdk bootstrap aws://YOUR_ACCOUNT_ID/YOUR_REGION
```
From `edge-cv-portal/infrastructure/` the CLI synthesizes the portal app before it bootstraps, which fails until `npm ci` has run there and the trusted UseCase account list is set.

**`ComputeStack requires a non-empty trustedUseCaseAccountIds list`** (from `deploy-auth.sh`, `deploy-infrastructure.sh` or a `cdk` command you run in `infrastructure/`): no trusted UseCase account list is set. Run `./deploy-portal.sh single-account`, which creates the SSM parameter on a first deploy, or set the list first: `aws ssm put-parameter --name /dda-portal/trusted-usecase-account-ids --type String --value <ACCOUNT_ID> --region <REGION>` (comma-separated for several accounts), or, for one run only, `export TRUSTED_USECASE_ACCOUNT_IDS=<ACCOUNT_ID>`. See [Trusted UseCase accounts](#12-deploy-the-portal-with-one-command).

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
# Multi-account: refresh the managed policy by redeploying the UseCase Account
# stack, which owns DDAPortalComponentAccessPolicy. Run in the UseCase Account,
# in the region the UseCase stack was deployed to, with the same Portal Account
# ID and External ID as before (see below).
cd edge-cv-portal
./deploy-portal.sh usecase --region <region>

# Single-account (or to fix one device role directly): add the permissions to the device role:
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

`./deploy-portal.sh usecase` redeploys the whole `DDAPortalUseCaseAccountStack`. The Portal Account ID and External ID you enter go into the `DDAPortalAccessRole` trust policy, so reuse the values from the first run (both are in `edge-cv-portal/usecase-account-*-config.txt`; answer `Y` when the script offers to reuse its External ID). A different value, including the new External ID a blank answer generates, locks the portal out of the account. A single-account setup has no script that updates an existing `DDAPortalComponentAccessPolicy` (`deploy-account-role.sh` leaves it as is), so use `put-role-policy` there.

Pass the region the UseCase stack was deployed to with `--region <region>`: the config file does not record it, and without the flag the command uses `AWS_REGION` / `AWS_DEFAULT_REGION`, else your CLI default region. To find that region (read-only, in the UseCase Account; it needs no default region):

```bash
for r in $(aws ec2 describe-regions --region us-east-1 --query 'Regions[].RegionName' --output text); do
  aws cloudformation describe-stacks --stack-name DDAPortalUseCaseAccountStack --region "$r" \
    --query 'Stacks[0].StackName' --output text >/dev/null 2>&1 && echo "$r"
done
```

Without the config file, read both values from the role's trust policy in the UseCase Account, then pass them explicitly (never leave the External ID blank, which generates a new one):

```bash
aws iam get-role --role-name DDAPortalAccessRole --query 'Role.AssumeRolePolicyDocument'
# Principal.AWS                          -> arn:aws:iam::<Portal Account ID>:root
# Condition.StringEquals."sts:ExternalId" -> the External ID
./deploy-portal.sh usecase --region <region> --non-interactive \
    --portal-account-id <Portal Account ID> --external-id <External ID>
```

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
│   ├── iam/                     # Installer and CloudFormation execution IAM policies
│   ├── deploy-portal.sh         # One-command portal deploy (orchestrates the deploy-*.sh scripts)
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

**MCP servers (AI assistants).** The portal and the LocalServer each serve a
Model Context Protocol endpoint, so an assistant such as Kiro can work with
them as the signed-in user, read-only in this release. The portal server is
deployed with the portal and connects through Cognito sign-in (remote OAuth;
no stdio package is distributed): see the
[Administrator Guide: MCP server (AI assistants)](edge-cv-portal/ADMIN_GUIDE.md#mcp-server-ai-assistants).
The edge server is off by default and enabled per device: see
[docs/edge-mcp-server.md](docs/edge-mcp-server.md).

| Document | Description |
|----------|-------------|
| [docs/user-guide/README.md](docs/user-guide/README.md) | User guide: one page per Portal section, the device web UI and the station HMI, with screenshots and every field explained |
| [edge-cv-portal/ADMIN_GUIDE.md](edge-cv-portal/ADMIN_GUIDE.md) | Portal administration guide |
| [docs/edge-mcp-server.md](docs/edge-mcp-server.md) | Edge MCP server: enabling, connecting, role gating and tools |
| [INFERENCE_UPLOADER_SETUP.md](INFERENCE_UPLOADER_SETUP.md) | Inference Uploader setup |
| [datasets/README.md](datasets/README.md) | Sample datasets guide |
| [Video Tutorials](#video-tutorials) | Video tutorial series: an overview and Modules 0-12 |

## Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

This project adheres to the [Amazon Open Source Code of Conduct](CODE_OF_CONDUCT.md).

## License

This project is licensed under the Apache License 2.0. See the [LICENSE](LICENSE) file for details.

## Support

- **GitHub Issues**: Report bugs and feature requests via [GitHub Issues](https://github.com/aws-samples/defect-detection-application/issues)
- **Discussions**: Join the community discussions for questions and support
