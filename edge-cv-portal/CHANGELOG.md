# Changelog

All notable changes to the DDA Portal will be documented in this file.

## [Unreleased]

### Changed - Use case bucket configuration (2026-10-14)

Portal handlers now read and write S3 only in the use case's **configured buckets**: its S3 bucket, its Data Account bucket, and the **Additional S3 buckets** a UseCaseAdmin allows (**Use Cases** → **Edit** → **Allowed buckets**, or the **Allow for this use case** action on a bucket in Data Management). Every caller-supplied `s3://` URI or bucket name — model validation, import, inspection and conversion, vLLM registration and its fit check, training manifests and manifest transforms, pre-labeled dataset manifests, Data Management folders and upload URLs, the few-shot example references of DDA labeling jobs — is checked against that set before any role is assumed or any S3 call is made. Other buckets answer **400 `bucket_not_allowed`** with a `remedy` field naming the fix (allow the bucket, or place the object in the use case's configured bucket); a malformed URI or bucket name answers 400 `invalid_s3_uri` / `invalid_bucket_name`. Buckets inside the set are reached with the role the portal's shared S3 helper has always chosen for them (the Data Account role for the Data Account bucket, the use case's cross-account role otherwise); the two operations that used a different credential path before are listed in the table below.

- `POST /models/validate` and `POST /models/inspect` require the DataScientist role on the use case, as `POST /models/import` and `POST /models/convert` already did; the Import Model page tells other roles that validating and importing models requires DataScientist.
- The pre-labeled dataset routes require membership of the use case (DataScientist to create, validate or delete a dataset).
- `POST /labeling` with the DDA backend requires the `create_labeling_jobs` permission on the use case (DataScientist or above), like every other DDA labeling route; its `example_images` must be `s3://` references inside the use case's allowed buckets.
- Buckets the portal creates (`POST /s3-buckets`; **Create new bucket** in the onboarding wizard) are tagged `dda-portal:managed=true`, `dda-portal:created-by=portal` and — when created for a use case — `dda-portal:usecase-id`, ABAC-enabled, and allowed for that use case automatically; only buckets created outside the portal need the manual tag and the Allow step. Creating a bucket without a use case is a PortalAdmin action.
- Attaching a bucket to a use case (`s3_bucket`, `data_s3_bucket`, an Additional S3 bucket) requires that the bucket carries the portal tag and is readable through the use case's role, and that no other use case has it configured or is named in its `dda-portal:usecase-id` tag; a PortalAdmin may allow a shared or claimed bucket deliberately (audited). The portal's own artifacts and component buckets can never be allowed.
- API: `GET /usecases/{id}` returns `allowed_s3_buckets` and `additional_s3_buckets`; `PUT /usecases/{id}` accepts `additional_s3_buckets_add` / `additional_s3_buckets_remove` (at most 10 names per request, 100 additional buckets per use case) and reports `added` / `already_present` / `removed` / `not_present`; `GET /usecases/{id}/data/buckets` entries carry `allowed` and `portal_created`; `GET /s3-buckets` entries carry `eligible` and `portal_created` (advisory; the server decides at creation); `POST /s3-buckets` answers 409 `bucket_already_exists` for a name the account already owns.
- Audit: `s3_bucket_refused` on every refusal; `s3_bucket_allowed` (with `attribute` and `source`: `api`, `create`, `configure`, `auto_add`) and `s3_bucket_removed` on every change to a use case's allowed set.
- Frontend: Data Management marks buckets **Allowed** / **Not allowed** / **Portal-created**, defaults to the first allowed bucket and offers the inline **Allow for this use case** action to UseCaseAdmins; the onboarding bucket picker marks **Tagged for the portal** / **Not tagged for the portal** / **Portal-created** and shows **Create new bucket** to PortalAdmins only; the Use Case Edit modal gains an **Allowed buckets** section.
- The shared Lambda layer now ships boto3/botocore 1.43.101 (needed for the bucket `PutBucketAbac` and `TagResource` operations); every function on the layer runs that SDK instead of the runtime's bundled one.
- `deploy-infrastructure.sh` prints a warning when the registry enforcement value resolved for a deploy is not `true`. Production deployments run `PORTAL_REGISTRY_ENFORCED=true ./deploy-infrastructure.sh` after `backfill_portal_registry.py` has been applied (Admin Guide, "The enforcement flag"); role resolution itself is unchanged.

**Is this a breaking change?** Yes, for the flows listed below; each has a remedy. Everything else keeps working unchanged: Smart Import (convert → the use case bucket → import), SageMaker training output and its compile/package/deploy, previously imported models whose artifact lives outside the set (compile, package and deploy still work), reads of `data_s3_bucket` on a separate Data Account, cross-region buckets, S3 browsing of the configured bucket, training from a labeling-job export or a transformed manifest, DDA labeling / auto-label / preview dataset reads, use cases whose configured bucket has a legacy (uppercase / underscore) name, and deployments running with registry enforcement off.

| Flow | Before | After | Remedy |
|---|---|---|---|
| Manual BYOM import (`POST /models/validate`, `/import`) of a `.tar.gz` from a bucket that is not a configured bucket of the use case | Worked if the use case's role could read it | 400 `bucket_not_allowed` | A UseCaseAdmin allows the bucket (Allow action / Additional S3 buckets), or upload to the use case bucket; BYOM guide updated |
| Training from a registered pre-labeled dataset whose manifest lives in another bucket; `POST /datasets/validate-manifest` with such a bucket | Worked if readable | 400 `bucket_not_allowed` | Allow the bucket, or re-register after copying the manifest into the use case bucket |
| `POST /training/transform-manifest` with an explicit `output_manifest_uri` in another bucket (API callers; the UI sends the source only) | Worked | 400 `bucket_not_allowed` | Allow the bucket or omit `output_manifest_uri` |
| `POST /models/validate` by Viewer / Operator / DataLabeler | Validation ran | 403 | None needed — these roles could never complete the import; the Import Model page explains the DataScientist requirement |
| `POST /models/vllm` with an `s3_model_artifact` in another bucket | HEAD succeeded if readable | 400 `bucket_not_allowed` | Allow the bucket |
| Smart Import (`POST /models/convert`) from a bucket trusted only through a `TRUSTED_MODEL_ACCOUNTS` substring match; `POST /models/inspect` by roles below DataScientist | Worked / ran | 400 / 403 | Name the bucket in `TRUSTED_MODEL_BUCKETS` or allow it; use a DataScientist account (no deployment sets `TRUSTED_MODEL_*` in the tree) |
| Pre-labeled dataset routes used by a non-member of the use case | Worked for any authenticated user | 403 | Be assigned a role on the use case |
| Data Management with several tagged buckets in the account | Every tagged bucket browsable by any member, writable by DataScientists | Only the allowed set; other tagged buckets show **Not allowed** with the Allow action. A bucket in a separate Data Account other than `data_s3_bucket` cannot be allowed (Additional S3 buckets live in the use case's account) | Allow in place; for Data Account buckets use `data_s3_bucket` |
| Multi-account onboarding (`POST /usecases`) whose `s3_bucket` is tagged **after** creation (the previous guide order) | 201 | 400 `bucket_not_eligible` (`no_tags` / `tagging_unreadable`) | Tag the bucket, enable ABAC on it, then create the use case (Admin Guide reordered) |
| Onboarding with a separate Data Account when the portal cannot assume `DDAPortalDataAccessRole` | 201 with the Data Account steps failed softly | 400 `bucket_not_eligible` (`credentials_unavailable`) | Grant the AssumeRole, or onboard without the Data Account fields |
| The portal tagging the customer's Data Account bucket during use-case creation | The portal wrote the tag | The portal only reads tags | Tag first: `aws s3control tag-resource --account-id <data account> --resource-arn arn:aws:s3:::<bucket> --tags Key=dda-portal:managed,Value=true` |
| `POST /s3-buckets` **without** `usecase_id` (the wizard's **Create new bucket**) by a non-PortalAdmin | 201 | 403; the control is hidden in the picker | Select an existing tagged bucket, or ask a PortalAdmin |
| `POST /s3-buckets` for a name this account already owns | Idempotent 201 | 409 `bucket_already_exists` | Select the existing bucket (an administrator tags it if it is untagged) |
| `PUT /usecases/{id}` / `POST /usecases/{id}/data/configure` changing `s3_bucket` / `data_s3_bucket` to an untagged bucket | 200 | 400 `bucket_not_eligible` | Tag (and ABAC-enable, for a cross-account bucket) first |
| `POST /training` by an API caller with a non-marketplace `model_source` and a `dataset_manifest_s3` in another bucket | 201 | 400 `bucket_not_allowed` | Allow the bucket |
| Labeling jobs created earlier with few-shot references outside the set, or with bare-key references | Worker read them; `rerun-prelabels` ran; labeler tasks showed the examples | `rerun-prelabels` answers 400 listing the buckets; pre-label generation fails for every task of such a job; labeler tasks omit those examples | Allow the bucket (`s3://` references), or recreate the job with examples uploaded through the wizard (the only remedy for bare-key references) |
| Model validate / import from `data_s3_bucket` on a separate-Data-Account use case | Read with the use case's cross-account role | Read with the Data Account role, like every other operation on that bucket | None if the Data Account role is assumable; otherwise as for the onboarding row above |
| Attaching a bucket another use case has configured, or that is tagged `dda-portal:usecase-id` for another use case — whether or not that use case still exists | Allowed | 400 `bucket_not_eligible` (`bucket_configured_on_other_usecase`) unless the caller is a PortalAdmin; the remedy says whether the other use case still exists | A PortalAdmin performs the attach (audited as sharing), or an administrator removes the `dda-portal:usecase-id` tag |
| Single-account onboarding selecting an existing **untagged** bucket in the picker | 201 | 400 `bucket_not_eligible` (`no_tags`) | Tag the bucket first (`aws s3control tag-resource --account-id <portal account> --resource-arn arn:aws:s3:::<bucket> --tags Key=dda-portal:managed,Value=true`; no ABAC step is needed in the portal account today), or have a PortalAdmin create it with **Create new bucket**; the picker's **Not tagged for the portal** badge warns before submit |
| Multi-account `POST /usecases` before the use-case account role can be assumed (trust policy or external id not yet in place) | 201 | 400 `bucket_not_eligible` (`credentials_unavailable`) | Deploy the use-case account role with the external id first (`deploy-account-role.sh`), then create |
| New DDA labeling jobs whose `example_images` contain an `s3://` reference outside the set, or a bare key (API callers; the wizard uploads to the data bucket) | Job created | 400 `validation_errors` (`bucket_not_allowed` / `invalid_s3_uri` per reference) | Upload the examples through the wizard, or allow the bucket |
| `POST /labeling` (DDA backend) by a principal without `create_labeling_jobs` on the use case | Job created | 403 | Be assigned DataScientist or above on the use case |
| Re-pointing a configured bucket (`PUT /usecases/{id}` changing `s3_bucket` / `data_s3_bucket`) while labeling jobs are in flight | Workers kept reading the old bucket | Workers refuse the old bucket; `rerun-prelabels` answers 400 | Keep the old bucket under Additional S3 buckets until the jobs finish, then remove it |
| Data Management operations on `s3_bucket` or an Additional S3 bucket of a separate-Data-Account use case | Reached with the Data Account role (S3 `AccessDenied` unless that role could reach the bucket) | Reached with the use case's cross-account role, like every other non-data bucket | None (`data_s3_bucket` and single-account use cases are unchanged) |
| vLLM fit check (`PUT /models/vllm/{id}/engine-configuration`) on a record whose `s3_model_artifact` lives outside the set | HEAD on the artifact; `passed` / `warnings` | `status: unverified`; the engine configuration still saves | Allow the bucket |
| A use case record that names one of the portal's own buckets (artifacts or component bucket) as `s3_bucket` / `data_s3_bucket` | Every operation worked against the portal's bucket with that use case's role | The entry is dropped from the allowed set: every operation refuses it with `bucket_not_allowed` and it cannot be re-attached (not grandfathered) | Re-point the use case at its own bucket before upgrading |
| Deleting a use case (UseCaseAdmin of that use case, or PortalAdmin) | Its buckets stayed tagged and could later be attached to any use case | Its portal-created buckets stay claimed by `dda-portal:usecase-id` and need a PortalAdmin to re-home; its administrator-tagged buckets without a claim can be allowed again by any UseCaseAdmin | Remove the `dda-portal:*` tags, or the bucket, if that storage must not be allowed for another use case (Admin Guide delete note) |
| Additional S3 buckets and labeling jobs (informational) | — | Labeling jobs always use the configured data bucket (`data_s3_bucket`, else `s3_bucket`), so an Additional S3 bucket is browsable and writable in Data Management and usable for import, manifests and transforms, but cannot be a labeling dataset | Copy or upload labeling data into the configured data bucket |
| Self-service use-case creation by a user who is not a PortalAdmin | The wizard accepted any bucket in the portal account, or created one | Completes only with a bucket an administrator has already tagged `dda-portal:managed=true`, configured on no other use case and carrying no `dda-portal:usecase-id` claim; the user cannot create a bucket in the wizard (**Create new bucket** hidden; `POST /s3-buckets` without `usecase_id` → 403). Any other bucket → 400 `bucket_not_eligible` | A PortalAdmin creates the bucket in the wizard (tagged automatically) or hands over a tagged, unconfigured, unclaimed bucket; or an AWS administrator tags a bucket for the user (`aws s3control tag-resource --account-id <portal account> --resource-arn arn:aws:s3:::<bucket> --tags Key=dda-portal:managed,Value=true`). Admin Guide, "Self-Service UseCase Creation" |

One API contract change that is not a flow: `PUT /usecases/{id}` on an id that does not exist used to answer 200 and write a record holding only the body's fields; it now answers 404 `usecase_not_found` and writes nothing. No client in the tree relied on the old behaviour (the frontend edits only listed use cases).

### Added - Data Accounts Management (2025-01-22)

#### Settings Page
- New Settings page for Portal Admins
- Data Accounts management UI (add, edit, delete, test connection)
- Connection testing to verify Data Account access
- Status indicators for registered Data Accounts

#### UseCase Onboarding Enhancement
- Data Account dropdown selector in UseCase onboarding wizard
- Auto-fill all fields when selecting from registered Data Accounts
- Three configuration methods: dropdown, file upload, or manual entry
- Smart behavior to prevent conflicts between methods

#### Simplified Data Account Setup
- Removed UseCase Account IDs requirement from Data Account deployment
- Portal automatically configures bucket policies during UseCase onboarding
- No redeployment needed when adding new UseCases
- Clear instructions in deployment script for portal registration

#### Backend
- New Lambda function: `data_accounts.py` with full CRUD operations
- 6 new API endpoints for Data Accounts management
- DynamoDB table: `dda-portal-data-accounts` with GSI on status
- Automatic bucket policy updates during UseCase creation

#### Documentation
- `DATA_ACCOUNTS_DEPLOYMENT.md` - Deployment guide
- `DATA_ACCOUNT_SIMPLIFIED_SETUP.md` - Simplified setup explanation
- `DATA_ACCOUNT_DROPDOWN_FEATURE.md` - Dropdown feature user guide
- `DEPLOYMENT_CHECKLIST.md` - Complete deployment checklist
- `IMPLEMENTATION_COMPLETE.md` - Implementation summary

#### Benefits
- **Faster UseCase Creation**: 5 seconds vs 2-3 minutes (dropdown vs manual)
- **No Manual Entry**: Select from dropdown instead of typing
- **Error-Free**: No typos or copy-paste mistakes
- **Scalable**: Register once, use for unlimited UseCases
- **Simplified Deployment**: No UseCase Account IDs needed upfront

### Changed
- Updated `deploy-account-role.sh` to remove UseCase Account IDs prompt for Data Account
- Updated `data-account-app.ts` to handle empty usecaseAccountIds array
- Updated `data-account-stack.ts` to support portal-managed bucket policies
- Enhanced deployment script with clear registration instructions

### Fixed
- TypeScript interface alignment between Settings and API responses
- Optional fields properly marked in DataAccount interface

---

## Previous Releases

### Initial Release
- Multi-tenant portal architecture
- UseCase Account management
- Data management and S3 integration
- Ground Truth labeling jobs
- SageMaker training integration
- Model compilation for edge devices
- Greengrass component management
- Device deployments and monitoring
- Audit logging
- Cross-account role management
