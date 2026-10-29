# DDA Portal - Administrator Guide

Complete guide for deploying, configuring, and managing the Defect Detection Application (DDA) Portal.

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Initial Portal Deployment](#initial-portal-deployment)
3. [Account Setup](#account-setup)
4. [Onboarding a New Customer](#onboarding-a-new-customer)
5. [Creating a UseCase](#creating-a-usecase)
6. [Portal Features](#portal-features)
7. [Troubleshooting](#troubleshooting)
8. [User Roles and Permissions](#user-roles-and-permissions)
9. [Portal Users and Privilege (Portal_Identity Registry)](#portal-users-and-privilege-portal_identity-registry)
10. [MCP server (AI assistants)](#mcp-server-ai-assistants)

**Related Guides:**
- [README: Deployment](../README.md#deployment) - Portal deployment, Build Fleet builds, accounts, and device setup
- [README: 3.1 Shared Components](../README.md#31-shared-components) - Greengrass component provisioning

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         PORTAL ACCOUNT                                   │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐    │
│  │  CloudFront │  │ API Gateway │  │   Cognito   │  │  DynamoDB   │    │
│  │  (Frontend) │  │   (REST)    │  │   (Auth)    │  │  (Storage)  │    │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘    │
│         │                │                                              │
│         └────────────────┼──────────────────────────────────────────────┤
│                          │                                              │
│                    Lambda Functions                                     │
│                          │                                              │
└──────────────────────────┼──────────────────────────────────────────────┘
                           │
            ┌──────────────┴──────────────┐
            │      AssumeRole (STS)       │
            │      with External ID       │
            ▼                             ▼
┌─────────────────────────────┐  ┌─────────────────────────────┐
│     USECASE ACCOUNT         │  │     DATA ACCOUNT (Optional) │
│  ┌─────────────────────┐    │  │  ┌─────────────────────┐    │
│  │ DDAPortalAccessRole │    │  │  │  DDADataAccessRole  │    │
│  └─────────────────────┘    │  │  └─────────────────────┘    │
│                             │  │                             │
│  • SageMaker Training       │  │  • S3 Training Data         │
│  • Ground Truth Labeling    │  │  • Tagged Buckets           │
│  • Greengrass Components    │  │                             │
│  • IoT Core Devices         │  │                             │
│  • S3 Buckets (if no Data)  │  │                             │
└─────────────────────────────┘  └─────────────────────────────┘
```

### Account Types

| Account | Purpose | Required |
|---------|---------|----------|
| **Portal Account** | Hosts the portal infrastructure (API, frontend, auth) | Yes |
| **UseCase Account** | Runs ML workloads (training, labeling, deployments) | Yes |
| **Data Account** | Stores training data in isolated S3 buckets | Optional |

**Flexibility**: All three can be the same AWS account for simple deployments, or separate accounts for enterprise isolation.

### Resource Scoping Model

The portal uses two different scoping models depending on where data is stored:

| Resource Type | Storage | Scoping | Notes |
|---------------|---------|---------|-------|
| Training Jobs | Portal DynamoDB | Per UseCase ID | Strictly isolated by `usecase_id` |
| Labeling Jobs | Portal DynamoDB | Per UseCase ID | Strictly isolated by `usecase_id` |
| Pre-labeled Datasets | Portal DynamoDB | Per UseCase ID | Strictly isolated by `usecase_id` |
| S3 Buckets | UseCase/Data Account | Per UseCase (allowed buckets) | The use case's configured buckets plus the Additional S3 buckets a UseCaseAdmin allows; every one tagged `dda-portal:managed=true` (see [Use case buckets and Additional S3 buckets](#use-case-buckets-and-additional-s3-buckets)) |
| Greengrass Components | UseCase Account | Per AWS Account | All components tagged `dda-portal:managed=true` |
| IoT Devices | UseCase Account | Per AWS Account | All devices tagged `dda-portal:managed=true` |

**Important**: For Components and Devices, selecting a UseCase in the portal determines which AWS account to query via cross-account role, and all portal-managed resources in that account will be visible. S3 access is narrower: the portal reads and writes only the selected use case's allowed buckets, and other tagged buckets in the account are listed as **Not allowed** until a UseCaseAdmin allows them.

**Recommendation**: Use **one UseCase per AWS account** for clear resource isolation. If you need multiple use cases, use separate AWS accounts.

> **Future Enhancement**: UseCase-level isolation for Components and Devices can be added by filtering on the `dda-portal:usecase-id` tag. Components already include this tag; devices would need to be updated. Buckets are already bound per use case through the allowed-buckets rule.

---

## Initial Portal Deployment

### Prerequisites

- AWS CLI configured with admin credentials, with a region: a default region (`aws configure`) or `AWS_REGION` (`deploy-portal.sh` also takes `--region <region>`)
- Node.js 18+, Python 3.11+
- AWS CDK: `npm install -g aws-cdk`
- Docker (the compute stack builds a Lambda container image during `cdk deploy`)

### Deploy the Portal (One Command)

Run the orchestrator from the `edge-cv-portal` directory with the Portal Account's credentials:

```bash
cd edge-cv-portal
./deploy-portal.sh single-account
```

It checks the prerequisites (AWS CLI, credentials, region, Node.js, CDK), bootstraps CDK if needed, and then runs the existing scripts in order:

1. `./deploy-account-role.sh single-account` — IAM roles and policies
2. `infrastructure/deploy-auth.sh` — the Cognito `EdgeCVPortalAuthStack`
3. `./deploy-infrastructure.sh` — the main CDK stacks (`cdk deploy --all`). On a new install a second `cdk deploy` follows for the workflow debug and telemetry stacks, which attach to the authorizer of the API that the first one creates
4. `./deploy-frontend.sh` — generates `frontend/public/config.json` from the stack outputs, builds and publishes the frontend, and redeploys the compute stack with the CloudFront domain

All output goes to the terminal and to `edge-cv-portal/deploy-portal-<UTC-timestamp>.log` (`--log-file <path>` to override). If a step fails, the command stops, prints the failed step and the log path, and rolls nothing back; fix the cause and re-run the same command. On success it prints a Deployment Summary with the Portal URL, the created role ARNs and each step's outcome.

Options: `--profile <name>`, `--region <region>`, `--sso` (SSO on the auth stack; export the `SSO_*` variables first, see [SSO_SETUP.md](infrastructure/SSO_SETUP.md)) and `--non-interactive` (CI). `./deploy-portal.sh --help` lists them all.

**Outputs you need below:** the `EdgeCVPortalAuthStack` outputs (`UserPoolId`, `UserPoolClientId`, `AuthConfig`) are printed by the auth step and recorded in the log; the Portal URL (`DistributionDomainName`) is in the Deployment Summary.

**Trusted UseCase accounts.** The portal may assume `DDAPortalAccessRole` only in the accounts on its trusted UseCase account list, which also bounds the detector-export image pull and the accounts that may send to the portal's device queues. On a first deploy with no list configured, `deploy-portal.sh single-account` uses the Portal Account ID and stores it in the SSM parameter `/dda-portal/trusted-usecase-account-ids` (it never overwrites an existing parameter). Every run resolves the list once, before any step: the CDK context value `trustedUseCaseAccountIds` (`infrastructure/cdk.json`, `infrastructure/cdk.context.json`, `~/.cdk.json`), else `TRUSTED_USECASE_ACCOUNT_IDS`, else the SSM parameter, else the list the deployed portal stacks grant; on a deployed portal it never falls back to the Portal Account ID, and it stops before any step when the list cannot be determined. The list and its source are printed (with a `⚠️  <stack>: removes <ids>` line when the deploy drops an account the deployed stacks trust, and a `⚠️  <stack>: adds <ids> (the portal may then assume their DDAPortalAccessRole)` line when it trusts a new one), logged, and shown in the Deployment Summary. Keep a multi-account list in the SSM parameter: [UseCase Account Setup](#usecase-account-setup). Anyone who can write the parameter decides, at the next deploy, which accounts' `DDAPortalAccessRole` the portal may assume, so restrict `ssm:PutParameter` on it to the administrators who deploy the portal.

**Manual path.** The same four scripts can be run by hand, in that order, from `edge-cv-portal/` (`deploy-auth.sh` from `edge-cv-portal/infrastructure/`). On an account/region that is not bootstrapped yet, bootstrap it first from `edge-cv-portal/`, which has no `cdk.json`, so nothing is synthesized: `cdk bootstrap aws://<ACCOUNT_ID>/<REGION>` (from `infrastructure/` the CLI would synthesize the portal app first, which fails until `npm ci` has run there and the trusted list is set). `deploy-auth.sh` and `deploy-infrastructure.sh` synthesize the whole portal app, so the trusted list must be set before you run them: the SSM parameter above (a first `./deploy-portal.sh single-account` creates it), `TRUSTED_USECASE_ACCOUNT_IDS` (that run only), or the CDK context value; otherwise they stop with `ComputeStack requires a non-empty trustedUseCaseAccountIds list`. `deploy-frontend.sh` resolves the list like `deploy-portal.sh` and, with none configured, uses the list the deployed stacks grant. `deploy-infrastructure.sh` has no such fallback: it deploys the list the CDK app reads (the CDK context value, `TRUSTED_USECASE_ACCOUNT_IDS`, then the SSM parameter) and prints it first, with the same `⚠️` lines for each account the deploy removes or adds. This is useful to repeat a single step, for example `./deploy-frontend.sh` after a frontend change. Run on their own, the scripts use `AWS_REGION` (or `AWS_DEFAULT_REGION`) when set, otherwise the CLI's default region; `deploy-auth.sh` also takes `--region`.

### Bucket compliance before deploying

The portal's Lambda roles reach use-case S3 buckets only when a bucket carries
`dda-portal:managed=true` **and** has ABAC enabled (the data-plane grants are
conditioned on that tag; tag discovery — `GetBucketTagging`, `GetBucketAbac` —
stays unconditioned so the portal can read tags before ABAC is on). The deploy
log prints the scoping in force as `Data-plane scoping: <value>`. Buckets the
portal itself owns (artifacts, component, inference results) are granted by
name and need nothing.

Before deploying this version over an existing portal, bring every registered
bucket into compliance — a bucket that is not tagged and ABAC-enabled loses
portal access at the deploy:

```bash
cd edge-cv-portal
# Read-only: tags, ABAC status and region of every bucket the use cases register
python3 scripts/portal-bucket-abac-audit.py \
  --usecases-table dda-portal-usecases \
  --training-jobs-table dda-portal-training-jobs
# Exit 0: every portal-account bucket compliant and no legacy-artifact bucket.
# Exit 1: findings (listed); exit 2: usage error or a local boto3 without the ABAC operations.

# Tag and enable ABAC on exactly the non-compliant buckets it printed
python3 scripts/portal-bucket-abac-audit.py \
  --usecases-table dda-portal-usecases \
  --training-jobs-table dda-portal-training-jobs \
  --apply --buckets <bucket> [<bucket> ...]
```

`--apply` refuses any name the read-only pass did not list as a portal-account
bucket of a use case. Buckets reached through a cross-account role are listed
as `not probed`; their account's administrator tags them and enables ABAC
(see [Tag S3 Buckets](#tag-s3-buckets)). Review existing tag-conditioned
policies (IAM and bucket policies) in the account before enabling ABAC — they
start matching on the enabled buckets.

**Legacy model artifacts.** The audit's `legacy-artifact-bucket <name>` lines
list buckets that stored model artifacts of single-account use cases point at
(typically a model imported from a bucket that was never registered); the summary
line `legacy-artifact buckets: N` must read `0` before the deploy. For each such
bucket choose one remedy:

1. tag it, enable ABAC on it and allow it for the owning use case (**Allow**, or
   `PUT /usecases/{id}` with `additional_s3_buckets_add`) — it then joins the
   audited set and devices can read it too;
2. re-import the model from the use case's own bucket; or
3. accept the loss: compile, package and publish of those models fail with
   `AccessDenied` after the deploy.

**`tag-objects-only` deployments.** If the deploy log reads
`Data-plane scoping: tag-objects-only`, object access follows the tag but bucket
listing is bounded by a list resolved at deploy time, so a bucket registered
after the deploy reports the remedy "redeploy the portal to browse this bucket";
run `./deploy-infrastructure.sh` again to include it.

### Create Admin User

The **first** PortalAdmin is provisioned by hand, because nobody can sign in to
the portal yet. This is the one legitimate exception to
"[provision users through the portal only](#provisioning-a-user--through-the-portal-only)":
the Cognito account alone grants nothing — the Portal_Identity row in
`dda-portal-user-roles` is what makes it a PortalAdmin. Create every subsequent
account in the portal's **User Manager**.

```bash
USER_POOL_ID="<your-user-pool-id>"

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

# Assign PortalAdmin role: the global Portal_Identity row is what grants it
aws dynamodb put-item \
  --table-name dda-portal-user-roles \
  --item "{
    \"user_id\": {\"S\": \"$USER_SUB\"},
    \"usecase_id\": {\"S\": \"global\"},
    \"role\": {\"S\": \"PortalAdmin\"},
    \"status\": {\"S\": \"enabled\"},
    \"username\": {\"S\": \"admin\"},
    \"email\": {\"S\": \"admin@company.com\"},
    \"assigned_at\": {\"N\": \"$(date +%s)000\"},
    \"assigned_by\": {\"S\": \"system\"}
  }"
```

> Setting `custom:role` on the Cognito account is **not** required and grants
> nothing — see
> [Portal Users and Privilege](#portal-users-and-privilege-portal_identity-registry).

---

## Account Setup

### UseCase Account Setup

**Single-account setup:** nothing to do here. `./deploy-portal.sh single-account` ([Deploy the Portal](#deploy-the-portal-one-command)) already created the `DDASageMakerExecutionRole`, and its first deploy put the Portal Account ID on the trusted UseCase account list. Continue with [Tag S3 Buckets](#tag-s3-buckets).

**Multi-account setup:** run the orchestrator with the `usecase` topology in the **UseCase Account** (where ML workloads will run), with that account's credentials:

```bash
cd edge-cv-portal
./deploy-portal.sh usecase
```

It runs only the account-role step and prompts for the Portal Account ID and the External ID (leave the External ID blank to generate one). Non-interactive (CI):

```bash
./deploy-portal.sh usecase --non-interactive \
    --portal-account-id 123456789012 --external-id abc-123
```

**What gets created:**
- `DDAPortalAccessRole` - Cross-account access role
- `DDASageMakerExecutionRole` - For training, compilation and labeling jobs
- `DDAPortalComponentAccessPolicy` - For Greengrass devices to download component artifacts

**Save these outputs:**
- Role ARN
- External ID
- SageMaker Execution Role ARN

The Deployment Summary prints the Role ARN and the External ID. All three are saved in the generated `usecase-account-*-config.txt` file (written to `edge-cv-portal/`); you'll upload it when creating the UseCase.

**Then trust the account in the Portal Account (before creating the UseCase).** The portal can assume `DDAPortalAccessRole` only in accounts on its trusted list ([Deploy the Portal](#deploy-the-portal-one-command)). With the Portal Account's credentials, add the UseCase Account ID to the list and redeploy the portal:

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

> `./deploy-account-role.sh` can still be run directly from `edge-cv-portal/` (menu option `1` = single-account, `2` = UseCase Account, `3` = Data Account); it is the script `deploy-portal.sh` runs for you.

### Data Account Setup (Optional)

If storing training data in a separate account, run the `data` topology in the **Data Account**:

```bash
cd edge-cv-portal
./deploy-portal.sh data
```

It prompts for the Portal Account ID, the External ID and, optionally, the data bucket names. For CI add `--non-interactive --portal-account-id <id> --external-id <id>`, and optionally `--data-buckets <bucket1,bucket2>`.

**What gets created:**
- `DDAPortalDataAccessRole` - For Portal to browse data and update bucket policies
- `DDASageMakerDataAccessRole` - For SageMaker cross-account access

**Save these outputs:**
- Portal Access Role ARN
- External ID

Both are printed in the Deployment Summary and saved in the generated `data-account-*-config.txt` file (written to `edge-cv-portal/`). To register the account, upload that file under **Settings** → **Data Accounts** → **Add Data Account** to auto-fill the fields.

**Note**: The bucket policy for SageMaker access is **automatically configured** when you onboard the UseCase in the portal. The Portal assumes the Data Account role and adds the necessary bucket policy statements.

### Tag S3 Buckets

The portal uses tag-based access, and a use case can only be configured with
(or allow) a bucket that already carries the portal tag. Tag each bucket with
the S3 Control `TagResource` API, run by the account that owns the bucket
(`ACCOUNT_ID` = the bucket owner's account id):

```bash
aws s3control tag-resource \
  --account-id ACCOUNT_ID \
  --resource-arn arn:aws:s3:::YOUR_BUCKET_NAME \
  --tags Key=dda-portal:managed,Value=true
```

For a bucket in the **portal account**, no further step is needed: registering
the tagged bucket with a use case (creating the use case with it, allowing it,
or configuring it as the data bucket) makes the portal enable ABAC on it,
because the portal's Lambda roles reach a use-case bucket only when it carries
the tag and ABAC is on. Enabling ABAC changes how every tag-conditioned policy
in the portal account evaluates for that bucket — review existing tag
conditions first.

For a bucket in a **UseCase or Data Account** (reached through a cross-account
role) also enable bucket ABAC yourself, because the account stack's role
conditions its S3 access on the `dda-portal:managed` tag and S3 evaluates that
condition only once ABAC is on:

```bash
aws s3api put-bucket-abac --bucket YOUR_BUCKET_NAME --abac-status Status=Enabled
```

> Enabling ABAC changes how every other tag-conditioned policy in that account
> evaluates for that bucket; review existing tag conditions first.
> `aws s3api put-bucket-tagging` still works on buckets without ABAC and remains
> the alternative there; once ABAC is enabled, tags are managed with
> `s3control tag-resource` / `untag-resource` only. Tag **before** creating the
> use case — the portal reads the tags when the bucket is configured and
> refuses an untagged or unreadable bucket (see
> [Use case buckets and Additional S3 buckets](#use-case-buckets-and-additional-s3-buckets)).

### Configure S3 Bucket CORS (Required for Uploads)

**Important**: Tagging alone is not enough. To upload files from the portal, you must also configure CORS on each bucket.

#### Why CORS is Required
The portal frontend (running in your browser) uploads files directly to S3 using presigned URLs. This is a "cross-origin" request (from CloudFront to S3), which S3 blocks by default.

#### Option 1: AWS Console (Recommended for Non-Technical Users)

1. Go to **S3** in AWS Console
2. Select your bucket
3. Go to **Permissions** tab
4. Scroll to **Cross-origin resource sharing (CORS)** → Click **Edit**
5. Paste this configuration (replace `YOUR_CLOUDFRONT_DOMAIN`):

```json
[
  {
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["GET", "PUT", "POST", "HEAD"],
    "AllowedOrigins": ["https://YOUR_CLOUDFRONT_DOMAIN"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000
  }
]
```

6. Click **Save changes**

#### Option 2: AWS CLI

```bash
# Replace values
BUCKET_NAME="your-bucket-name"
CLOUDFRONT_DOMAIN="d3qeryypza4i9i.cloudfront.net"

aws s3api put-bucket-cors --bucket $BUCKET_NAME --cors-configuration '{
  "CORSRules": [{
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["GET", "PUT", "POST", "HEAD"],
    "AllowedOrigins": ["https://'"$CLOUDFRONT_DOMAIN"'"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000
  }]
}'
```

#### Option 3: Helper Script

```bash
cd edge-cv-portal
./configure-bucket-cors.sh YOUR_BUCKET_NAME YOUR_CLOUDFRONT_DOMAIN
```

#### Verify CORS Configuration

```bash
aws s3api get-bucket-cors --bucket YOUR_BUCKET_NAME
```

### AWS Marketplace Subscription

**Required in UseCase Account** before training:

1. Go to [Computer Vision Defect Detection Model](https://aws.amazon.com/marketplace/pp/prodview-j72hhmlt6avp6)
2. Click **Continue to Subscribe** → **Accept Offer**
3. Wait for activation (~2 minutes)

Verify:
```bash
aws sagemaker list-algorithms --name-contains "computer-vision-defect-detection"
```

---

## Onboarding a New Customer

### Complete Onboarding Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    USECASE ONBOARDING FLOW                               │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  1. Deploy UseCaseAccountStack (in UseCase Account)                     │
│     └─> Creates: DDAPortalAccessRole, DDASageMakerExecutionRole,        │
│                  DDAPortalComponentAccessPolicy                          │
│     └─> Then, in the Portal Account: add its ID to the trusted          │
│         UseCase account list, re-run deploy-portal.sh single-account    │
│                                                                          │
│  2. (Optional) Deploy DataAccountStack (in Data Account)                │
│     └─> Creates: DDAPortalDataAccessRole                                │
│                                                                          │
│  3. Tag the use case's S3 buckets and enable ABAC (bucket owner acct)   │
│     └─> Tags: dda-portal:managed=true (s3control tag-resource)          │
│     └─> Enables: bucket ABAC (s3api put-bucket-abac)                    │
│                                                                          │
│  4. Create UseCase in Portal UI                                         │
│     └─> Stores: Account IDs, Role ARNs, External IDs, bucket names      │
│     └─> Checks: each configured bucket is tagged and readable           │
│                                                                          │
│  5. Provision Shared Components (in Portal)                             │
│     └─> Creates: Greengrass components in UseCase Account               │
│     └─> Updates: S3 bucket policy for cross-account access              │
│                                                                          │
│  6. Add Device, run setup command (on device)                           │
│     └─> Creates: GreengrassV2TokenExchangeRole                          │
│     └─> Attaches: DDAPortalComponentAccessPolicy                        │
│     └─> Tags: Device for portal discovery                               │
│                                                                          │
│  7. Deploy to Device (in Portal)                                        │
│     └─> Device downloads artifacts from Portal's S3 bucket              │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

The bucket is tagged (and, for a bucket reached through a cross-account role,
ABAC-enabled) **before** the use case is created: the portal reads the bucket's
tags through the use case's role when the use case is created and refuses a
bucket it cannot confirm (`400 bucket_not_eligible`). The cross-account role's
tag condition is evaluated only once ABAC is on, and enabling ABAC changes how
any other tag-conditioned policy in that account evaluates for that bucket, so
review existing tag conditions first.

### Step 1: Create Cognito Users

Create each user in the portal's **User Manager** (sign in as a PortalAdmin →
user menu top-right → **User Manager** → **Create user**). That path writes both
the Cognito account and its Portal_Identity registry row, which is what grants
portal access — see
[Portal Users and Privilege](#portal-users-and-privilege-portal_identity-registry).

Creating the account with the CLI instead leaves it with **no registry row**, so
it can sign in and is then denied every route:

```bash
# Not sufficient on its own — the portal User Manager is the supported path
aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username user@customer.com \
  --user-attributes Name=email,Value=user@customer.com \
  --temporary-password TempPass123!
```

### Step 2: Deploy UseCase Account Role

Have the customer run in their AWS account (details in [UseCase Account Setup](#usecase-account-setup)):

```bash
cd edge-cv-portal
./deploy-portal.sh usecase
# Enter the Portal Account ID and External ID when prompted
```

Then, in the Portal Account, add the customer's UseCase Account ID to the trusted UseCase account list and redeploy the portal (`./deploy-portal.sh single-account`), as described in [UseCase Account Setup](#usecase-account-setup). Until then the portal cannot assume the customer's `DDAPortalAccessRole`.

### Step 3: Tag the Customer's S3 Buckets and Enable ABAC

The customer runs this in the account that owns each bucket the use case will
be configured with (its S3 bucket and, if used, its Data Account bucket),
**before** the use case is created:

```bash
# Tag the bucket for portal access (ACCOUNT_ID = the account that owns the bucket)
aws s3control tag-resource \
  --account-id ACCOUNT_ID \
  --resource-arn arn:aws:s3:::training-data-bucket \
  --tags Key=dda-portal:managed,Value=true

# Enable bucket ABAC so the cross-account role's tag condition is evaluated
aws s3api put-bucket-abac --bucket training-data-bucket --abac-status Status=Enabled

# Configure CORS for uploads (replace CLOUDFRONT_DOMAIN)
aws s3api put-bucket-cors --bucket training-data-bucket --cors-configuration '{
  "CORSRules": [{
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["GET", "PUT", "POST", "HEAD"],
    "AllowedOrigins": ["https://CLOUDFRONT_DOMAIN"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000
  }]
}'
```

**Note**: Tagging, ABAC and CORS are all required for a cross-account bucket.
Tagging makes the bucket eligible for a use case; ABAC makes the role's tag
condition take effect (enabling it also changes how any other tag-conditioned
policy in that account evaluates for the bucket — review existing conditions
first); CORS enables browser uploads. `aws s3api put-bucket-tagging` still works
on buckets without ABAC and remains the alternative there. The portal no longer
writes this tag on the customer's bucket itself.

### Step 4: Create UseCase in Portal

1. Login to portal as PortalAdmin
2. Go to **Use Cases** → **Onboard New Use Case**
3. Enter:
   - **Name**: Customer project name
   - **AWS Account ID**: Customer's UseCase Account ID
   - **Cross-Account Role ARN**: From deploy script output
   - **External ID**: From deploy script output
   - **SageMaker Execution Role ARN**: From deploy script output
   - **S3 Bucket** (and **Data S3 Bucket**, if a Data Account is used): the
     buckets tagged in Step 3. The portal reads each bucket's tags through the
     use case's role as part of creation; an untagged bucket, a bucket whose
     tags cannot be read (ABAC off), or a bucket another use case already has
     configured answers `400 bucket_not_eligible` with a `remedy` naming the fix.

### Step 5: Assign User Roles

Assign roles in the portal: **Use Cases** → **Actions** → **Manage Team** for
per-UseCase roles, or the **User Manager** for the account-level (`global`) role.
Both write the Portal_Identity registry, which is what the API resolves
privilege from.

The equivalent direct write, for scripted onboarding:

```bash
# Get user's sub
USER_SUB=$(aws cognito-idp admin-get-user \
  --user-pool-id $USER_POOL_ID \
  --username user@customer.com \
  --query 'UserAttributes[?Name==`sub`].Value' --output text)

# Assign to usecase (usecase_id='global' would set the account-level role)
aws dynamodb put-item \
  --table-name dda-portal-user-roles \
  --item "{
    \"user_id\": {\"S\": \"$USER_SUB\"},
    \"usecase_id\": {\"S\": \"<USECASE_ID>\"},
    \"role\": {\"S\": \"UseCaseAdmin\"},
    \"status\": {\"S\": \"enabled\"},
    \"username\": {\"S\": \"user@customer.com\"},
    \"email\": {\"S\": \"user@customer.com\"},
    \"assigned_at\": {\"N\": \"$(date +%s)000\"},
    \"assigned_by\": {\"S\": \"admin\"}
  }"
```

> A per-UseCase row does **not** replace the account's `global` row: with
> enforcement on, an account with no enabled `global` row is denied everywhere.
> See [Portal Users and Privilege](#portal-users-and-privilege-portal_identity-registry).

---

## Creating a UseCase

Only a PortalAdmin creates use cases (see
[UseCase Creation (PortalAdmin Only)](#usecase-creation-portaladmin-only)).
Every API route and the authorization it carries is listed in
[infrastructure/ROUTE_AUTH.md](infrastructure/ROUTE_AUTH.md), generated from
the synthesized stacks and checked by the infrastructure tests.

### Before creating a multi-account use case

Two steps come before the portal accepts a use case in another account:

1. **Trust the account.** Add the account id to the portal's trusted use-case
   account list and redeploy the portal: `-c trustedUseCaseAccountIds=<id>,<id>`
   on the deploy, the `TRUSTED_USECASE_ACCOUNT_IDS` environment variable, or
   the SSM parameter `/dda-portal/trusted-usecase-account-ids` (in that order
   of precedence; then `./deploy-infrastructure.sh`). An account outside the
   list is refused with `400 ACCOUNT_NOT_TRUSTED` before the portal makes any
   STS call.
2. **Deploy the account stack.** Run `./deploy-account-role.sh` (option 1) in
   the use-case account. `cross_account_role_arn` must be that stack's
   `DDAPortalAccessRole` (`arn:aws:iam::<account>:role/DDAPortalAccessRole`;
   any other role name is refused with `400 INVALID_ROLE_ARN`), and
   `external_id` must be the external id the stack printed.

**Proof of control.** Before the use case is written and before any grant on
the portal's component bucket or event bus, the portal assumes
`DDAPortalAccessRole` with the supplied external id. If that fails, the create
is refused with `400 ACCOUNT_CONTROL_NOT_VERIFIED` (deploy the account stack in
that account and supply its external id); nothing is written. When STS itself
is unavailable the answer is `503 STS_UNAVAILABLE` (retry). The wizard's
**Verify Role** step runs the same checks and shows the same remedies.

| Error code | Meaning | Remedy |
|---|---|---|
| `INSUFFICIENT_PERMISSIONS` (403) | The caller is not a PortalAdmin | Ask a PortalAdmin |
| `INVALID_ACCOUNT_ID` / `INVALID_ROLE_ARN` (400) | An account id is not twelve digits, or a role ARN is not the account stack's role in that account | Correct the value |
| `ACCOUNT_NOT_TRUSTED` (400) | The account is not in the trusted list | Add it to `trustedUseCaseAccountIds` and redeploy |
| `ACCOUNT_CONTROL_NOT_VERIFIED` (400) | The role could not be assumed with the external id | Deploy `DDAPortalUseCaseAccountStack` in the account and supply its external id |
| `STS_UNAVAILABLE` (503) | STS did not answer | Retry |

**Shared components.** Creating a use case provisions the portal's shared
components into its account. Re-running that from **Use Cases** → **Actions** →
**Re-provision Shared Components** (`POST /shared-components/provision`) is a
PortalAdmin action; the item is hidden for other roles and the route answers
`403 INSUFFICIENT_PERMISSIONS` to them.

**Moving a use case to another account.** Moves are API-only:
`PUT /usecases/{id}` with `account_id`, `cross_account_role_arn`, `external_id`
and `sagemaker_execution_role_arn` (a role in the **new** account), after the
new account has been trusted and its account stack deployed as above. The
portal runs the same checks and proof of control on the new values before it
writes anything; a `sagemaker_execution_role_arn` still in the old account is
refused with `400 INVALID_ROLE_ARN`. After the write it removes the old
account's grants (when no other use case uses that account) and writes the new
account's grants, reported as `grants_removed` and `grants_added`; re-provision
the shared components afterwards. Moving a multi-account use case back to the
portal account is refused (`400 INVALID_ACCOUNT_ID`). The **Edit** modal does
not move use cases — it carries neither `external_id` nor
`sagemaker_execution_role_arn` — and saving it with unchanged account fields
runs none of these checks.

### Via Portal UI

1. Navigate to **Use Cases** → **Onboard New Use Case**
2. Fill in the wizard:
   - **Basic Info**: Name, description, cost center
   - **AWS Account**: Role ARN, External ID, SageMaker Role ARN
   - **S3 Storage**: Bucket name and prefix for outputs
   - **Data Account Configuration**: Choose where training data is stored

**Single-account use cases (bucket in the portal account):** an existing bucket
selected in the wizard's bucket picker must already carry
`dda-portal:managed=true`. Tag it first with `aws s3control tag-resource
--account-id PORTAL_ACCOUNT_ID --resource-arn arn:aws:s3:::BUCKET --tags
Key=dda-portal:managed,Value=true`. No separate ABAC step is needed: registering
a tagged portal-account bucket makes the portal enable ABAC on it (the portal's
roles reach a use-case bucket only when it is tagged and ABAC is on). Enabling
ABAC changes how every tag-conditioned policy in the portal account evaluates
for that bucket, so review existing tag conditions first. Or create the bucket
with the picker's **Create new bucket** control, which tags it, enables ABAC on
it and allows it for the use case automatically.

**Registration report.** Every response that attaches a bucket (use-case
create, the Edit modal's save, **Allow** on a bucket, bucket creation, the
data-account configure) carries `registration` — per bucket `tagged`,
`abac_enabled`, `iam_reachable` and `remedy`. The attach has succeeded either
way; a non-null `remedy` names the one step left (tag the bucket, enable ABAC on
it, ask the bucket's account administrator to enable ABAC, or redeploy the
portal) and the UI shows it beside the bucket.
The picker marks each bucket **Tagged for the portal** / **Not tagged for the
portal** / **Portal-created**; the marking is advisory (tag discovery is
regional) and the server decides when the use case is created — an untagged
bucket answers `400 bucket_not_eligible`.

#### Data Account Options

| Option | When to Use | What Happens |
|--------|-------------|--------------|
| **Same as UseCase Account** | Data is in the same account as SageMaker | No extra role assumption; simplest setup |
| **Separate Data Account** | Data is in a centralized data lake | Portal assumes Data Account role; SageMaker uses bucket policy |

**For Separate Data Account**, you'll need:
- Data Account ID
- Data Account Role ARN (`DDAPortalDataAccessRole`)
- Data Account External ID
- Data S3 Bucket name

> **📖 See [Data Account Setup (Optional)](#data-account-setup-optional) for setup instructions.

### Via API

```bash
curl -X POST "$API_URL/usecases" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Manufacturing Line 1",
    "account_id": "123456789012",
    "cross_account_role_arn": "arn:aws:iam::123456789012:role/DDAPortalAccessRole",
    "external_id": "UUID-FROM-DEPLOY-SCRIPT",
    "sagemaker_execution_role_arn": "arn:aws:iam::123456789012:role/DDASageMakerExecutionRole"
  }'
```

### Update DynamoDB External ID

If you regenerate the External ID:

```bash
aws dynamodb update-item \
  --table-name edge-cv-portal-usecases \
  --key '{"usecase_id": {"S": "<USECASE_ID>"}}' \
  --update-expression "SET external_id = :eid" \
  --expression-attribute-values '{":eid": {"S": "<NEW_EXTERNAL_ID>"}}'
```

---

## Portal Features

### Data Management
- Browse the use case's allowed buckets: its configured buckets plus the
  Additional S3 buckets a UseCaseAdmin has allowed (below). Other buckets tagged
  `dda-portal:managed=true` in the account are listed as **Not allowed** with an
  **Allow for this use case** action for UseCaseAdmins
- Upload training images via presigned URLs
- Organize data into folders

### Use case buckets and Additional S3 buckets

Every S3 read or write the portal performs on behalf of a use case is bound to
that use case's **allowed buckets**:

- its configured **S3 bucket** (`s3_bucket`),
- its configured **Data S3 bucket** (`data_s3_bucket`, when a Data Account is
  used), and
- the **Additional S3 buckets** a UseCaseAdmin (or PortalAdmin) has allowed for
  it — **Use Cases** → **Edit** → **Allowed buckets**, or the inline **Allow for
  this use case** action on a **Not allowed** bucket in Data Management.

This covers Data Management (folder listing, folder creation, upload URLs),
model validation and import (`POST /models/validate`, `/import`, `/inspect`,
`/convert`), vLLM registration and its fit check, training manifests and
manifest transforms, pre-labeled dataset manifests, and the few-shot example
references of DDA labeling jobs. A request naming any other bucket is refused
**before** any role is assumed or any S3 call is made, with
`400 bucket_not_allowed` and a `remedy` field; the UI renders the refusal with
the Allow action where the caller may update the use case. A malformed URI or
bucket name answers `400 invalid_s3_uri` / `invalid_bucket_name`.
`GET /usecases/{id}` returns the computed `allowed_s3_buckets` list;
`GET /usecases/{id}/data/buckets` marks each bucket `allowed` / `portal_created`.

**Allowing a bucket.** `PUT /usecases/{id}` with `additional_s3_buckets_add` /
`additional_s3_buckets_remove` (at most 10 names per request, 100 additional
buckets per use case). The same rules apply when a use case is created or when
its configured `s3_bucket` / `data_s3_bucket` is changed. Before anything is
written the portal checks that the bucket:

1. is not one of the portal's own buckets (below);
2. is configured on **no other use case** and carries **no
   `dda-portal:usecase-id` tag naming another use case** — otherwise
   `400 bucket_not_eligible` (`bucket_configured_on_other_usecase`) unless the
   caller is a PortalAdmin (see sharing, below);
3. carries `dda-portal:managed=true` (or `dda-portal:created-by=portal`),
   readable through the use case's role — otherwise `400 bucket_not_eligible`
   with reason `no_tags` / `tag_missing` (tag it, see
   [Tag S3 Buckets](#tag-s3-buckets)), `tagging_unreadable` (untagged, or
   tagged but ABAC off / no `s3:GetBucketTagging` grant for the role),
   `bucket_not_found` or `credentials_unavailable` (the use case's role could
   not be assumed).

Once a tagged portal-account bucket passes these checks, the portal enables ABAC
on it (audited as `bucket_abac_enabled`) and probes that its role can reach it;
the response's `registration[bucket]` reports `tagged`, `abac_enabled`,
`iam_reachable` and, when something is still missing, a `remedy` the UI shows
beside the bucket. Enabling ABAC changes how every tag-conditioned policy in the
portal account evaluates for that bucket — review existing tag conditions
before allowing a bucket that other policies condition on.

**Device reads of allowed buckets.** Devices may read an allowed bucket from
workflow and trigger payloads only when the bucket is in the use case's own
account; the portal writes the device grant for it (the
`DDAUseCaseDeviceAccess` policy on the account's
`GreengrassV2TokenExchangeRole`) immediately when the bucket is allowed, and
again on the next deployment. The device role is one per account, so a bucket
allowed for one use case becomes readable by the devices of every use case in
that account — allow a sensitive bucket knowing that reach. Allowed buckets in
another account and the training-data bucket (`data_s3_bucket`) are not readable
from devices — keep payload references to them out of workflows, or publish the
objects from the use case's bucket.

The device grant covers every out-of-pattern bucket (one not matching `dda-*` or
`*-dda-*`, which the account stack's device policy already covers) of a use
case's `s3_bucket` and allowed buckets, one statement per use case. Its size
budget holds about 19 use cases with one 63-character out-of-pattern bucket each
(more with shorter names); beyond it the response reports
`policy_size_limit` with the remedy (name the bucket within the pattern, remove
allowed buckets the use case no longer needs, or consolidate). The outcome is
reported as `device_grants.device_access` on deployment create and on a `PUT`
that changed the device-readable buckets, and as `grants_removed.device_access`
on `DELETE`. Removing the last use case statement of an account cannot leave an
empty policy; the response names the one manual command
(`aws iam delete-role-policy --role-name GreengrassV2TokenExchangeRole
--policy-name DDAUseCaseDeviceAccess`).

**Inference-results bucket override.** A use case whose inference uploader
writes to a results bucket of its own (`inference_uploader_s3_bucket`) instead
of `dda-inference-results-<account>`: an override in the portal account must be
tagged `dda-portal:managed=true` and ABAC-enabled like any registered bucket, or
the captures and workflow-tuning views cannot read it. The bucket audit
(`scripts/portal-bucket-abac-audit.py`, below) lists use cases with an override.

**Models imported before the IAM scoping change.** A model imported from a
bucket outside the use case's allowed buckets before this change keeps that
bucket in its stored artifact URI; on a single-account use case compile,
package and publish of that model read it on the portal's role, which after the
change reaches only tagged, ABAC-enabled buckets. The bucket audit lists such
buckets as `legacy-artifact-bucket`; each needs one of the three remedies in
[Bucket compliance before deploying](#bucket-compliance-before-deploying)
before the deploy. The ModelConverter function's `TRUSTED_MODEL_BUCKETS` /
`TRUSTED_MODEL_ACCOUNTS` variables are empty in every deployment (no stack sets
them; a console edit is reverted at the next deploy), and any bucket ever placed
there must be tagged `dda-portal:managed=true` and ABAC-enabled like a registered
bucket, or the converter's downloads from it are denied.

Additional S3 buckets are reached with the use case's own role (the portal's
execution role for a single-account use case), so they must live in the use
case's account: a bucket in a separate Data Account other than the configured
`data_s3_bucket` cannot be allowed, and Data Management hides the Allow action
on such a use case. Buckets created in the portal account can only be allowed
for single-account use cases (`account_mismatch`).

**Portal-created buckets.** A PortalAdmin can create a bucket in the onboarding
wizard (**Create new bucket**), and a UseCaseAdmin can create one for an
existing single-account use case through the API (`POST /s3-buckets` with
`usecase_id`). The
portal creates it with the tags `dda-portal:managed=true`,
`dda-portal:created-by=portal` and (for a use case) `dda-portal:usecase-id`,
enables ABAC on it, reads the tags back, and allows it for the use case
automatically — the UseCaseAdmin never has to add a portal-created bucket by
hand. Only buckets created outside the portal need the manual tag and the
Allow step. A name the account already owns answers `409 bucket_already_exists`
(select the existing bucket instead; if it is untagged an administrator tags it).

**Sharing a bucket between use cases — PortalAdmin only.** A bucket configured
on another use case, or tagged `dda-portal:usecase-id` for one — **even a use
case that has since been deleted** — can only be allowed for another use case
by a PortalAdmin. A UseCaseAdmin sees `400 bucket_not_eligible` whose remedy
says whether the other use case still exists; the PortalAdmin's attach is
audited (`s3_bucket_allowed` with `shared_with_usecases`, and `stale_claim`
when the tag named a deleted use case). To re-home a deleted use case's
portal-created bucket, a PortalAdmin allows it for (or configures it on) the new
use case, or an AWS administrator removes the claim first:
`aws s3control untag-resource --account-id ACCOUNT_ID --resource-arn
arn:aws:s3:::BUCKET --tag-keys dda-portal:usecase-id`.

**The portal's own buckets can never be allowed.** The portal artifacts bucket
and the Greengrass component bucket are excluded from every use case's allowed
set by rule, whatever their tags say and whoever asks (PortalAdmin included).
A use case record that names one of them loses that entry: every operation on
it is refused with `bucket_not_allowed` and it cannot be re-attached — re-point
such a use case at its own bucket.

**Labeling jobs always use the configured data bucket** (`data_s3_bucket`, else
`s3_bucket`). An Additional S3 bucket is browsable and writable in Data
Management and usable for model import, manifests and transforms, but it
**cannot be a labeling dataset** — copy or upload labeling data into the
configured data bucket. When you re-point a use case's configured bucket while
labeling jobs are still running, keep the old bucket under Additional S3
buckets until they finish: the jobs recorded the old bucket at creation and
their workers refuse a bucket outside the set.

**Deleting a use case (UseCaseAdmins and PortalAdmins).** Deleting a use case
removes its record only; its buckets keep their contents **and their tags** —
the portal removes no tags. Remove the `dda-portal:*` tags, or the bucket, if
that storage must not be allowed for another use case
(`aws s3control untag-resource --account-id ACCOUNT_ID --resource-arn
arn:aws:s3:::BUCKET --tag-keys dda-portal:managed dda-portal:created-by
dda-portal:usecase-id`). A portal-created bucket stays claimed for the deleted
use case (`dda-portal:usecase-id`) until a PortalAdmin allows it elsewhere or
an administrator removes that tag; an administrator-tagged bucket without such
a claim can be allowed again by any UseCaseAdmin.

**Audit.** Every refusal writes an `s3_bucket_refused` row (bucket and purpose,
never a key); every change to a use case's allowed set writes
`s3_bucket_allowed` (with `attribute` and `source`: `api`, `create`,
`configure` or `auto_add`) or `s3_bucket_removed`.

### Labeling (Ground Truth)
- Create bounding box labeling jobs
- Monitor labeling progress
- Use pre-labeled datasets for quick starts

### Training
- Start SageMaker training jobs using Marketplace algorithm
- Monitor training progress and metrics
- View training logs

### Model Compilation
- Compile models for edge devices (x86-64, ARM64)
- Target CPU or GPU inference
- Automatic Greengrass component creation

### Greengrass Components
- View portal-created components (tagged `dda-portal:managed=true`)
- Component versioning
- Deploy to edge devices

### Device Management
- View portal-managed Greengrass core devices (tagged `dda-portal:managed=true`)
- Monitor device status, installed components, and deployments
- Devices are registered with **Add Device** (see [Device Setup](#device-setup))

### Device Setup

Register devices with **Add Device** on the **Devices** page and run the generated setup command on each device ([README Step 7](../README.md#step-7-set-up-edge-devices-add-device)). The command runs `station_install/setup_station.sh` on the device with short-lived credentials, so the device needs no AWS CLI credentials. The setup:
- Installs Java, Python 3.11, the AWS CLI, GStreamer, and Docker
- Downloads and installs AWS IoT Greengrass Core v2
- Creates an IoT Thing in the device group and provisions certificates
- Sets up `GreengrassV2TokenExchangeRole` for device credentials
- Attaches `DDAPortalComponentAccessPolicy` for cross-account S3 access and ECR image pulls, and warns when it cannot verify or attach it
- Tags the Greengrass Core Device with `dda-portal:managed=true` for portal discovery

**Prerequisite:** Deploy `UseCaseAccountStack` first (creates `DDAPortalComponentAccessPolicy`).

**For Existing Devices** (set up outside Add Device, or before portal tagging):
```bash
# Tag the Greengrass Core Device (not IoT Thing)
aws greengrassv2 tag-resource \
  --resource-arn arn:aws:greengrass:REGION:ACCOUNT:coreDevices:THING_NAME \
  --tags "dda-portal:managed=true"

# Attach the component access policy
aws iam attach-role-policy \
  --role-name GreengrassV2TokenExchangeRole \
  --policy-arn "arn:aws:iam::ACCOUNT:policy/DDAPortalComponentAccessPolicy"
```

### Deployments
- Create Greengrass deployments
- Target specific devices or groups
- Monitor deployment status
- Each device or thing group has one current deployment, and a new deployment for the same target replaces it. Change it with **Revise Deployment** ([README: Revise a Deployment](../README.md#revise-a-deployment))

---

## Troubleshooting

### "Insufficient permissions" (403) for a user who should have access

**Cause**: with registry enforcement on, the account has no **enabled** global
Portal_Identity row (`dda-portal-user-roles`, `usecase_id='global'`). A Cognito
`custom:role` attribute grants nothing. The handler log shows
`Registry denial: user <sub> has no enabled Portal_Identity …` and the audit
entry records `identity_source=absent`.

**Fix**: provision the account in the portal's **User Manager**, or re-run
`backfill_portal_registry.py --apply`. Full procedure, including how to read the
row and how to turn enforcement off again:
[Portal Users and Privilege](#portal-users-and-privilege-portal_identity-registry).

A 500 `Authorization check failed` is a different problem — the registry lookup
itself failed (check the table's health), and is deliberately not reported as a
permission denial.

### "Access Denied" on Cross-Account Operations

**Cause 1**: the UseCase Account is not on the portal's trusted UseCase account list, so the portal's roles may not assume its `DDAPortalAccessRole`. **Verify Role** in the onboarding wizard shows `AccessDenied: ` followed by STS's message that the portal's role is not authorized to perform `sts:AssumeRole` on that role, and creating the use case answers `400 bucket_not_eligible` ("The portal could not assume the role configured for this bucket").

**Fix**: add the account ID to the list and redeploy the portal, as in [UseCase Account Setup](#usecase-account-setup). The `Trusted UseCase accounts:` line that `./deploy-portal.sh single-account` prints before its first step shows the list it deploys.

**Cause 2**: External ID mismatch between IAM role and DynamoDB.

**Fix**:
```bash
# Check current DynamoDB value
aws dynamodb get-item \
  --table-name edge-cv-portal-usecases \
  --key '{"usecase_id": {"S": "<ID>"}}'

# Update to match IAM role
aws dynamodb update-item \
  --table-name edge-cv-portal-usecases \
  --key '{"usecase_id": {"S": "<ID>"}}' \
  --update-expression "SET external_id = :eid" \
  --expression-attribute-values '{":eid": {"S": "<CORRECT_EXTERNAL_ID>"}}'
```

### "Algorithm does not exist" on Training

**Cause**: AWS Marketplace subscription not active in UseCase Account.

**Fix**: Subscribe to the algorithm in the UseCase Account (not Portal Account).

### Components Not Showing

**Cause**: Components not tagged with `dda-portal:managed=true`.

**Fix**: Only components created through the portal are shown. Create a new component via the training → compilation workflow.

### Devices Not Showing

**Cause**: Devices not tagged with `dda-portal:managed=true` or not set up with **Add Device**.

**Fix**: 
1. Set the device up with **Add Device** (the setup tags the core device)
2. For existing devices, manually tag them:
```bash
aws greengrassv2 tag-resource \
  --resource-arn arn:aws:greengrass:REGION:ACCOUNT:coreDevices:THING_NAME \
  --tags "dda-portal:managed=true"
```
3. Verify the device is a Greengrass Core Device (not just an IoT Thing)

### Deployment Fails with "S3 Access Denied" on Device

**Symptom**: Deployment shows `FAILED_NO_STATE_CHANGE` with "S3 HeadObject returns 403 Access Denied".

**Cause**: The device's `GreengrassV2TokenExchangeRole` doesn't have permission to access the Portal Account's component bucket.

**Fix**:
1. Verify `DDAPortalComponentAccessPolicy` exists in the UseCase Account:
```bash
aws iam get-policy --policy-arn "arn:aws:iam::USECASE_ACCOUNT:policy/DDAPortalComponentAccessPolicy"
```

2. If missing, redeploy `UseCaseAccountStack`:
```bash
cd edge-cv-portal/infrastructure
npm run build
rm -rf cdk.out
cdk deploy -a "npx ts-node bin/usecase-account-app.ts" \
  -c portalAccountId=PORTAL_ACCOUNT_ID \
  -c externalId=YOUR_EXTERNAL_ID \
  --require-approval never
```

3. Attach the policy to the device role:
```bash
aws iam attach-role-policy \
  --role-name GreengrassV2TokenExchangeRole \
  --policy-arn "arn:aws:iam::USECASE_ACCOUNT:policy/DDAPortalComponentAccessPolicy"
```

4. Verify the Portal's component bucket policy allows the UseCase Account:
```bash
aws s3api get-bucket-policy --bucket dda-component-REGION-PORTAL_ACCOUNT
```

The policy should include both `GreengrassV2TokenExchangeRole` and `greengrass.amazonaws.com` service principal.

### Component Provisioning Fails with "Artifact Cannot Be Accessed"

**Symptom**: Re-provisioning shared components fails with "Specified artifact resource cannot be accessed".

**Cause**: The Greengrass service can't validate the S3 artifact during `CreateComponentVersion`.

**Fix**: Update the Portal's component bucket policy to include the Greengrass service principal:
```bash
# This is automatically done during provisioning, but if it fails:
aws s3api put-bucket-policy --bucket dda-component-REGION-PORTAL_ACCOUNT --policy '{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowUseCaseAccountsGreengrassAccess",
      "Effect": "Allow",
      "Principal": {
        "AWS": [
          "arn:aws:iam::USECASE_ACCOUNT:role/DDAPortalAccessRole",
          "arn:aws:iam::USECASE_ACCOUNT:role/GreengrassV2TokenExchangeRole"
        ]
      },
      "Action": ["s3:GetObject", "s3:GetObjectVersion", "s3:GetBucketLocation"],
      "Resource": [
        "arn:aws:s3:::dda-component-REGION-PORTAL_ACCOUNT",
        "arn:aws:s3:::dda-component-REGION-PORTAL_ACCOUNT/*"
      ]
    },
    {
      "Sid": "AllowGreengrassServiceAccess",
      "Effect": "Allow",
      "Principal": {
        "Service": "greengrass.amazonaws.com"
      },
      "Action": ["s3:GetObject", "s3:GetBucketLocation"],
      "Resource": [
        "arn:aws:s3:::dda-component-REGION-PORTAL_ACCOUNT",
        "arn:aws:s3:::dda-component-REGION-PORTAL_ACCOUNT/*"
      ],
      "Condition": {
        "StringEquals": {
          "aws:SourceAccount": ["PORTAL_ACCOUNT", "USECASE_ACCOUNT"]
        }
      }
    }
  ]
}'
```

### S3 Buckets Not Showing

**Cause**: Bucket not tagged.

**Fix** (run by the account that owns the bucket; `ACCOUNT_ID` = that account):
```bash
aws s3control tag-resource \
  --account-id ACCOUNT_ID \
  --resource-arn arn:aws:s3:::BUCKET_NAME \
  --tags Key=dda-portal:managed,Value=true
```

`aws s3api put-bucket-tagging` still works on buckets without ABAC and remains
the alternative there. For a bucket reached through a cross-account role also
enable ABAC (`aws s3api put-bucket-abac --bucket BUCKET_NAME --abac-status
Status=Enabled`), otherwise the role's tag condition is not evaluated and the
portal cannot read the bucket's tags.

### S3 Bucket Listed as "Not allowed", or an Operation Answers `bucket_not_allowed`

**Cause**: The bucket is tagged but is not one of the use case's allowed buckets
(its configured buckets plus its Additional S3 buckets).

**Fix**: A UseCaseAdmin uses **Allow for this use case** on the bucket in Data
Management, or adds it under **Use Cases** → **Edit** → **Allowed buckets**. If
the attach answers `bucket_not_eligible`, the `reason` and `remedy` fields say
what is missing (tag, ABAC / tag read, the bucket being configured on or tagged
for another use case — a PortalAdmin can allow a shared bucket). See
[Use case buckets and Additional S3 buckets](#use-case-buckets-and-additional-s3-buckets).

### CORS Error When Uploading Files

**Symptom**: Browser console shows `No 'Access-Control-Allow-Origin' header` error.

**Cause**: S3 bucket CORS not configured for portal uploads.

**Fix**: Configure CORS on the bucket (see [Configure S3 Bucket CORS](#configure-s3-bucket-cors-required-for-uploads) section above).

Quick CLI fix:
```bash
BUCKET="your-bucket"
DOMAIN="your-cloudfront-domain.cloudfront.net"

aws s3api put-bucket-cors --bucket $BUCKET --cors-configuration '{
  "CORSRules": [{
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["GET", "PUT", "POST", "HEAD"],
    "AllowedOrigins": ["https://'"$DOMAIN"'"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000
  }]
}'
```

### Training Job Fails with "Access Denied" to Data Bucket

**Symptom**: SageMaker training job fails with S3 access denied error.

**Cause**: When using a separate Data Account, the bucket policy doesn't allow the UseCase Account's SageMaker role.

**Fix**: Deploy (or redeploy) the Data Account stack with the bucket name:

```bash
cd edge-cv-portal/infrastructure
cdk deploy -a "npx ts-node bin/data-account-app.ts" \
  -c portalAccountId=PORTAL_ID \
  -c usecaseAccountIds=USECASE_ID \
  -c dataBucketNames=your-data-bucket
```

This creates a bucket policy allowing `DDASageMakerExecutionRole` from the UseCase Account to read from the bucket.

### Labeling Job Can't Find Images in Data Account

**Symptom**: "No images found" when creating labeling job, but images exist.

**Cause**: Portal can't assume Data Account role or wrong bucket configured.

**Fix**:
1. Verify UseCase has correct `data_account_role_arn` and `data_s3_bucket`:
   ```bash
   aws dynamodb get-item \
     --table-name edge-cv-portal-usecases \
     --key '{"usecase_id": {"S": "YOUR_ID"}}' \
     --query 'Item.{data_account_id:data_account_id.S, data_s3_bucket:data_s3_bucket.S, data_account_role_arn:data_account_role_arn.S}'
   ```

2. Verify External ID matches between DynamoDB and IAM role trust policy

3. Test role assumption manually:
   ```bash
   aws sts assume-role \
     --role-arn arn:aws:iam::DATA_ACCOUNT:role/DDAPortalDataAccessRole \
     --role-session-name test \
     --external-id YOUR_EXTERNAL_ID
   ```

### Lambda Timeout on Large Operations

**Cause**: Cross-account API calls taking too long.

**Fix**: Check CloudWatch logs for the specific Lambda function:
```bash
aws logs tail /aws/lambda/EdgeCVPortalComputeStack-ComponentsHandler --follow
```

---

## User Roles and Permissions

### Role Hierarchy

The portal uses a two-layer permission model, both layers held in the
Portal_Identity registry (`dda-portal-user-roles`):

1. **Account-level role** (`usecase_id='global'`): determines global capabilities
2. **UseCase assignment** (`usecase_id=<usecase>`): determines which usecases a
   user can access, and overrides the global role within that usecase

The Cognito / SSO `custom:role` attribute is descriptive metadata and is **not**
a privilege source — see
[Portal Users and Privilege](#portal-users-and-privilege-portal_identity-registry).

### Available Roles

| Role | Scope | Description |
|------|-------|-------------|
| **PortalAdmin** | Global | Super user with full access to all usecases and user management |
| **UseCaseAdmin** | Per-UseCase | Full access within assigned usecases, can manage team members |
| **DataScientist** | Per-UseCase | Can create labeling jobs, training jobs, and manage models |
| **Operator** | Per-UseCase | Can create deployments, manage devices, view logs |
| **Viewer** | Per-UseCase | Read-only access to view usecases, jobs, models, deployments |

A role held at the account level (`global`) applies to every use case, assigned
or not, until a use-case row overrides it there; the use-case list in the UI
shows a user's assignments, not their reach.

### Permission Matrix

| Action | PortalAdmin | UseCaseAdmin | DataScientist | Operator | Viewer |
|--------|:-----------:|:------------:|:-------------:|:--------:|:------:|
| **UseCase Management** |
| Create UseCase | ✅ | ❌ | ❌ | ❌ | ❌ |
| View All UseCases | ✅ | ❌ | ❌ | ❌ | ❌ |
| View Assigned UseCases | ✅ | ✅ | ✅ | ✅ | ✅ |
| Update UseCase | ✅ | ✅ | ❌ | ❌ | ❌ |
| Allow / remove Additional S3 buckets | ✅ | ✅ | ❌ | ❌ | ❌ |
| Allow a bucket configured on or tagged for another use case | ✅ | ❌ | ❌ | ❌ | ❌ |
| Create S3 bucket in the onboarding wizard (no use case yet) | ✅ | ❌ | ❌ | ❌ | ❌ |
| Delete UseCase (see the delete note under [Use case buckets](#use-case-buckets-and-additional-s3-buckets)) | ✅ | ✅ | ❌ | ❌ | ❌ |
| Manage Team Members | ✅ | ✅ | ❌ | ❌ | ❌ |
| Re-provision Shared Components | ✅ | ❌ | ❌ | ❌ | ❌ |
| **Labeling** |
| Create Labeling Job | ✅ | ✅ | ✅ | ❌ | ❌ |
| View Labeling Jobs | ✅ | ✅ | ✅ | ✅ | ✅ |
| Delete Labeling Job | ✅ | ✅ | ✅ | ❌ | ❌ |
| **Training** |
| Create Training Job | ✅ | ✅ | ✅ | ❌ | ❌ |
| View Training Jobs | ✅ | ✅ | ✅ | ✅ | ✅ |
| Stop Training Job | ✅ | ✅ | ✅ | ❌ | ❌ |
| **Models** |
| View Models | ✅ | ✅ | ✅ | ✅ | ✅ |
| Compile Model | ✅ | ✅ | ✅ | ❌ | ❌ |
| Package Model | ✅ | ✅ | ✅ | ❌ | ❌ |
| Publish Component | ✅ | ✅ | ✅ | ❌ | ❌ |
| Delete Model | ✅ | ✅ | ✅ | ❌ | ❌ |
| **Deployments** |
| Create Deployment | ✅ | ✅ | ❌ | ✅ | ❌ |
| View Deployments | ✅ | ✅ | ✅ | ✅ | ✅ |
| Cancel Deployment | ✅ | ✅ | ❌ | ✅ | ❌ |
| **Devices** |
| View Devices | ✅ | ✅ | ✅ | ✅ | ✅ |
| Restart Greengrass | ✅ | ✅ | ❌ | ✅ | ❌ |
| Reboot Device | ✅ | ✅ | ❌ | ✅ | ❌ |
| Browse Files | ✅ | ✅ | ❌ | ✅ | ❌ |
| View Logs | ✅ | ✅ | ✅ | ✅ | ✅ |
| Update Config | ✅ | ✅ | ❌ | ✅ | ❌ |

### UseCase Creation (PortalAdmin Only)

Creating a use case is a PortalAdmin action: `POST /usecases` (and the
onboarding wizard's **Verify Role** step, `POST /usecases/verify-role`)
requires the `create_usecases` permission, which only the PortalAdmin role
holds. Any other caller receives `403 INSUFFICIENT_PERMISSIONS` and the attempt
is audited as `unauthorized_access`; the **Onboard New Use Case** action is
hidden for them (cosmetic — the server decides). A use case is created for an
account the portal already trusts and controls through the account stack
(see [Creating a UseCase](#creating-a-usecase)).

When a PortalAdmin creates a use case:
1. The use case is created in DynamoDB
2. The creator is assigned as **UseCaseAdmin** for that use case
3. The PortalAdmin (or that UseCaseAdmin) adds the other team members via
   "Manage Team"

A UseCaseAdmin who needs a new use case asks a PortalAdmin. The PortalAdmin
picks or creates its bucket in the wizard (**Create new bucket** tags the
bucket, enables ABAC on it and allows it for the new use case), or an AWS
administrator tags an existing bucket first
(`aws s3control tag-resource --account-id PORTAL_ACCOUNT_ID --resource-arn
arn:aws:s3:::BUCKET --tags Key=dda-portal:managed,Value=true`). Registering a
tagged portal-account bucket makes the portal enable ABAC on it, which changes
how every tag-conditioned policy in the portal account evaluates for that
bucket — review existing tag conditions first.

Selecting a bucket that is untagged or configured on another use case answers
`400 bucket_not_eligible` (`no_tags` or `bucket_configured_on_other_usecase`).
After creation the use case's buckets follow the rules in
[Use case buckets and Additional S3 buckets](#use-case-buckets-and-additional-s3-buckets);
its UseCaseAdmin can allow further tagged buckets and create buckets for it
through the API.

### Managing Team Members

UseCaseAdmins and PortalAdmins can manage team members for their usecases:

1. Go to **Use Cases** page
2. Click **Actions** → **Manage Team** for the usecase
3. Add users by email and assign a role
4. Remove users as needed

### PortalAdmin as Fallback

PortalAdmins have global access to all usecases. This ensures:
- No usecase becomes orphaned if the UseCaseAdmin leaves
- PortalAdmins can reassign admins when needed
- Emergency access is always available

### Setting User Roles

Set roles through the portal's **User Manager** (see
[Portal Users and Privilege](#portal-users-and-privilege-portal_identity-registry)).
The User Manager writes both the Cognito `custom:role` attribute **and** the
Portal_Identity registry row, and the registry row is what grants privilege.

Valid role values: `PortalAdmin`, `UseCaseAdmin`, `DataScientist`, `Operator`, `Viewer`, `DataLabeler`

> **Do not** grant a role with `aws cognito-idp admin-update-user-attributes
> --user-attributes Name=custom:role,...`. With registry enforcement on, that
> attribute grants **nothing** — it is descriptive metadata that is recorded in
> the audit trail and never used as a privilege source. An account whose only
> "role" is a `custom:role` attribute is denied every route with HTTP 403.

---

## Portal Users and Privilege (Portal_Identity Registry)

This section is the operator runbook for the portal's authorization model after
the JWT-role privilege-escalation fix
(`.kiro/specs/portal-jwt-role-privilege-escalation`). Read it before creating
users, changing roles, or enabling enforcement.

### The registry is the authority

Portal privilege comes from a **Portal_Identity row** in the DynamoDB table
`dda-portal-user-roles`, keyed (`user_id` = Cognito `sub`, `usecase_id`):

| Attribute | Meaning |
|-----------|---------|
| `user_id` | The account's Cognito `sub` |
| `usecase_id` | `global` = the account-level entry; any other value = a UseCase assignment |
| `role` | `PortalAdmin` / `UseCaseAdmin` / `DataScientist` / `Operator` / `Viewer` / `DataLabeler` |
| `status` | `enabled` or `disabled` (a row written before this fix carries none and is treated as enabled) |
| `username`, `email` | Human-readable attribution, captured at write time |
| `assigned_by`, `assigned_at` | Who provisioned the row and when (`backfill` for rows the backfill script wrote) |

Resolution on every request (`shared_utils.RBACManager.get_user_role`):

1. No **enabled global** row for the caller's `sub` → **no role** → HTTP 403
   `Insufficient permissions`, audited with `identity_source=absent` and the
   claimed `custom:role`.
2. An enabled global row → that row's role.
3. An enabled UseCase row, when the request is scoped to that UseCase →
   that row's role, overriding the global role (unchanged Team Management
   precedence).
4. The registry lookup **failing** (DynamoDB error) → HTTP 500
   `Authorization check failed`, audited `result='failure'`. An outage is never
   recorded as a permission decision and never downgrades the caller to
   `Viewer`.

The token's `custom:role` claim is **Claimed_Role**: descriptive metadata only.
It is recorded in audit entries (a mismatch between claim and registry role is
itself a signal) and never grants anything. This is the fix: a single
`cognito-idp:AdminCreateUser` call carrying `custom:role=PortalAdmin` no longer
mints a privileged portal principal, because that account has no registry row.

Every audit entry in `dda-portal-audit-log` now also carries `username`,
`email`, `source_ip`, `user_agent`, and `identity_source`, captured from the
request at write time, so deleting the Cognito account afterwards cannot erase
who acted.

> The User Manager's account list shows each account's Cognito `custom:role`
> attribute, not its registry role. For accounts provisioned through the portal
> the two agree. To read what actually decides privilege, query the registry
> (see [Verifying an account](#verifying-an-account)).

### Provisioning a user — through the portal only

Use the portal's **User Manager** (sign in as a PortalAdmin → user menu
top-right → **User Manager**, route `/admin/user-manager`). It is the only path
that keeps Cognito and the registry in step:

| Action | Cognito | Registry (`usecase_id='global'`) |
|--------|---------|----------------------------------|
| Create account | `AdminCreateUser` (+ attributes) | row written with `role`, `username`, `email`, `status='enabled'` |
| Change role | `custom:role` updated | `role` updated — **this** is the value that takes effect |
| Disable / enable | `AdminDisableUser` / `AdminEnableUser` | `status` set `disabled` / `enabled` |
| Delete | `AdminDeleteUser` | global row removed, then every per-UseCase row |

Notes:

- If the Cognito account is created but the registry write fails, the API
  answers **502** and records the partial state in the audit trail. The
  account is inert (no registry row → denied), so the safe recovery is to
  delete it in the User Manager and create it again.
- **Disabling** matters: Cognito's own disable only blocks new sign-ins, so an
  already-issued token keeps working until it expires. The registry row's
  `status=disabled` is what stops that token on its **next request**. The same
  is true of deletion.
- Per-UseCase grants keep being made through **Manage Team** on the UseCases
  page; they write the same table with the UseCase's id as `usecase_id`.
- Creating users with `aws cognito-idp admin-create-user` is **not**
  provisioning. Such an account can sign in and gets a valid token, and is
  then denied every route. The only exception is the bootstrap admin, below.

#### Bootstrapping the first PortalAdmin

The first PortalAdmin cannot be created through the portal (nobody can sign in
yet), so it is provisioned by hand — Cognito account plus registry row. See
[Create Admin User](#create-admin-user). Every subsequent account should go
through the User Manager.

### Backfilling the registry (before enforcement)

The registry was historically populated only by Team Management's per-UseCase
grants, so an existing portal has almost no global rows. Enabling enforcement
first would deny **every** user, including the bootstrap `admin`. Run the
backfill first: `edge-cv-portal/backfill_portal_registry.py` writes the global
row each existing enabled pool account needs, carrying the role that account
effectively has today (its `custom:role`, or `Viewer` when absent), so enabling
enforcement changes nobody's access.

It is **dry run by default**, idempotent, and re-runnable: it never overwrites
an existing row (conditional write), skips **disabled** Cognito accounts so they
stay denied, and touches only `usecase_id='global'` rows.

```bash
cd edge-cv-portal

# 1. Review the plan — writes nothing
./backfill_portal_registry.py \
  --user-pool-id us-east-1_XXXXXXXXX --region us-east-1

# 2. Apply, after reviewing every line of the plan
./backfill_portal_registry.py \
  --user-pool-id us-east-1_XXXXXXXXX --region us-east-1 --apply

# 3. Re-run the dry run: every account should now report `exists`
./backfill_portal_registry.py \
  --user-pool-id us-east-1_XXXXXXXXX --region us-east-1
```

Flags: `--table-name` (default `$USER_ROLES_TABLE`, else
`dda-portal-user-roles`), `--region`, `--page-size`. Credentials need
`cognito-idp:ListUsers` on the pool and `dynamodb:GetItem`/`PutItem` on the
table. Exit status: 0 = run completed, 1 = at least one account could not be
processed, 2 = usage error.

What to check in the plan before applying:

- Every account you expect is listed, and the `role=` column matches the access
  that account should keep. `claim=<none>` accounts are backfilled as `Viewer`.
- `exists` lines say the row was left unchanged; a
  `(differs from claim …)` note means the registry and the attribute disagree —
  the registry wins, which is the point, but it is worth a look.
- `skip-disabled` accounts stay denied by design. Re-enable them in the
  User Manager if that is wrong.
- `error` lines mean that account could not be processed (the registry read or
  write failed) and still has **no** row: fix the cause and re-run.

### The enforcement flag

`PORTAL_REGISTRY_ENFORCED` is the single switch (design Decision 4). It is a
Lambda environment variable on every portal handler that resolves roles, and a
fresh install starts with it **off**.

| Flag | Behaviour |
|------|-----------|
| off (fresh-install default, `false`) | Legacy resolution — the `custom:role` claim still grants — **plus** a `would_deny` WARNING naming every request enforcement would have denied. A dry run against real traffic. |
| on (`true`) | The registry is authoritative, per [The registry is the authority](#the-registry-is-the-authority). |

Deploy with it on (only after the backfill):

```bash
cd edge-cv-portal
PORTAL_REGISTRY_ENFORCED=true ./deploy-infrastructure.sh
```

**Production posture.** Production deployments run with enforcement **on**:
`PORTAL_REGISTRY_ENFORCED=true ./deploy-infrastructure.sh`, after
`backfill_portal_registry.py` has been applied and the accounts verified (the
[Rollout order](#rollout-order) below). With the flag off, every authenticated
account without a use-case assignment still resolves to Viewer through the
legacy path, and the use-case bucket rules above apply to that principal like
to any other member. `deploy-infrastructure.sh` prints a warning to stderr
whenever the value resolved for a deploy is not `true`, so a deploy that leaves
enforcement off is never silent.

The script passes this through as the CDK context value
`-c portalRegistryEnforced=true`. Accepted affirmatives are `1`, `true`, `yes`,
`on`, `enabled` (trimmed, case-insensitive) — exactly what the Lambda layer
accepts, so a value that reads "on" at deploy time can never land as "off" in
the handler. Accepted negatives are `0`, `false`, `no`, `off`, `disabled`; the
app refuses to synthesize any other value, so a typo such as `ture` stops the
deploy instead of deploying enforcement off. To turn it back off, deploy with
`PORTAL_REGISTRY_ENFORCED=false`.

**Unset keeps the deployed value.** When the variable is unset (or empty),
`deploy-infrastructure.sh`, `deploy-frontend.sh` (its ComputeStack redeploy),
`infrastructure/deploy_portal_fixes.sh` and `infrastructure/deploy-auth.sh`
read the value the deployed `EdgeCVPortal*` handlers carry and pass that, so a
routine deploy never changes enforcement. A mixed state resolves to on. With no
portal handler deployed yet (a fresh install) they pass
`-c allowLegacyRoles=true`, the acknowledgement the app requires to deploy the
CDK default, off; `cdk.json` deliberately carries neither key. If the deployed
value cannot be read (credentials, `lambda:ListFunctions` permission), the
deploy stops and asks for an explicit `PORTAL_REGISTRY_ENFORCED=true` or
`=false`. The logic lives in `scripts/portal-registry-enforcement.sh`. Each
script logs the effective value and where it came from —
`Portal_Identity enforcement: <value> (requested)`, `(kept from deployed)`, or
`CDK default (off), acknowledged (fresh install)` — and `deploy-portal.sh`
keeps that line in its Deployment_Log. `deploy-auth.sh` passes the value
although the Auth stack never reads it, because the CDK CLI synthesizes the
whole app before it deploys one stack.

A bare `npx cdk deploy`, `cdk synth` or `cdk diff` in `infrastructure/`
(including `npm run deploy` and `npm run synth`) runs none of the scripts, so
the app asks for the value itself: it refuses to synthesize until you pass
`-c portalRegistryEnforced=true` or `-c portalRegistryEnforced=false` (or
another accepted spelling above), or, on a fresh install,
`-c allowLegacyRoles=true` to deploy with enforcement off. An unrecognized
value is refused the same way, and the message names it. With the npm scripts,
pass the flag after `--`, for example
`npm run deploy -- -c portalRegistryEnforced=true`.

Check what is deployed:

```bash
aws lambda list-functions --region us-east-1 \
  --query "Functions[?starts_with(FunctionName, 'EdgeCVPortal')].Environment.Variables.PORTAL_REGISTRY_ENFORCED" \
  --output text | tr '\t' '\n' | sort | uniq -c
```

**Size the gap before flipping.** With the flag off, mine the dry-run WARNINGs
from the deployed logs — each one names a principal the backfill has not
covered:

```bash
# Log group names are CDK-generated (EdgeCVPortal<Stack>-<Handler><hash>);
# sweep every portal handler rather than guessing one.
for LG in $(aws logs describe-log-groups --region us-east-1 \
  --log-group-name-prefix /aws/lambda/EdgeCVPortal \
  --query 'logGroups[].logGroupName' --output text); do
  aws logs filter-log-events --region us-east-1 --log-group-name "$LG" \
    --filter-pattern would_deny --start-time $(( ($(date +%s) - 86400) * 1000 )) \
    --query 'events[].message' --output text
done
```

Each line carries the `sub`, the scope, `claimed_role`, `claimed_username`, and
the `legacy_role` the caller is getting today. A quiet log across all portal
handlers, over a period that covers your real users, is the signal that
enforcement is safe to enable. (`would_deny undetermined` lines mean the
registry lookup itself failed — fix that first; they say nothing about
coverage.)

### Rollout order

1. Deploy with the flag **off** (the default on a fresh install, otherwise
   `PORTAL_REGISTRY_ENFORCED=false`) and confirm every portal handler shows
   `PORTAL_REGISTRY_ENFORCED=false`.
2. Backfill: dry run → review → `--apply` → dry run again (all `exists`).
3. Watch the `would_deny` WARNINGs until they stop for real users.
4. Verify each known account resolves the role you expect (below).
5. Redeploy with `PORTAL_REGISTRY_ENFORCED=true`.
6. Re-verify: the bootstrap `admin` can still reach the User Manager, build
   operators can still `POST /builds`, and a UseCase member still sees their
   UseCase.
7. Confirm the hole is closed: create a throwaway pool account with
   `custom:role=PortalAdmin` and **no** registry row, present its token to
   `POST /builds` — expect **403** — then delete the account and confirm the
   denial's audit row still names its username and email.

If enforcement locks someone out, the fastest fix is to add their global row
(User Manager, or the backfill script re-run for a newly created account).
Redeploying with `PORTAL_REGISTRY_ENFORCED=false` restores the previous
behaviour wholesale (an unset variable now keeps enforcement on).

### Verifying an account

```bash
USER_POOL_ID="us-east-1_XXXXXXXXX"
USERNAME="user@company.com"

USER_SUB=$(aws cognito-idp admin-get-user \
  --user-pool-id "$USER_POOL_ID" --username "$USERNAME" \
  --query 'UserAttributes[?Name==`sub`].Value' --output text)

# The row that decides privilege (global scope)
aws dynamodb get-item --table-name dda-portal-user-roles \
  --key "{\"user_id\":{\"S\":\"$USER_SUB\"},\"usecase_id\":{\"S\":\"global\"}}"

# Every row for the account, global plus per-UseCase
aws dynamodb query --table-name dda-portal-user-roles \
  --key-condition-expression 'user_id = :u' \
  --expression-attribute-values "{\":u\":{\"S\":\"$USER_SUB\"}}"
```

An account is provisioned when the `global` row exists, `status` is `enabled`
(or absent), and `role` names the intended role. Unexpected 403s under
enforcement are almost always a missing or `disabled` global row; the handler
log records `Registry denial: user <sub> has no enabled Portal_Identity …` and
the audit entry records `identity_source=absent`.

### Defense in depth and detection — not the fix

Two further controls ship with this fix. **Neither prevents the incident**, and
neither should be mistaken for the actual control, which is the registry
enforced in the shared RBAC layer:

- **SRP-only app client.** The `dda-portal-client` app client no longer enables
  the `USER_PASSWORD_AUTH` or `ADMIN_USER_PASSWORD_AUTH` (`ADMIN_NO_SRP_AUTH`)
  flows, so a password issued out of band with `AdminSetUserPassword` cannot be
  exchanged for portal tokens through those flows. This is **defense in depth
  only**: an actor holding `cognito-idp:UpdateUserPoolClient` can simply
  re-enable the flows, and SRP itself works with an admin-set password.
  Browser sign-in, the new-password challenge, forgot-password and token
  refresh are unaffected (Amplify signs in with SRP by default; the portal's
  refresh uses `REFRESH_TOKEN_AUTH`, which Cognito always allows).
- **Out-of-band Cognito administration alerts.** An EventBridge rule
  `dda-portal-cognito-admin-activity` matches CloudTrail `AdminCreateUser`,
  `AdminSetUserPassword`, `AdminUpdateUserAttributes`, `AdminAddUserToGroup`,
  `AdminEnableUser`, `AdminDisableUser` and `AdminDeleteUser` on **this** user
  pool from any caller other than the portal's User Manager Lambda role, and
  publishes to the SNS topic `dda-portal-cognito-admin-alerts` (stack output
  `CognitoAdminActivityTopicArn`). The message carries the event name, caller
  ARN, source IP, user agent, event time, pool id, region and CloudTrail
  `eventID`. This is **detection only**: it prevents nothing, and an actor with
  `events:DisableRule` can silence it.

  Two operator steps are required for it to work:

  ```bash
  # 1. Subscribe someone (subscriptions are managed out of band)
  aws sns subscribe --region us-east-1 \
    --topic-arn "$(aws cloudformation describe-stacks \
      --stack-name EdgeCVPortalComputeStack --region us-east-1 \
      --query 'Stacks[0].Outputs[?OutputKey==`CognitoAdminActivityTopicArn`].OutputValue' \
      --output text)" \
    --protocol email --notification-endpoint oncall@company.com

  # 2. Confirm a CloudTrail trail logs management events in this region —
  #    without one, "AWS API Call via CloudTrail" events never reach
  #    EventBridge and the rule stays silent. The portal does not create a
  #    trail (an account/organization trail normally already exists; a second
  #    one would duplicate delivery and cost).
  aws cloudtrail describe-trails --region us-east-1 \
    --query 'trailList[].{Name:Name,Multi:IsMultiRegionTrail,Home:HomeRegion}'
  ```

**The complementary preventive control lives outside this repo.** Anyone who
can call `cognito-idp:Admin*` on the pool can still create accounts, set
passwords and delete users — they just cannot obtain portal privilege that way,
and the actions are attributable and alerted. Restricting `cognito-idp:Admin*`
to the portal's own User Manager role via an **SCP or a permission boundary**
is the control that stops the calls themselves; it is an operational task for
whoever governs the AWS account, and this repo cannot enforce it. Likewise,
anyone able to edit IAM, CloudFormation or the Lambda code can grant themselves
anything — that is an explicit non-goal of the fix.

#### Known caveat

The `/admin/*` User Manager routes are additionally gated by
`user_admin.require_portal_admin`, which reads the `custom:role` **claim**
rather than the registry, so under enforcement an unprovisioned principal
claiming `PortalAdmin` can still reach those routes (all its other portal access
is denied). Treat `cognito-idp:AdminUpdateUserAttributes` on the pool as
User-Manager-equivalent privilege until that check is moved onto the registry,
and keep the alerting above subscribed.

---

## MCP server (AI assistants)

The portal serves a Model Context Protocol (MCP) endpoint, so an AI assistant
such as Kiro IDE or Kiro CLI can call portal tools as the signed-in user. The
Portal_MCP_Server is one Lambda behind its own HTTP API
(`EdgeCVPortalMcpStack`). It signs users in through a dedicated app client on
the portal's existing Cognito user pool, dispatches each tool call to the
existing handler Lambda of the matching REST route, applies the same role and
permission checks as the web UI, and writes every call to the Audit_Log.

Phase 1 delivers read-only tools only: the server lists and accepts only the
Phase-1 rows of the [tool tables](#mcp-tools). The edge devices have their own
MCP server: [docs/edge-mcp-server.md](../docs/edge-mcp-server.md).

No stdio MCP package is distributed, so the portal's only connection form is
remote OAuth: the client connects to the HTTPS endpoint and signs the user in
through the Cognito hosted UI.

### Enabling and disabling the MCP stack

`./deploy-infrastructure.sh` deploys `EdgeCVPortalMcpStack` by default; no
setting is needed.

- The MCP Lambda layer (`McpLayer`: `mcp`, `mangum` and their dependencies)
  is built during `cdk synth`, like the other bundled portal layers, so no
  separate build step comes before the deploy. `backend/layers/mcp/build.sh`
  installs the x86_64 CPython 3.11 wheels pinned in
  `backend/layers/mcp/requirements.lock.txt` with the host's `pip`, or inside
  the Python 3.11 bundling image when the host has no `pip` (Docker needed
  then). Either way pip needs network access to the package index.
- The synth fails, and nothing is deployed, when a pinned wheel cannot be
  installed.
- To change a version, edit `backend/layers/mcp/requirements.txt` and
  regenerate the lock with the `uv pip compile` command in its header.

Disabling it on a deployed portal takes **two steps, in this order**:

```bash
# 1. With the stack still in the app (no DEPLOY_MCP_SERVER=false yet), in the
#    deploy environment, passing the enforcement value that is deployed:
cd infrastructure && npx cdk destroy EdgeCVPortalMcpStack --force \
  -c portalRegistryEnforced=<the deployed value> && cd ..
# 2. Deploy without it:
DEPLOY_MCP_SERVER=false ./deploy-infrastructure.sh
```

- Step 1 runs in the environment a deploy uses, because `cdk destroy`
  synthesizes the whole portal app to find the stack:
  - `AWS_REGION` exported, set to the portal's region;
  - the trusted UseCase account list resolvable from the CDK context value
    `trustedUseCaseAccountIds`, `TRUSTED_USECASE_ACCOUNT_IDS` or the SSM
    parameter `/dda-portal/trusted-usecase-account-ids`, as for
    `deploy-infrastructure.sh`; otherwise it stops with `ComputeStack requires
    a non-empty trustedUseCaseAccountIds list`;
  - `-c portalRegistryEnforced=true` or `=false`. Without it the app refuses
    to synthesize (`portalRegistryEnforced is unset`, see
    [The enforcement flag](#the-enforcement-flag)). `cdk destroy` deploys no
    template, so the value only satisfies that check and changes nothing.
    Pass the deployed value anyway; the "Check what is deployed" query in that
    section prints it.
- Only the literal value `false` (trimmed, case-insensitive) turns the stack
  off. Any other value, unset or empty included, leaves it on.
- The order matters: the MCP stack imports handler ARNs that
  `EdgeCVPortalComputeStack` and three other stacks export. With
  `DEPLOY_MCP_SERVER=false` those stacks drop the exports, and CloudFormation
  refuses to delete an export that a deployed stack still imports, so the
  whole portal deploy would stop. `cdk deploy --all` never deletes a stack that
  only left the app, so step 1 removes it explicitly.
- `--force` skips `cdk destroy`'s confirmation prompt. Step 1 also deletes the
  MCP app client, its resource server and the dispatch-map parameter.
- **The guard message.** With `DEPLOY_MCP_SERVER=false`, the deploy script
  first checks whether `EdgeCVPortalMcpStack` exists. If it does, the script
  stops with `❌ DEPLOY_MCP_SERVER=false, but EdgeCVPortalMcpStack is still
  deployed.` and prints the step-1 command, already carrying the region and
  the CDK context the script resolved (the enforcement value included), so it
  can be run as printed from the same shell. If the check itself fails
  (credentials, region), it stops with `Could not read whether
  EdgeCVPortalMcpStack is deployed` rather than guess. Nothing is deployed in
  either case.

To turn it back on, build the layer as above and run `./deploy-infrastructure.sh`
without the variable.

### Stack outputs

| Output | Value |
|---|---|
| `McpServerUrl` | The canonical endpoint, `https://<api-id>.execute-api.<region>.amazonaws.com/mcp` (every tool) |
| `McpViewUrls` | The five view endpoints, `…/mcp/data`, `…/mcp/models`, `…/mcp/devices`, `…/mcp/workflows`, `…/mcp/admin` (one area's tools each) |
| `McpUserPoolClientId` | The MCP app client id (public client, no secret) |
| `McpOAuthScope` | The scope clients request, `<McpServerUrl>/portal.tools` |
| `McpRedirectUri` | The registered callback, `http://localhost:7899/oauth/callback` |
| `McpProtectedResourceMetadataUrl` | The root Protected Resource Metadata document (RFC 9728) |
| `McpAuthorizationServerMetadataUrl` | The authorization-server metadata facade (RFC 8414) |

```bash
aws cloudformation describe-stacks --stack-name EdgeCVPortalMcpStack \
  --query 'Stacks[0].Outputs' --output table
```

### Settings (CDK context)

Set these in the `context` block of `infrastructure/cdk.json` (which
`deploy-infrastructure.sh` picks up), then run `./deploy-infrastructure.sh`.

| Context key | Default | Effect |
|---|---|---|
| `mcpRedirectUris` | empty | Extra OAuth callback URLs (comma-separated) beside `http://localhost:7899/oauth/callback`, for a client on another port. Each must be `http://localhost[:port]/path` or `https://…`: Cognito registers plain-HTTP callbacks for `localhost` only, and the synth refuses anything else. |
| `mcpMutatingToolsEnabled` | `false` | Lists and accepts mutating tools. In Phase 1 no mutating tool is delivered, so this changes nothing yet. |
| `mcpAllowedOrigins` | empty | `Origin` values (`http(s)://host[:port]`, no path) the endpoints accept. Empty by default: any request carrying an `Origin` header is refused. Set it only if the deployed-stack smoke check shows your client sending an `Origin` header. |

### Timeout alarm

The stack creates `McpTimeoutAlarm`: it fires when the MCP Lambda reaches its
own 29 s timeout, which is the only trace of a tool call the Lambda could not
answer (the client sees a bare gateway error, with no tool result and no audit
entry). The alarm has no action; attach the portal's notification topic:

```bash
aws cloudwatch describe-alarms --alarm-name-prefix EdgeCVPortalMcpStack \
  --query 'MetricAlarms[].AlarmName'
# Then add the topic as the alarm action in the console, or with
# `aws cloudwatch put-metric-alarm` repeating the alarm's settings plus --alarm-actions <topic-arn>.
```

The Lambda logs to `/dda-portal/mcp-server`: one `mcp_tool` line per tool
completion (`sub`, `username`, `tool`, `view`, `outcome`, `category`, `status`,
`duration_ms`, `request_id`, `client`, `server_version`; never arguments,
tokens or email).

### Roles and the registry-enforcement flag

MCP resolves roles from the Portal_Identity_Registry, like the web UI
([The enforcement flag](#the-enforcement-flag)). With
`PORTAL_REGISTRY_ENFORCED=false`, every `global`-scoped tool resolves a
non-PortalAdmin to **Viewer**. So these tools are usable through MCP by
PortalAdmins only, until enforcement is on **and** the account has a `global`
registry row naming its role (the User Manager writes it; older accounts need
the [backfill](#backfilling-the-registry-before-enforcement)):

- the labeler self-service tools, `portal_data_labeler_*`
  (`portal_data_labeler_jobs_list`, `_pool_get`, `_task_image_url_get`,
  `_task_next`, and the Phase-2 `_task_steal`, `_task_submit`,
  `_task_presentation_failure`);
- the build tools, `portal_workflows_build*`, including the build-fleet reads
  `portal_workflows_build_servers_list` and `portal_workflows_build_config_get`.

A PortalAdmin without a `global` registry row is a Viewer through MCP. MCP is
never more permissive than the web UI in either mode.

### Sign-in and authentication

- **The MCP app client allows only the hosted-UI authorization-code grant
  (with PKCE) and the refresh grant** (`ExplicitAuthFlows:
  ALLOW_REFRESH_TOKEN_AUTH`). There is no username/password sign-in against it
  from any script. Access tokens last 1 hour, refresh tokens 30 days.
- **The MCP routes carry no API Gateway authorizer.** The Lambda authenticates
  every request itself: it verifies the Cognito access token's signature,
  expiry, issuer, `token_use`, client id, audience and scope, and answers
  `401` otherwise. The discovery documents are unauthenticated by design
  (RFC 9728, RFC 8414).
- A token the pool signed that fails one of the claim checks (expired, wrong
  `token_use`, client id, scope or audience) writes an `authentication_failed`
  audit entry. A missing or unsigned token is answered `401` without one.
- **Disabling a portal account ends its MCP access when the client's current
  access token expires**, up to 60 minutes later, because the server validates
  the token itself. The next refresh is refused, and the client's tool calls
  fail from then on (observed 2026-10-26).

### Configure Kiro IDE or CLI

Copy the values from the stack outputs into `.kiro/settings/mcp.json` (the
workspace) or `~/.kiro/settings/mcp.json` (the user). No secret is involved:

```jsonc
{
  "mcpServers": {
    "dda-portal": {
      "url": "https://<api-id>.execute-api.<region>.amazonaws.com/mcp",   // McpServerUrl, or a view: .../mcp/devices
      "oauth": {
        "clientId": "<McpUserPoolClientId>",
        "redirectUri": "http://localhost:7899/oauth/callback",          // McpRedirectUri, copied unchanged
        "oauthScopes": ["https://<api-id>.execute-api.<region>.amazonaws.com/mcp/portal.tools"]  // McpOAuthScope
      },
      "disabled": false,
      "autoApprove": ["portal_admin_whoami", "portal_devices_list", "portal_devices_get"],
      "disabledTools": []
    }
  }
}
```

- The same entry works for Kiro CLI; the app client has no secret, so no
  confidential-client fields are needed.
- To use a view endpoint, add a separate server entry with the view URL (one
  of `McpViewUrls`) beside the aggregate one. In Kiro IDE 1.4.26, changing the
  `url` of a connected entry and reconnecting kept the client on the old URL.
  Kiro keeps a token per server entry, so the new entry signs in on its own.
- `autoApprove` did not suppress the first approval prompt in Kiro IDE 1.4.26
  or Kiro CLI 2.27.0; expect to approve the first call of a listed tool once.
- **`redirectUri` must be the literal registered value**
  `http://localhost:7899/oauth/callback`. Kiro binds its loopback listener to
  the configured value and Cognito matches callback URLs exactly. Kiro's
  documentation example `http://127.0.0.1:8080/oauth/callback` does not apply:
  Cognito registers plain-HTTP callbacks for `localhost` only. For another
  port, register it with `mcpRedirectUris`.
- `oauthScopes` is set so Kiro requests only the portal scope. If a client
  build still adds default scopes and Cognito answers `invalid_scope`, use the
  fallback `"oauthScopes": []`: Cognito then issues every scope the client
  allows, which is exactly `portal.tools`.
- The repository's `.kiro/settings/mcp.json` carries a disabled `dda-portal`
  entry with placeholder values.

### Discovery layout

| URL | Document |
|---|---|
| `https://<api-id>.execute-api.<region>.amazonaws.com/.well-known/oauth-protected-resource` | Root Protected Resource Metadata: the canonical `/mcp` document, byte for byte |
| `…/.well-known/oauth-protected-resource/mcp` | Protected Resource Metadata of `/mcp` |
| `…/.well-known/oauth-protected-resource/mcp/<view>` | One document per view endpoint, `resource` = that view's URL |
| `…/.well-known/oauth-authorization-server` | The authorization-server metadata facade (RFC 8414) |

- Every Protected Resource Metadata document names
  `authorization_servers: ["https://<api-id>.execute-api.<region>.amazonaws.com"]`,
  the MCP API's own origin, and the facade at that origin carries the same
  string as its `issuer`.
- **Why the facade exists.** MCP clients read the authorization server's
  RFC 8414 metadata, and Cognito serves none at the locations they try (both
  answer 400). Cognito's OpenID Connect document also omits
  `code_challenge_methods_supported`, which MCP clients must read as "no PKCE"
  and refuse. The facade is a static document that points at the Cognito
  hosted-UI `authorize`, `token` and `revoke` endpoints and the pool's
  `jwks_uri`, and declares `S256`. It issues nothing: Cognito mints and
  verifies every code and token (tokens still carry the Cognito issuer).
- **Why clients use the well-known URIs.** The Lambda's `401` carries a
  `WWW-Authenticate` challenge naming the metadata URL. The portal's HTTP API
  passes that header through unchanged (observed 2026-10-25); a REST API in
  front of the Lambda would deliver it under the remapped name
  `X-Amzn-Remapped-WWW-Authenticate`, and a client may ignore the header
  anyway. The MCP specification obliges clients to fall back to the well-known
  URIs (path-suffixed first, then the root), and the portal relies on that.
- Challenge header as observed through API Gateway (first deployed-stack smoke
  check and the owner's Kiro IDE session, 2026-10-25): the HTTP API passed the
  Lambda's challenge through unchanged, as a plain `www-authenticate` header;
  no `x-amzn-remapped-www-authenticate` header appeared. The value was
  `Bearer error="invalid_token", error_description="Authentication required",
  resource_metadata="<the endpoint's path-suffixed PRM URL>"` on `/mcp` and on
  each view endpoint, with an `HTTP/2 401`, `content-type: application/json`
  response whose body is `{"error": "invalid_token", "error_description":
  "Authentication required"}`. Clients that do read the header can follow it;
  the well-known fallback above still applies to those that do not.

### Reading MCP audit entries

- In the portal's **Audit Logs** page, the **Channel** filter shows All, Web &
  API (`channel=api`) or MCP (`channel=mcp`) entries.
- Every entry written after this change carries `channel` (`api` or `mcp`).
  MCP entries also carry `mcp_tool`, `mcp_server`, `mcp_client` and
  `mcp_request_id`.
- `action = mcp_tool_call` is one tool call: `result` is `success`, `failure`
  or `denied`; `details` holds the tool, `usecase_id`, the backend status, the
  error category, `duration_ms` and the identifier arguments. A `denied` result
  with `reason: usecase_binding` means the object belongs to another Use_Case
  than the one named.
- `action = authentication_failed` is a rejected token: `resource_type =
  mcp_server`, `resource_id` = the endpoint URL, `details.reason` one of
  `expired`, `token_use`, `client_id`, `scope`, `audience`, and `mcp_tool = -`.
- `unauthorized_access` and `authorization_unavailable` entries written by the
  MCP Lambda carry the same marker attributes, and entries the backing
  handlers write during an MCP call carry them too.
- The Channel filter scans the table page by page (only the page is sorted
  newest-first), so a page can hold few MCP rows among the web polling entries
  and MCP entries may appear out of time order across pages. Entries written
  before the MCP release carry no `channel` attribute; they count as Web & API.

### Versions

| Item | Value |
|---|---|
| Supported MCP protocol revisions | `2026-07-28`, `2025-11-25`, `2025-06-18`, `2025-03-26` |
| `mcp` Python SDK | `2.3.0` (Lambda layer, with `mangum` `0.22.0`) |
| Portal_MCP_Server version | `1.0.0` |
| Tested Kiro IDE and CLI versions | Kiro IDE 1.4.26 (OAuth sign-in, tool calls, token refresh and a view endpoint observed 2026-10-25/26); Kiro CLI 2.27.0 (OAuth sign-in and a tool call observed 2026-10-26); both negotiated MCP protocol `2026-07-28` |
| Kiro and MCP documentation checked | 2026-10-18 and 2026-10-19 |

### Troubleshooting MCP sign-in

**Every MCP request, discovery included, answers HTTP 500.** The MCP Lambda
could not import its packages: the deployed `McpLayer` does not hold them, for
example after a deploy from an older tree that packaged the layer directory
without building it (see
[Enabling and disabling the MCP stack](#enabling-and-disabling-the-mcp-stack)).
No tool result, audit entry or `McpTimeoutAlarm` state change follows. The log
group `/dda-portal/mcp-server` shows `Runtime.ImportModuleError: Unable to
import module 'mcp_portal.handler'` with `No module named 'mangum'` (or
`'mcp'`):

```bash
aws logs filter-log-events --log-group-name /dda-portal/mcp-server \
  --filter-pattern ImportModuleError --max-items 5 --query 'events[].message'
```

Run `./deploy-infrastructure.sh` again from this tree: the synth bundles the
layer from `backend/layers/mcp/requirements.lock.txt`.

**"OAuth issuer mismatch".** Fetch the root Protected Resource Metadata and the
facade, and compare `authorization_servers[0]` with `issuer` byte for byte
(scheme, host, no trailing slash):

```bash
curl -s https://<api-id>.execute-api.<region>.amazonaws.com/.well-known/oauth-protected-resource
curl -s https://<api-id>.execute-api.<region>.amazonaws.com/.well-known/oauth-authorization-server
```

**`invalid_scope` from Cognito.** The client requested a scope the app client
does not allow (for example `offline_access` or `openid`). Set `oauthScopes` to
the `McpOAuthScope` output, or to `[]`.

**The client reports no `WWW-Authenticate` header.** The portal's HTTP API
passes that header through unchanged (observed 2026-10-25), so a missing
header points at something in between: a REST API in front of the Lambda
remaps it to `X-Amzn-Remapped-WWW-Authenticate`. Either way the client can
use well-known discovery, which the portal serves; check that both URLs above
answer.

**`Failed to start OAuth redirect server: localhost:7899 is already in use`.**
The redirect URI pins that port, so only one Kiro window or `kiro-cli` session
can run the sign-in at a time; close the other, or sign in from one instance
and let the others reuse the stored tokens. Two other holders of the port:

- A remote window that forwards port 7899 from the remote host to the local
  machine. Stop forwarding it in that window's Ports view. To keep it from
  being forwarded again automatically, set
  `"remote.portsAttributes": {"7899": {"onAutoForward": "ignore"}}` in that
  window's settings.
- `kiro-cli` in WSL with mirrored networking. WSL then shares the Windows
  localhost, so the CLI contends for the same port as a Kiro window or a port
  forward on Windows; while another holds it, the CLI fails to start the
  server with `Address already in use`.

**`connection timed out after 60000ms` during sign-in.** Kiro CLI 2.27.0
gives the browser sign-in one minute, although `kiro-cli mcp status` prints a
longer server timeout. Finish the browser sign-in within a minute, then retry
from `/mcp`.

**Advertising the Cognito issuer directly.** The alternative, a Protected
Resource Metadata document naming the Cognito issuer as the authorization
server, is not supported by Kiro's current documentation, which requires
RFC 8414 metadata; the portal therefore serves the facade.

### MCP tools

Each tool's description ends with its classification. List tools take
`limit` (50 by default, at most 200, unless the tool's schema states
other bounds). Tools of the `usecase` scope take
`usecase_id` and are authorized in that Use_Case; `global` tools are
authorized at the account level. Phase 1 is delivered; Phase 2 and Phase 3
rows are listed for reference and are neither listed nor accepted by the
server until their release.

**`admin` view** (15 tools, `…/mcp/admin`)

| Tool | Phase | Classification | Permission | Scope |
|---|---|---|---|---|
| `portal_admin_accounts_list` | 1 | Read-only. | `manage_users` | global |
| `portal_admin_audit_logs_query` | 1 | Read-only. | `system_admin` | global |
| `portal_admin_edge_sync_devices_list` | 1 | Read-only. | `manage_users` | global |
| `portal_admin_usecase_get` | 1 | Read-only. | `view_usecases` | usecase |
| `portal_admin_usecases_list` | 1 | Read-only. | `view_usecases` | global |
| `portal_admin_user_permissions_get` | 1 | Read-only. | `manage_users` | global |
| `portal_admin_users_list` | 1 | Read-only. | `manage_users` | global |
| `portal_admin_whoami` | 1 | Read-only. | - (any signed-in principal) | global |
| `portal_admin_account_role_change` | 3 | MUTATING. | `manage_users` | global |
| `portal_admin_edge_sync_device_sync` | 3 | MUTATING, DESTRUCTIVE — requires confirm=device_id. | `manage_users` | global |
| `portal_admin_role_assign` | 3 | MUTATING. | `manage_users` | usecase |
| `portal_admin_role_remove` | 3 | MUTATING, DESTRUCTIVE — requires confirm=user_id. | `manage_users` | usecase |
| `portal_admin_usecase_create` | 3 | MUTATING. | `create_usecases` | global |
| `portal_admin_usecase_delete` | 3 | MUTATING, DESTRUCTIVE — requires confirm=usecase_id. | `delete_usecases` | usecase |
| `portal_admin_usecase_update` | 3 | MUTATING. | `update_usecases` | usecase |

**`devices` view** (34 tools, `…/mcp/devices`)

| Tool | Phase | Classification | Permission | Scope |
|---|---|---|---|---|
| `portal_devices_cameras_conflicts_get` | 1 | Read-only. | `view_devices` | usecase |
| `portal_devices_cameras_list` | 1 | Read-only. | `view_devices` | usecase |
| `portal_devices_components_get` | 1 | Read-only. | `view_deployments` | usecase |
| `portal_devices_components_list` | 1 | Read-only. | `view_deployments` | usecase |
| `portal_devices_components_schema_get` | 1 | Read-only. | `view_deployments` | usecase |
| `portal_devices_deployment_for_target_get` | 1 | Read-only. | `view_deployments` | usecase |
| `portal_devices_deployment_get` | 1 | Read-only. | `view_deployments` | usecase |
| `portal_devices_deployments_list` | 1 | Read-only. | `view_deployments` | usecase |
| `portal_devices_get` | 1 | Read-only. | `view_devices` | usecase |
| `portal_devices_list` | 1 | Read-only. | `view_devices` | usecase |
| `portal_devices_logs_get` | 1 | Read-only. | `view_device_logs` | usecase |
| `portal_devices_logs_list` | 1 | Read-only. | `view_device_logs` | usecase |
| `portal_devices_registrations_list` | 1 | Read-only. | `manage_devices` | usecase |
| `portal_devices_shared_components_available_list` | 1 | Read-only. | `view_deployments` | global |
| `portal_devices_shared_components_list` | 1 | Read-only. | `view_deployments` | usecase |
| `portal_devices_shared_components_status_get` | 1 | Read-only. | `system_admin` | global |
| `portal_devices_static_image_get` | 1 | Read-only. | `view_devices` | usecase |
| `portal_devices_static_video_get` | 1 | Read-only. | `view_devices` | usecase |
| `portal_devices_thing_groups_list` | 1 | Read-only. | `manage_devices` | usecase |
| `portal_devices_camera_add` | 2 | MUTATING. | `manage_devices` | usecase |
| `portal_devices_camera_update` | 2 | MUTATING. | `manage_devices` | usecase |
| `portal_devices_cameras_refresh` | 2 | MUTATING. | `manage_devices` | usecase |
| `portal_devices_logs_analyze` | 2 | Read-only. | `view_device_logs` | usecase |
| `portal_devices_static_image_unpin` | 2 | MUTATING, DESTRUCTIVE — requires confirm=device_id. | `manage_devices` | usecase |
| `portal_devices_static_video_unpin` | 2 | MUTATING, DESTRUCTIVE — requires confirm=device_id. | `manage_devices` | usecase |
| `portal_devices_camera_delete` | 3 | MUTATING, DESTRUCTIVE — requires confirm=camera_id. | `manage_devices` | usecase |
| `portal_devices_component_delete` | 3 | MUTATING, DESTRUCTIVE — requires confirm=component_arn. | `update_usecases` | usecase |
| `portal_devices_delete` | 3 | MUTATING, DESTRUCTIVE — requires confirm=device_id. | `manage_devices` | usecase |
| `portal_devices_deployment_cancel` | 3 | MUTATING, DESTRUCTIVE — requires confirm=deployment_id. | `manage_deployments` | usecase |
| `portal_devices_deployment_create` | 3 | MUTATING, DESTRUCTIVE — requires confirm=<target_thing_group \| target_devices[0]>. | `create_deployments` | usecase |
| `portal_devices_flags_update` | 3 | MUTATING. | `node-designer:manage` | usecase |
| `portal_devices_registration_delete` | 3 | MUTATING, DESTRUCTIVE — requires confirm=registration_id. | `manage_devices` | usecase |
| `portal_devices_shared_components_provision` | 3 | MUTATING. | `system_admin` | usecase |
| `portal_devices_shared_components_update_all` | 3 | MUTATING. | `system_admin` | global |

**`models` view** (21 tools, `…/mcp/models`)

| Tool | Phase | Classification | Permission | Scope |
|---|---|---|---|---|
| `portal_models_compilation_get` | 1 | Read-only. | `view_training_jobs` | usecase |
| `portal_models_format_spec_get` | 1 | Read-only. | `view_models` | global |
| `portal_models_get` | 1 | Read-only. | `view_models` | usecase |
| `portal_models_inspect` | 1 | Read-only. | `promote_models` | usecase |
| `portal_models_list` | 1 | Read-only. | `view_models` | usecase |
| `portal_models_training_get` | 1 | Read-only. | `view_training_jobs` | usecase |
| `portal_models_training_list` | 1 | Read-only. | `view_training_jobs` | usecase |
| `portal_models_training_logs_get` | 1 | Read-only. | `view_training_jobs` | usecase |
| `portal_models_types_get` | 1 | Read-only. | `view_models` | global |
| `portal_models_validate` | 1 | Read-only. | `promote_models` | usecase |
| `portal_models_vllm_engine_spec_get` | 1 | Read-only. | `view_models` | global |
| `portal_models_compilation_create` | 2 | MUTATING. | `create_training_jobs` | usecase |
| `portal_models_convert` | 2 | MUTATING. | `promote_models` | usecase |
| `portal_models_import` | 2 | MUTATING. | `promote_models` | usecase |
| `portal_models_manifest_transform` | 2 | MUTATING. | `create_training_jobs` | usecase |
| `portal_models_stage_set` | 2 | MUTATING. | `promote_models` | usecase |
| `portal_models_training_create` | 2 | MUTATING. | `create_training_jobs` | usecase |
| `portal_models_vllm_engine_configuration_set` | 2 | MUTATING. | `promote_models` | usecase |
| `portal_models_vllm_import` | 2 | MUTATING. | `promote_models` | usecase |
| `portal_models_package` | 3 | MUTATING. | `promote_models` | usecase |
| `portal_models_publish` | 3 | MUTATING. | `promote_models` | usecase |

**`data` view** (48 tools, `…/mcp/data`)

`portal_data_datasets_list` walks the bucket prefix to its `max_depth`
(3 by default): on large buckets pass `max_depth` 1 and a `limit`, since the
default depth can exceed the tool timeout.

| Tool | Phase | Classification | Permission | Scope |
|---|---|---|---|---|
| `portal_data_buckets_list` | 1 | Read-only. | `view_datasets` | usecase |
| `portal_data_captures_list` | 1 | Read-only. | `view_datasets` | usecase |
| `portal_data_datasets_count` | 1 | Read-only. | `view_datasets` | usecase |
| `portal_data_datasets_list` | 1 | Read-only. | `view_datasets` | usecase |
| `portal_data_datasets_preview_get` | 1 | Read-only. | `view_datasets` | usecase |
| `portal_data_folders_list` | 1 | Read-only. | `view_datasets` | usecase |
| `portal_data_labeler_jobs_list` | 1 | Read-only. | `labeling:tasks-self` | global |
| `portal_data_labeler_pool_get` | 1 | Read-only. | `labeling:tasks-self` | global |
| `portal_data_labeler_task_image_url_get` | 1 | Read-only. | `labeling:tasks-self` | global |
| `portal_data_labeler_task_next` | 1 | Read-only. | `labeling:tasks-self` | global |
| `portal_data_labeling_job_get` | 1 | Read-only. | `view_labeling_jobs` | usecase |
| `portal_data_labeling_jobs_list` | 1 | Read-only. | `view_labeling_jobs` | usecase |
| `portal_data_labeling_preview_get` | 1 | Read-only. | `manage_labeling_jobs` | usecase |
| `portal_data_labeling_review_get` | 1 | Read-only. | `update_usecases` | usecase |
| `portal_data_labeling_teams_list` | 1 | Read-only. | `labeling-teams:manage` | usecase |
| `portal_data_manifest_validate` | 1 | Read-only. | `manage_datasets` | usecase |
| `portal_data_prelabeled_get` | 1 | Read-only. | `view_datasets` | usecase |
| `portal_data_prelabeled_list` | 1 | Read-only. | `view_datasets` | usecase |
| `portal_data_synthetic_models_list` | 1 | Read-only. | `manage_datasets` | usecase |
| `portal_data_synthetic_prompt_templates_get` | 1 | Read-only. | `manage_datasets` | usecase |
| `portal_data_synthetic_session_get` | 1 | Read-only. | `manage_datasets` | usecase |
| `portal_data_synthetic_sessions_list` | 1 | Read-only. | `manage_datasets` | usecase |
| `portal_data_workteams_list` | 1 | Read-only. | `view_labeling_jobs` | usecase |
| `portal_data_folder_create` | 2 | MUTATING. | `manage_datasets` | usecase |
| `portal_data_labeler_task_presentation_failure` | 2 | MUTATING. | `labeling:tasks-self` | global |
| `portal_data_labeler_task_steal` | 2 | MUTATING. | `labeling:tasks-self` | global |
| `portal_data_labeler_task_submit` | 2 | MUTATING. | `labeling:tasks-self` | global |
| `portal_data_labeling_job_create` | 2 | MUTATING. | `create_labeling_jobs` | usecase |
| `portal_data_labeling_prelabels_rerun` | 2 | MUTATING. | `manage_labeling_jobs` | usecase |
| `portal_data_labeling_preview_run` | 2 | MUTATING. | `manage_labeling_jobs` | usecase |
| `portal_data_labeling_review_decide` | 2 | MUTATING. | `update_usecases` | usecase |
| `portal_data_prelabeled_create` | 2 | MUTATING. | `manage_datasets` | usecase |
| `portal_data_synthetic_previews_approve` | 2 | MUTATING. | `manage_datasets` | usecase |
| `portal_data_synthetic_prompt_templates_set` | 2 | MUTATING. | `manage_datasets` | usecase |
| `portal_data_synthetic_session_create` | 2 | MUTATING. | `manage_datasets` | usecase |
| `portal_data_synthetic_session_generate` | 2 | MUTATING. | `manage_datasets` | usecase |
| `portal_data_synthetic_session_integrate` | 2 | MUTATING. | `manage_datasets` | usecase |
| `portal_data_synthetic_session_update` | 2 | MUTATING. | `manage_datasets` | usecase |
| `portal_data_configure` | 3 | MUTATING. | `update_usecases` | usecase |
| `portal_data_labeling_job_delete` | 3 | MUTATING, DESTRUCTIVE — requires confirm=job_id. | `manage_labeling_jobs` | usecase |
| `portal_data_labeling_job_stop` | 3 | MUTATING, DESTRUCTIVE — requires confirm=job_id. | `manage_labeling_jobs` | usecase |
| `portal_data_labeling_review_finalize` | 3 | MUTATING. | `update_usecases` | usecase |
| `portal_data_labeling_team_create` | 3 | MUTATING. | `labeling-teams:manage` | usecase |
| `portal_data_labeling_team_delete` | 3 | MUTATING, DESTRUCTIVE — requires confirm=team_id. | `labeling-teams:manage` | usecase |
| `portal_data_labeling_team_member_add` | 3 | MUTATING. | `labeling-teams:manage` | usecase |
| `portal_data_labeling_team_member_remove` | 3 | MUTATING, DESTRUCTIVE — requires confirm=user_id. | `labeling-teams:manage` | usecase |
| `portal_data_prelabeled_delete` | 3 | MUTATING, DESTRUCTIVE — requires confirm=dataset_id. | `manage_datasets` | usecase |
| `portal_data_synthetic_session_retrain` | 3 | MUTATING. | `create_training_jobs` | usecase |

**`workflows` view** (65 tools, `…/mcp/workflows`)

| Tool | Phase | Classification | Permission | Scope |
|---|---|---|---|---|
| `portal_workflows_build_branches_list` | 1 | Read-only. | `builds:read` | global |
| `portal_workflows_build_config_get` | 1 | Read-only. | `builds:read` | global |
| `portal_workflows_build_get` | 1 | Read-only. | `builds:read` | global |
| `portal_workflows_build_logs_get` | 1 | Read-only. | `builds:read` | global |
| `portal_workflows_build_servers_list` | 1 | Read-only. | `builds:read` | global |
| `portal_workflows_builds_list` | 1 | Read-only. | `builds:read` | global |
| `portal_workflows_custom_node_type_get` | 1 | Read-only. | `node-designer:read` | usecase |
| `portal_workflows_custom_node_types_list` | 1 | Read-only. | `node-designer:read` | usecase |
| `portal_workflows_generation_job_get` | 1 | Read-only. | `workflow:create` | usecase |
| `portal_workflows_get` | 1 | Read-only. | `workflow:read` | usecase |
| `portal_workflows_list` | 1 | Read-only. | `workflow:read` | usecase |
| `portal_workflows_node_catalog_get` | 1 | Read-only. | `workflow:read` | usecase |
| `portal_workflows_plugin_builds_get` | 1 | Read-only. | `node-designer:read` | usecase |
| `portal_workflows_plugin_component_get` | 1 | Read-only. | `node-designer:read` | usecase |
| `portal_workflows_plugin_get` | 1 | Read-only. | `node-designer:read` | usecase |
| `portal_workflows_plugin_gst_properties_get` | 1 | Read-only. | `node-designer:read` | usecase |
| `portal_workflows_plugin_modules_list` | 1 | Read-only. | `node-designer:read` | usecase |
| `portal_workflows_plugin_source_get` | 1 | Read-only. | `node-designer:read` | usecase |
| `portal_workflows_plugin_version_get` | 1 | Read-only. | `node-designer:read` | usecase |
| `portal_workflows_plugins_list` | 1 | Read-only. | `node-designer:read` | usecase |
| `portal_workflows_simulation_get` | 1 | Read-only. | `node-designer:read` | usecase |
| `portal_workflows_test_dataset_get` | 1 | Read-only. | `workflow:read` | usecase |
| `portal_workflows_test_datasets_list` | 1 | Read-only. | `workflow:read` | usecase |
| `portal_workflows_test_run_get` | 1 | Read-only. | `workflow:read` | usecase |
| `portal_workflows_test_runs_list` | 1 | Read-only. | `workflow:read` | usecase |
| `portal_workflows_versions_list` | 1 | Read-only. | `workflow:read` | usecase |
| `portal_workflows_build_retry` | 2 | MUTATING. | `builds:submit` | global |
| `portal_workflows_build_submit` | 2 | MUTATING. | `builds:submit` | global |
| `portal_workflows_create` | 2 | MUTATING. | `workflow:create` | usecase |
| `portal_workflows_custom_node_type_register` | 2 | MUTATING. | `node-designer:register` | usecase |
| `portal_workflows_custom_node_type_update` | 2 | MUTATING. | `node-designer:manage` | usecase |
| `portal_workflows_duplicate` | 2 | MUTATING. | `workflow:create` | usecase |
| `portal_workflows_generate` | 2 | MUTATING. | `workflow:create` | usecase |
| `portal_workflows_plugin_adjust_revision` | 2 | MUTATING. | `node-designer:manage` | usecase |
| `portal_workflows_plugin_architectures_set` | 2 | MUTATING. | `node-designer:manage` | usecase |
| `portal_workflows_plugin_build` | 2 | MUTATING. | `node-designer:manage` | usecase |
| `portal_workflows_plugin_create` | 2 | MUTATING. | `node-designer:create` | usecase |
| `portal_workflows_plugin_generate_message` | 2 | MUTATING. | `node-designer:generate` | usecase |
| `portal_workflows_plugin_generate_start` | 2 | MUTATING. | `node-designer:generate` | usecase |
| `portal_workflows_plugin_generate_status_get` | 2 | Read-only. | `node-designer:generate` | usecase |
| `portal_workflows_plugin_import` | 2 | MUTATING. | `node-designer:import` | usecase |
| `portal_workflows_plugin_new_version` | 2 | MUTATING. | `node-designer:manage` | usecase |
| `portal_workflows_plugin_select_plugins` | 2 | MUTATING. | `node-designer:manage` | usecase |
| `portal_workflows_plugin_simulate` | 2 | MUTATING. | `node-designer:simulate` | usecase |
| `portal_workflows_plugin_source_set` | 2 | MUTATING, DESTRUCTIVE — requires confirm=plugin_id. | `node-designer:manage` | usecase |
| `portal_workflows_plugin_update` | 2 | MUTATING. | `node-designer:manage` | usecase |
| `portal_workflows_rename` | 2 | MUTATING. | `workflow:save` | usecase |
| `portal_workflows_test_run_start` | 2 | MUTATING. | `workflow:test` | usecase |
| `portal_workflows_update` | 2 | MUTATING. | `workflow:save` | usecase |
| `portal_workflows_validate` | 2 | MUTATING. | `workflow:edit` | usecase |
| `portal_workflows_build_cancel` | 3 | MUTATING, DESTRUCTIVE — requires confirm=build_id. | `builds:cancel` | global |
| `portal_workflows_build_config_set` | 3 | MUTATING. | `system_admin` | global |
| `portal_workflows_build_server_create` | 3 | MUTATING. | `system_admin` | global |
| `portal_workflows_build_server_delete` | 3 | MUTATING, DESTRUCTIVE — requires confirm=server_name. | `system_admin` | global |
| `portal_workflows_build_server_start` | 3 | MUTATING. | `system_admin` | global |
| `portal_workflows_build_server_stop` | 3 | MUTATING, DESTRUCTIVE — requires confirm=server_id. | `system_admin` | global |
| `portal_workflows_custom_node_type_delete` | 3 | MUTATING, DESTRUCTIVE — requires confirm=node_type_id. | `node-designer:manage` | usecase |
| `portal_workflows_custom_node_type_deprecate` | 3 | MUTATING, DESTRUCTIVE — requires confirm=node_type_id. | `node-designer:manage` | usecase |
| `portal_workflows_delete` | 3 | MUTATING, DESTRUCTIVE — requires confirm=workflow_id. | `workflow:delete` | usecase |
| `portal_workflows_package` | 3 | MUTATING. | `workflow:package` | usecase |
| `portal_workflows_plugin_delete` | 3 | MUTATING, DESTRUCTIVE — requires confirm=plugin_id. | `node-designer:manage` | usecase |
| `portal_workflows_plugin_demote` | 3 | MUTATING, DESTRUCTIVE — requires confirm=plugin_id. | `node-designer:promote-demote` | usecase |
| `portal_workflows_plugin_promote` | 3 | MUTATING, DESTRUCTIVE — requires confirm=plugin_id. | `node-designer:promote-demote` | usecase |
| `portal_workflows_plugin_review` | 3 | MUTATING, DESTRUCTIVE — requires confirm=plugin_id. | `node-designer:security-review` | usecase |
| `portal_workflows_test_dataset_delete` | 3 | MUTATING, DESTRUCTIVE — requires confirm=dataset_id. | `workflow:test` | usecase |

---

## Support

- **Logs**: CloudWatch Logs under `/aws/lambda/EdgeCVPortal*`
- **API Errors**: Check browser DevTools Network tab
- **Infrastructure**: `cdk diff` to see pending changes
