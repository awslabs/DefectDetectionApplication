# Teardown Audit — Open-Source Generation Models Exploration

**Spec:** `.kiro/specs/opensource-generation-models/`
**Account / Region:** Portal_Account 164152369890, us-east-1
**Resource tag:** `exploration=opensource-generation-models`
**Protocol:** `../benchmark-protocol.md` §7 (7-step teardown checklist)
**Executed:** 2026-08-17, tasks 5.1 (teardown) and 5.3 (portal-untouched verification)
**Caller identity for every command below:**
`arn:aws:sts::164152369890:assumed-role/admin-iam/i-06ff3cbe428cbcd95`

Properties verified here: **Property 6 — Teardown completeness** (Req 2.8, 9.3)
and **Property 7 — Portal non-modification** (Req 9.1, 9.2, 9.4).

Exploration infrastructure window (for attribution): first tagged resource
created 2026-08-17T13:23:50Z (IAM role), first instance launch 13:27:02Z, last
instance terminate 21:14:08Z.

---

## Inventory before teardown (tag-filtered catch-all)

```
$ aws resourcegroupstaggingapi get-resources --region us-east-1 \
    --tag-filters Key=exploration,Values=opensource-generation-models \
    --query 'ResourceTagMappingList[].ResourceARN' --output json
[
    "arn:aws:ssm:us-east-1:164152369890:parameter/opensource-genmodels/hf-token",
    "arn:aws:ec2:us-east-1:164152369890:volume/vol-0f522525f0eef6b5d",
    "arn:aws:iam::164152369890:instance-profile/opensource-genmodels-benchmark-role",
    "arn:aws:s3:::opensource-genmodels-benchmark-164152369890",
    "arn:aws:ec2:us-east-1:164152369890:instance/i-0a5ecae8136b7dca2",
    "arn:aws:ec2:us-east-1:164152369890:security-group/sg-08e3670672a8ec892",
    "arn:aws:ec2:us-east-1:164152369890:instance/i-0e2e34b3c412c5da3"
]
```

Seven tagged resources at teardown start. Nothing outside this list was
touched; no CloudFormation stack, portal bucket, portal role, or portal
resource of any kind was modified or deleted (see step 7).

---

## Step 1 — Terminate tagged EC2 instances

Already terminated in Phase C (pixart-r1 `i-01b4e3e5e2fe0eb99`, pixart-r2
`i-01361c1f436a6cab7`, flux1-r1, large-r1 `i-0a5ecae8136b7dca2`); re-verified
here.

```
$ aws ec2 describe-instances --region us-east-1 \
    --filters "Name=tag:exploration,Values=opensource-generation-models" \
    --query 'Reservations[].Instances[].{Id:InstanceId,State:State.Name,Type:InstanceType,Launch:LaunchTime}' \
    --output table
-----------------------------------------------------------------------------------
|                                DescribeInstances                                |
+----------------------+-----------------------------+-------------+--------------+
|          Id          |           Launch            |    State    |    Type      |
+----------------------+-----------------------------+-------------+--------------+
|  i-0a5ecae8136b7dca2 |  2026-08-17T20:31:24+00:00  |  terminated |  g6e.8xlarge |
+----------------------+-----------------------------+-------------+--------------+
```

Verification query (protocol §7.1 wording — "no non-terminated instances"):

```
$ aws ec2 describe-instances --region us-east-1 \
    --filters "Name=tag:exploration,Values=opensource-generation-models" \
              "Name=instance-state-name,Values=pending,running,shutting-down,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output json
[]
```

The earlier instances have already aged out of the EC2 terminated-instance
record entirely:

```
$ aws ec2 describe-instances --region us-east-1 --instance-ids i-0e2e34b3c412c5da3 --output json
{
    "Reservations": []
}
```

**Result: EMPTY (no billable instance).**

## Step 2 — Delete tagged EBS volumes and snapshots

The tagging index listed `vol-0f522525f0eef6b5d`, but the volume no longer
exists — it was root-volume-on-terminate deleted with its instance:

```
$ aws ec2 describe-volumes --region us-east-1 --volume-ids vol-0f522525f0eef6b5d --output json
An error occurred (InvalidVolume.NotFound) when calling the DescribeVolumes operation:
The volume 'vol-0f522525f0eef6b5d' does not exist.
```

```
$ aws ec2 describe-volumes --region us-east-1 \
    --filters "Name=tag:exploration,Values=opensource-generation-models" \
    --query 'Volumes[].VolumeId' --output json
[]

$ aws ec2 describe-snapshots --region us-east-1 --owner-ids self \
    --filters "Name=tag:exploration,Values=opensource-generation-models" \
    --query 'Snapshots[].SnapshotId' --output json
[]
```

**Result: EMPTY. Nothing to delete** (no snapshot was ever created; volumes went
with their instances).

## Step 3 — Delete tagged SageMaker endpoints / endpoint-configs / models

Optional task 4.4 was **never executed** — skipped by explicit **user decision
on 2026-08-17** to stop provisioning and move to the analysis phases (see the
"Optional task 4.4" section of `README.md`) — so no SageMaker resource was
created. Verified anyway:

> **⚠️ Correction (task 7.1, 2026-08-17T23:32:17Z):** an earlier version of this
> paragraph attributed the skip to "SageMaker endpoint quotas for g6e instance
> types are 0 on this account". That was **factually wrong** — live values are
> `ml.g6e.xlarge` = 4, `ml.g6e.2xlarge` = 4, `ml.g6e.4xlarge`/`8xlarge`/
> `12xlarge`/`16xlarge` = 1 (see `../quota-audit.md` §1–2), and a quota of 1 is
> enough for one short-lived measurement endpoint. Quota was not the blocker;
> the user decision was. Nothing in this audit's teardown result changes: no
> tagged SageMaker resource was created or survives.

```
$ aws sagemaker list-endpoints --region us-east-1 --query 'Endpoints[].EndpointName' --output json
[]

$ aws resourcegroupstaggingapi get-resources --region us-east-1 \
    --resource-type-filters sagemaker \
    --tag-filters Key=exploration,Values=opensource-generation-models \
    --query 'ResourceTagMappingList[].ResourceARN' --output json
[]
```

`list-endpoint-configs` / `list-models` do return entries on this account
(6 endpoint-configs, 11 models) — all pre-existing account assets created
between 2019-09-26 and 2025-02-03 (`blazingtext-*`, `sagemaker-xgboost-*`,
`lama-model-13484-*`, `image-classification-recycle`,
`defect-detection-classification-2025-01-17-*`, `pytorch-training-2021-*`).
**None carries the exploration tag and none was created or touched by this
exploration — left in place, out of scope.**

**Result: EMPTY for tagged SageMaker resources.**

## Step 4 — Copy representative outputs, then delete the benchmark S3 bucket

### 4a. Evidence check before deletion

Bucket contents immediately before deletion: **100 objects, 63,829,747 bytes**
across `harness/` (frozen cases + harness scripts) and
`runs/{pixart-alpha-r1, pixart-sigma-r1, flux1schnell-r1, flux1dev-r1, flux2-r1, hunyuan21-r1}/`.

Every representative output already committed under
`benchmark-results/<model>/run-001/outputs/` was byte-compared against its
bucket object (local `md5sum` vs S3 `ETag`, all single-part uploads) — **27/27
MATCH, 0 DIFF**.

Files pulled from the bucket before deletion because they were missing from the
committed evidence:

| pulled object | destination |
|---|---|
| `runs/pixart-alpha-r1/outputs/t2i-002.png` | `pixart-alpha/run-001/outputs/t2i-002.png` |
| `runs/pixart-sigma-r1/outputs/t2i-002.png` | `pixart-sigma/run-001/outputs/t2i-002.png` |
| `runs/hunyuan21-r1/outputs/t2i-002.png` | `hunyuanimage/run-001/outputs/t2i-002.png` |
| `runs/hunyuan21-r1/outputs/t2i-003.png` | `hunyuanimage/run-001/outputs/t2i-003.png` |
| `runs/hunyuan21-r1/run.log`, `meta.json` | `hunyuanimage/run-001/run-log.txt`, `meta.json` |
| `runs/flux2-r1/run.log`, `meta.json` | `flux.2/run-001/run-log.txt`, `meta.json` |
| `runs/flux1dev-r1/driver.log` | `flux.1-dev/run-001/driver-log.txt` |
| `runs/flux1schnell-r1/driver.log` | `flux.1-schnell/run-001/driver-log.txt` |

Run logs are committed with a `-log.txt` suffix because the repo's root
`.gitignore` excludes `*.log`; contents are unchanged from the bucket objects.
All four were scanned for credential patterns (Hugging Face tokens, AWS access
keys, private-key headers) before commit — clean.

Committed representative coverage after the pull (protocol §8: representative,
not exhaustive — the full sets lived in the now-deleted bucket and are
referenced by `output_uri` in each `metrics.json`):

| run dir | committed outputs | coverage |
|---|---|---|
| `pixart-alpha/run-001/` | t2i-001…004 | 4/4 attempted cases (all t2i; 9 inpaint cases were `failed/unsupported_task`, no output exists) |
| `pixart-sigma/run-001/` | t2i-001…004 | 4/4 attempted |
| `hunyuanimage/run-001/` | t2i-001…004 | 4/4 attempted |
| `flux.1-schnell/run-001/` | inpaint-001, inpaint-005, inpaint-102, t2i-001, t2i-004 | 5/13 representative (small/large synthetic mask + real cookie photo + 2 t2i) |
| `flux.1-dev/run-001/` | same 5 case ids | 5/13 representative |
| `flux.2/run-001/` | same 5 case ids | 5/13 representative |

The three FLUX runs share the same 5 representative case ids so cross-model
comparison of the committed images is like-for-like.

### 4b. Deletion

```
$ aws s3api get-bucket-tagging --bucket opensource-genmodels-benchmark-164152369890 --output json
{
    "TagSet": [
        {
            "Key": "exploration",
            "Value": "opensource-generation-models"
        }
    ]
}

$ aws s3 rm s3://opensource-genmodels-benchmark-164152369890/ --recursive --quiet
$ aws s3 rb s3://opensource-genmodels-benchmark-164152369890
remove_bucket: opensource-genmodels-benchmark-164152369890
```

Verification:

```
$ aws s3api head-bucket --bucket opensource-genmodels-benchmark-164152369890
An error occurred (404) when calling the HeadBucket operation: Not Found

$ aws s3 ls | grep -c opensource-genmodels-benchmark
0
```

**Result: bucket gone.** No other bucket in the account was listed for
deletion, and no portal bucket was touched.

## Step 5 — Delete temporary security groups / key pairs / IAM roles + instance profiles

**Key pairs:** none were ever created (all instances were SSM-driven, no SSH).

```
$ aws ec2 describe-key-pairs --region us-east-1 \
    --filters "Name=tag:exploration,Values=opensource-generation-models" --output json
{
    "KeyPairs": []
}
```

**Security group** (`opensource-genmodels-benchmark-sg`, description
"benchmark SSM-only, no ingress (exploration=opensource-generation-models)") —
confirmed no attached network interfaces first:

```
$ aws ec2 describe-network-interfaces --region us-east-1 \
    --filters "Name=group-id,Values=sg-08e3670672a8ec892" \
    --query 'NetworkInterfaces[].NetworkInterfaceId' --output json
[]

$ aws ec2 delete-security-group --region us-east-1 --group-id sg-08e3670672a8ec892
{
    "Return": true,
    "GroupId": "sg-08e3670672a8ec892"
}
```

**IAM instance profile + role** (`opensource-genmodels-benchmark-role`, created
2026-08-17T13:23:50Z, tagged `exploration=opensource-generation-models`).
Inline policies removed with it: `benchmark-bucket-access`,
`benchmark-hf-token-read`, and `pr1c-patchpolicy-s3` (the last one was attached
*to this exploration role* by the account's SSM patch-policy automation, not by
the exploration). Managed policies detached: `AmazonSSMManagedInstanceCore`,
`AmazonSSMPatchAssociation`.

```
$ aws iam remove-role-from-instance-profile --instance-profile-name opensource-genmodels-benchmark-role \
      --role-name opensource-genmodels-benchmark-role
$ aws iam delete-instance-profile --instance-profile-name opensource-genmodels-benchmark-role
$ aws iam delete-role-policy --role-name opensource-genmodels-benchmark-role --policy-name benchmark-bucket-access
$ aws iam delete-role-policy --role-name opensource-genmodels-benchmark-role --policy-name benchmark-hf-token-read
$ aws iam delete-role-policy --role-name opensource-genmodels-benchmark-role --policy-name pr1c-patchpolicy-s3
$ aws iam detach-role-policy --role-name opensource-genmodels-benchmark-role \
      --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
$ aws iam detach-role-policy --role-name opensource-genmodels-benchmark-role \
      --policy-arn arn:aws:iam::aws:policy/AmazonSSMPatchAssociation
$ aws iam delete-role --role-name opensource-genmodels-benchmark-role
```

**Extra exploration-created resource, not in the protocol's 5 categories — the
temporary Hugging Face token parameter.** Tagged, SecureString, described
"temporary HF token for opensource-generation-models benchmark (delete at
teardown)". Deleted under step 5 as a temporary credential created for the
benchmark:

```
$ aws ssm delete-parameter --region us-east-1 --name /opensource-genmodels/hf-token
$ aws ssm describe-parameters --region us-east-1 \
    --parameter-filters "Key=Name,Values=/opensource-genmodels/hf-token" \
    --query 'Parameters[].Name' --output json
[]
```

## Step 6 — Tag-filtered verification sweep (all queries must return empty)

Sweep re-run until stable. Literal transcript, final sweep:

```
### sweep_timestamp_utc: 2026-08-17T21:37:06Z

$ aws ec2 describe-instances --region us-east-1 --filters Name=tag:exploration,Values=opensource-generation-models 'Name=instance-state-name,Values=pending,running,shutting-down,stopping,stopped' --query 'Reservations[].Instances[].InstanceId' --output json
[]

$ aws ec2 describe-volumes --region us-east-1 --filters Name=tag:exploration,Values=opensource-generation-models --query 'Volumes[].VolumeId' --output json
[]

$ aws ec2 describe-snapshots --region us-east-1 --owner-ids self --filters Name=tag:exploration,Values=opensource-generation-models --query 'Snapshots[].SnapshotId' --output json
[]

$ aws sagemaker list-endpoints --region us-east-1 --query 'Endpoints[].EndpointName' --output json
[]

$ aws resourcegroupstaggingapi get-resources --region us-east-1 --resource-type-filters sagemaker --tag-filters Key=exploration,Values=opensource-generation-models --query 'ResourceTagMappingList[].ResourceARN' --output json
[]

$ aws s3api head-bucket --bucket opensource-genmodels-benchmark-164152369890

aws: [ERROR]: An error occurred (404) when calling the HeadBucket operation: Not Found

$ aws s3 ls | grep -c opensource-genmodels-benchmark
0

$ aws ec2 describe-security-groups --region us-east-1 --filters Name=tag:exploration,Values=opensource-generation-models --query 'SecurityGroups[].GroupId' --output json
[]

$ aws ec2 describe-key-pairs --region us-east-1 --filters Name=tag:exploration,Values=opensource-generation-models --query 'KeyPairs[].KeyName' --output json
[]

$ aws iam list-roles --query 'Roles[?contains(RoleName, `genmodels`)].RoleName' --output json
[]

$ aws iam list-instance-profiles --query 'InstanceProfiles[?contains(InstanceProfileName, `genmodels`)].InstanceProfileName' --output json
[]

$ aws ssm describe-parameters --region us-east-1 --parameter-filters Key=Name,Values=/opensource-genmodels/hf-token --query 'Parameters[].Name' --output json
[]
```

**Every per-service tag-filtered query returns empty. Property 6 holds.**

### Residual tagging-index entries (stale, not live resources)

The Resource Groups Tagging API catch-all still echoes three EC2 records that
no longer exist as resources:

```
$ aws resourcegroupstaggingapi get-resources --region us-east-1 --tag-filters Key=exploration,Values=opensource-generation-models --query 'ResourceTagMappingList[].ResourceARN' --output json
[
    "arn:aws:ec2:us-east-1:164152369890:volume/vol-0f522525f0eef6b5d",
    "arn:aws:ec2:us-east-1:164152369890:instance/i-0a5ecae8136b7dca2",
    "arn:aws:ec2:us-east-1:164152369890:instance/i-0e2e34b3c412c5da3"
]
```

Per-service proof that all three are gone / non-billable:

| ARN | authoritative state | evidence |
|---|---|---|
| `volume/vol-0f522525f0eef6b5d` | does not exist | `describe-volumes --volume-ids` → `InvalidVolume.NotFound` |
| `instance/i-0a5ecae8136b7dca2` | `terminated` | `describe-instances` table above |
| `instance/i-0e2e34b3c412c5da3` | no longer in EC2 records | `describe-instances --instance-ids` → `{"Reservations": []}` |

This is the documented EC2/tagging-index retention lag: terminated instances
remain visible for roughly an hour and their tag index entries linger a while
after the underlying resource is gone. **Nothing is left running and nothing is
accruing charges.** No further action is possible on these ARNs — there is no
resource left to delete.

## Step 7 — Portal-untouched verification (task 5.3, Property 7)

### 7a. CloudFormation stack snapshot diff

Post-exploration snapshot: `post-exploration-stacks.json`, captured
2026-08-17T21:31:09Z, same shape as `pre-exploration-stacks.json`
(`StackName` + `LastUpdatedTime`, falling back to `CreationTime`).

```
pre count 225   post count 225
added: []
removed: []
```

**The exploration created and deleted zero CloudFormation stacks.** It never
invoked CDK or CloudFormation at all: its only API surface was EC2, S3, IAM,
SSM Parameter Store, and SSM Run Command.

16 stacks show a newer `LastUpdatedTime`. Every one is attributable to
concurrent, unrelated portal work or to account-level AWS automation:

| stack | pre → post `LastUpdatedTime` | attribution |
|---|---|---|
| `EdgeCVPortalSyntheticDataStack` | 04:18:19Z → **20:43:35Z** | ⚠️ **expected diff, explained** — user-approved portal bugfix spec `synthetic-imaging-layer-empty` (empty Pillow Lambda layer). Commit `ba906c2` "fix(infra): bundle Pillow into the SyntheticImagingLayer at synth time", branch `spec/synthetic-imaging-layer-empty`, worktree `/home/ubuntu/github/jp5/main/dda-imaging-fix`. Stack events confirm the real change: `SyntheticImagingLayer6E5D81DF` `AWS::Lambda::LayerVersion` replaced (new version created 20:43:44Z, old version deleted 20:44:06Z), `SyntheticDataHandler791E53E7` updated to the new layer arn |
| `EdgeCVPortalAuthStack` | 2026-08-17T00:56:41Z → 20:37:04Z | same `ba906c2` deploy — the portal deploy script runs `cdk deploy --all`, so unaffected stacks get incidental `CDKMetadata` / asset-hash updates. Events for this stack show `CDKMetadata` as the only changed resource |
| `EdgeCVPortalStorageStack` | 00:57:02Z → 20:37:25Z | same deploy (`CDKMetadata` + `PortalArtifactsBucket31E9D392` no-op update) |
| `EdgeCVPortalTestRunnerStack` | 04:11:42Z → 20:38:17Z | same deploy (`CDKMetadata` + `TestRunStepsHandler` asset re-publish) |
| `EdgeCVPortalComputeStack` | 04:23:16Z → 20:46:19Z | same deploy (Lambda asset re-publishes: `ModelConverterHandler`, `CompilationEventsHandler`, `UserAdminHandler`, `QuickSetupHandler`, nested API stacks) |
| `EdgeCVPortalComputeStack-ApiGatewayNestedStack…-I1VTU544AK48` | 04:15:29Z → 20:40:31Z | same deploy (nested child of ComputeStack) |
| `EdgeCVPortalComputeStack-CameraRegistryApiNestedStack…-10ZGSZI4EQR5K` | 04:15:52Z → 20:41:06Z | same deploy (nested child) |
| `EdgeCVPortalComputeStack-UserAdminApiNestedStack…-Q9WHJWF3IOJ6` | 04:16:03Z → 20:41:18Z | same deploy (nested child) |
| `EdgeCVPortalComputeStack-QuickSetupApiNestedStack…-MLB34VDHTI8G` | 04:16:15Z → 20:41:30Z | same deploy (nested child) |
| `EdgeCVPortalComputeStack-DdaLabelingApiNestedStack…-1G35354IU2CQ2` | 04:16:27Z → 20:41:42Z | same deploy (nested child) |
| `EdgeCVPortalComputeStack-WorkflowManagerGapsApiNestedStack…-LH5KZ24LMLMS` | 04:16:38Z → 20:41:54Z | same deploy (nested child) |
| `EdgeCVPortalNodeDesignerStack` | 04:19:17Z → 20:44:27Z | same deploy (nested-stack passthrough only) |
| `EdgeCVPortalBuildFleetStack` | 04:20:35Z → 15:15:17Z | separate concurrent portal Lambda deploy by other in-flight portal work (events: `BuildDispatcherHandler`, `BuildConfigHandler`, `BuildEventsHandler`, `BuildFleetHandler`, `BuildJobsHandler` asset updates). Not the exploration — the exploration modified no portal source and ran no deploy script |
| `EdgeCVPortalFrontendStack` | 2026-08-17T01:05:18Z → 07:37:49Z | concurrent frontend deploy at 07:37Z, **before** the exploration created its first resource (13:23:50Z) |
| `EdgeCVPortalNodeDesignerStack-NodeDesignerApiNestedStack…-1VQIOTM3QZU0B` | 04:19:45Z → 07:40:22Z | same 07:3xZ pre-exploration deploy window |
| `pr1c-installer` | 2026-08-14T17:43:19Z → 2026-08-17T17:43:19Z | **not a portal stack** — AWS Systems Manager Quick Setup patch-policy installer (created 2024-07-10, untagged, account-level). Self-refresh on a fixed daily schedule (identical 17:43:19 time-of-day 3 days apart); events show it updating its own `Pr1cRefresherFunction` + its own IAM roles. This automation is also what attached the `pr1c-patchpolicy-s3` inline policy to the exploration's own instance role (removed in step 5). It changed no portal resource |

**Assertion:** the exploration itself introduced **no** CloudFormation stack
changes — no stack added, none removed, and none of the 16 `LastUpdatedTime`
deltas is produced by an exploration action. The one substantive resource
change in the window (`EdgeCVPortalSyntheticDataStack` → new
`SyntheticImagingLayer` version) belongs to the unrelated, user-approved
`synthetic-imaging-layer-empty` bugfix spec at commit `ba906c2`. Req 9.1 / 9.2
hold.

### 7b. `git status` over `edge-cv-portal/` and `src/` (this working tree)

Working tree: `/home/ubuntu/github/jp5/main/DefectDetectionApplication`,
branch `spec/opensource-generation-models-exploration`.

```
$ git status --porcelain --untracked-files=no edge-cv-portal/ src/
(no output)

$ git diff --stat HEAD -- edge-cv-portal/ src/
(no output)
```

**No tracked file under `edge-cv-portal/` or `src/` is modified, added, or
deleted.** The only tracked change on this branch is under
`.kiro/specs/opensource-generation-models/`.

`git status` without `--untracked-files=no` lists 57 pre-existing untracked
paths under `edge-cv-portal/` (deploy `*.out` logs, `.defect8-build-*.json`,
`cdk.out.bak-*/` directories). None was created by the exploration — the
newest is `deploy-frontend-picker-pagination-20260817T042106Z.out` at
2026-08-17T04:23Z, more than nine hours before the exploration's first API
call (13:23:50Z), and all are byproducts of earlier portal deploy sessions.

**Property 7 holds.**

---

## Teardown outcome

| protocol §7 step | outcome |
|---|---|
| 1. Terminate tagged EC2 instances | ✅ verified empty (1 terminated record aging out) |
| 2. Delete tagged EBS volumes / snapshots | ✅ verified empty (root volumes deleted on terminate; no snapshots) |
| 3. Delete tagged SageMaker endpoints / configs / models | ✅ verified empty (task 4.4 skipped, none created) |
| 4. Copy representative outputs, delete benchmark bucket | ✅ 27/27 committed outputs byte-verified, 8 missing artifacts pulled, bucket deleted (100 objects / 63.8 MB) |
| 5. Delete temporary SGs / key pairs / IAM roles + instance profiles | ✅ 1 SG, 0 key pairs, 1 role + 1 instance profile, plus the temporary HF-token SSM parameter |
| 6. Tag-filtered verification queries all empty | ✅ all per-service queries empty; 3 stale tagging-index ARNs documented with per-service non-existence proof |
| 7. Portal-untouched verification | ✅ 0 stacks added/removed; 16 `LastUpdatedTime` deltas all attributed to unrelated work/automation; `git status` clean over `edge-cv-portal/` and `src/` |

**Nothing left behind that is billable or ambiguous.** Two categories were
deliberately left in place:

1. The 6 pre-existing SageMaker endpoint-configs and 11 models (2019–2025) —
   untagged, not created by this exploration, portal/account assets. Out of
   scope per the "do not delete anything not tagged for this exploration" rule.
2. The stale tagging-index ARNs above — no resource exists behind them.

Total exploration spend per the ledger: **USD 11.11 of the 500 Cost_Cap**
(reconciliation status in `README.md`).
