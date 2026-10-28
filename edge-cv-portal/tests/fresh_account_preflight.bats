#!/usr/bin/env bats
#
# deploy-portal.sh's fresh-account preflight (README "Docker", "Component
# artifact bucket" and "Retrying a failed first deploy"):
#   * the container runtime the CDK app bundles its assets with (Docker, or the
#     command CDK_DOCKER names) and --install-docker;
#   * the RETAIN resources with fixed names that a failed first create of
#     EdgeCVPortalComputeStack leaves behind;
#   * the Greengrass component artifact bucket the compute stack configures but
#     never creates.
#
# Hermetic: the AWS environment is scrubbed in setup, every AWS answer comes
# from the stub (helpers/stub.sh), and the tests that need Docker absent run
# on a PATH without any docker (setup_file), so a Docker installed on the host
# does not change their outcome. The orchestrator's steps stay stubs.

# shellcheck disable=SC2016  # single quotes on purpose: code for bash -c, and $(...) matched literally

bats_require_minimum_version 1.5.0

load test_helper

COMPUTE=EdgeCVPortalComputeStack
BUCKET=dda-component-eu-west-1-123456789012
STEPS="deploy-account-role.sh deploy-auth.sh deploy-infrastructure.sh deploy-frontend.sh"
OWNED_BY_YOU="An error occurred (BucketAlreadyOwnedByYou) when calling the CreateBucket operation: Your previous request to create the named bucket succeeded and you already own it."
TAKEN="An error occurred (BucketAlreadyExists) when calling the CreateBucket operation: The requested bucket name is not available. The bucket namespace is shared by all users of the system. Please select a different name and try again."

# PATH_WITHOUT_DOCKER: one link per command of the caller's PATH, the first
# match as a PATH lookup finds it, except docker.
setup_file() {
  PATH_WITHOUT_DOCKER="$(mktemp -d "${BATS_TMPDIR:-/tmp}/portal-no-docker.XXXXXX")"
  export PATH_WITHOUT_DOCKER
  local d f name
  local -A seen=()
  local -a links=()
  local IFS=:
  for d in $PATH; do
    [ -d "$d" ] || continue
    for f in "$d"/*; do
      name="${f##*/}"
      if [ "$name" != docker ] && [ -z "${seen["$name"]:-}" ] && [ -f "$f" ] && [ -x "$f" ]; then
        seen["$name"]=1
        links+=("$f")
      fi
    done
  done
  ln -s "${links[@]}" "$PATH_WITHOUT_DOCKER/"
}
teardown_file() { rm -rf "${PATH_WITHOUT_DOCKER:?}"; }

setup() {
  portal_harness_setup
  portal_scrub_aws_env
  _install_stub "$PORTAL_BIN/npm"
  _install_stub "$PORTAL_BIN/sleep"
  portal_require_stubbed_tools npm sleep
  # print_summary needs the Portal URL, or the orchestrator exits 1.
  set_cfn_output EdgeCVPortalFrontendStack DistributionDomainName d1.cloudfront.net
  trusted_param_absent
  portal_not_deployed
  install_real_if_present deploy-portal.sh
  install_real_if_present scripts/portal-trusted-accounts.sh
  install_real_if_present scripts/portal-registry-enforcement.sh
  install_real_if_present scripts/portal-mcp-stack.sh
}
teardown() { portal_harness_teardown; }

# ---- runners ----------------------------------------------------------------
# orchestrate <topology> [option ...] -> deploy-portal.sh, non-interactive,
# Deployment_Log in the sandbox
orchestrate() {
  run bash -c 'cd "$PORTAL_TREE" && ./deploy-portal.sh "$@" --non-interactive --log-file "$PORTAL_SANDBOX/deploy.log"' \
    _ "$@" < /dev/null
}
# orchestrate_without_docker <topology> [option ...] -> the same with no docker
# on PATH: the stub removed and the system tools reached through
# PATH_WITHOUT_DOCKER only
orchestrate_without_docker() {
  rm -f "$PORTAL_BIN/docker"
  run env PATH="$PORTAL_BIN:$PATH_WITHOUT_DOCKER" \
    bash -c 'cd "$PORTAL_TREE" && ./deploy-portal.sh "$@" --non-interactive --log-file "$PORTAL_SANDBOX/deploy.log"' \
    _ "$@" < /dev/null
}
# distro <ID> <ID_LIKE> <VERSION_CODENAME> <UBUNTU_CODENAME> -> the
# /etc/os-release fields in_script answers
distro() { export FAKE_ID="$1" FAKE_ID_LIKE="$2" FAKE_VERSION_CODENAME="$3" FAKE_UBUNTU_CODENAME="$4"; }
# in_script <bash code> -> run the code with deploy-portal.sh's functions
# loaded, /etc/os-release replaced by the distro fields, a non-root user, and
# dpkg (amd64), systemctl and dnf as no-op functions, so every install command
# reaches the sudo stub and none runs.
in_script() {
  run bash -c '
    source "$PORTAL_TREE/deploy-portal.sh"
    _os_release_field() {
      case "$1" in
        ID) printf "%s" "$FAKE_ID" ;;
        ID_LIKE) printf "%s" "$FAKE_ID_LIKE" ;;
        VERSION_CODENAME) printf "%s" "$FAKE_VERSION_CODENAME" ;;
        UBUNTU_CODENAME) printf "%s" "$FAKE_UBUNTU_CODENAME" ;;
      esac
    }
    id() { if [ "${1:-}" = -u ]; then echo 1000; else echo tester; fi; }
    dpkg() { echo amd64; }
    systemctl() { :; }
    dnf() { :; }
    '"$1" < /dev/null
}

# ---- assertions -------------------------------------------------------------
# stopped_before_any_change -> no step, no CDK call and no AWS write ran
stopped_before_any_change() {
  [ "$(stub_call_count cdk)" = 0 ]
  [ "$(stub_call_count npx)" = 0 ]
  local step
  for step in $STEPS; do
    [ "$(stub_call_count "$step")" = 0 ]
  done
  [ "$(stub_call_count_matching aws '^ssm put-parameter')" = 0 ]
  [ "$(stub_call_count_matching aws '^s3api create-bucket')" = 0 ]
  [ "$(stub_call_count_matching aws ' delete-')" = 0 ]
}
# every_step_ran -> each of the four steps ran
every_step_ran() {
  local step
  for step in $STEPS; do
    stub_called "$step"
  done
}

# ---------------------------------------------------------------------------
# The component artifact bucket
# ---------------------------------------------------------------------------

@test "established account: the bucket is checked against the deploying account and left as it is" {
  set_stack_status "$COMPUTE" UPDATE_COMPLETE
  orchestrate single-account --region eu-west-1
  [ "$status" -eq 0 ]
  local head; head="$(stub_call_index aws '^s3api head-bucket')"
  [ -n "$head" ]
  stub_call_has_pair "$head" --bucket "$BUCKET"
  stub_call_has_pair "$head" --region eu-west-1
  stub_call_has_pair "$head" --expected-bucket-owner 123456789012
  [ "$(stub_call_count_matching aws '^s3api create-bucket')" = 0 ]
  output_has_line "Component bucket: s3://$BUCKET (already exists)"
  # a deployed compute stack owns its retained resources: none is looked up
  [ "$(stub_call_count_matching aws '^(ecr|dynamodb) ')" = 0 ]
  every_step_ran
}

@test "fresh account: the bucket is created with its LocationConstraint before the bootstrap and the first step" {
  missing_stacks "$COMPUTE"
  component_bucket_absent
  unset_bootstrap_version
  orchestrate single-account --region eu-west-1
  [ "$status" -eq 0 ]
  local create boot
  create="$(stub_call_index aws '^s3api create-bucket')"
  boot="$(stub_call_index cdk '^bootstrap')"
  [ -n "$create" ]
  [ -n "$boot" ]
  [ "$create" -lt "$boot" ]
  [ "$create" -lt "$(stub_order_of deploy-account-role.sh)" ]
  stub_call_has_pair "$create" --bucket "$BUCKET"
  stub_call_has_pair "$create" --region eu-west-1
  stub_call_has_pair "$create" --create-bucket-configuration LocationConstraint=eu-west-1
  output_has_line "Component bucket: s3://$BUCKET is absent; creating it"
  output_has_line "✓ Created s3://$BUCKET"
  # the new account was searched for leftovers, and had none
  [ "$(stub_call_count_matching aws '^ecr describe-repositories --repository-names dda-detector-export ')" = 1 ]
  [ "$(stub_call_count_matching aws '^dynamodb describe-table --table-name dda-portal-edge-credentials ')" = 1 ]
  [ "$(stub_call_count_matching aws '^dynamodb describe-table --table-name dda-portal-account-sync ')" = 1 ]
  every_step_ran
}

@test "fresh account in us-east-1: the bucket is created without a LocationConstraint" {
  component_bucket_absent
  orchestrate single-account --region us-east-1
  [ "$status" -eq 0 ]
  local create; create="$(stub_call_index aws '^s3api create-bucket')"
  [ -n "$create" ]
  stub_call_has_pair "$create" --bucket dda-component-us-east-1-123456789012
  stub_call_has_pair "$create" --region us-east-1
  [ "$(stub_call_args_n "$create" | grep -c -- --create-bucket-configuration)" = 0 ]
  every_step_ran
}

@test "the account of the --profile names the bucket and is its expected owner" {
  set_profile_account prodprofile 333333333333
  component_bucket_absent
  orchestrate single-account --profile prodprofile --region eu-west-1
  [ "$status" -eq 0 ]
  local head create
  head="$(stub_call_index aws '^s3api head-bucket')"
  create="$(stub_call_index aws '^s3api create-bucket')"
  stub_call_has_pair "$head" --expected-bucket-owner 333333333333
  stub_call_has_pair "$create" --bucket dda-component-eu-west-1-333333333333
}

@test "a refused bucket check (403) stops before any step and creates nothing" {
  head_bucket_fails "An error occurred (403) when calling the HeadBucket operation: Forbidden"
  orchestrate single-account --region eu-west-1
  [ "$status" -eq 2 ]
  [[ "$output" == *"cannot determine whether s3://$BUCKET exists — the HeadBucket call was denied."* ]]
  [[ "$output" == *"(403)"* ]]
  [ "$(stub_call_count_matching aws '^s3api create-bucket')" = 0 ]
  [ "$(stub_call_count deploy-account-role.sh)" = 0 ]
  [ "$(stub_call_count cdk)" = 0 ]
}

@test "a bucket check that fails for another reason is reported, and nothing is created" {
  head_bucket_fails "An error occurred (503) when calling the HeadBucket operation: Slow Down"
  orchestrate single-account --region eu-west-1
  [ "$status" -eq 2 ]
  [[ "$output" == *"could not check whether s3://$BUCKET exists"* ]]
  [[ "$output" == *"Slow Down"* ]]
  [ "$(stub_call_count_matching aws '^s3api create-bucket')" = 0 ]
  [ "$(stub_call_count deploy-account-role.sh)" = 0 ]
}

@test "a create answered BucketAlreadyOwnedByYou counts as present, and the deploy goes on" {
  component_bucket_absent
  create_bucket_fails "$OWNED_BY_YOU"
  orchestrate single-account --region eu-west-1
  [ "$status" -eq 0 ]
  output_has_line "Component bucket: s3://$BUCKET already exists (owned by this account)"
  every_step_ran
}

@test "a create answered BucketAlreadyExists stops before any step" {
  component_bucket_absent
  create_bucket_fails "$TAKEN"
  orchestrate single-account --region eu-west-1
  [ "$status" -eq 2 ]
  [[ "$output" == *"the bucket name '$BUCKET' is already taken by another AWS account."* ]]
  [ "$(stub_call_count deploy-account-role.sh)" = 0 ]
  [ "$(stub_call_count cdk)" = 0 ]
}

# ---------------------------------------------------------------------------
# Resources a failed first create leaves behind
# ---------------------------------------------------------------------------

@test "compute stack absent with all three leftovers: stops before any change, prints their cleanup and deletes nothing" {
  missing_stacks "$COMPUTE"
  component_bucket_absent
  unset_bootstrap_version
  ecr_repositories_exist dda-detector-export
  dynamodb_tables_exist dda-portal-edge-credentials dda-portal-account-sync
  orchestrate single-account --region eu-west-1
  [ "$status" -eq 2 ]
  [[ "$output" == *"$COMPUTE does not exist, so this deploy creates it from scratch, but resources it creates with fixed names are still present."* ]]
  output_has_line "      aws ecr delete-repository --repository-name dda-detector-export --region eu-west-1"
  output_has_line "      aws dynamodb delete-table --table-name dda-portal-edge-credentials --region eu-west-1"
  output_has_line "      aws dynamodb delete-table --table-name dda-portal-account-sync --region eu-west-1"
  stopped_before_any_change
  # not even the first-deploy trusted account list was stored
  [ -z "$(trusted_param_value)" ]
}

@test "ROLLBACK_COMPLETE, the state a failed first create leaves: the one leftover table is reported" {
  set_stack_status "$COMPUTE" ROLLBACK_COMPLETE
  dynamodb_tables_exist dda-portal-account-sync
  orchestrate single-account --region eu-west-1
  [ "$status" -eq 2 ]
  [[ "$output" == *"$COMPUTE is in ROLLBACK_COMPLETE, so this deploy creates it from scratch"* ]]
  output_has_line "    DynamoDB table 'dda-portal-account-sync' (AccountSyncTable)"
  [[ "$output" != *"dda-detector-export"* ]]
  [[ "$output" != *"dda-portal-edge-credentials"* ]]
  stopped_before_any_change
}

@test "ROLLBACK_FAILED and REVIEW_IN_PROGRESS are a create from scratch too" {
  ecr_repositories_exist dda-detector-export
  local st
  for st in ROLLBACK_FAILED REVIEW_IN_PROGRESS; do
    : > "$STUB_TRACE"
    unset STUB_CFN_STACK_STATUSES
    set_stack_status "$COMPUTE" "$st"
    orchestrate single-account --region eu-west-1
    [ "$status" -eq 2 ]
    [[ "$output" == *"$COMPUTE is in $st"* ]]
    output_has_line "    ECR repository 'dda-detector-export' (DetectorExportRepository)"
    stopped_before_any_change
  done
}

@test "a deployed compute stack (UPDATE_ROLLBACK_COMPLETE) owns those resources: nothing is looked up or reported" {
  set_stack_status "$COMPUTE" UPDATE_ROLLBACK_COMPLETE
  ecr_repositories_exist dda-detector-export
  dynamodb_tables_exist dda-portal-edge-credentials dda-portal-account-sync
  orchestrate single-account --region eu-west-1
  [ "$status" -eq 0 ]
  [ "$(stub_call_count_matching aws '^(ecr|dynamodb) ')" = 0 ]
  every_step_ran
}

@test "a compute stack lookup that fails is not taken for an absent stack" {
  deny_describe_stacks "$COMPUTE"
  ecr_repositories_exist dda-detector-export
  orchestrate single-account --region eu-west-1
  [ "$status" -eq 0 ]
  [ "$(stub_call_count_matching aws '^(ecr|dynamodb) ')" = 0 ]
  every_step_ran
}

# ---------------------------------------------------------------------------
# The container runtime
# ---------------------------------------------------------------------------

@test "Docker missing: stops before any AWS write or account lookup, with the install instructions" {
  orchestrate_without_docker single-account --region eu-west-1
  [ "$status" -eq 2 ]
  [[ "$output" == *"Docker not found on PATH"* ]]
  [[ "$output" == *"Re-run this script with --install-docker"* ]]
  stopped_before_any_change
  [ "$(stub_call_count_matching aws '^s3api ')" = 0 ]
  [ "$(stub_call_count_matching aws "^cloudformation describe-stacks --stack-name $COMPUTE")" = 0 ]
}

@test "Docker installed but its daemon unreachable: says so, with the error" {
  stub_exit docker 1
  stub_output docker "" "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"
  orchestrate single-account --region eu-west-1
  [ "$status" -eq 2 ]
  [[ "$output" == *"Docker is installed but its daemon is not reachable."* ]]
  [[ "$output" == *"Is the docker daemon running?"* ]]
  stopped_before_any_change
}

@test "Docker socket permission denied: the docker group advice" {
  stub_exit docker 1
  stub_output docker "" "permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock: dial unix /var/run/docker.sock: connect: permission denied"
  orchestrate single-account --region eu-west-1
  [ "$status" -eq 2 ]
  [[ "$output" == *"cannot reach the daemon socket (permission denied)"* ]]
  [[ "$output" == *"newgrp docker"* ]]
  stopped_before_any_change
}

@test "CDK_DOCKER=finch: finch is checked, not docker" {
  _install_stub "$PORTAL_BIN/finch"
  export CDK_DOCKER=finch
  orchestrate single-account --region eu-west-1
  [ "$status" -eq 0 ]
  [ "$(stub_call_count finch)" = 1 ]
  [ "$(stub_args_of finch)" = info ]
  [ "$(stub_call_count docker)" = 0 ]
  every_step_ran
}

@test "CDK_DOCKER naming a missing command stops, and --install-docker installs nothing" {
  _install_stub "$PORTAL_BIN/sudo"
  export CDK_DOCKER=dda-test-no-such-runtime
  orchestrate single-account --region eu-west-1 --install-docker
  [ "$status" -eq 2 ]
  [[ "$output" == *"CDK_DOCKER is set to 'dda-test-no-such-runtime', but that command is not on PATH"* ]]
  [ "$(stub_call_count sudo)" = 0 ]
  [ "$(stub_call_count docker)" = 0 ]
  stopped_before_any_change
}

@test "a CDK_DOCKER runtime whose info fails: its error, without the Docker advice" {
  _install_stub "$PORTAL_BIN/finch"
  stub_exit finch 1
  stub_output finch "" "FATA[0000] VM is not running"
  export CDK_DOCKER=finch
  orchestrate single-account --region eu-west-1
  [ "$status" -eq 2 ]
  [[ "$output" == *"'finch info' failed (CDK_DOCKER=finch)"* ]]
  [[ "$output" == *"VM is not running"* ]]
  [[ "$output" != *"systemctl"* ]]
  stopped_before_any_change
}

@test "--install-docker: installs through sudo for this host's distribution, then the deploy goes on" {
  local family
  family="$(bash -c 'source "$PORTAL_TREE/deploy-portal.sh"; _docker_family' < /dev/null)"
  [ -n "$family" ] || skip "--install-docker does not recognize this host's distribution"
  # a non-root id, so every install command reaches the sudo stub; dpkg,
  # systemctl and dnf stubbed so the commands do not depend on the host
  local tool
  for tool in sudo id dpkg systemctl dnf; do _install_stub "$PORTAL_BIN/$tool"; done
  stub_output id 1000
  stub_output dpkg amd64
  orchestrate_without_docker single-account --region eu-west-1 --install-docker
  [ "$status" -eq 0 ]
  output_has_line "Installing Docker (--install-docker, distro family: $family)..."
  [ -n "$(stub_call_index sudo ' install -y docker')" ]
  # the docker the install provided answered `docker info`
  [ "$(stub_call_count docker)" = 1 ]
  every_step_ran
}

# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

@test "usecase topology: no container runtime, leftover or bucket check" {
  missing_stacks "$COMPUTE"
  ecr_repositories_exist dda-detector-export
  component_bucket_absent
  stub_exit docker 1
  orchestrate usecase --portal-account-id 999999999999 --external-id ext-1 --region eu-west-1
  stub_called deploy-account-role.sh
  [ "$(stub_call_count docker)" = 0 ]
  [ "$(stub_call_count_matching aws '^s3api ')" = 0 ]
  [ "$(stub_call_count_matching aws "^cloudformation describe-stacks --stack-name $COMPUTE")" = 0 ]
  [ "$(stub_call_count_matching aws '^(ecr|dynamodb) ')" = 0 ]
}

# ---------------------------------------------------------------------------
# Docker install instructions and commands per distribution (functions only)
# ---------------------------------------------------------------------------

@test "Docker's apt repository: ubuntu for Ubuntu and its derivatives, debian for the rest of the family" {
  local show='printf "%s %s %s" "$(_docker_family)" "$(_docker_apt_repo)" "$(_docker_apt_codename)"'
  distro ubuntu debian jammy jammy
  in_script "$show"
  [ "$output" = "debian ubuntu jammy" ]
  distro linuxmint "ubuntu debian" virginia jammy
  in_script "$show"
  [ "$output" = "debian ubuntu jammy" ]
  distro debian "" bookworm ""
  in_script "$show"
  [ "$output" = "debian debian bookworm" ]
  distro raspbian debian bookworm ""
  in_script "$show"
  [ "$output" = "debian debian bookworm" ]
}

@test "the install instructions follow the distribution" {
  distro debian "" bookworm ""
  in_script '_docker_install_hint 2>&1'
  [[ "$output" == *"https://download.docker.com/linux/debian/gpg"* ]]
  [[ "$output" == *'$(. /etc/os-release && echo "$VERSION_CODENAME") stable'* ]]
  [[ "$output" == *'$(dpkg --print-architecture)'* ]]
  [[ "$output" != *"linux/ubuntu"* ]]
  [[ "$output" != *"@REPO@"* && "$output" != *"@CODENAME@"* ]]
  distro ubuntu debian noble noble
  in_script '_docker_install_hint 2>&1'
  [[ "$output" == *"https://download.docker.com/linux/ubuntu/gpg"* ]]
  [[ "$output" == *'$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable'* ]]
  [[ "$output" == *'`docker.io`'* ]]
  distro amzn "centos rhel fedora" "" ""
  in_script '_docker_install_hint 2>&1'
  [[ "$output" == *"sudo dnf install -y docker"* ]]
  [[ "$output" != *"download.docker.com"* ]]
  distro arch "" "" ""
  in_script '_docker_install_hint 2>&1'
  [[ "$output" == *"https://docs.docker.com/engine/install/"* ]]
  [[ "$output" != *"apt-get"* && "$output" != *"dnf"* ]]
}

@test "--install-docker on Debian: Docker's debian repository, keyed on VERSION_CODENAME" {
  _install_stub "$PORTAL_BIN/sudo"
  distro debian "" bookworm ""
  in_script '_docker_install'
  [ "$status" -eq 0 ]
  local curl tee
  curl="$(stub_call_index sudo '^curl ')"
  tee="$(stub_call_index sudo '^tee /etc/apt/sources.list.d/docker.list')"
  [ -n "$curl" ]
  [ -n "$tee" ]
  stub_call_has_pair "$curl" -fsSL https://download.docker.com/linux/debian/gpg
  [ "$(stub_call_stdin_n "$tee")" = "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" ]
  [ -n "$(stub_call_index sudo '^apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin$')" ]
  [ -n "$(stub_call_index sudo '^usermod -aG docker tester$')" ]
}
