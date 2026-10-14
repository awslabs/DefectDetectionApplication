# Requirements Document

## Introduction

Deploying the Edge CV Portal today requires an operator to run several scripts by hand, in the right order, from the `edge-cv-portal` directory: `deploy-account-role.sh` (interactive; sets up IAM roles and, for multi-account topologies, the UseCase/Data account CDK stacks), `infrastructure/deploy-auth.sh` (Cognito auth stack), `deploy-infrastructure.sh` (the main CDK stacks), and `deploy-frontend.sh` (builds and publishes the frontend, wires CloudFront domain into the backend for CORS). Getting the ordering, prerequisites, and cross-step values right is error-prone.

This feature adds a single, dead-simple command — the Portal_Deploy_Command — that deploys the entire portal end to end by orchestrating the existing scripts in the correct order. It PRESERVES the existing interactive setup script (`deploy-account-role.sh`) rather than replacing it: the one-command entrypoint invokes that script and reuses its question-asking flow instead of duplicating the prompts. The individual scripts continue to work standalone, unchanged in observable behavior. The orchestrator adds prerequisite checks, safe re-runs, end-of-run output of key values (portal URL, role ARNs, external IDs), and clear failure reporting. It supports the interactive path by default and an optional non-interactive mode for CI, driven by flags and environment variables.

## Glossary

- **Portal**: The Edge CV Portal (`edge-cv-portal`) application: React frontend, Lambda-backed REST API, and CDK-managed AWS infrastructure.
- **Portal_Deploy_Command**: The new single command (a shell script under `edge-cv-portal`, invoked as one command by the operator) that orchestrates the full portal deployment end to end.
- **Operator**: The person with shell access and AWS credentials who runs the Portal_Deploy_Command.
- **Account_Role_Script**: The existing interactive script `edge-cv-portal/deploy-account-role.sh` that prompts for deployment type (single-account, usecase, or data), Portal Account ID, External ID, and data bucket names, and creates IAM roles and/or deploys the UseCase/Data account CDK stacks.
- **Auth_Script**: The existing script `edge-cv-portal/infrastructure/deploy-auth.sh` that deploys the `EdgeCVPortalAuthStack` (Cognito) and supports `--sso`, `--profile`, and `--region` options.
- **Infrastructure_Script**: The existing script `edge-cv-portal/deploy-infrastructure.sh` that builds and deploys the main CDK stacks with `cdk deploy --all`.
- **Frontend_Script**: The existing script `edge-cv-portal/deploy-frontend.sh` that generates `config.json` from CDK outputs, builds the frontend, publishes to S3, invalidates CloudFront, and redeploys the compute stack with the CloudFront domain for CORS.
- **Cors_Script**: The existing script `edge-cv-portal/configure-bucket-cors.sh` that applies CORS rules to an S3 bucket.
- **Deployment_Step**: A single orchestrated unit of work run by the Portal_Deploy_Command that invokes one existing script (or a well-defined portion of one).
- **Deployment_Topology**: The selected account arrangement for the deployment: `single-account`, `usecase`, or `data`, matching the choices offered by the Account_Role_Script.
- **Interactive_Mode**: The default execution mode in which the Portal_Deploy_Command lets the Account_Role_Script (and any other interactive step) prompt the Operator for input on the terminal.
- **Non_Interactive_Mode**: An execution mode, selected by flag or environment variable, in which the Portal_Deploy_Command supplies all required inputs from flags and environment variables and performs no interactive prompting.
- **Prerequisite_Check**: A verification the Portal_Deploy_Command performs before making any changes: AWS CLI present, AWS credentials valid, AWS region resolved, Node.js and CDK available, and CDK bootstrap present and at the required version.
- **CDK_Bootstrap_Version**: The version number of the CDK bootstrap stack in the target account/region; the existing scripts require version 21 or higher.
- **Deployment_Summary**: The end-of-run report printed by the Portal_Deploy_Command containing the key outputs of the deployment (Portal URL, created role ARNs, External IDs, and per-step outcomes).
- **Deployment_Log**: The file to which the Portal_Deploy_Command records the output of every Deployment_Step.

## Requirements

### Requirement 1: Single Command Orchestrates the Full Deployment

**User Story:** As an operator, I want one command that deploys the entire portal, so that I do not have to run and order the individual scripts myself.

#### Acceptance Criteria

1. THE Portal_Deploy_Command SHALL deploy the Portal end to end by invoking the Account_Role_Script, the Auth_Script, the Infrastructure_Script, and the Frontend_Script as Deployment_Steps within a single command invocation.
2. THE Portal_Deploy_Command SHALL execute the Deployment_Steps in an order that satisfies each step's input dependencies, deploying the Auth_Script and Infrastructure_Script before the Frontend_Script so that the Frontend_Script can read the auth and compute stack outputs it requires.
3. WHEN every Deployment_Step completes successfully, THE Portal_Deploy_Command SHALL exit with exit code zero.
4. WHERE the selected Deployment_Topology is `single-account`, THE Portal_Deploy_Command SHALL run the Account_Role_Script single-account role setup before the Deployment_Step that deploys the compute stack, so that the IAM roles the compute stack and portal depend on exist first.

### Requirement 2: Preserve and Reuse the Interactive Setup Script

**User Story:** As an operator familiar with the existing setup, I want the one-command deploy to reuse the current question-asking script, so that the interactive setup I rely on is preserved rather than replaced.

#### Acceptance Criteria

1. THE Portal_Deploy_Command SHALL invoke the existing Account_Role_Script to perform account and role setup rather than reimplementing the Account_Role_Script prompts.
2. WHILE running in Interactive_Mode, THE Portal_Deploy_Command SHALL allow the Account_Role_Script to prompt the Operator for the deployment type, Portal Account ID, External ID, and data bucket names on the terminal, and SHALL pass the Operator's responses through to the Account_Role_Script.
3. THE Portal_Deploy_Command SHALL leave the Account_Role_Script, Auth_Script, Infrastructure_Script, Frontend_Script, and Cors_Script runnable as standalone commands with the same observable behavior they have when the Portal_Deploy_Command is not used.
4. WHERE the Account_Role_Script reuses an External ID from an existing `*-config.txt` file on a re-deploy, THE Portal_Deploy_Command SHALL preserve that behavior by not overriding the External ID the Account_Role_Script resolves.

### Requirement 3: Prerequisite Verification Before Changes

**User Story:** As an operator, I want the command to check prerequisites before it changes anything, so that I fail fast instead of leaving a half-configured account.

#### Acceptance Criteria

1. WHEN the Portal_Deploy_Command starts, THE Portal_Deploy_Command SHALL complete the Prerequisite_Check before invoking any Deployment_Step that creates or modifies AWS resources.
2. IF the AWS CLI is not resolvable on the PATH, THEN THE Portal_Deploy_Command SHALL write a message to standard error identifying the missing AWS CLI, SHALL not invoke any Deployment_Step, and SHALL exit with a non-zero exit code.
3. IF the AWS account identity check does not return a valid AWS account identifier, THEN THE Portal_Deploy_Command SHALL write a message to standard error identifying the credential validation failure, SHALL not invoke any Deployment_Step, and SHALL exit with a non-zero exit code.
4. IF no AWS region is resolved from the AWS configuration or environment, THEN THE Portal_Deploy_Command SHALL write a message to standard error identifying the missing region, SHALL not invoke any Deployment_Step, and SHALL exit with a non-zero exit code.
5. IF Node.js or the CDK command is not resolvable on the PATH, THEN THE Portal_Deploy_Command SHALL write a message to standard error identifying which tool is missing, SHALL not invoke any Deployment_Step, and SHALL exit with a non-zero exit code.
6. IF the CDK_Bootstrap_Version is absent or is a numeric value less than 21, THEN THE Portal_Deploy_Command SHALL run CDK bootstrap for the resolved AWS account identifier and AWS region before invoking any Deployment_Step that deploys a CDK stack.
7. IF the CDK bootstrap invocation exits with a non-zero exit code, THEN THE Portal_Deploy_Command SHALL write a message to standard error identifying the bootstrap failure, SHALL not invoke any Deployment_Step that deploys a CDK stack, and SHALL exit with a non-zero exit code.
8. WHEN the Prerequisite_Check completes without failure, THE Portal_Deploy_Command SHALL print the resolved AWS account identifier and the resolved AWS region to standard output before invoking the first Deployment_Step that creates or modifies AWS resources.

### Requirement 4: Deployment Topology Selection

**User Story:** As an operator, I want to choose whether I am deploying a single-account portal or a multi-account (portal/usecase/data) topology, so that the command deploys the right set of resources.

#### Acceptance Criteria

1. WHILE running in Interactive_Mode, THE Portal_Deploy_Command SHALL let the Operator select the Deployment_Topology through the Account_Role_Script menu whose options are `single-account`, `usecase`, and `data`.
2. WHERE the Operator provides the Deployment_Topology as a command-line value, THE Portal_Deploy_Command SHALL pass that value to the Account_Role_Script as its positional deployment-type argument.
3. IF a Deployment_Topology value other than `single-account`, `usecase`, or `data` is supplied, THEN THE Portal_Deploy_Command SHALL reject the value with an error identifying the invalid Deployment_Topology and SHALL exit with a non-zero exit code without running any Deployment_Step.
4. WHERE the selected Deployment_Topology is `usecase` or `data`, THE Portal_Deploy_Command SHALL run the Account_Role_Script cross-account setup for that topology and SHALL NOT run the Frontend_Script or Infrastructure_Script for that account.

### Requirement 5: Non-Interactive Mode for CI

**User Story:** As a platform engineer, I want a non-interactive mode driven by flags and environment variables, so that I can run the deployment in CI without a human answering prompts.

#### Acceptance Criteria

1. WHERE Non_Interactive_Mode is selected by a command-line flag or environment variable, THE Portal_Deploy_Command SHALL obtain the Deployment_Topology, Portal Account ID, External ID, and the data bucket names required for the selected Deployment_Topology exclusively from command-line flags and environment variables.
2. WHERE Non_Interactive_Mode is selected, THE Portal_Deploy_Command SHALL perform no interactive prompting on any terminal or standard input stream for any Deployment_Step.
3. IF Non_Interactive_Mode is selected and one or more inputs required for the selected Deployment_Topology are missing or empty, THEN THE Portal_Deploy_Command SHALL report every missing required input by name in a single failure message and SHALL exit with a non-zero exit code without running any Deployment_Step that creates or modifies AWS resources.
4. WHERE Non_Interactive_Mode is selected, THE Portal_Deploy_Command SHALL invoke the Account_Role_Script using its non-interactive positional-argument path rather than its interactive menu.
5. WHERE Non_Interactive_Mode is selected and the same input is supplied by both a command-line flag and an environment variable, THE Portal_Deploy_Command SHALL use the command-line flag value.
6. WHERE the Auth_Script is run with SSO enabled through the Portal_Deploy_Command, THE Portal_Deploy_Command SHALL pass the SSO configuration to the Auth_Script through the Auth_Script's supported `--sso` option and the Auth_Script's supported SSO environment variables.

### Requirement 6: Failure Handling and Resumability

**User Story:** As an operator, I want a failed step to stop the deployment with a clear message and let me resume, so that I do not have to restart the entire deployment from scratch or corrupt the account state.

#### Acceptance Criteria

1. IF a Deployment_Step exits with a non-zero exit code, THEN THE Portal_Deploy_Command SHALL stop before starting any subsequent Deployment_Step, SHALL print to standard output the name of the failed Deployment_Step and the Deployment_Log location, and SHALL exit with a non-zero exit code.
2. IF a Deployment_Step exits with a non-zero exit code, THEN THE Portal_Deploy_Command SHALL retain the AWS resource changes made by every Deployment_Step that completed successfully earlier in the same run and SHALL NOT roll those changes back.
3. WHEN the Portal_Deploy_Command is re-run after a previous run stopped at a failed Deployment_Step, and every Deployment_Step in the re-run exits with exit code zero, THE Portal_Deploy_Command SHALL exit with exit code zero and SHALL leave the Portal reachable at the same Portal URL and with the same created role ARNs as a run in which every Deployment_Step succeeded on its first attempt.
4. WHEN a Deployment_Step starts, THE Portal_Deploy_Command SHALL print the name of that Deployment_Step to standard output.
5. WHEN a Deployment_Step runs, THE Portal_Deploy_Command SHALL record that Deployment_Step's standard output and standard error in the Deployment_Log.
6. WHEN the Portal_Deploy_Command starts, THE Portal_Deploy_Command SHALL print the Deployment_Log location to standard output before running the first Deployment_Step.

### Requirement 7: Surface Deployment Outputs

**User Story:** As an operator, I want the key outputs shown at the end, so that I can access the portal and register accounts without hunting through CloudFormation.

#### Acceptance Criteria

1. WHEN every Deployment_Step completes successfully and the selected Deployment_Topology is `single-account`, THE Portal_Deploy_Command SHALL print the Deployment_Summary to standard output after the final Deployment_Step, and the Deployment_Summary SHALL include the Portal URL served by the CloudFront distribution.
2. WHEN the Account_Role_Script creates one or more IAM roles or cross-account roles during the run, THE Portal_Deploy_Command SHALL include each created role's ARN in the Deployment_Summary.
3. WHERE the Deployment_Topology is `usecase` or `data`, THE Portal_Deploy_Command SHALL include the External ID and the cross-account role ARN in the Deployment_Summary so that the Operator can register the account in the Portal.
4. THE Portal_Deploy_Command SHALL exclude AWS credential secret material, including AWS secret access keys and AWS session tokens, from the Deployment_Summary, standard output, and the Deployment_Log.
5. WHEN the Deployment_Summary is printed, THE Portal_Deploy_Command SHALL include, for every Deployment_Step defined for the selected Deployment_Topology, that step's outcome as exactly one of success, failure, or not-run.
6. IF the Portal URL cannot be resolved from the CloudFront distribution outputs after the Frontend_Script Deployment_Step completes, THEN THE Portal_Deploy_Command SHALL include in the Deployment_Summary an indication that the Portal URL is unavailable together with the Deployment_Log location, and SHALL exit with a non-zero exit code.

### Requirement 8: Do Not Regress Existing Scripts

**User Story:** As a maintainer, I want the existing individual scripts to keep working, so that people who prefer the step-by-step flow are not broken by the new command.

#### Acceptance Criteria

1. THE Portal_Deploy_Command SHALL invoke the Account_Role_Script, Auth_Script, Infrastructure_Script, Frontend_Script, and Cors_Script while leaving each of those script files' contents byte-for-byte identical to their contents before the invocation.
2. WHEN an Operator runs the Account_Role_Script, Auth_Script, Infrastructure_Script, Frontend_Script, or Cors_Script directly with its documented arguments, THE invoked script SHALL produce the same exit code, the same set of created or modified files, and the same set of created or modified AWS resources that it produced when run before the Portal_Deploy_Command existed.
3. THE Portal_Deploy_Command SHALL invoke each existing script from the same working directory the Operator uses when running that script standalone, so that the script's relative-path operations resolve to the same files as a standalone run.
4. WHERE the Frontend_Script requires the `TRUSTED_USECASE_ACCOUNT_IDS`, `DATA_BUCKET_ALLOWLIST`, or CloudFront-domain inputs it reads today, THE Portal_Deploy_Command SHALL pass those inputs to the Frontend_Script with values identical to the values the Frontend_Script reads when run standalone, so that the Frontend_Script's compute-stack redeploy produces the same set of created or modified AWS resources as a standalone run.
