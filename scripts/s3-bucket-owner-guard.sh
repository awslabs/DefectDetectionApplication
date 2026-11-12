# scripts/s3-bucket-owner-guard.sh
# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Sourceable S3 bucket-ownership guard for the DERIVED component bucket.
#
# The Greengrass component artifact bucket is named deterministically from
# the publishing account and region (dda-component-<region>-<account>). S3
# bucket names are global, so anyone who knows a target account id can
# create that exact name in their own account first. Without an ownership
# assertion the publish scripts would then upload component artifacts into
# the attacker's bucket, and the recipe would point every edge device at
# attacker-controlled code (S3 bucket squatting / confused deputy).
#
# Every S3 call these helpers issue therefore asserts ExpectedBucketOwner,
# and a bucket of the derived name that exists under another account is a
# HARD ERROR: never adopted, never written to, never "worked around" with a
# different name.
#
#   . "$(dirname "$0")/scripts/s3-bucket-owner-guard.sh"
#   dda_component_bucket_name "$REGION" "$ACCOUNT"             # echoes the name
#   dda_ensure_owned_bucket "$BUCKET" "$REGION" "$ACCOUNT"     # verify / create / re-verify
#   dda_s3_put_object "$BUCKET" "$KEY" "$FILE" "$REGION" "$ACCOUNT"
#
# Return codes: 0 on success. 2 is the fail-closed code for a bucket that
# exists but is not owned by $ACCOUNT (or whose ownership cannot be
# determined). Callers must treat any non-zero return as fatal.

DDA_COMPONENT_BUCKET_PREFIX="${DDA_COMPONENT_BUCKET_PREFIX:-dda-component}"

# dda_component_bucket_name REGION ACCOUNT -> echoes dda-component-<region>-<account>.
# This is exactly the name GDK derives from gdk-config.json's
# `"bucket": "dda-component"` and that compute-stack.ts / deploy-portal.sh
# derive, so one definition keeps every publisher aligned.
dda_component_bucket_name() {
    local region="$1" account="$2"
    if [ -z "$region" ] || [ -z "$account" ] || [ "$account" = "None" ]; then
        echo "s3-bucket-owner-guard: cannot derive the component bucket name (region='${region}' account='${account}')" >&2
        return 2
    fi
    printf '%s-%s-%s' "$DDA_COMPONENT_BUCKET_PREFIX" "$region" "$account"
}

# _dda_owner_conflict BUCKET ACCOUNT DETAIL -> prints the fail-closed message.
_dda_owner_conflict() {
    local bucket="$1" account="$2" detail="$3"
    cat >&2 <<EOF

❌ SECURITY ERROR: S3 bucket ownership conflict for s3://${bucket}

   The bucket name is derived from this account (${account}) and region, but
   S3 did not confirm that account ${account} owns it. Either the name has
   been taken by ANOTHER AWS account (possible S3 bucket squatting: anyone
   who knows this account id can pre-create the name) or these credentials
   cannot read the bucket.

   Refusing to publish: nothing was uploaded and no component version was
   created. Do NOT work around this by choosing another bucket name or by
   adopting the existing bucket. Confirm the owner with
     aws s3api head-bucket --bucket ${bucket} --expected-bucket-owner ${account}
   and escalate to your security contact if the bucket is not yours.

   Underlying error:
EOF
    printf '%s\n' "$detail" | sed 's/^/     /' >&2
    echo "" >&2
}

# dda_bucket_owned BUCKET REGION ACCOUNT
#   0 -> the bucket exists and ACCOUNT owns it (HeadBucket + ExpectedBucketOwner)
#   1 -> the bucket does not exist (404)
#   2 -> anything else: 403 (exists under another account, or no access) or an
#        error that says nothing about ownership. Never treated as absent.
# The error text is left in DDA_BUCKET_OWNER_ERROR for the caller's message.
dda_bucket_owned() {
    local bucket="$1" region="$2" account="$3"
    local err rc=0
    DDA_BUCKET_OWNER_ERROR=""
    err="$(aws s3api head-bucket --bucket "$bucket" --region "$region" \
        --expected-bucket-owner "$account" 2>&1)" || rc=$?
    if [ "$rc" -eq 0 ]; then
        return 0
    fi
    DDA_BUCKET_OWNER_ERROR="$err"
    # Only a 404 means absent. A 403 is NOT absence: --expected-bucket-owner
    # makes S3 answer 403 for a bucket of that name owned by any other account.
    if printf '%s' "$err" | grep -qiE '\(404\)|NoSuchBucket|Not Found'; then
        return 1
    fi
    return 2
}

# dda_ensure_owned_bucket BUCKET REGION ACCOUNT
# Verifies that BUCKET is owned by ACCOUNT, creating it when (and only when)
# S3 reports it absent, and re-verifying ownership right after the create so
# a name taken between the check and the create is still caught. Any outcome
# other than "exists and ACCOUNT owns it" returns 2 after printing the
# fail-closed message.
dda_ensure_owned_bucket() {
    local bucket="$1" region="$2" account="$3"
    if [ -z "$bucket" ] || [ -z "$region" ] || [ -z "$account" ] || [ "$account" = "None" ]; then
        echo "s3-bucket-owner-guard: bucket, region and account are all required to verify ownership (bucket='${bucket}' region='${region}' account='${account}')" >&2
        return 2
    fi

    local owned_rc=0
    dda_bucket_owned "$bucket" "$region" "$account" || owned_rc=$?
    if [ "$owned_rc" -eq 0 ]; then
        echo "✓ Component bucket s3://${bucket} exists and is owned by account ${account}"
        return 0
    fi
    if [ "$owned_rc" -ne 1 ]; then
        _dda_owner_conflict "$bucket" "$account" "$DDA_BUCKET_OWNER_ERROR"
        return 2
    fi

    echo "Component bucket s3://${bucket} does not exist; creating it in ${region}"
    # us-east-1 REJECTS a LocationConstraint; every other region REQUIRES one.
    local create_err create_rc=0
    if [ "$region" = "us-east-1" ]; then
        create_err="$(aws s3api create-bucket --bucket "$bucket" --region "$region" 2>&1)" || create_rc=$?
    else
        create_err="$(aws s3api create-bucket --bucket "$bucket" --region "$region" \
            --create-bucket-configuration "LocationConstraint=${region}" 2>&1)" || create_rc=$?
    fi
    if [ "$create_rc" -ne 0 ]; then
        if printf '%s' "$create_err" | grep -qi 'BucketAlreadyOwnedByYou'; then
            # A concurrent publish got there first; the re-verification below
            # is what decides.
            echo "Component bucket s3://${bucket} was created concurrently by this account"
        elif printf '%s' "$create_err" | grep -qi 'BucketAlreadyExists'; then
            _dda_owner_conflict "$bucket" "$account" "$create_err"
            return 2
        else
            echo "❌ Failed to create s3://${bucket}:" >&2
            printf '%s\n' "$create_err" | sed 's/^/     /' >&2
            return 2
        fi
    else
        echo "✓ Created s3://${bucket}"
    fi

    # Verify ownership immediately after the create, before anything is written.
    owned_rc=0
    dda_bucket_owned "$bucket" "$region" "$account" || owned_rc=$?
    if [ "$owned_rc" -ne 0 ]; then
        _dda_owner_conflict "$bucket" "$account" "${DDA_BUCKET_OWNER_ERROR:-HeadBucket did not confirm ownership after CreateBucket}"
        return 2
    fi
    echo "✓ Verified s3://${bucket} is owned by account ${account}"
    return 0
}

# dda_gdk_config_assert_owner GDK_CONFIG_JSON ACCOUNT
# `gdk component publish` uploads with its own S3 client (boto3 upload_file),
# so the ExpectedBucketOwner assertion is handed to GDK through the publish
# option it already supports: publish.options.file_upload_args is passed
# verbatim as the upload ExtraArgs. Rewrites the file in place for every
# component in the config. Requires python3 (as the publish scripts already do).
dda_gdk_config_assert_owner() {
    local config="$1" account="$2"
    if [ ! -f "$config" ]; then
        echo "s3-bucket-owner-guard: gdk config '${config}' not found" >&2
        return 2
    fi
    if [ -z "$account" ] || [ "$account" = "None" ]; then
        echo "s3-bucket-owner-guard: an account id is required to assert the GDK upload owner" >&2
        return 2
    fi
    python3 - "$config" "$account" <<'PYEOF' || return 2
import json, sys
path, account = sys.argv[1], sys.argv[2]
with open(path) as f:
    config = json.load(f)
for name, component in config.get("component", {}).items():
    publish = component.setdefault("publish", {})
    options = publish.setdefault("options", {})
    upload_args = options.setdefault("file_upload_args", {})
    upload_args["ExpectedBucketOwner"] = account
with open(path, "w") as f:
    json.dump(config, f, indent=2)
    f.write("\n")
PYEOF
    echo "✓ GDK uploads will assert ExpectedBucketOwner=${account} (${config})"
    return 0
}

# dda_s3_put_object BUCKET KEY FILE REGION ACCOUNT
# Uploads FILE to s3://BUCKET/KEY asserting ExpectedBucketOwner=ACCOUNT on
# the PutObject itself (the high-level `aws s3 cp` has no such option). A
# bucket owned by another account answers 403 and the upload fails closed.
dda_s3_put_object() {
    local bucket="$1" key="$2" file="$3" region="$4" account="$5"
    if [ -z "$bucket" ] || [ -z "$key" ] || [ -z "$file" ] || [ -z "$region" ] || [ -z "$account" ] || [ "$account" = "None" ]; then
        echo "s3-bucket-owner-guard: bucket, key, file, region and account are all required for an owner-asserted upload" >&2
        return 2
    fi
    if [ ! -f "$file" ]; then
        echo "s3-bucket-owner-guard: upload source '${file}' is not a file" >&2
        return 2
    fi
    local err rc=0
    err="$(aws s3api put-object --bucket "$bucket" --key "$key" --body "$file" \
        --region "$region" --expected-bucket-owner "$account" 2>&1)" || rc=$?
    if [ "$rc" -ne 0 ]; then
        if printf '%s' "$err" | grep -qiE '\(403\)|AccessDenied|Forbidden'; then
            _dda_owner_conflict "$bucket" "$account" "$err"
        else
            echo "❌ Upload of ${file} to s3://${bucket}/${key} failed:" >&2
            printf '%s\n' "$err" | sed 's/^/     /' >&2
        fi
        return 2
    fi
    return 0
}
