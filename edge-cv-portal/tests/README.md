# deploy-portal.sh test harness

Offline `bats` test harness for the one-command portal deploy orchestrator
(`edge-cv-portal/deploy-portal.sh`). No AWS account or network is required.

## Prerequisites

[Bats](https://github.com/bats-core/bats-core) (Bash Automated Testing System),
version 1.5+ (`registry_enforcement.bats` and the trusted-account, region and usecase/data files call `bats_require_minimum_version 1.5.0` and use `run --separate-stderr`; `harness.bats` and `cdk_context_guard.bats` still run on 1.2). Distribution packages can be older; check `bats --version` and install from source if it is below 1.5.

```bash
# macOS
brew install bats-core
# Debian/Ubuntu
sudo apt-get install bats
# From source
git clone https://github.com/bats-core/bats-core.git && cd bats-core && sudo ./install.sh /usr/local
```

Verify with `bats --version`.

## Running

```bash
cd edge-cv-portal/tests
bats harness.bats      # self-tests for the harness itself
bats .                 # run every *.bats file in this directory
```

No CI workflow runs this suite yet; run it locally before changing a deploy script. `harness.bats` and the trusted-account, region and usecase/data files scrub the AWS environment themselves in their setup (`portal_scrub_aws_env`) and refuse to run unless the tools resolve to the stubs (`portal_require_stubbed_tools`).

## Layout

| File | Purpose |
|------|---------|
| `helpers/stub.sh` | Single generic stub backing every stubbed executable. Records each invocation (name, order, `$PWD`, args, selected env vars, stdin) to `$STUB_TRACE` and emits caller-controlled stdout/stderr/exit codes. Serves canned `aws` responses for a clean account (`ecr describe-repositories` and `dynamodb describe-table` find nothing; `s3api head-bucket` finds the component bucket), plus opt-in ones while their variable is set: the SSM parameter `/dda-portal/trusted-usecase-account-ids` (`STUB_SSM_TRUSTED_FILE`, `STUB_SSM_TRUSTED_GET_STDERR`, `STUB_SSM_TRUSTED_PUT_STDERR`), `cloudformation get-template` (`STUB_CFN_TEMPLATES_DIR`), missing stacks (`STUB_CFN_MISSING_STACKS`), per-profile accounts and regions (`STUB_AWS_PROFILE_ACCOUNTS`, `STUB_AWS_PROFILE_REGIONS`) and stubs that leave stdin unread (`STUB_IGNORE_STDIN_FOR`). `aws configure export-credentials` always fails. |
| `test_helper.bash` | `bats` setup helper: builds a temp `bin/` (prepended to `PATH`) with PATH-shadow stubs for `deploy-account-role.sh`, `deploy-auth.sh`, `deploy-infrastructure.sh`, `deploy-frontend.sh`, `configure-bucket-cors.sh` and for `aws`/`node`/`cdk`/`npx`/`docker`; seeds a fake `edge-cv-portal` working tree; and provides seed + trace-query helpers. |
| `fixtures/trusted-accounts/` | Hand-written stack templates (not synthesized) that grant `sts:AssumeRole` on one account, two accounts (and the same two reordered), the pre-scoping wildcard, or no `DDAPortalAccessRole` at all. |
| `trusted_accounts_helper.bats` | `scripts/portal-trusted-accounts.sh` on its own: the source order (CDK context, `TRUSTED_USECASE_ACCOUNT_IDS`, SSM, the deployed stacks), every fail-closed case, the comparison with the deployed stacks and the create-only store. |
| `trusted_accounts_deploy.bats` | The scripts that use the trusted list: `deploy-portal.sh` single-account (first deploy, re-run, every stop before a step, the store error in the Deployment_Log), the compute redeploys of `deploy-frontend.sh` and `deploy_portal_fixes.sh`, what `deploy-infrastructure.sh` shows without changing the list, and `deploy-auth.sh`'s exported `AWS_PROFILE`. |
| `region_profile.bats` | One region for every call of each deploy script (an exported region before the configured one, a hint when there is none) and `deploy-portal.sh`'s profile and region order. |
| `usecase_data_setup.bats` | UseCase/Data account setup: `npm ci` before the first `npx` call, a bootstrap through that account's own CDK app, and one region throughout. |
| `harness.bats` | Self-tests proving the harness works offline. |
| `registry_enforcement.bats` | `scripts/portal-registry-enforcement.sh` and the three deploy scripts that use it: an unset `PORTAL_REGISTRY_ENFORCED` keeps the deployed value, an explicit one wins, a fresh install passes no enforcement value, and a failed lookup stops the deploy. |
| `cdk_context_guard.bats` | Every script that synthesizes the portal app passes the enforcement context: the pinned set of four scripts, every `cdk bootstrap` naming an app with `-a` or carrying the context itself, `deploy-auth.sh`'s bootstrap and deploy context on a fresh install, a deployed value and a failed lookup, and the provenance and `Data-plane scoping:` lines of the deploy scripts. |

## Using the harness in a test

```bash
load test_helper

setup()    { portal_harness_setup; portal_scrub_aws_env; portal_require_stubbed_tools; }
teardown() { portal_harness_teardown; }

@test "example" {
  set_account_id "123456789012"
  set_region "us-east-1"
  set_bootstrap_version "21"
  set_cfn_output EdgeCVPortalFrontendStack DistributionDomainName d1.cloudfront.net
  seed_config_file usecase 123456789012          # optional *-config.txt fixture
  stub_exit deploy-infrastructure.sh 1           # make a step fail
  stub_output deploy-frontend.sh "done" ""       # control a step's output

  install_deploy_portal                          # copy the real deploy-portal.sh in
  run bash -c 'cd "$PORTAL_TREE" && ./deploy-portal.sh single-account'

  # assert against the recorded trace
  stub_called deploy-auth.sh
  [ "$(stub_pwd_of deploy-auth.sh)" = "$PORTAL_TREE/infrastructure" ]
  [ "$(stub_order_of deploy-account-role.sh)" = "1" ]
}
```

### Key seed helpers

- `set_account_id <id>` / `fail_identity` — `aws sts get-caller-identity`.
- `set_region <region>` — `aws configure get region` (empty simulates none).
- `set_bootstrap_version <n>` / `unset_bootstrap_version` — SSM bootstrap version.
- `set_cfn_output <stack> <key> <value>` — `aws cloudformation describe-stacks`.
- `set_deployed_enforcement [value ...]` / `fail_lambda_list` — the `PORTAL_REGISTRY_ENFORCED` values `aws lambda list-functions` reports (none = nothing deployed), or a failed lookup.
- `seed_config_file <usecase|data> <account> [key=value ...]` — fake `*-config.txt`.
- `stub_output <name> <stdout> [stderr]`, `stub_exit <name> <code>` — per-step output/exit.
- `portal_scrub_aws_env` — unset every AWS identity, credential, region and deploy input from the caller's shell; `AWS_CONFIG_FILE`/`AWS_SHARED_CREDENTIALS_FILE` `/dev/null`, `AWS_EC2_METADATA_DISABLED=true`, `HOME` and `TMPDIR` in the sandbox.
- `portal_require_stubbed_tools [name ...]` — fail unless `aws`, `node`, `cdk`, `npx`, `docker` and the named tools resolve to the stubs.
- `install_real_if_present <path>` — copy a real script into the fake tree; a file the checkout does not have is skipped.
- `stub_tools_ignore_stdin` — the tool stubs leave stdin unread, so answers piped to a real `deploy-account-role.sh` reach its prompts.
- `trusted_param_absent` / `trusted_param_found <ids>` / `trusted_param_read_fails [error]` / `trusted_param_write_fails [error]` / `trusted_param_value` — the SSM parameter `/dda-portal/trusted-usecase-account-ids`.
- `portal_not_deployed` / `deploy_stack_template <stack> <fixture>` / `deploy_portal_stacks <compute> [<nodedesigner>|-] [<syntheticdata>|-]` / `deny_stack_template <stack>` — the templates `cloudformation get-template` serves.
- `missing_stacks <stack ...>` — `cloudformation describe-stacks` says these do not exist.
- `set_profile_account <profile> <id>` / `set_profile_region <profile> <region>` — what `sts` and `configure get region` answer under `AWS_PROFILE=<profile>`.
- `set_cdk_json_context <json>` / `set_cdk_context_json <json>` / `set_user_cdk_json <json>` — the CDK context files.
- `seed_frontend_stack_outputs` — the stack outputs `deploy-frontend.sh` and the Deployment Summary read.

### Key trace-query helpers

`stub_calls`, `stub_called <name>`, `stub_call_count <name>`, `stub_order_of <name>`,
`stub_pwd_of <name>`, `stub_args_of <name>`, `stub_env_of <name>`,
`stub_env_value <name> <VAR>`, `stub_stdin_of <name>`.

Per call, rather than the first call of a name:

- `stub_trace_table` — one row per call: index, name, `$PWD`, args and env (space-joined).
- `stub_call_index <name> <ERE>` / `stub_call_count_matching <name> <ERE>` — the first matching call's index, and how many calls match (the ERE is matched against the space-joined args).
- `stub_call_args_n <n>` / `stub_call_env_n <n>` / `stub_call_env_value_n <n> <VAR>` / `stub_call_pwd_n <n>` / `stub_call_has_pair <n> <option> <value>` — one call's args, env and `$PWD`.
- `stub_env_values_all <name> <VAR>` — the value of `VAR` in every call of `<name>` (`<unset>` when not set).
- `stub_regions_used` — the region each regional `aws`/`npx`/`cdk` call would use.
- `output_has_line <text>` / `log_has_line <text>` / `log_line_of <text>` — exact lines of the last `run` output and of `$PORTAL_SANDBOX/deploy.log`.

## Optional npm alias (`deploy:portal`)

The canonical entrypoint is the bash script `edge-cv-portal/deploy-portal.sh`,
matching the existing deploy scripts and the on-device/steering conventions.

There is currently **no portal-root `package.json`** (neither at the repository
root nor under `edge-cv-portal/` — only `edge-cv-portal/frontend` and
`edge-cv-portal/infrastructure` carry their own specialized CDK/build
manifests). Because no suitable root manifest exists, **no npm alias is defined
and none is created**; introducing a manifest solely for an alias is out of
scope (Req 1.1).

If a portal-root `package.json` is ever introduced, add a thin passthrough entry
only — it must carry no behavior of its own:

```jsonc
{
  "scripts": {
    // path is relative to the repository root that holds this package.json
    "deploy:portal": "./edge-cv-portal/deploy-portal.sh"
  }
}
```

Invoke it as:

```bash
npm run deploy:portal -- <TOPOLOGY> [options]
# e.g. npm run deploy:portal -- single-account
```

All deployment behavior stays in `deploy-portal.sh`; the alias is a passthrough.
