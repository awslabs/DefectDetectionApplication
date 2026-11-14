#!/usr/bin/env bats
#
# Tests for edge-cv-portal/iam/setup-account.sh, the one-time administrator
# setup of a least-privilege install. Offline: `aws` and `cdk` are fakes.
#
# The fake `aws` (Python) keeps IAM state in $STUB_STATE: managed policies with
# their versions and default version, roles with their trust policy,
# permissions boundary and attached policies, and the EdgeCVPortal* stacks with
# their resources; tags it adds go to $STUB_STATE/tagged. It records every call in
# $STUB_STATE/calls.log and fails on a call it does not know, so a new AWS call
# in the script shows up here. The fake `cdk` records its arguments and working
# directory in $STUB_STATE/cdk.log.
#
# The script runs from a copy of edge-cv-portal/iam/ in the sandbox, so its
# policy files are the real ones and its working tree is never touched.

bats_require_minimum_version 1.5.0

ACCOUNT=111111111111
REGION_NAME=us-east-1
PRINCIPAL=arn:aws:iam::111111111111:role/Admin

setup() {
    SANDBOX="$(mktemp -d)"
    BIN="$SANDBOX/bin"
    export STUB_STATE="$SANDBOX/state"
    mkdir -p "$BIN" "$STUB_STATE/policies" "$STUB_STATE/roles" "$STUB_STATE/stacks" "$SANDBOX/home"
    IAM_SRC="$(cd "${BATS_TEST_DIRNAME}/../iam" && pwd)"
    cp -R "$IAM_SRC" "$SANDBOX/iam"
    SCRIPT="$SANDBOX/iam/setup-account.sh"
    _write_fakes

    # No real credentials, profile, region or tools.
    unset AWS_PROFILE AWS_DEFAULT_PROFILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY \
          AWS_SESSION_TOKEN AWS_REGION AWS_DEFAULT_REGION CDK_DEFAULT_ACCOUNT \
          CDK_DEFAULT_REGION
    export AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null
    export AWS_EC2_METADATA_DISABLED=true HOME="$SANDBOX/home"
    export STUB_ACCOUNT="$ACCOUNT"
    export PATH="$BIN:/usr/bin:/bin"
    [ "$(command -v aws)" = "$BIN/aws" ]
    [ "$(command -v cdk)" = "$BIN/cdk" ]
}

teardown() {
    [ -n "${SANDBOX:-}" ] && rm -rf "$SANDBOX"
}

_write_fakes() {
    cat > "$BIN/aws" <<'PY'
#!/usr/bin/env python3
import json
import os
import pathlib
import re
import sys

state = pathlib.Path(os.environ["STUB_STATE"])
args = sys.argv[1:]
with open(state / "calls.log", "a") as log:
    log.write(" ".join(args) + "\n")


def opt(name):
    return args[args.index(name) + 1] if name in args else None


def fail(code):
    sys.stderr.write(f"An error occurred ({code})\n")
    sys.exit(254)


def document(value):
    return pathlib.Path(value[len("file://"):]).read_text()


def policy_dir(arn):
    return state / "policies" / arn.rsplit("/", 1)[1]


def versions(pdir):
    return sorted((pdir / "versions").glob("v*.json"), key=lambda f: int(f.stem[1:]))


def add_version(pdir, text, set_default):
    (pdir / "versions").mkdir(parents=True, exist_ok=True)
    existing = versions(pdir)
    number = int(existing[-1].stem[1:]) + 1 if existing else 1
    (pdir / "versions" / f"v{number}.json").write_text(text)
    if set_default:
        (pdir / "default").write_text(f"v{number}")


command = args[:2]
if command == ["sts", "get-caller-identity"]:
    print(os.environ["STUB_ACCOUNT"])
elif command == ["configure", "get"]:
    region = os.environ.get("STUB_CONFIG_REGION", "")
    if not region:
        sys.exit(1)
    print(region)
elif command == ["iam", "get-policy"]:
    pdir = policy_dir(opt("--policy-arn"))
    if not pdir.exists():
        fail("NoSuchEntity")
    print((pdir / "default").read_text())
elif command == ["iam", "get-policy-version"]:
    pdir = policy_dir(opt("--policy-arn"))
    print((pdir / "versions" / f"{opt('--version-id')}.json").read_text())
elif command == ["iam", "list-policy-versions"]:
    pdir = policy_dir(opt("--policy-arn"))
    query = opt("--query")
    if query == "length(Versions)":
        print(len(versions(pdir)))
    elif query.startswith("sort_by(Versions[?IsDefaultVersion==`false`], &CreateDate)[0]"):
        default = (pdir / "default").read_text()
        print(next(f.stem for f in versions(pdir) if f.stem != default))
    else:
        fail("UnknownQuery")
elif command == ["iam", "delete-policy-version"]:
    pdir = policy_dir(opt("--policy-arn"))
    (pdir / "versions" / f"{opt('--version-id')}.json").unlink()
elif command == ["iam", "create-policy-version"]:
    add_version(policy_dir(opt("--policy-arn")), document(opt("--policy-document")),
                "--set-as-default" in args)
    print("{}")
elif command == ["iam", "create-policy"]:
    pdir = state / "policies" / opt("--policy-name")
    if pdir.exists():
        fail("EntityAlreadyExists")
    add_version(pdir, document(opt("--policy-document")), True)
    print("{}")
elif command == ["iam", "get-role"]:
    rdir = state / "roles" / opt("--role-name")
    if not rdir.exists():
        fail("NoSuchEntity")
    if opt("--query") == "Role.PermissionsBoundary.PermissionsBoundaryArn":
        boundary = rdir / "boundary"
        print(boundary.read_text() if boundary.exists() else "None")
    else:
        print(json.dumps({"Role": {"RoleName": opt("--role-name")}}))
elif command == ["iam", "put-role-permissions-boundary"]:
    (state / "roles" / opt("--role-name") / "boundary").write_text(opt("--permissions-boundary"))
elif command == ["iam", "create-role"]:
    rdir = state / "roles" / opt("--role-name")
    rdir.mkdir(parents=True)
    (rdir / "trust.json").write_text(document(opt("--assume-role-policy-document")))
    print("{}")
elif command == ["iam", "attach-role-policy"]:
    with open(state / "roles" / opt("--role-name") / "attached", "a") as fh:
        fh.write(opt("--policy-arn") + "\n")
elif command == ["cloudformation", "describe-stacks"]:
    if opt("--query") != "Stacks[?starts_with(StackName, 'EdgeCVPortal')].StackName":
        fail("UnknownQuery")
    print("\t".join(sorted(f.name for f in (state / "stacks").glob("EdgeCVPortal*"))))
elif command == ["cloudformation", "list-stack-resources"]:
    query = opt("--query")
    types = re.findall(r"ResourceType=='([^']+)'", query)
    if not types or not query.endswith(".[ResourceType,PhysicalResourceId]"):
        fail("UnknownQuery")
    for line in (state / "stacks" / opt("--stack-name")).read_text().splitlines():
        if line.split("\t")[0] in types:
            print(line)
elif command in (["ec2", "create-tags"], ["kms", "tag-resource"]):
    resource = opt("--resources") or opt("--key-id")
    if resource in os.environ.get("STUB_TAG_FAIL", "").split():
        fail("KMSInvalidStateException")
    with open(state / "tagged", "a") as fh:
        fh.write(f"{args[0]} {resource} {opt('--tags')}\n")
else:
    sys.stderr.write("fake aws: unhandled call: " + " ".join(args) + "\n")
    sys.exit(255)
PY
    cat > "$BIN/cdk" <<'SH'
#!/usr/bin/env bash
printf '%s|%s\n' "$PWD" "$*" >> "$STUB_STATE/cdk.log"
exit "${STUB_CDK_EXIT:-0}"
SH
    chmod +x "$BIN/aws" "$BIN/cdk"
}

# --- seed and query helpers --------------------------------------------------

# _filled <file> -> the policy file with this test's account and region.
_filled() {
    sed -e "s/ACCOUNT_ID/${ACCOUNT}/g" -e "s/REGION/${REGION_NAME}/g" "$SANDBOX/iam/$1"
}

# _seed_policy <name> <document>... : a managed policy whose versions are the
# given documents, the last one being the default.
_seed_policy() {
    local name="$1" n=0 doc
    shift
    mkdir -p "$STUB_STATE/policies/$name/versions"
    for doc in "$@"; do
        n=$((n + 1))
        printf '%s' "$doc" > "$STUB_STATE/policies/$name/versions/v$n.json"
    done
    printf 'v%s' "$n" > "$STUB_STATE/policies/$name/default"
}

# _seed_role <name> [boundary-arn]
_seed_role() {
    mkdir -p "$STUB_STATE/roles/$1"
    printf '{"Version":"2012-10-17","Statement":[]}' > "$STUB_STATE/roles/$1/trust.json"
    [ -n "${2:-}" ] && printf '%s' "$2" > "$STUB_STATE/roles/$1/boundary"
    return 0
}

# _seed_stack <name> <type> <physical-id> [<type> <physical-id>]... : a stack
# and the resources list-stack-resources reports for it.
_seed_stack() {
    local name="$1"
    shift
    : > "$STUB_STATE/stacks/$name"
    while [ $# -gt 1 ]; do
        printf '%s\t%s\n' "$1" "$2" >> "$STUB_STATE/stacks/$name"
        shift 2
    done
}

# _seed_portal_up_to_date : every portal policy exists with the current document.
_seed_portal_up_to_date() {
    _seed_policy dda-cfn-exec-portal-iam "$(_filled portal-cfn-execution-iam-policy.json)"
    _seed_policy dda-cfn-exec-portal-app "$(_filled portal-cfn-execution-app-policy.json)"
    _seed_policy dda-cfn-exec-portal-platform "$(_filled portal-cfn-execution-platform-policy.json)"
    _seed_policy dda-cfn-exec-portal-network "$(_filled portal-cfn-execution-network-policy.json)"
    _seed_policy dda-installer-portal "$(_filled portal-installer-policy.json)"
    _seed_policy dda-sagemaker-execution-boundary "$(_filled sagemaker-execution-boundary-policy.json)"
    _seed_policy dda-portal-permissions-boundary "$(_filled portal-permissions-boundary-policy.json)"
}

_run_setup() {
    run --separate-stderr bash "$SCRIPT" "$@"
}

_calls_matching() {  # <ERE> -> number of recorded aws calls matching it
    grep -cE "$1" "$STUB_STATE/calls.log" 2>/dev/null || true
}

_policy_default_doc() {  # <name> -> the default version's document
    local name="$1"
    cat "$STUB_STATE/policies/$name/versions/$(cat "$STUB_STATE/policies/$name/default").json"
}

_mutating_calls() {
    _calls_matching '^(iam (create-policy|create-policy-version|delete-policy-version|put-role-permissions-boundary|create-role|attach-role-policy)|ec2 create-tags|kms tag-resource) '
}

ARN_PREFIX="arn:aws:iam::${ACCOUNT}:policy"

# ============================================================================
# Arguments
# ============================================================================

@test "no account kind: usage error before any AWS call" {
    _run_setup --installer-principal "$PRINCIPAL" --region "$REGION_NAME"
    [ "$status" -eq 2 ]
    [[ "$stderr" == *"Name the account to set up"* ]]
    [ ! -s "$STUB_STATE/calls.log" ]
}

@test "missing --installer-principal: refused unless --no-installer-role" {
    _run_setup portal --region "$REGION_NAME"
    [ "$status" -eq 2 ]
    [[ "$stderr" == *"--installer-principal is required"* ]]
    [ ! -s "$STUB_STATE/calls.log" ]
}

@test "an installer principal that is not an ARN is refused" {
    _run_setup portal --installer-principal Admin --region "$REGION_NAME"
    [ "$status" -eq 2 ]
    [[ "$stderr" == *"must be an IAM ARN"* ]]
}

@test "an unknown argument is refused" {
    _run_setup portal --installer-principal "$PRINCIPAL" --bogus
    [ "$status" -eq 2 ]
    [[ "$stderr" == *"Unknown argument: --bogus"* ]]
}

@test "no region from the flag, the environment or the profile: refused" {
    _run_setup portal --installer-principal "$PRINCIPAL"
    [ "$status" -eq 2 ]
    [[ "$stderr" == *"No region"* ]]
}

@test "the profile's region is used when no flag or variable names one" {
    export STUB_CONFIG_REGION=eu-west-1
    _run_setup portal --installer-principal "$PRINCIPAL"
    [ "$status" -eq 0 ]
    grep -qF "|bootstrap aws://${ACCOUNT}/eu-west-1 " "$STUB_STATE/cdk.log"
}

# ============================================================================
# Portal Account, first run
# ============================================================================

@test "portal, fresh account: creates the seven policies with the account and region filled in" {
    _run_setup portal --installer-principal "$PRINCIPAL" --region "$REGION_NAME"
    [ "$status" -eq 0 ]
    for name in dda-cfn-exec-portal-iam dda-cfn-exec-portal-app dda-cfn-exec-portal-platform \
                dda-cfn-exec-portal-network dda-installer-portal dda-sagemaker-execution-boundary \
                dda-portal-permissions-boundary; do
        [ "$(_calls_matching "^iam create-policy --policy-name ${name} ")" -eq 1 ]
        ! _policy_default_doc "$name" | grep -qE 'ACCOUNT_ID|"REGION"|:REGION:'
    done
    _policy_default_doc dda-installer-portal | grep -qF "$ACCOUNT"
    [ "$(_policy_default_doc dda-installer-portal)" = "$(_filled portal-installer-policy.json)" ]
    [ "$(_calls_matching '^iam create-policy-version ')" -eq 0 ]
}

@test "portal: bootstraps CDK with the four execution policies and the permissions boundary, from the iam directory" {
    _run_setup portal --installer-principal "$PRINCIPAL" --region "$REGION_NAME"
    [ "$status" -eq 0 ]
    [ "$(wc -l < "$STUB_STATE/cdk.log")" -eq 1 ]
    expected="$SANDBOX/iam|bootstrap aws://${ACCOUNT}/${REGION_NAME}"
    expected+=" --cloudformation-execution-policies ${ARN_PREFIX}/dda-cfn-exec-portal-iam"
    expected+=" --cloudformation-execution-policies ${ARN_PREFIX}/dda-cfn-exec-portal-app"
    expected+=" --cloudformation-execution-policies ${ARN_PREFIX}/dda-cfn-exec-portal-platform"
    expected+=" --cloudformation-execution-policies ${ARN_PREFIX}/dda-cfn-exec-portal-network"
    expected+=" --custom-permissions-boundary dda-portal-permissions-boundary"
    [ "$(cat "$STUB_STATE/cdk.log")" = "$expected" ]
    # The boundary policy exists before the bootstrap names it.
    [ "$(_calls_matching '^iam create-policy --policy-name dda-portal-permissions-boundary ')" -eq 1 ]
    ! _policy_default_doc dda-portal-permissions-boundary | grep -q REGION
}

@test "portal: creates DDAInstaller trusting the principal and attaches the installer policy" {
    _run_setup portal --installer-principal "$PRINCIPAL" --region "$REGION_NAME"
    [ "$status" -eq 0 ]
    python3 - "$STUB_STATE/roles/DDAInstaller/trust.json" "$PRINCIPAL" <<'PY'
import json, sys
trust = json.load(open(sys.argv[1]))
assert trust["Statement"] == [{"Effect": "Allow", "Principal": {"AWS": sys.argv[2]},
                               "Action": "sts:AssumeRole"}], trust
PY
    [ "$(cat "$STUB_STATE/roles/DDAInstaller/attached")" = "${ARN_PREFIX}/dda-installer-portal" ]
}

@test "portal: --installer-role-name names the role" {
    _run_setup portal --installer-principal "$PRINCIPAL" --region "$REGION_NAME" \
        --installer-role-name DDAInstallerEu
    [ "$status" -eq 0 ]
    [ -f "$STUB_STATE/roles/DDAInstallerEu/trust.json" ]
    [ ! -e "$STUB_STATE/roles/DDAInstaller" ]
}

@test "portal: no DDASageMakerExecutionRole yet, so no boundary is put on a role" {
    _run_setup portal --installer-principal "$PRINCIPAL" --region "$REGION_NAME"
    [ "$status" -eq 0 ]
    [ "$(_calls_matching '^iam put-role-permissions-boundary ')" -eq 0 ]
}

# ============================================================================
# Portal Account: tagging the resources of an earlier deploy
# ============================================================================

@test "portal, no portal stacks yet: nothing to tag" {
    _seed_stack SomeOtherStack AWS::EC2::VPC vpc-other
    _run_setup portal --installer-principal "$PRINCIPAL" --region "$REGION_NAME"
    [ "$status" -eq 0 ]
    [[ "$output" == *"No portal KMS keys or network resources yet"* ]]
    [ ! -e "$STUB_STATE/tagged" ]
}

@test "portal: tags the KMS keys and network resources of every EdgeCVPortal stack, nested ones too" {
    _seed_stack EdgeCVPortalComputeStack AWS::KMS::Key key-1 AWS::Lambda::Function fn-1 AWS::KMS::Alias alias/dda-portal/x
    _seed_stack EdgeCVPortalNodeDesignerStack-NodeDesignerApiNestedStack-ABC \
        AWS::EC2::VPC vpc-1 AWS::EC2::Subnet subnet-1 AWS::EC2::RouteTable rtb-1 \
        AWS::EC2::SubnetRouteTableAssociation rtbassoc-1 AWS::EC2::InternetGateway igw-1 \
        AWS::EC2::SecurityGroup sg-1 AWS::EC2::VPCEndpoint vpce-1
    _seed_stack SomeOtherStack AWS::EC2::VPC vpc-other AWS::KMS::Key key-other
    _run_setup portal --installer-principal "$PRINCIPAL" --region "$REGION_NAME"
    [ "$status" -eq 0 ]
    [ "$(sort "$STUB_STATE/tagged")" = "$(printf '%s\n' \
        "ec2 igw-1 Key=dda-portal:managed,Value=true" \
        "ec2 rtb-1 Key=dda-portal:managed,Value=true" \
        "ec2 sg-1 Key=dda-portal:managed,Value=true" \
        "ec2 subnet-1 Key=dda-portal:managed,Value=true" \
        "ec2 vpc-1 Key=dda-portal:managed,Value=true" \
        "ec2 vpce-1 Key=dda-portal:managed,Value=true" \
        "kms key-1 TagKey=dda-portal:managed,TagValue=true" | sort)" ]
    [[ "$output" == *"dda-portal:managed=true on 7 portal KMS key(s) and network resource(s)"* ]]
    # Before the conditioned execution policies are written.
    last_tag=$(grep -nE '^(ec2 create-tags|kms tag-resource) ' "$STUB_STATE/calls.log" | tail -1 | cut -d: -f1)
    first_policy=$(grep -nE '^iam create-policy ' "$STUB_STATE/calls.log" | head -1 | cut -d: -f1)
    [ "$last_tag" -lt "$first_policy" ]
}

@test "portal: a resource that cannot be tagged is reported and the setup goes on" {
    _seed_stack EdgeCVPortalComputeStack AWS::KMS::Key key-pending AWS::KMS::Key key-1
    export STUB_TAG_FAIL=key-pending
    _run_setup portal --installer-principal "$PRINCIPAL" --region "$REGION_NAME"
    [ "$status" -eq 0 ]
    [[ "$stderr" == *"1 resource(s) could not be tagged"* ]]
    [ "$(cat "$STUB_STATE/tagged")" = "kms key-1 TagKey=dda-portal:managed,TagValue=true" ]
    [ -d "$STUB_STATE/policies/dda-cfn-exec-portal-network" ]
}

@test "usecase and data accounts tag nothing" {
    _seed_stack EdgeCVPortalComputeStack AWS::KMS::Key key-1
    for kind in usecase data; do
        _run_setup "$kind" --installer-principal "$PRINCIPAL" --region "$REGION_NAME"
        [ "$status" -eq 0 ]
    done
    [ "$(_calls_matching '^cloudformation ')" -eq 0 ]
    [ ! -e "$STUB_STATE/tagged" ]
}

# ============================================================================
# Re-runs and earlier installs
# ============================================================================

@test "re-run with unchanged documents creates no policy or version" {
    _seed_portal_up_to_date
    _seed_role DDAInstaller
    _run_setup portal --installer-principal "$PRINCIPAL" --region "$REGION_NAME"
    [ "$status" -eq 0 ]
    [ "$(_calls_matching '^iam (create-policy|create-policy-version|delete-policy-version) ')" -eq 0 ]
    [ "$(_calls_matching '^iam create-role ')" -eq 0 ]
    [[ "$output" == *"✓ dda-installer-portal is up to date"* ]]
}

@test "a changed document becomes the new default version" {
    _seed_portal_up_to_date
    _seed_policy dda-installer-portal '{"Version":"2012-10-17","Statement":[]}'
    _run_setup portal --installer-principal "$PRINCIPAL" --region "$REGION_NAME"
    [ "$status" -eq 0 ]
    [ "$(_calls_matching '^iam create-policy-version ')" -eq 1 ]
    [ "$(_calls_matching "^iam create-policy-version --policy-arn ${ARN_PREFIX}/dda-installer-portal .* --set-as-default")" -eq 1 ]
    [ "$(_policy_default_doc dda-installer-portal)" = "$(_filled portal-installer-policy.json)" ]
}

@test "a policy with five versions loses its oldest non-default version first" {
    _seed_portal_up_to_date
    old='{"Version":"2012-10-17","Statement":[]}'
    _seed_policy dda-installer-portal "$old" "$old" "$old" "$old" "$old"
    _run_setup portal --installer-principal "$PRINCIPAL" --region "$REGION_NAME"
    [ "$status" -eq 0 ]
    first_delete=$(grep -nE '^iam delete-policy-version ' "$STUB_STATE/calls.log" | head -1 | cut -d: -f1)
    first_create=$(grep -nE '^iam create-policy-version ' "$STUB_STATE/calls.log" | head -1 | cut -d: -f1)
    [ -n "$first_delete" ] && [ "$first_delete" -lt "$first_create" ]
    grep -qE "^iam delete-policy-version --policy-arn ${ARN_PREFIX}/dda-installer-portal --version-id v1$" \
        "$STUB_STATE/calls.log"
    [ ! -e "$STUB_STATE/policies/dda-installer-portal/versions/v1.json" ]
    [ "$(cat "$STUB_STATE/policies/dda-installer-portal/default")" = "v6" ]
}

@test "an earlier DDASageMakerExecutionRole without the boundary gets it" {
    _seed_role DDASageMakerExecutionRole
    _run_setup portal --installer-principal "$PRINCIPAL" --region "$REGION_NAME"
    [ "$status" -eq 0 ]
    [ "$(cat "$STUB_STATE/roles/DDASageMakerExecutionRole/boundary")" = \
      "${ARN_PREFIX}/dda-sagemaker-execution-boundary" ]
}

@test "a DDASageMakerExecutionRole that has the boundary is left alone" {
    _seed_role DDASageMakerExecutionRole "${ARN_PREFIX}/dda-sagemaker-execution-boundary"
    _run_setup portal --installer-principal "$PRINCIPAL" --region "$REGION_NAME"
    [ "$status" -eq 0 ]
    [ "$(_calls_matching '^iam put-role-permissions-boundary ')" -eq 0 ]
    [[ "$output" == *"DDASageMakerExecutionRole has the boundary"* ]]
}

@test "an existing installer role keeps its trust policy and gets the policy attached" {
    _seed_role DDAInstaller
    _run_setup portal --installer-principal "$PRINCIPAL" --region "$REGION_NAME"
    [ "$status" -eq 0 ]
    [ "$(_calls_matching '^iam create-role ')" -eq 0 ]
    [ "$(cat "$STUB_STATE/roles/DDAInstaller/trust.json")" = '{"Version":"2012-10-17","Statement":[]}' ]
    [ "$(cat "$STUB_STATE/roles/DDAInstaller/attached")" = "${ARN_PREFIX}/dda-installer-portal" ]
}

@test "--no-installer-role creates and attaches nothing on a role" {
    _run_setup portal --no-installer-role --region "$REGION_NAME"
    [ "$status" -eq 0 ]
    [ "$(_calls_matching '^iam (create-role|attach-role-policy) ')" -eq 0 ]
    [[ "$output" == *"Give the installer the policy ${ARN_PREFIX}/dda-installer-portal"* ]]
}

@test "--name-suffix suffixes the execution and installer policies, not the boundary" {
    _run_setup portal --installer-principal "$PRINCIPAL" --region eu-west-1 --name-suffix -eu-west-1
    [ "$status" -eq 0 ]
    for name in dda-cfn-exec-portal-iam-eu-west-1 dda-cfn-exec-portal-app-eu-west-1 \
                dda-cfn-exec-portal-platform-eu-west-1 dda-cfn-exec-portal-network-eu-west-1 \
                dda-installer-portal-eu-west-1 dda-sagemaker-execution-boundary \
                dda-portal-permissions-boundary; do
        [ -d "$STUB_STATE/policies/$name" ]
    done
    [ ! -d "$STUB_STATE/policies/dda-installer-portal" ]
    grep -qF -- "--cloudformation-execution-policies ${ARN_PREFIX}/dda-cfn-exec-portal-iam-eu-west-1" \
        "$STUB_STATE/cdk.log"
}

# ============================================================================
# Dry run and failures
# ============================================================================

@test "--dry-run changes nothing and prints the calls it would make" {
    _seed_role DDASageMakerExecutionRole
    _seed_stack EdgeCVPortalComputeStack AWS::KMS::Key key-1 AWS::EC2::VPC vpc-1
    _run_setup portal --installer-principal "$PRINCIPAL" --region "$REGION_NAME" --dry-run
    [ "$status" -eq 0 ]
    [ "$(_mutating_calls)" -eq 0 ]
    [ ! -e "$STUB_STATE/cdk.log" ]
    [ -z "$(ls "$STUB_STATE/policies")" ]
    [ ! -e "$STUB_STATE/roles/DDASageMakerExecutionRole/boundary" ]
    [[ "$stderr" == *"+ aws iam create-policy --policy-name dda-installer-portal"* ]]
    [[ "$stderr" == *"+ aws iam put-role-permissions-boundary --role-name DDASageMakerExecutionRole"* ]]
    [[ "$stderr" == *"+ cdk bootstrap aws://${ACCOUNT}/${REGION_NAME}"* ]]
    [[ "$stderr" == *"+ aws kms tag-resource --key-id key-1"* ]]
    [[ "$stderr" == *"+ aws ec2 create-tags --resources vpc-1"* ]]
    [ ! -e "$STUB_STATE/tagged" ]
    [[ "$output" == *"(dry run) dda-installer-portal created"* ]]
}

@test "a failed cdk bootstrap stops before the installer role" {
    export STUB_CDK_EXIT=1
    _run_setup portal --installer-principal "$PRINCIPAL" --region "$REGION_NAME"
    [ "$status" -ne 0 ]
    [ "$(_calls_matching '^iam (create-role|attach-role-policy) ')" -eq 0 ]
}

@test "no CDK CLI: stops with the install hint before bootstrapping" {
    rm "$BIN/cdk"
    _run_setup portal --installer-principal "$PRINCIPAL" --region "$REGION_NAME"
    [ "$status" -eq 1 ]
    [[ "$stderr" == *"The AWS CDK CLI was not found"* ]]
    [ "$(_calls_matching '^iam (create-role|attach-role-policy) ')" -eq 0 ]
}

# ============================================================================
# UseCase and Data accounts
# ============================================================================

@test "usecase: its own execution and installer policies and permissions boundary" {
    _run_setup usecase --installer-principal "$PRINCIPAL" --region "$REGION_NAME"
    [ "$status" -eq 0 ]
    [ "$(ls "$STUB_STATE/policies" | sort | tr '\n' ' ')" = "dda-cfn-exec-usecase dda-installer-usecase dda-usecase-permissions-boundary " ]
    [ "$(_policy_default_doc dda-cfn-exec-usecase)" = "$(_filled usecase-cfn-execution-policy.json)" ]
    [ "$(_policy_default_doc dda-usecase-permissions-boundary)" = "$(_filled usecase-permissions-boundary-policy.json)" ]
    ! _policy_default_doc dda-usecase-permissions-boundary | grep -qE 'ACCOUNT_ID|:REGION:'
    [ "$(cat "$STUB_STATE/cdk.log")" = "$SANDBOX/iam|bootstrap aws://${ACCOUNT}/${REGION_NAME} --cloudformation-execution-policies ${ARN_PREFIX}/dda-cfn-exec-usecase --custom-permissions-boundary dda-usecase-permissions-boundary" ]
    [[ "$output" == *"./deploy-portal.sh usecase"* ]]
}

@test "usecase and data: the boundary keeps its name with --name-suffix" {
    _run_setup usecase --installer-principal "$PRINCIPAL" --region "$REGION_NAME" --name-suffix -eu-west-1
    [ "$status" -eq 0 ]
    [ "$(ls "$STUB_STATE/policies" | sort | tr '\n' ' ')" = "dda-cfn-exec-usecase-eu-west-1 dda-installer-usecase-eu-west-1 dda-usecase-permissions-boundary " ]
    grep -qF -- "--custom-permissions-boundary dda-usecase-permissions-boundary" "$STUB_STATE/cdk.log"
}

@test "data: its own execution and installer policies and permissions boundary, no Marketplace step" {
    sed -i 's/DATA_BUCKET_NAME/my-data-bucket/g' "$SANDBOX/iam/data-cfn-execution-policy.json"
    _run_setup data --installer-principal "$PRINCIPAL" --region "$REGION_NAME"
    [ "$status" -eq 0 ]
    [ "$(ls "$STUB_STATE/policies" | sort | tr '\n' ' ')" = "dda-cfn-exec-data dda-data-permissions-boundary dda-installer-data " ]
    [ "$(_policy_default_doc dda-data-permissions-boundary)" = "$(_filled data-permissions-boundary-policy.json)" ]
    [ "$(cat "$STUB_STATE/cdk.log")" = "$SANDBOX/iam|bootstrap aws://${ACCOUNT}/${REGION_NAME} --cloudformation-execution-policies ${ARN_PREFIX}/dda-cfn-exec-data --custom-permissions-boundary dda-data-permissions-boundary" ]
    [[ "$output" != *"still names DATA_BUCKET_NAME"* ]]
    [[ "$output" == *"./deploy-portal.sh data"* ]]
    [[ "$output" != *"AWSMarketplaceManageSubscriptions"* ]]
}

@test "data: reminds about DATA_BUCKET_NAME while the execution policy names it" {
    python3 - "$SANDBOX/iam/data-cfn-execution-policy.json" <<'PY'
import json, sys
path = sys.argv[1]
policy = json.load(open(path))
if "DATA_BUCKET_NAME" not in json.dumps(policy):
    policy["Statement"].append({"Sid": "PlaceholderBucket", "Effect": "Allow",
                                "Action": "s3:GetBucketPolicy",
                                "Resource": "arn:aws:s3:::DATA_BUCKET_NAME"})
    json.dump(policy, open(path, "w"), indent=2)
PY
    _run_setup data --installer-principal "$PRINCIPAL" --region "$REGION_NAME"
    [ "$status" -eq 0 ]
    [[ "$output" == *"data-cfn-execution-policy.json still names DATA_BUCKET_NAME"* ]]
}
