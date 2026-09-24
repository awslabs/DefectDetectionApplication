# Requirements: Private Repository Plugin Import

## Introduction

The Node_Designer's Import_View clones a plugin repository, enumerates the
GStreamer plugins it contains, and builds the selected ones
(custom-node-designer Requirement 4). It only works for repositories that
allow anonymous read: `plugin_importer.validate_repo_url` accepts
`http`/`https`/`git` URLs with no credential of any kind, and the
`dda-plugin-fetch` CodeBuild project runs a bare `git clone "$REPO_URL"`.
A private repository fails at the clone.

The custom-node-source-lifecycle feature already solved credential handling
for the other direction: a **Git_Connection** holds a provider, an HTTPS
repository URL, a default branch, and a Git_Credential token stored in AWS
Secrets Manager. The `dda-plugin-git-sync` CodeBuild project resolves that
token itself through a `SECRETS_MANAGER`-typed environment variable and uses
it via `GIT_ASKPASS`, so no Lambda and no command line ever holds it.

This feature lets an Import use an existing verified Git_Connection instead
of an anonymous URL, so a plugin living in a private GitHub or GitLab
repository can be imported through the normal Import_View with its plugin
enumeration, per-plugin selection, and multi-architecture build flow. It
adds no new credential surface: the token continues to live only in Secrets
Manager and to be read only by CodeBuild.

### Existing behavior this feature must preserve

- Anonymous imports keep working exactly as today: an Import request with a
  `repo_url` and no Git_Connection behaves byte-for-byte as it does now
  (Requirement 6.1).
- The public GStreamer module catalog, its Module_Index_Cache, and the
  multi-revision (`arch_revisions`) import path are untouched.
- The Lambda functions never gain `secretsmanager:GetSecretValue`.

## Glossary

- **Import**: the existing flow that clones a repository into a
  Plugin_Record's source prefix and enumerates its plugins.
- **Git_Connection**: the existing per-Use_Case record holding a provider,
  repository URL, default branch, verification status, and the ARN of a
  Secrets Manager secret containing the Git_Credential token.
- **Fetch_Project**: the `dda-plugin-fetch` CodeBuild project that performs
  the clone and syncs the tree to S3.
- **Authenticated_Fetch**: a Fetch_Project execution that clones using a
  Git_Connection's token.
- **Import_Source**: the resolved origin of an Import — either an
  Anonymous_URL or a Git_Connection plus an optional subdirectory.
- **Failure_Category**: the existing sync failure vocabulary
  (`authentication`, `not_found`, `unreachable`, `diverged`,
  `push_rejected`, `invalid_source`, `internal`).

## Requirements

### Requirement 1: Import from a Git_Connection

**User Story:** As a computer vision engineer, I want to import a plugin from
our private repository by picking the Git_Connection we already use, so that
I do not have to make the repository public or paste a token into the import
form.

#### Acceptance Criteria

1. WHEN a user with `node-designer:import` submits an Import naming a
   `connection_id`, THE Portal SHALL resolve the Git_Connection, use its
   repository URL as the clone target, and start an Authenticated_Fetch.
2. THE Portal SHALL accept an Import request that carries EITHER a
   `repo_url` OR a `connection_id`, and SHALL reject a request carrying both
   or neither, identifying which field is missing or conflicting.
3. IF the named Git_Connection does not exist, or belongs to a different
   Use_Case than the Import, THEN THE Portal SHALL reject the Import
   identifying the connection, and SHALL start no build.
4. IF the named Git_Connection's verification status is not `verified`, THEN
   THE Portal SHALL reject the Import identifying the verification status
   (mirroring the existing Push/Pull precondition).
5. WHEN an Import names a Git_Connection and a `path`, THE Portal SHALL
   enumerate and import only the plugins found under that subdirectory of
   the cloned tree, and SHALL reject a `path` that is absolute, empty, or
   contains a `..` segment.
6. WHEN an Import names a Git_Connection, THE Portal SHALL clone the
   `branch` given in the request, defaulting to the Git_Connection's default
   branch, and WHEN the request also names a `revision` THE Portal SHALL
   check that tag or commit out after cloning, exactly as it does for an
   anonymous Import; absent a `revision` the imported tree SHALL be the
   branch head.
8. WHEN an Import request sets `shallow` to true, THE Portal SHALL clone at
   depth 1 (fetching a named `revision` at depth 1 and falling back to a
   full history only when the host refuses), and WHEN `shallow` is absent or
   false THE Portal SHALL clone exactly as it does today, for both source
   kinds.
7. THE Portal SHALL record on the Plugin_Record which Git_Connection an
   Import used, and SHALL NOT record the repository URL's credentials, the
   token, or the secret's value.

### Requirement 2: Token confinement

**User Story:** As a security reviewer, I want an authenticated import to
handle the token exactly as the existing sync path does, so that adding this
feature does not widen the blast radius of a leaked credential.

#### Acceptance Criteria

1. THE Portal SHALL pass the Git_Credential to the Fetch_Project only as a
   `SECRETS_MANAGER`-typed environment variable naming the Git_Connection's
   secret, and SHALL NOT read the secret's value in any Lambda.
2. THE Fetch_Project SHALL authenticate git through `GIT_ASKPASS` and SHALL
   NOT place the token in a command line, a remote URL, a git config file
   that outlives the build, or the build's environment echo.
3. THE Fetch_Project's IAM role SHALL be able to read only secrets matching
   the Git_Connection secret prefix, and no other secret.
4. WHEN an Authenticated_Fetch fails, THE Portal SHALL redact any token-like
   substring from the recorded import finding and from any log excerpt it
   stores, using the existing redaction helper.
5. THE Portal SHALL NOT return a Git_Credential token, a secret ARN's value,
   or an authenticated clone URL in any API response.

### Requirement 3: Failure reporting

**User Story:** As a computer vision engineer, I want a failed private import
to tell me whether the problem was the token, the path, or the repository, so
that I can fix the right thing.

#### Acceptance Criteria

1. WHEN an Authenticated_Fetch fails, THE Portal SHALL classify the failure
   into a Failure_Category using the same classifier as the sync path and
   SHALL record it on the Plugin_Record's import finding.
2. WHEN the failure category is `authentication`, THE Import_View SHALL
   explain that the Git_Connection's token was rejected and SHALL name the
   Git connections page as where to re-verify it; THE Portal SHALL NOT
   start a re-verification as a side effect of an Import.
5. WHEN an Import is rejected because the Git_Connection is not `verified`,
   THE Import_View SHALL show the rejection with the connection's current
   verification status and SHALL NOT start a re-verification.
3. WHEN the failure category is `not_found`, THE Import_View SHALL explain
   that the repository, revision, or path does not exist.
4. IF the cloned tree contains no recognizable GStreamer plugin under the
   Import's `path`, THEN THE Portal SHALL record that finding distinctly
   from a clone failure, and SHALL leave the Plugin_Record in the existing
   failed-import state.

### Requirement 4: Import_View surface

**User Story:** As a computer vision engineer, I want the import form to
offer our connections alongside the public-URL option, so that both kinds of
import live in one place.

#### Acceptance Criteria

1. THE Import_View SHALL offer a source choice between a public repository
   URL and a Git_Connection of the Use_Case, defaulting to the public URL so
   the existing flow is unchanged for existing users.
2. WHEN the Git_Connection source is chosen, THE Import_View SHALL list only
   `verified` connections of the Use_Case, and SHALL show the connection's
   repository URL and default branch for confirmation.
3. WHEN no verified Git_Connection exists for the Use_Case, THE Import_View
   SHALL say so and SHALL link to the Git connections page.
4. THE Import_View SHALL offer, for the Git_Connection source, an optional
   subdirectory input validated with the same rule as the Repository_Path,
   an optional branch input pre-filled with the connection's default branch,
   and the existing optional revision input.
5. THE Import_View SHALL NOT offer a token input anywhere.
6. THE Import_View SHALL offer a "shallow clone" option for both source
   kinds, unchecked by default, and SHALL send it only when checked.

### Requirement 5: Linking the imported version for later sync

**User Story:** As a computer vision engineer, I want a plugin imported from
our repository to stay connected to it, so that I can pull later changes
without setting the link up by hand.

#### Acceptance Criteria

1. WHEN an Import from a Git_Connection succeeds, THE Portal SHALL record a
   Git_Link on the imported Plugin_Version carrying that connection, the
   resolved branch, and the Import's subdirectory as the Repository_Path.
2. THE Portal SHALL record the Import's resolved commit as the Git_Link's
   last sync of kind `pull`, so the Divergence_Guard has a baseline.
3. THE Portal SHALL leave every existing Git_Link behavior unchanged: the
   linked version can Push, Pull, and Unlink exactly as a hand-linked one.

### Requirement 6: Preservation

**User Story:** As a portal operator, I want the anonymous import path to
behave exactly as before, so that this feature cannot regress the flow
everyone already uses.

#### Acceptance Criteria

1. *For any* Import request that names a `repo_url` and no `connection_id`,
   and leaves `shallow` absent or false, THE Portal SHALL produce the same
   Fetch_Project invocation, the same Plugin_Record fields, and the same
   enumeration and selection behavior as before this feature.
2. THE Fetch_Project SHALL clone anonymously when no Git_Connection is
   named, and its role's added secret-read permission SHALL be unused on
   that path.
3. THE Portal SHALL keep the existing `repo_url` validation rule for
   anonymous imports, including its rejection of ssh and file URLs.

### Requirement 7: Authorization and audit

#### Acceptance Criteria

1. THE Portal SHALL require `node-designer:import` in the Import's Use_Case
   for an Import from a Git_Connection, the same permission an anonymous
   Import requires.
2. THE Portal SHALL permit an Import to use only Git_Connections belonging
   to the Import's own Use_Case.
3. WHEN an Import from a Git_Connection starts, THE Portal SHALL record the
   acting user, the Use_Case, the Git_Connection identifier, the
   subdirectory, and the revision in the existing audit log, without the
   token.
