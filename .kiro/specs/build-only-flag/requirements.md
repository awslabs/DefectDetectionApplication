# Requirements Document

## Introduction

The edge component build script (`gdk-component-build-and-publish.sh`) currently always publishes after building: it validates AWS credentials up front, builds the Docker images and packages the Greengrass artifact, then publishes to ECR/S3/Greengrass, tags the component, and offers an optional InferenceUploader build. The script already supports a publish-only mode via the `SKIP_PUBLISH` counterpart convention `SKIP_BUILD=1`. This feature adds the symmetric build-only mode: `SKIP_PUBLISH=1` runs the clean + build + package steps and then exits successfully without publishing, tagging, or the InferenceUploader prompt. Because publishing is skipped and the Docker base images pull from anonymous registries (nvcr.io and public.ecr.aws), the up-front AWS credential check is relaxed to a non-fatal warning in build-only mode.

## Glossary

- **Build_Script**: The shell script `gdk-component-build-and-publish.sh` at the repository root that builds and publishes the LocalServer Greengrass component.
- **Build_Phase**: The steps of the Build_Script that clean build directories, run `gdk component build` (which invokes `build-custom.sh` to build Docker images, run test gates, and package the component zip into `greengrass-build/artifacts/`).
- **Publish_Phase**: The steps of the Build_Script that publish the built artifact (GDK publish or ECR+S3 large-artifact path), tag the component for portal discovery, and offer the optional InferenceUploader build.
- **Build_Only_Mode**: The Build_Script behavior when the environment variable `SKIP_PUBLISH` equals `1`: the Build_Phase runs and the Publish_Phase is skipped.
- **Publish_Only_Mode**: The existing Build_Script behavior when the environment variable `SKIP_BUILD` equals `1`: the Build_Phase is skipped and the Publish_Phase runs against existing artifacts.
- **Credential_Check**: The Build_Script's up-front `aws sts get-caller-identity` validation that currently exits with a non-zero code when AWS credentials are invalid or expired.

## Requirements

### Requirement 1: Build-only mode skips publishing

**User Story:** As a developer, I want to run the edge component build without publishing, so that I can produce and inspect build artifacts locally without pushing anything to AWS.

#### Acceptance Criteria

1. WHEN the Build_Script is invoked with the environment variable `SKIP_PUBLISH` set to `1`, THE Build_Script SHALL execute the Build_Phase and skip the Publish_Phase.
2. WHEN Build_Only_Mode completes the Build_Phase successfully, THE Build_Script SHALL exit with code 0 and print a completion message stating that publishing was skipped and how to publish later (re-run with `SKIP_BUILD=1`).
3. WHILE Build_Only_Mode is active, THE Build_Script SHALL skip the component tagging step and the InferenceUploader prompt.
4. WHEN the Build_Script is invoked with `SKIP_PUBLISH` unset or set to a value other than `1`, THE Build_Script SHALL execute both the Build_Phase and the Publish_Phase (existing behavior preserved).

### Requirement 2: Credential handling in build-only mode

**User Story:** As a developer with an expired AWS session, I want the build-only mode to run without valid AWS credentials, so that I can build artifacts while offline from AWS.

#### Acceptance Criteria

1. WHILE Build_Only_Mode is active, IF the Credential_Check fails, THEN THE Build_Script SHALL print a warning and continue with the Build_Phase.
2. WHILE Build_Only_Mode is inactive, IF the Credential_Check fails, THEN THE Build_Script SHALL exit with a non-zero code (existing behavior preserved).
3. WHILE Build_Only_Mode is active, IF no AWS region is resolvable from configuration or environment variables, THEN THE Build_Script SHALL use a placeholder region in the generated `gdk-config.json` and continue with the Build_Phase.

### Requirement 3: Conflicting mode rejection

**User Story:** As a developer, I want the script to reject contradictory mode combinations, so that I do not silently run a script invocation that does nothing.

#### Acceptance Criteria

1. IF both `SKIP_BUILD` and `SKIP_PUBLISH` are set to `1`, THEN THE Build_Script SHALL print an error explaining the conflict and exit with a non-zero code before performing any build or publish step.

### Requirement 4: Documentation in usage text

**User Story:** As a developer reading the script, I want the usage/help comments to describe the new mode, so that I can discover it without reading the implementation.

#### Acceptance Criteria

1. THE Build_Script SHALL document `SKIP_PUBLISH=1` alongside the existing `SKIP_BUILD=1` convention in the usage comment block, including at least one invocation example.
