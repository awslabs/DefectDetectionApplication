# Requirements Document

## Introduction

The Custom Node Designer (spec: custom-node-designer) creates, generates, and imports GStreamer plugins as Plugin_Records that are built per Target_Architecture, simulated, security-reviewed, and published as Plugin_Components. The Custom Node Code Assist (spec: custom-node-code-assist) attaches a Bedrock-backed Code_Assistant to the Python hook editor of the create and generate wizards. Both features stop at creation time: once a Plugin_Record exists its source can only be viewed, the Code_Assistant cannot see why a build or simulation failed and cannot touch the C or meson files where scaffold build failures usually originate, the plugin source lives only in portal storage, and the Target_Architectures chosen at creation cannot be extended without hand-driving a build round whose result never reaches the deployable Plugin_Component.

This feature closes those gaps on the Plugin_Record detail page:

1. **Source_Editor** — edit every file of a version's Source_Tree after creation, in place while the version is in `dev` and as a new version otherwise, with stale-build tracking and a one-click rebuild.
2. **Git sync** — link a Use_Case to a GitHub or GitLab repository through a token-based Git_Connection whose token lives in AWS Secrets Manager, push a version's Source_Tree to the repository as a commit, and pull repository changes back into the portal (bidirectional; the portal remains the build and lifecycle authority).
3. **Diagnostic-aware code assistance** — hand the failing architecture's build log or a simulation failure to the Code_Assistant so the configured Bedrock model proposes a fix for whichever Source_Tree file needs it (Python hook, C skeleton, meson build configuration), plus a paste-your-own-error path on every Code_Editing_Surface.
4. **Architecture_Addition** — add Target_Architectures to an existing version, build only the new ones, and republish the Plugin_Component as a patch version so the new architectures become deployable. This includes a new arm64 JetPack 7 plugin build target, and it fixes the existing gap in which a rebuild after the Plugin_Component is registered never reaches devices.

## Glossary

Terms inherited from spec custom-node-designer and spec custom-node-code-assist keep their meaning there: Portal, LocalServer, Node_Designer, Plugin_Record, Plugin_Scaffold, Frame_Processing_Hook, Plugin_Build_Service, Plugin_Artifact, Plugin_Library, Plugin_Component, Plugin_Importer, Plugin_Simulator, Component_Packager, Workflow_Component, Deployment_Service, Lifecycle_State, Target_Architecture, Test_Device, Use_Case, UseCaseAdmin, PortalAdmin, Code_Assistant, Code_Assist_Generator, Code_Editing_Surface, Node_Contract, Bedrock_Configuration.

- **Plugin_Version**: One version item of a Plugin_Record (`plugin_id` + integer `version`), carrying its own Source_Tree, Lifecycle_State, security review decision, per-architecture Plugin_Artifacts, and Plugin_Component pointer.
- **Source_Tree**: The complete set of source files of one Plugin_Version, stored as individual text objects under the version's `plugin-sources/{usecase_id}/{plugin_id}/{version}/` S3 prefix.
- **Source_Revision**: A monotonically increasing integer on a Plugin_Version, incremented by every change to its Source_Tree (save, pull, architecture addition that adds files). A Plugin_Artifact records the Source_Revision it was built from.
- **Stale_Artifact**: A Plugin_Artifact whose recorded Source_Revision is lower than the Plugin_Version's current Source_Revision — the binary no longer corresponds to the stored source.
- **Source_Editor**: The Portal capability introduced by this feature: the tabbed, editable view of a Plugin_Version's Source_Tree on the Plugin_Record detail page, with save, save-as-new-version, add-file, delete-file, and rebuild actions.
- **Detail_Page**: The existing Plugin_Record detail page of the Node_Designer (`/node-designer/plugins/{pluginId}`), which shows lifecycle state, per-architecture build status, and actions for one Plugin_Version.
- **Git_Connection**: A per-Use_Case record naming a Git provider (GitHub or GitLab, including self-hosted GitLab), an HTTPS repository URL, a default branch, and a reference to a Git_Credential. Managed by UseCaseAdmins of the Use_Case and PortalAdmins.
- **Git_Credential**: A personal or project access token authorizing HTTPS Git operations against the Git_Connection's repository. Stored exclusively in AWS Secrets Manager; the Portal stores only the secret's ARN.
- **Git_Sync_Service**: The Portal backend component introduced by this feature (a Lambda handler plus a dedicated CodeBuild project with Git available) that verifies Git_Connections and executes Push and Pull operations.
- **Sync_Operation**: One asynchronous run of the Git_Sync_Service — kind `verify`, `push`, or `pull` — with a status (`queued`, `running`, `succeeded`, `failed`), a result, and on failure a Failure_Category and a credential-redacted log excerpt.
- **Failure_Category**: The machine-readable classification of a failed Sync_Operation: `authentication` (the Git_Credential was rejected), `not_found` (repository, branch, ref, or Git_Link path does not exist), `unreachable` (host unresolvable or connection timed out), `diverged` (the repository changed under the Git_Link path since the last sync), `push_rejected` (the remote refused the push after retry), `invalid_source` (the pulled tree is empty, too large, or fails scaffold validation), or `internal` (any other failure).
- **Git_Link**: The association of a Plugin_Version with a Git_Connection plus a branch and a Repository_Path. New Plugin_Versions inherit the Git_Link of the version they are created from.
- **Repository_Path**: The directory inside the repository (relative to its root) that mirrors a Plugin_Version's Source_Tree. A Push replaces the contents of exactly this directory; a Pull reads exactly this directory.
- **Push**: A Sync_Operation that clones the Git_Link's branch, replaces the Repository_Path contents with the Plugin_Version's Source_Tree plus a Sync_Manifest, commits, and pushes.
- **Pull**: A Sync_Operation that fetches a ref of the Git_Link's repository and installs the Repository_Path contents as the Source_Tree of the same Plugin_Version (in-place mode, `dev` only) or of a newly created Plugin_Version (new-version mode).
- **Sync_Manifest**: The `dda-plugin.json` file the Push writes into the Repository_Path recording the plugin id, version, kind, name, scaffold declaration (when present), Source_Revision, and the pushing user and time. It is never stored as part of a Source_Tree.
- **Divergence_Guard**: The Push check that compares the Repository_Path contents at the remote branch head with the contents at the commit recorded by the last successful Push or Pull of the Plugin_Record; a difference means someone changed the mirrored directory in the repository since the portal last synchronized.
- **Build_Diagnostics**: The compiler and build-system error excerpt recorded on a failed Plugin_Artifact build (`logTail`), attributed to one Target_Architecture.
- **Simulation_Diagnostics**: The failure message and plugin error output recorded on a failed Plugin_Simulator run.
- **Diagnostic_Context**: The optional error material attached to a Code_Assistant request: kind (`build`, `simulation`, or `user`), the Target_Architecture for build kind, and the error text.
- **Plugin_Source_Contract**: The Node_Contract introduced by this feature for Source_Tree files that are not the Frame_Processing_Hook (C skeleton source, meson build configurations, README): the Code_Assist_Generator returns the complete replacement content of one Source_Tree file with no entry-point validation.
- **Target_File**: The Source_Tree file a Code_Assistant response applies to. It defaults to the file whose editor tab is active and may be redirected by the Code_Assist_Generator to another file of the same Source_Tree.
- **Architecture_Addition**: The operation that adds one or more Target_Architectures to an existing Plugin_Version, renders any missing per-architecture build configuration for scaffold-kind records, and builds only the added architectures.
- **Component_Revision**: The patch number of a Plugin_Component version. The Plugin_Component of Plugin_Version `v` is published as `v.0.n`, where `n` starts at 0 and increments each time the set of built Plugin_Artifacts changes (rebuild after an edit, Architecture_Addition, retry that adds a previously failed architecture).
- **Build_Target_Registry**: The set of Target_Architectures for which the Plugin_Build_Service has a configured build project (today x86_64, x86_64_nvidia, arm64_jp4, arm64_jp5, arm64_jp6; extended by this feature with arm64_jp7).

## Requirements

### Requirement 1: Post-Creation Source Editing

**User Story:** As a computer vision engineer, I want to edit the source files of a custom node after it has been created, save my changes, and rebuild, so that I can fix and evolve a plugin without recreating it from scratch.

#### Acceptance Criteria

1. WHEN a user opens the Detail_Page of a Plugin_Version, THE Source_Editor SHALL present every file of the version's Source_Tree in a tabbed editor, loaded through a single request that returns the contents of all text files of the Source_Tree.
2. WHILE a Source_Tree file exceeds 512 KiB or is not valid UTF-8 text, THE Source_Editor SHALL list the file with its size as read-only and SHALL NOT offer editing for that file.
3. THE Source_Editor SHALL allow a user to add a new file at a relative path confined to the Source_Tree and to delete an existing file, and IF a submitted path escapes the Source_Tree or is empty, THEN THE Portal SHALL reject the save identifying the invalid path and SHALL change nothing.
4. WHILE the Plugin_Version's Lifecycle_State is `dev`, THE Source_Editor SHALL permit saving edits in place: THE Portal SHALL write the submitted files, delete the files the user deleted, increment the Source_Revision, and leave every other Source_Tree file unchanged.
5. WHILE the Plugin_Version's Lifecycle_State is `test` or `prod`, THE Portal SHALL reject an in-place save with an error identifying the Lifecycle_State, and THE Source_Editor SHALL offer "Save as new version" instead.
6. WHEN a user saves as new version, THE Portal SHALL create a new Plugin_Version (numbered one above the Plugin_Record's latest version, Lifecycle_State `dev`, security review pending) whose Source_Tree is a complete copy of the edited version's Source_Tree with the user's edits, additions, and deletions applied, and whose Git_Link, requested Target_Architectures, and DeepStream flag are copied from the edited version, and SHALL record the originating version in the new version's provenance.
7. WHEN a save (in place or as new version) is requested for a scaffold-kind Plugin_Version, THE Portal SHALL validate the resulting Source_Tree against the recorded scaffold declaration, and IF the Source_Tree is not buildable, THEN THE Portal SHALL reject the save listing every defect and SHALL change nothing.
8. WHEN a save increments the Source_Revision, THE Portal SHALL mark every existing Plugin_Artifact of that Plugin_Version as a Stale_Artifact, and THE Detail_Page SHALL show each stale architecture as requiring a rebuild.
9. WHEN a Plugin_Artifact build is started, THE Plugin_Build_Service SHALL record the Plugin_Version's current Source_Revision on the per-architecture artifact entry, and a Plugin_Artifact SHALL be reported stale if and only if its recorded Source_Revision is lower than the version's current Source_Revision.
10. WHEN a save succeeds, THE Source_Editor SHALL offer a single action that starts builds for the version's requested Target_Architectures, and THE Portal SHALL NOT start builds without that explicit action.
11. WHILE the Source_Editor holds unsaved edits, THE Source_Editor SHALL display an unsaved indicator and SHALL ask for confirmation before navigating away from the Detail_Page.
12. IF a save fails for any reason, THEN THE Source_Editor SHALL keep the user's unsaved edits in the editor and SHALL display the error.
13. THE Source_Editor SHALL render the Code_Assistant on every editable Source_Tree file tab as specified in Requirement 5.

### Requirement 2: Git Connections and Credentials

**User Story:** As a use case administrator, I want to connect my use case to a GitHub or GitLab repository using an access token that the portal keeps secret, so that plugin source can be synchronized with the repository my team already uses.

#### Acceptance Criteria

1. WHEN a UseCaseAdmin of a Use_Case or a PortalAdmin creates a Git_Connection, THE Portal SHALL collect a display name, a provider (`github` or `gitlab`), an HTTPS repository URL, a default branch, and a Git_Credential token.
2. IF the repository URL does not use the `https` scheme or is not a syntactically valid URL, THEN THE Portal SHALL reject the Git_Connection identifying the URL and SHALL store nothing.
3. WHEN a Git_Connection is created, THE Portal SHALL store the Git_Credential token in AWS Secrets Manager under a secret dedicated to that Git_Connection and SHALL record only the secret's ARN on the Git_Connection.
4. THE Portal SHALL NOT return a Git_Credential token in any API response, SHALL NOT write it to any log or audit entry, and SHALL NOT pass it to the Git_Sync_Service execution environment as a plaintext environment variable; the execution environment SHALL resolve the token from Secrets Manager itself.
5. WHEN a Git_Connection is created or its token or repository URL is updated, THE Git_Sync_Service SHALL run a `verify` Sync_Operation that performs an authenticated read-only listing of the repository's references, and THE Portal SHALL record the Git_Connection's verification status as `verifying` until the operation settles and then `verified` or `failed` with the Failure_Category.
6. WHILE a Git_Connection's verification status is `failed` or `verifying`, THE Portal SHALL reject Push and Pull requests through that Git_Connection identifying the verification status.
7. WHEN a UseCaseAdmin or PortalAdmin updates a Git_Connection's token, THE Portal SHALL replace the secret's value so that the previous token is no longer used by any subsequent Sync_Operation.
8. WHEN a UseCaseAdmin or PortalAdmin deletes a Git_Connection, THE Portal SHALL schedule the secret for deletion, remove the Git_Connection, and leave Plugin_Versions linked to it with their recorded sync provenance intact, and THE Detail_Page SHALL show such a Git_Link as disconnected with Push and Pull unavailable.
9. THE Portal SHALL permit users holding `node-designer:read` in the Use_Case to view a Git_Connection's name, provider, repository URL, default branch, and verification status, and SHALL permit only UseCaseAdmins of the Use_Case and PortalAdmins to create, update, verify, or delete Git_Connections.
10. WHEN a Git_Connection is created, updated, verified, or deleted, THE Portal SHALL record the action, the acting user, the Use_Case, and a timestamp in the existing audit log without the token.

### Requirement 3: Push Plugin Source to the Repository

**User Story:** As a computer vision engineer, I want to push a plugin version's source to our Git repository, so that it is version-controlled, reviewable by my team, and editable in a regular IDE.

#### Acceptance Criteria

1. WHEN a user with `node-designer:manage` links a Plugin_Version to a Git_Connection, THE Portal SHALL record a Git_Link with a branch (defaulting to the Git_Connection's default branch) and a Repository_Path (defaulting to the plugin's sanitized name), and SHALL reject a Repository_Path that is absolute, empty, or contains a `..` segment.
2. WHEN a user with `node-designer:manage` requests a Push for a linked Plugin_Version, THE Git_Sync_Service SHALL create a Sync_Operation of kind `push`, respond immediately with the operation identifier, and execute the operation asynchronously.
3. WHEN a Push executes, THE Git_Sync_Service SHALL clone the Git_Link's branch at its current head, replace the contents of the Repository_Path with the Plugin_Version's complete Source_Tree plus the Sync_Manifest, remove files under the Repository_Path that are absent from the Source_Tree, commit with a message identifying the plugin name, version, Source_Revision, and pushing user, and push the commit to the branch.
4. THE Git_Sync_Service SHALL NOT modify any file outside the Repository_Path and SHALL NOT force-push.
5. IF the Git_Link's branch does not exist in the repository, THEN THE Git_Sync_Service SHALL create the branch from the Git_Connection's default branch (or as the repository's first commit when the repository is empty) before committing.
6. WHEN a Push finds the Repository_Path contents at the branch head identical to the Source_Tree plus Sync_Manifest, THE Git_Sync_Service SHALL complete the Sync_Operation as succeeded with the current head commit recorded and SHALL create no commit.
7. WHEN the Divergence_Guard detects that the Repository_Path changed in the repository since the Plugin_Record's last recorded sync commit, and the Push request did not set `force`, THE Git_Sync_Service SHALL fail the Sync_Operation with Failure_Category `diverged` listing the changed files and SHALL leave the repository unchanged.
8. WHEN a Push request sets `force`, THE Git_Sync_Service SHALL skip the Divergence_Guard and replace the Repository_Path contents regardless of intervening repository changes, and THE Portal SHALL record `force` on the Sync_Operation.
9. IF the remote rejects the push because the branch advanced during the operation, THEN THE Git_Sync_Service SHALL re-fetch the branch head, re-apply the Repository_Path replacement, and retry the push once, and IF the retry is rejected, THEN the Sync_Operation SHALL fail with Failure_Category `push_rejected`.
10. WHEN a Push succeeds, THE Portal SHALL record on the Plugin_Version the resolved commit SHA, branch, Repository_Path, Source_Revision, pushing user, and timestamp as the last sync, and SHALL record the same on the Sync_Operation result.
11. THE Portal SHALL permit a Push for a Plugin_Version in any Lifecycle_State and SHALL NOT change the Plugin_Version's Lifecycle_State, security review decision, Source_Revision, or Plugin_Artifacts as a result of a Push.
12. IF a Push or Pull is requested for a Plugin_Version that already has a Sync_Operation in status `queued` or `running`, THEN THE Portal SHALL reject the request identifying the in-flight operation.
13. IF a Push is requested for a Plugin_Version whose Source_Tree contains unsaved edits in the Source_Editor, THEN THE Source_Editor SHALL ask the user to save or discard the edits before starting the Push.

### Requirement 4: Pull Repository Changes into the Portal

**User Story:** As a computer vision engineer, I want to pull changes my team committed to the repository back into the portal, so that code edited in an IDE can be built, simulated, reviewed, and deployed through the portal.

#### Acceptance Criteria

1. WHEN a user with `node-designer:manage` requests a Pull for a linked Plugin_Version, THE Portal SHALL accept an optional ref (branch, tag, or commit SHA; defaulting to the Git_Link's branch head) and a mode (`in_place` or `new_version`), create a Sync_Operation of kind `pull`, respond immediately with the operation identifier, and execute the operation asynchronously.
2. WHILE the Plugin_Version's Lifecycle_State is `test` or `prod`, THE Portal SHALL reject a Pull in `in_place` mode identifying the Lifecycle_State and SHALL accept only `new_version` mode.
3. WHEN a Pull executes, THE Git_Sync_Service SHALL fetch the requested ref, read the Repository_Path contents excluding the Sync_Manifest, any `.git` directory, and symbolic links, and stage them for installation without modifying any Plugin_Version until the staged tree is validated.
4. IF the requested ref does not exist or the Repository_Path is absent or empty at that ref, THEN the Sync_Operation SHALL fail with Failure_Category `not_found` and no Plugin_Version SHALL be created or modified.
5. IF the staged tree exceeds 50 MiB in total or 2,000 files, THEN the Sync_Operation SHALL fail with Failure_Category `invalid_source` identifying the exceeded limit and no Plugin_Version SHALL be created or modified.
6. WHEN the target Plugin_Version is scaffold-kind, THE Portal SHALL validate the staged tree against the recorded scaffold declaration, and IF the tree is not buildable, THEN the Sync_Operation SHALL fail with Failure_Category `invalid_source` listing every defect and no Plugin_Version SHALL be created or modified.
7. WHEN a Pull in `in_place` mode is validated, THE Portal SHALL replace the Plugin_Version's Source_Tree with the staged tree (deleting files absent from the staged tree), increment the Source_Revision, and mark every existing Plugin_Artifact as stale as specified in Requirement 1.8.
8. WHEN a Pull in `new_version` mode is validated, THE Portal SHALL create a new Plugin_Version as specified in Requirement 1.6 whose Source_Tree is the staged tree, and SHALL record the pull in the new version's provenance.
9. WHEN a Pull succeeds, THE Portal SHALL record on the affected Plugin_Version the resolved commit SHA, the requested ref, branch, Repository_Path, pulling user, and timestamp as the last sync, and SHALL record the same together with the affected version number on the Sync_Operation result.
10. THE Detail_Page SHALL list the Sync_Operations of a Plugin_Version newest first with kind, status, Failure_Category, commit SHA, acting user, and timestamps, and SHALL poll an in-flight operation until it settles.
11. WHEN a Sync_Operation fails, THE Portal SHALL present the Failure_Category with a plain-language explanation and the credential-redacted log excerpt.

### Requirement 5: Diagnostic-Aware Code Assistance

**User Story:** As a computer vision engineer, I want to hand a failing build log or simulation error to the code assistant and get a proposed fix for the right file, so that I can resolve build and runtime problems without deciphering compiler output myself.

#### Acceptance Criteria

1. WHEN a Plugin_Version has a failed Plugin_Artifact build for a Target_Architecture, THE Detail_Page SHALL offer a "Fix with AI" action beside that architecture's failure that opens the Source_Editor with the Code_Assistant pre-loaded with a Diagnostic_Context of kind `build` carrying that architecture and its Build_Diagnostics.
2. WHEN a Plugin_Version has a failed Plugin_Simulator run, THE Portal SHALL offer the same action from the simulator view with a Diagnostic_Context of kind `simulation` carrying the Simulation_Diagnostics.
3. THE Code_Assistant SHALL provide, on every Code_Editing_Surface, an optional error-output field into which the user can paste an error message, submitted as a Diagnostic_Context of kind `user`.
4. THE Code_Assist_Generator SHALL accept an optional Diagnostic_Context whose text is at most 16 KiB, and IF the text exceeds 16 KiB, THEN THE Code_Assistant SHALL keep the last 16 KiB before submission and indicate the truncation.
5. WHEN a Diagnostic_Context is present, THE Code_Assist_Generator SHALL include the diagnostic kind, the Target_Architecture when present, and the diagnostic text in the Bedrock invocation together with the current file's code, and SHALL instruct the model to diagnose the cause and return the corrected complete file.
6. WHEN the Code_Assistant is used on a Source_Editor tab, THE Code_Assist_Generator SHALL receive the path of the active file, the paths of every other Source_Tree file, and the contents of the other text files of the Source_Tree up to a total of 256 KiB (largest files omitted first, omitted paths still listed), so the model can reason across the plugin's files.
7. WHEN the Code_Assist_Generator determines that the fix belongs in a Source_Tree file other than the active file, THE Code_Assist_Generator SHALL return that file's path as the Target_File together with its complete corrected content, and THE Code_Assistant SHALL display which file the proposal applies to before the user accepts.
8. IF the Code_Assist_Generator returns a Target_File path that is not among the Source_Tree paths provided in the request, THEN THE Code_Assist_Generator SHALL reject the model output with an error indicating the invalid target file and SHALL retain the prompt for resubmission.
9. WHEN the Target_File is the Frame_Processing_Hook, THE Code_Assist_Generator SHALL validate the returned code under the `frame_hook` Node_Contract; WHEN the Target_File is any other Source_Tree file, THE Code_Assist_Generator SHALL apply the Plugin_Source_Contract and SHALL reject only empty output.
10. THE Code_Assist_Generator SHALL describe, in the Node_Designer system prompt, the Plugin_Scaffold layout (the C skeleton element embedding the Python Frame_Processing_Hook through an appsink/appsrc bridge, declared parameters surfacing as GObject properties, one meson build configuration per Target_Architecture) and each Target_Architecture's build platform (operating system release, GStreamer version, and build tooling) so that diagnoses account for the failing target's toolchain.
11. WHEN a user accepts a proposal, THE Code_Assistant SHALL replace the Target_File's content in the Source_Editor and mark the file as edited, and THE Source_Editor SHALL offer to save and rebuild the failed Target_Architectures.
12. THE Code_Assistant SHALL apply the existing behaviors of spec custom-node-code-assist unchanged: review before apply, prompt retained on failure, Bedrock_Configuration reuse, failure categories, nothing persisted by the assistant itself, and the `node-designer:generate` authorization for the Node_Designer surface.

### Requirement 6: Adding Target Architectures After Creation

**User Story:** As a computer vision engineer, I want to add device architectures to an existing custom node version and build only those, so that a plugin created for one device family can be delivered to newly acquired hardware without recreating it.

#### Acceptance Criteria

1. WHEN a user with `node-designer:manage` requests an Architecture_Addition on a Plugin_Version, THE Portal SHALL accept one or more Target_Architectures, and IF any of them is unknown, already requested for the version, outside the Build_Target_Registry, or disallowed by the version's DeepStream flag, THEN THE Portal SHALL reject the request identifying each offending architecture and SHALL change nothing.
2. WHILE the Plugin_Version's Lifecycle_State is `prod`, THE Portal SHALL reject an Architecture_Addition identifying the Lifecycle_State and directing the user to create a new version.
3. WHEN an Architecture_Addition is accepted for a scaffold-kind Plugin_Version, THE Portal SHALL render the per-architecture build configuration file for each added Target_Architecture from the recorded scaffold declaration into the Source_Tree where no such file exists, SHALL NOT overwrite an existing file, SHALL record the added architectures in the version's scaffold declaration, and SHALL increment the Source_Revision only when a file was added.
4. WHEN an Architecture_Addition is accepted, THE Plugin_Build_Service SHALL append the added Target_Architectures to the version's requested architectures without removing any previously requested architecture and SHALL start builds for exactly the added architectures.
5. THE Plugin_Build_Service SHALL treat a Plugin_Version's requested architectures as the union of every architecture ever requested for that version, so that a retry of a subset of architectures never removes the others from the build status view or from Plugin_Component packaging.
6. THE Detail_Page SHALL offer an "Add architectures" action listing only Target_Architectures in the Build_Target_Registry that are not yet requested for the version, and after a successful addition SHALL show the added architectures in the per-architecture build status.
7. THE Portal SHALL expose the Build_Target_Registry to the Detail_Page, and THE Detail_Page SHALL offer for building only architectures in the Build_Target_Registry.
8. IF a build is requested for a Target_Architecture outside the Build_Target_Registry, THEN THE Portal SHALL reject the request with a client error identifying the architecture and the registry contents.

### Requirement 7: JetPack 7 Plugin Build Target

**User Story:** As an operator with Jetson Thor devices, I want custom node plugins built for arm64 JetPack 7, so that workflows using custom nodes can be deployed to those devices.

#### Acceptance Criteria

1. THE Plugin_Build_Service SHALL include `arm64_jp7` in the Build_Target_Registry with a dedicated per-architecture build project executing on an ARM build fleet with the same source, staging, library-prefix, and signing-key access scoping as the existing per-architecture build projects.
2. THE `arm64_jp7` plugin build image SHALL be based on the same CUDA 13 Ubuntu 24.04 arm64 base image as the JetPack 7 LocalServer image (spec: jetpack7-support), SHALL provide the GStreamer development packages, meson, ninja, and the AWS CLI, and SHALL run the same build entrypoint contract as the existing images.
3. WHEN a build result arrives for the `arm64_jp7` build project, THE Plugin_Build_Service SHALL record it on the Plugin_Version exactly as for the existing architectures, including artifact signing and versioned Plugin_Library storage.
4. THE Portal SHALL record `arm64_jp7`'s build platform GStreamer version in the platform compatibility table consulted by the Plugin_Importer, and SHALL treat `arm64_jp7` as a platform whose toolchain supports GStreamer's meson subproject fallback.
5. WHEN a Plugin_Component is published with an `arm64_jp7` Plugin_Artifact, THE Component_Packager SHALL emit a platform manifest for `arm64_jp7` with the `variant` attribute set to `arm64_jp7`, consistent with the existing JetPack manifests.
6. THE plugin build image build script SHALL include `arm64_jp7` in its default architecture set.

### Requirement 8: Plugin_Component Republish After Artifact Changes

**User Story:** As an operator, I want the deployable plugin component to always reflect the latest successfully built binaries of a plugin version, so that rebuilds and added architectures actually reach devices.

#### Acceptance Criteria

1. WHEN all requested builds of a Plugin_Version have settled with at least one success and the set of successfully built Plugin_Artifacts (architecture and checksum) differs from the set recorded on the version's registered Plugin_Component, THE Component_Packager SHALL publish a new Plugin_Component version with the next Component_Revision (`v.0.n+1`) carrying one platform manifest per successfully built architecture.
2. WHEN the set of successfully built Plugin_Artifacts equals the set recorded on the registered Plugin_Component, THE Component_Packager SHALL NOT publish a new Plugin_Component version.
3. WHEN a Plugin_Component version is published, THE Portal SHALL record on the Plugin_Version's component pointer the component version, the Component_Revision, the built architectures, and the per-architecture artifact checksums the component carries.
4. THE Component_Packager SHALL leave previously published Plugin_Component versions unchanged.
5. WHEN a Workflow_Component that depends on a Plugin_Component is deployed, THE Deployment_Service SHALL evaluate the architecture gate against the architectures recorded on the Plugin_Version's current component pointer, so that architectures added after the workflow was packaged are deployable without repackaging the workflow.
6. THE Workflow_Component recipe SHALL continue to pin each Plugin_Component dependency to the version range `>=v.0.0 <v+1.0.0`, so that Greengrass resolves the newest Component_Revision of the pinned Plugin_Version.

### Requirement 9: Access Control and Audit

**User Story:** As a portal administrator, I want every new source, sync, and architecture operation gated by the existing Node_Designer roles and recorded in the audit log, so that native code changes and repository access remain traceable.

#### Acceptance Criteria

1. THE Portal SHALL require `node-designer:manage` in the Plugin_Version's Use_Case (UseCaseAdmin) or the PortalAdmin role for saving source, saving as new version, linking or unlinking a Git_Link, Push, Pull, and Architecture_Addition, and SHALL require `node-designer:generate` for Code_Assistant use on the Source_Editor.
2. THE Portal SHALL permit users holding `node-designer:read` in the Use_Case to view the Source_Tree, Git_Link, and Sync_Operation history in read-only form, and THE Source_Editor SHALL omit save, sync, add-architecture, and Code_Assistant entry points for such users.
3. IF a user without the required permission attempts any operation in criterion 1, THEN THE Portal SHALL deny the operation with the standard authorization error and SHALL record the denied attempt in the audit log.
4. WHEN source is saved, a new version is created, a Git_Link is set or removed, a Sync_Operation is started or settles, or an Architecture_Addition is accepted, THE Portal SHALL record the action, the acting user (or the operation's initiating user for asynchronous settlement), the Plugin_Record, version, and a timestamp in the existing audit log.
5. THE Git_Sync_Service SHALL redact any credential material from Sync_Operation log excerpts before storing them.

### Requirement 10: Compatibility with Existing Node_Designer Flows

**User Story:** As a computer vision engineer, I want the existing create, generate, import, build, and review flows to keep working unchanged, so that this feature adds capabilities without disrupting current plugins.

#### Acceptance Criteria

1. THE Portal SHALL treat a Plugin_Version without a recorded Source_Revision as Source_Revision 1 and a Plugin_Artifact without a recorded Source_Revision as built from Source_Revision 1, so that existing records report no Stale_Artifacts.
2. THE Portal SHALL treat a Plugin_Version without a registered Component_Revision as Component_Revision 0 and its registered component version `v.0.0` as carrying the architectures recorded on its component pointer, so that the first change after this feature deploys publishes `v.0.1`.
3. THE create wizard's and generate panel's source submission SHALL continue to save in place on their newly created `dev` version through the same save endpoint.
4. THE Plugin_Importer's asynchronous import, plugin selection, and per-platform revision adjustment SHALL continue to function unchanged for imported Plugin_Versions, and imported Plugin_Versions SHALL be editable, linkable, and extendable with architectures like scaffold and generated ones (without scaffold validation).
5. Plugin_Versions without a Git_Link SHALL show no sync controls beyond the option to link a Git_Connection.
