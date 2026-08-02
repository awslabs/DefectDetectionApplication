# Station Quick Setup

One-line, token-authenticated provisioning of a Greengrass edge station from the
Edge CV Portal — no repository checkout and no operator AWS login on the station.

## Flow

1. In the portal (**Devices → Add Device**) an operator registers a device. The
   backend mints a single-use **Setup_Token** (≤90-minute lifetime, only a hash
   stored at rest) and returns a one-line **Setup_Command**:

   ```
   curl -fsSL <api>/quick-setup/bootstrap -o /tmp/dda-qs.sh \
     && echo "<sha256>  /tmp/dda-qs.sh" | sha256sum -c - \
     && sudo bash /tmp/dda-qs.sh --endpoint <api>/quick-setup --token <token>
   ```

2. The operator runs it on the station. `bootstrap.sh`:
   - checks prerequisites (curl/wget, sha256sum, root, supported Ubuntu/arch, ≥2GB free),
   - `POST /quick-setup/bundle` → downloads `setup-bundle.tar.gz`, verifies its
     SHA-256 against the manifest, then extracts and `exec`s `run.sh`.

3. `run.sh` exchanges the token for **scoped, short-lived Provisioning_Credentials**
   (`POST /quick-setup/credentials`), runs the existing `setup_station.sh`
   provisioning logic with those credentials injected as environment variables,
   joins the registered Device_Group, and reports success/failure back to the
   portal (`POST /quick-setup/status`) using a per-registration report secret.

The scripts here are packaged into the Setup_Bundle at deploy time by
`edge-cv-portal/infrastructure/scripts/build-quick-setup-bundle.sh` and served
from the portal artifacts bucket — the station never needs repository access.

## Credential model & the station-provisioning role

`POST /quick-setup/credentials` mints least-privilege credentials by calling
`sts:AssumeRole` **with a per-device session policy**
(`backend/functions/session_policy.py::build_session_policy`). The effective
permissions are the *intersection* of the assumed role and that session policy,
so every issuance is scoped to the single registered thing name and Device_Group.

Which role is assumed depends on the Use_Case:

- **Cross-account Use_Cases** — the Use_Case account's `DDAPortalAccessRole`
  (its `cross_account_role_arn`), the standard portal cross-account pattern.
- **Same-account Use_Cases** — these are onboarded with the *account root* ARN
  (`arn:aws:iam::<account>:root`) as `cross_account_role_arn`, which is **not
  assumable**. For these the `quick_setup` Lambda instead assumes a dedicated
  **`DDAStationProvisioningRole`** in the portal account.

### `DDAStationProvisioningRole`

- **Created by CDK** in `edge-cv-portal/infrastructure/lib/compute-stack.ts`
  (construct id `StationProvisioningRole`) as part of `EdgeCVPortalComputeStack`
  — it is infrastructure-as-code and provisioned on every `cdk deploy`; there is
  no manual step.
- **Trusted only by the `quick_setup` Lambda role** (`QuickSetupHandlerRole`).
  No other principal can assume it.
- Holds the provisioning **action ceiling** (the same action set as
  `build_session_policy`: IoT thing/group/cert/policy/endpoint/role-alias,
  Greengrass TES role setup, `greengrass:TagResource`, `sts:GetCallerIdentity`).
  Because the Lambda always assumes it *with* the per-device session policy, the
  station only ever receives the narrowed, device-scoped subset.
- The Lambda receives its ARN via the `QUICK_SETUP_PROVISIONING_ROLE_ARN`
  environment variable and selects it automatically when a Use_Case's
  `cross_account_role_arn` ends in `:root`.

No action is required to enable this for same-account portals beyond deploying
`EdgeCVPortalComputeStack`.

## Detected Target_Architecture recording

On a successful run, `run.sh` determines the Station's DDA **Target_Architecture**
via `detect_arch.sh` (`detect_target_architecture`) — one of
`x86_64`, `x86_64_nvidia`, `arm64_jp4`, `arm64_jp5`, or `arm64_jp6`, or nothing
when it cannot be resolved. Detection is read-only: on Jetson hosts the JetPack
major is read from the L4T release (`/etc/nv_tegra_release`, falling back to the
`nvidia-l4t-core` package version), which distinguishes JetPack 4/5/6 where the
kernel CPU arch (`aarch64`) does not; on x86_64 the value is `x86_64_nvidia` when
an NVIDIA GPU runtime is detectable, else `x86_64`. If the architecture cannot be
determined it is simply omitted — provisioning still completes normally.

When a value is determined it is included in the `POST /quick-setup/status`
completion report (`target_architecture`). After the report's authenticated
transition to `completed` succeeds, the `quick_setup` Lambda records that value
(when it is in the fixed set) onto the portal **Devices** table
(`dda-portal-devices`, keyed by `device_id` = IoT Thing name) as
`target_architecture`, so the device is **deployment-gate-ready** without an
admin having to set it manually. This closes the onboarding gap where a
quick-setup device's first architecture-gated deployment (for example a vLLM
model) would otherwise be rejected for a missing architecture.

### Devices-table write permission

Recording the architecture is the one **net-new IAM permission** in this feature:
the `quick_setup` Lambda role (`QuickSetupHandlerRole`) is granted
`grantWriteData` on the portal Devices table in
`edge-cv-portal/infrastructure/lib/compute-stack.ts`. It is created via CDK
(infrastructure-as-code, applied on every `cdk deploy`) and scoped to the single
portal table the deployment architecture gate reads. The write is **best-effort**:
a Devices-table failure is logged and swallowed so a successful completion still
returns 200, and the recording never touches the registration transition or the
manual admin `PUT /api/v1/devices/{id}` architecture writer (an admin can still
view and override the value after onboarding).

## Files

| File            | Purpose                                                              |
|-----------------|---------------------------------------------------------------------|
| `bootstrap.sh`  | Prerequisite checks, bundle download + checksum gate, hands off to `run.sh`. |
| `run.sh`        | Credential exchange, `setup_station.sh` orchestration, status reporting. |
| `detect_arch.sh`| Read-only DDA `Target_Architecture` detection (sourced by `run.sh`). |
| `tests/`        | `bats` shell tests for the scripts.                                 |
