#!/bin/bash
#
# One-time administrator setup for a least-privilege DDA install (README,
# "IAM Permissions for Installation"). Run it as an administrator, once per
# account and region, before anyone runs the deploy scripts with the installer
# role. Re-running it is safe: it updates the policies in place.
#
#   edge-cv-portal/iam/setup-account.sh <portal|usecase|data> \
#       --installer-principal <principal-arn> [options]
#
# For the Portal Account (also a single-account install) use "portal".
#
# It:
#   1. fills ACCOUNT_ID and REGION into this directory's policy files;
#   2. creates the policies, or adds a new default version when one exists and
#      its document changed: the CloudFormation execution policies
#      (dda-cfn-exec-*), the installer policy (dda-installer-*) and, for the
#      Portal Account, the DDASageMakerExecutionRole permissions boundary
#      (dda-sagemaker-execution-boundary);
#   3. runs `cdk bootstrap` with the execution policies instead of
#      AdministratorAccess (from this directory, which has no cdk.json);
#   4. creates the installer role trusting --installer-principal, unless it
#      exists or --no-installer-role is given, and attaches the installer
#      policy.
#
# It does not attach AWSMarketplaceManageSubscriptions: attach it to the
# installer role for the Marketplace subscription and detach it afterwards.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<'EOF'
Usage: setup-account.sh <portal|usecase|data> --installer-principal <arn> [options]

  portal | usecase | data       The account to set up. "portal" is also the
                                single-account install.
Options:
  --installer-principal <arn>   The user or role that assumes the installer
                                role, for example an IAM Identity Center role.
  --installer-role-name <name>  Installer role name (default: DDAInstaller).
  --no-installer-role           Do not create or change an installer role, for
                                example with an IAM Identity Center permission
                                set that uses the dda-installer-* policy.
  --name-suffix <suffix>        Suffix for the dda-cfn-exec-* and
                                dda-installer-* policy names, for a second
                                region in the same account (for example
                                -eu-west-1).
  --region <region>             Region to bootstrap (default: AWS_REGION,
                                AWS_DEFAULT_REGION, then the profile's region).
  --profile <name>              AWS CLI profile.
  --dry-run                     Print the AWS and CDK calls that change
                                anything instead of making them.
  --help                        Show this help.
EOF
}

KIND=""
INSTALLER_PRINCIPAL=""
INSTALLER_ROLE_NAME="DDAInstaller"
CREATE_INSTALLER_ROLE=1
NAME_SUFFIX=""
REGION_ARG=""
DRY_RUN=0

while [ $# -gt 0 ]; do
    case "$1" in
        portal|usecase|data) KIND="$1" ;;
        --installer-principal) INSTALLER_PRINCIPAL="${2:-}"; shift ;;
        --installer-role-name) INSTALLER_ROLE_NAME="${2:-}"; shift ;;
        --no-installer-role) CREATE_INSTALLER_ROLE=0 ;;
        --name-suffix) NAME_SUFFIX="${2:-}"; shift ;;
        --region) REGION_ARG="${2:-}"; shift ;;
        --profile) export AWS_PROFILE="${2:-}"; shift ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [ -z "$KIND" ]; then
    echo "Name the account to set up: portal, usecase or data." >&2
    usage >&2
    exit 2
fi
if [ "$CREATE_INSTALLER_ROLE" -eq 1 ] && [ -z "$INSTALLER_PRINCIPAL" ]; then
    echo "--installer-principal is required (or pass --no-installer-role)." >&2
    exit 2
fi
if [ -n "$INSTALLER_PRINCIPAL" ] && [[ "$INSTALLER_PRINCIPAL" != arn:* ]]; then
    echo "--installer-principal must be an IAM ARN, got: $INSTALLER_PRINCIPAL" >&2
    exit 2
fi

export AWS_PAGER=""
REGION="${REGION_ARG:-${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}}"
if [ -z "$REGION" ]; then
    echo "No region: pass --region or set AWS_REGION." >&2
    exit 2
fi
export AWS_REGION="$REGION"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# run <cmd...>: run a command that changes something, or print it (to stderr,
# so a caller's >/dev/null does not hide it) on --dry-run.
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        {
            printf '  +'
            printf ' %q' "$@"
            printf '\n'
        } >&2
    else
        "$@"
    fi
}

# done_msg <text>: report a change that was made (or, on --dry-run, planned).
done_msg() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "  (dry run) $1"
    else
        echo "✓ $1"
    fi
}

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

# fill <file> -> path of a copy with ACCOUNT_ID and REGION filled in.
fill() {
    local out="$WORK_DIR/$1"
    sed -e "s/ACCOUNT_ID/${ACCOUNT_ID}/g" -e "s/REGION/${REGION}/g" "$SCRIPT_DIR/$1" > "$out"
    printf '%s' "$out"
}

# same_document <json-a> <file-b>: true when both are the same policy document.
same_document() {
    python3 - "$1" "$2" <<'PY'
import json, sys
try:
    a = json.loads(sys.argv[1])
    with open(sys.argv[2], encoding="utf-8") as fh:
        b = json.load(fh)
except ValueError:
    sys.exit(1)
sys.exit(0 if a == b else 1)
PY
}

# upsert_policy <name> <filled-file>: create the managed policy, or make the
# file its new default version when the document changed. IAM keeps at most
# five versions, so the oldest non-default one goes first when it is full.
upsert_policy() {
    local name="$1" doc="$2" arn default current count oldest
    arn="arn:aws:iam::${ACCOUNT_ID}:policy/${name}"
    if default="$(aws iam get-policy --policy-arn "$arn" --query Policy.DefaultVersionId --output text 2>/dev/null)"; then
        current="$(aws iam get-policy-version --policy-arn "$arn" --version-id "$default" \
            --query PolicyVersion.Document --output json)"
        if same_document "$current" "$doc"; then
            echo "✓ $name is up to date"
            return 0
        fi
        count="$(aws iam list-policy-versions --policy-arn "$arn" --query 'length(Versions)' --output text)"
        if [ "$count" -ge 5 ]; then
            oldest="$(aws iam list-policy-versions --policy-arn "$arn" \
                --query 'sort_by(Versions[?IsDefaultVersion==`false`], &CreateDate)[0].VersionId' --output text)"
            run aws iam delete-policy-version --policy-arn "$arn" --version-id "$oldest"
        fi
        run aws iam create-policy-version --policy-arn "$arn" \
            --policy-document "file://$doc" --set-as-default >/dev/null
        done_msg "$name updated"
    else
        run aws iam create-policy --policy-name "$name" --policy-document "file://$doc" >/dev/null
        done_msg "$name created"
    fi
}

case "$KIND" in
    portal)
        EXEC_FILES=(portal-cfn-execution-iam-policy.json portal-cfn-execution-app-policy.json
                    portal-cfn-execution-platform-policy.json)
        EXEC_NAMES=(dda-cfn-exec-portal-iam dda-cfn-exec-portal-app dda-cfn-exec-portal-platform)
        ;;
    usecase)
        EXEC_FILES=(usecase-cfn-execution-policy.json)
        EXEC_NAMES=(dda-cfn-exec-usecase)
        ;;
    data)
        EXEC_FILES=(data-cfn-execution-policy.json)
        EXEC_NAMES=(dda-cfn-exec-data)
        ;;
esac
INSTALLER_FILE="${KIND}-installer-policy.json"
INSTALLER_NAME="dda-installer-${KIND}${NAME_SUFFIX}"

echo "DDA least-privilege setup: $KIND account $ACCOUNT_ID, region $REGION"
[ "$DRY_RUN" -eq 1 ] && echo "(dry run: changes are printed, not made)"
echo ""

echo "▶ Policies"
EXEC_ARNS=()
for i in "${!EXEC_FILES[@]}"; do
    name="${EXEC_NAMES[$i]}${NAME_SUFFIX}"
    upsert_policy "$name" "$(fill "${EXEC_FILES[$i]}")"
    EXEC_ARNS+=("arn:aws:iam::${ACCOUNT_ID}:policy/${name}")
done
upsert_policy "$INSTALLER_NAME" "$(fill "$INSTALLER_FILE")"
if [ "$KIND" = "portal" ]; then
    # IAM is global and the boundary names no region: one per account.
    BOUNDARY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/dda-sagemaker-execution-boundary"
    upsert_policy dda-sagemaker-execution-boundary "$(fill sagemaker-execution-boundary-policy.json)"
    # A DDASageMakerExecutionRole from an earlier install has no boundary, and
    # the installer may write its inline policies only once it has one.
    if CURRENT_BOUNDARY="$(aws iam get-role --role-name DDASageMakerExecutionRole \
            --query 'Role.PermissionsBoundary.PermissionsBoundaryArn' --output text 2>/dev/null)"; then
        if [ "$CURRENT_BOUNDARY" = "$BOUNDARY_ARN" ]; then
            echo "✓ DDASageMakerExecutionRole has the boundary"
        else
            run aws iam put-role-permissions-boundary --role-name DDASageMakerExecutionRole \
                --permissions-boundary "$BOUNDARY_ARN"
            done_msg "dda-sagemaker-execution-boundary set on the existing DDASageMakerExecutionRole"
        fi
    fi
fi
if [ "$KIND" = "data" ] && grep -q DATA_BUCKET_NAME "$SCRIPT_DIR/data-cfn-execution-policy.json" 2>/dev/null; then
    echo "  Note: data-cfn-execution-policy.json still names DATA_BUCKET_NAME. If you will pass data bucket"
    echo "  names to deploy-portal.sh data, put those buckets there and run this script again."
fi
echo ""

echo "▶ CDK bootstrap with the execution policies"
if command -v cdk >/dev/null 2>&1; then
    CDK=(cdk)
elif [ -x "$SCRIPT_DIR/../infrastructure/node_modules/.bin/cdk" ]; then
    CDK=("$SCRIPT_DIR/../infrastructure/node_modules/.bin/cdk")
else
    echo "The AWS CDK CLI was not found: install it (npm install -g aws-cdk) or run npm ci in" >&2
    echo "edge-cv-portal/infrastructure, then run this script again." >&2
    exit 1
fi
BOOTSTRAP_ARGS=(bootstrap "aws://${ACCOUNT_ID}/${REGION}")
for arn in "${EXEC_ARNS[@]}"; do
    BOOTSTRAP_ARGS+=(--cloudformation-execution-policies "$arn")
done
echo "  Every CDK app in this account and region that uses the default bootstrap qualifier"
echo "  shares this execution role, so after this step those apps get only these policies."
( cd "$SCRIPT_DIR" && run "${CDK[@]}" "${BOOTSTRAP_ARGS[@]}" )
echo ""

echo "▶ Installer role"
INSTALLER_POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${INSTALLER_NAME}"
if [ "$CREATE_INSTALLER_ROLE" -eq 0 ]; then
    echo "  Skipped (--no-installer-role). Give the installer the policy $INSTALLER_POLICY_ARN."
else
    if aws iam get-role --role-name "$INSTALLER_ROLE_NAME" >/dev/null 2>&1; then
        echo "✓ $INSTALLER_ROLE_NAME exists; its trust policy is left as it is"
    else
        TRUST_DOC="$WORK_DIR/installer-trust.json"
        printf '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"AWS":"%s"},"Action":"sts:AssumeRole"}]}\n' \
            "$INSTALLER_PRINCIPAL" > "$TRUST_DOC"
        run aws iam create-role --role-name "$INSTALLER_ROLE_NAME" \
            --assume-role-policy-document "file://$TRUST_DOC" >/dev/null
        done_msg "$INSTALLER_ROLE_NAME created, trusting $INSTALLER_PRINCIPAL"
    fi
    run aws iam attach-role-policy --role-name "$INSTALLER_ROLE_NAME" --policy-arn "$INSTALLER_POLICY_ARN"
    done_msg "$INSTALLER_NAME attached to $INSTALLER_ROLE_NAME"
fi
echo ""

case "$KIND" in
    portal) NEXT="./deploy-portal.sh single-account (or the Portal Account of a multi-account install)" ;;
    usecase) NEXT="./deploy-portal.sh usecase" ;;
    data) NEXT="./deploy-portal.sh data" ;;
esac
echo "Done. The installer now assumes ${INSTALLER_ROLE_NAME:-the installer role} and runs, from edge-cv-portal/:"
echo "  $NEXT"
if [ "$KIND" != "data" ]; then
    echo "For the Marketplace subscription, attach AWSMarketplaceManageSubscriptions to the installer"
    echo "role, subscribe, then detach it."
fi
