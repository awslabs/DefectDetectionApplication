# deploy-portal.sh test harness

Offline `bats` test harness for the one-command portal deploy orchestrator
(`edge-cv-portal/deploy-portal.sh`). No AWS account or network is required.

## Prerequisites

[Bats](https://github.com/bats-core/bats-core) (Bash Automated Testing System),
version 1.2+.

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

## Layout

| File | Purpose |
|------|---------|
| `helpers/stub.sh` | Single generic stub backing every stubbed executable. Records each invocation (name, order, `$PWD`, args, selected env vars, stdin) to `$STUB_TRACE` and emits caller-controlled stdout/stderr/exit codes. Serves canned `aws` responses. |
| `test_helper.bash` | `bats` setup helper: builds a temp `bin/` (prepended to `PATH`) with PATH-shadow stubs for `deploy-account-role.sh`, `deploy-auth.sh`, `deploy-infrastructure.sh`, `deploy-frontend.sh`, `configure-bucket-cors.sh` and for `aws`/`node`/`cdk`/`npx`; seeds a fake `edge-cv-portal` working tree; and provides seed + trace-query helpers. |
| `harness.bats` | Self-tests proving the harness works offline. |

## Using the harness in a test

```bash
load test_helper

setup()    { portal_harness_setup; }
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
- `seed_config_file <usecase|data> <account> [key=value ...]` — fake `*-config.txt`.
- `stub_output <name> <stdout> [stderr]`, `stub_exit <name> <code>` — per-step output/exit.

### Key trace-query helpers

`stub_calls`, `stub_called <name>`, `stub_call_count <name>`, `stub_order_of <name>`,
`stub_pwd_of <name>`, `stub_args_of <name>`, `stub_env_of <name>`,
`stub_env_value <name> <VAR>`, `stub_stdin_of <name>`.

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
