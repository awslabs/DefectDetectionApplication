# Requirements Document

## Introduction

This feature adds a User Manager tool to the Edge CV Portal, reachable from the settings dropdown in the top navigation and visible only to Portal Administrators. The tool lets administrators manage portal user accounts: create new accounts, change passwords, trigger a forgot-password flow that issues a temporary password, change user roles, and disable, re-enable, or delete accounts. Accounts managed in the portal can be synchronized to the LocalServer edge component so that, when a configuration option is enabled, operators can log in locally on the edge device using cached credentials even while the internet connection is down. When the local-login configuration is disabled, edge access remains open (unauthenticated) exactly as it works today, and the existing bearer/JWT token support on the edge remains functional in all cases.

The portal already authenticates users through an Amazon Cognito user pool with role-based access control (roles include PortalAdmin, UseCaseAdmin, DataScientist, Operator, Viewer). The LocalServer edge component already supports optional token-based authorization: when an authorization settings file is present, API requests require a valid bearer token; when absent, access is open.

## Glossary

- **Portal**: The Edge CV Portal web application (React frontend and Lambda backend behind API Gateway) used to manage the defect detection system.
- **User_Manager**: The new administrator-facing tool in the Portal for managing user accounts, passwords, and roles.
- **Portal_Backend**: The Portal's Lambda-based API layer that performs user management operations against the User_Pool.
- **User_Pool**: The Amazon Cognito user pool that stores portal user accounts and issues JWT tokens.
- **Portal_Administrator**: A portal user whose role is PortalAdmin.
- **Portal_Role**: One of the defined portal roles: PortalAdmin, UseCaseAdmin, DataScientist, Operator, Viewer.
- **Temporary_Password**: A system-generated, single-use password delivered to a user that must be changed at next sign-in.
- **LocalServer**: The Greengrass edge component (FastAPI backend and local React web UI) that runs on the edge device.
- **Account_Sync_Service**: The mechanism that transfers user account records (including credential verifiers and roles) from the Portal to the LocalServer.
- **Local_Credential_Cache**: The store on the edge device that holds synchronized account records used for local login.
- **Edge_Credential_Verifier**: The salted, one-way credential verifier record that the Portal stores for an account and includes in sync payloads so that the LocalServer can verify Local_Login passwords.
- **Local_Login**: Authentication performed by the LocalServer against the Local_Credential_Cache without requiring internet connectivity.
- **Local_Login_Configuration**: The edge-side configuration setting that enables or disables Local_Login on a given edge device.
- **Existing_Token_Auth**: The LocalServer's current bearer-token authorization mechanism, enabled by the presence of the authorization settings file.
- **Local_Session_Token**: A signed token issued by the LocalServer after a successful Local_Login, used to authorize subsequent LocalServer API requests.
- **Password_Policy**: The set of rules governing password composition (minimum length, character classes) enforced by the User_Pool.

## Requirements

### Requirement 1: User Manager Access and Visibility

**User Story:** As a Portal_Administrator, I want a User Manager tool available from the settings dropdown in the upper-right navigation, so that I can manage user accounts without leaving the portal.

#### Acceptance Criteria

1. WHEN a Portal_Administrator opens the settings dropdown in the top navigation, THE Portal SHALL display a User Manager menu item.
2. WHEN a user whose Portal_Role is not PortalAdmin opens the settings dropdown, THE Portal SHALL NOT display the User Manager menu item.
3. WHEN a Portal_Administrator selects the User Manager menu item, THE Portal SHALL navigate to the User_Manager page.
4. IF a signed-in user whose Portal_Role is not PortalAdmin navigates directly to the User_Manager page URL, THEN THE Portal SHALL display an access-denied notice and SHALL NOT render User_Manager content.
5. IF a request whose validated User_Pool JWT token does not include the PortalAdmin Portal_Role reaches a User_Manager API endpoint, THEN THE Portal_Backend SHALL reject the request with an authorization error (HTTP 403) and SHALL NOT perform the requested operation.
6. IF an unauthenticated user navigates directly to the User_Manager page URL, THEN THE Portal SHALL redirect the user to the Portal sign-in page.
7. IF a request without a valid User_Pool JWT token reaches a User_Manager API endpoint, THEN THE Portal_Backend SHALL reject the request and SHALL NOT perform the requested operation.

### Requirement 2: User Account Listing

**User Story:** As a Portal_Administrator, I want to see all portal user accounts with their status and roles, so that I can find and manage accounts.

#### Acceptance Criteria

1. WHEN a Portal_Administrator opens the User_Manager page, THE User_Manager SHALL display all accounts in the User_Pool, showing for each account the username, email, Portal_Role, the account status as reported by the User_Pool, and enabled/disabled state.
2. WHEN a Portal_Administrator enters a filter term, THE User_Manager SHALL restrict the displayed accounts to those whose username or email contains the filter term as a case-insensitive substring.
3. WHEN a filter term matches no accounts, THE User_Manager SHALL display an empty list without an error message.
4. WHEN a Portal_Administrator clears the filter term, THE User_Manager SHALL restore the display of all accounts in the User_Pool.
5. IF the Portal_Backend fails to retrieve the account list, THEN THE User_Manager SHALL display an error message describing the failure and SHALL NOT display a partial or stale account list.

### Requirement 3: Password Change

**User Story:** As a Portal_Administrator, I want to set a new password on a user account, so that I can help users who need their password changed.

#### Acceptance Criteria

1. WHEN a Portal_Administrator submits a new password for a selected account, THE Portal_Backend SHALL set the new password on that account in the User_Pool using the permanence setting selected by the Portal_Administrator.
2. WHEN a Portal_Administrator initiates a password change for a selected account, THE User_Manager SHALL require the Portal_Administrator to select exactly one of two options before submission: a permanent password, or a temporary password that must be changed at the user's next sign-in.
3. IF the submitted password violates the Password_Policy, THEN THE Portal_Backend SHALL reject the change without modifying the account's existing password and THE User_Manager SHALL display the specific policy rule that was violated to the Portal_Administrator.
4. WHEN a password change succeeds, THE User_Manager SHALL display a confirmation message identifying the affected account.
5. IF the User_Pool operation fails for a reason other than a Password_Policy violation, THEN THE User_Manager SHALL display an error message indicating the password change failed and the account's existing password SHALL remain unchanged.

### Requirement 4: Forgot Password with Temporary Password

**User Story:** As a Portal_Administrator, I want to trigger a forgot-password action that sends the user a Temporary_Password, so that locked-out users can regain access.

#### Acceptance Criteria

1. WHEN a Portal_Administrator triggers the forgot-password action for an account that has a verified email address, THE Portal_Backend SHALL generate a Temporary_Password that conforms to the Password_Policy and deliver it to the account's registered email address.
2. WHEN an account signs in with a valid Temporary_Password, THE User_Pool SHALL require that account to set a new password conforming to the Password_Policy before granting access.
3. WHEN THE Portal_Backend confirms delivery of the Temporary_Password to the account's registered email address, THE User_Manager SHALL display a confirmation that the Temporary_Password was sent, without displaying the Temporary_Password value.
4. IF the account has no verified email address, THEN THE Portal_Backend SHALL reject the forgot-password action without generating or delivering a Temporary_Password, and THE User_Manager SHALL display an error message indicating that the account has no verified email address.
5. IF Temporary_Password generation or delivery fails, THEN THE Portal_Backend SHALL preserve the account's existing credentials unchanged and THE User_Manager SHALL display an error message indicating that the Temporary_Password was not sent.
6. IF a sign-in is attempted with a Temporary_Password that has already been consumed by a prior successful sign-in, THEN THE User_Pool SHALL reject the sign-in attempt with an authentication error.

### Requirement 5: Role Change

**User Story:** As a Portal_Administrator, I want to change the Portal_Role of a user account, so that I can grant or reduce a user's access as responsibilities change.

#### Acceptance Criteria

1. WHEN a Portal_Administrator selects a new Portal_Role for an account and confirms, THE Portal_Backend SHALL update the account's Portal_Role in the User_Pool.
2. THE User_Manager SHALL offer only the defined Portal_Role values (PortalAdmin, UseCaseAdmin, DataScientist, Operator, Viewer) as role choices, with the account's current Portal_Role indicated as the current selection.
3. IF a user management action would remove the PortalAdmin role from, or disable, the last remaining enabled PortalAdmin account, THEN THE Portal_Backend SHALL reject the action and THE User_Manager SHALL display the reason.
4. WHEN an account's Portal_Role changes, THE Portal_Backend SHALL record the change in the portal audit log with the acting administrator, the affected account, the previous role, and the new role.
5. WHEN a role change is rejected, THE Portal_Backend SHALL record an audit log entry for the rejected attempt with the acting administrator, the affected account, and the rejection reason.
6. IF the User_Pool update fails during a role change, THEN THE Portal_Backend SHALL leave the account's Portal_Role unchanged and THE User_Manager SHALL display an error message indicating the role change failed.
7. WHEN a role change completes successfully, THE User_Manager SHALL display a confirmation of the change and refresh the displayed account list to show the account's new Portal_Role.
8. WHEN a JWT token is issued for an account after its Portal_Role has changed, THE Portal_Backend SHALL include the account's new Portal_Role in the token.

### Requirement 6: Audit Logging of User Management Actions

**User Story:** As a Portal_Administrator, I want user management actions recorded in the audit log, so that account changes are traceable.

#### Acceptance Criteria

1. WHEN a password change, forgot-password action, role change, account creation, account disable action, account enable action, or account deletion completes successfully, THE Portal_Backend SHALL record exactly one audit log entry containing the identity of the acting user, the identity of the affected account, the action type, and the date and time at which the action completed.
2. IF a user management action is initiated by the account holder rather than an administrator (such as a self-service forgot-password action), THEN THE Portal_Backend SHALL record the affected account's identity as the acting user in the audit log entry.
3. THE Portal_Backend SHALL exclude password values, password hashes, and Temporary_Password values from audit log entries.
4. IF the audit log entry for a user management action cannot be recorded, THEN THE Portal_Backend SHALL reject the user management action and leave the affected account in its state prior to the action.
5. IF the audit log entry for a user management action cannot be recorded, THEN THE Portal_Backend SHALL display to the User_Manager an error message indicating that the action was not applied.

### Requirement 7: Account Sync to the Edge

**User Story:** As a Portal_Administrator, I want portal accounts synchronized to the LocalServer edge component, so that operators can authenticate locally on the edge device.

#### Acceptance Criteria

1. WHEN a Portal_Administrator initiates an account sync for an edge device, THE Account_Sync_Service SHALL transfer the selected account records (username, email, Portal_Role, enabled/disabled state, and a credential verifier) to that device's Local_Credential_Cache.
2. WHEN any synchronized account attribute (username, email, credential verifier, Portal_Role, or enabled/disabled state) changes in the Portal, THE Account_Sync_Service SHALL include the updated account record in the next sync to each configured edge device.
3. THE Account_Sync_Service SHALL transfer credential material only in the form of a salted, one-way credential verifier, and SHALL exclude plaintext passwords from sync payloads, transport, and storage.
4. WHEN a sync completes, THE Account_Sync_Service SHALL report the sync result (success, or failure with a reason) and the sync completion timestamp to the Portal, and THE User_Manager SHALL display the last sync status and timestamp per edge device.
5. WHEN a sync completes with zero account changes to transfer, THE Account_Sync_Service SHALL report the sync as successful.
6. IF a sync attempt fails due to an unreachable edge device, THEN THE Account_Sync_Service SHALL retain the pending account changes until they are successfully delivered to that device.
7. WHILE an edge device has undelivered pending account changes, THE Account_Sync_Service SHALL attempt delivery of all pending account changes to that device at intervals not exceeding 5 minutes until delivery succeeds.
8. WHEN an account is disabled or deleted in the Portal, THE Account_Sync_Service SHALL mark that account as disabled in the Local_Credential_Cache on the next sync.
9. IF an edge device does not acknowledge a sync transfer within 60 seconds of transfer initiation, THEN THE Account_Sync_Service SHALL record the sync as failed with a reason indicating the device was unreachable.

### Requirement 8: Local Cached Login on the Edge

**User Story:** As an operator at a site with an unreliable internet connection, I want to log in to the LocalServer using my synchronized portal account, so that I can use the edge component when the internet is down.

#### Acceptance Criteria

1. WHERE the Local_Login_Configuration is enabled, THE LocalServer SHALL require authentication for all LocalServer web UI access and API requests, except for the login screen and the login endpoint.
2. WHERE the Local_Login_Configuration is enabled, WHEN a user submits a username and password that match an enabled account in the Local_Credential_Cache, THE LocalServer SHALL grant access and issue a Local_Session_Token valid for 12 hours from the time of issuance.
3. WHERE the Local_Login_Configuration is enabled, WHEN a user submits credentials that do not match an enabled account in the Local_Credential_Cache, THE LocalServer SHALL reject the login attempt with an authentication error (HTTP 401) whose response is identical whether or not the submitted username exists in the Local_Credential_Cache.
4. THE LocalServer SHALL validate Local_Login credentials against the Local_Credential_Cache without requiring internet connectivity.
5. WHEN the LocalServer receives an API request bearing a valid Local_Session_Token, defined as a token whose signature verifies successfully, that has not expired, and that was issued to an account currently marked enabled in the Local_Credential_Cache, THE LocalServer SHALL authorize the request.
6. IF an account in the Local_Credential_Cache is marked disabled, THEN THE LocalServer SHALL reject login attempts for that account and SHALL reject requests bearing any Local_Session_Token previously issued to that account.
7. WHEN the LocalServer receives an API request bearing a Local_Session_Token that is expired, malformed, or fails signature verification, THE LocalServer SHALL reject the request with an authentication error (HTTP 401).
8. WHERE the Local_Login_Configuration is enabled, WHEN an unauthenticated user requests a LocalServer web UI page other than the login screen, THE LocalServer SHALL present the login screen.
9. WHERE the Local_Login_Configuration is enabled, WHEN the LocalServer receives an API request that bears neither a valid Local_Session_Token nor valid Existing_Token_Auth credentials (per Requirement 10), THE LocalServer SHALL reject the request with an authentication error (HTTP 401).
10. IF an account accumulates 5 consecutive failed Local_Login attempts, THEN THE LocalServer SHALL reject all subsequent login attempts for that account for 15 minutes, regardless of the credentials submitted.

### Requirement 9: Open Access When Local Login Is Disabled

**User Story:** As a site administrator who has not enabled local login, I want edge access to keep working exactly as it does today, so that enabling this feature is fully optional.

#### Acceptance Criteria

1. WHERE the Local_Login_Configuration is disabled and Existing_Token_Auth is not configured, THE LocalServer SHALL serve web UI requests without requiring authentication.
2. WHERE the Local_Login_Configuration is disabled, THE LocalServer SHALL present the web UI without a local login screen and without prompting for credentials.
3. WHERE the Local_Login_Configuration is disabled and Existing_Token_Auth is not configured, THE LocalServer SHALL serve API requests without requiring authentication.
4. WHERE the Local_Login_Configuration is disabled and Existing_Token_Auth is configured, THE LocalServer SHALL reject API requests that do not carry a valid bearer token under Existing_Token_Auth with an authentication error (HTTP 401).
5. WHERE the Local_Login_Configuration is disabled, IF the LocalServer receives a Local_Login request, THEN THE LocalServer SHALL reject the request with an error indicating that Local_Login is disabled and SHALL NOT issue a Local_Session_Token.

### Requirement 10: Retention of Existing Token Support

**User Story:** As an integrator using the existing bearer-token authorization on the edge, I want the current token support retained, so that existing integrations keep working regardless of this feature.

#### Acceptance Criteria

1. WHERE Existing_Token_Auth is configured on an edge device, WHEN an API request carries a bearer token that the existing validation mechanism confirms as valid, THE LocalServer SHALL authorize the request regardless of the Local_Login_Configuration state.
2. WHERE both Existing_Token_Auth is configured and the Local_Login_Configuration is enabled, THE LocalServer SHALL authorize an API request when the request carries either a valid bearer token under Existing_Token_Auth or a valid Local_Session_Token.
3. WHEN the Local_Login_Configuration changes state (enabled to disabled or disabled to enabled), THE LocalServer SHALL continue to determine Existing_Token_Auth solely from the presence of the authorization settings file and SHALL NOT modify that file's contents.
4. WHERE Existing_Token_Auth is configured and the Local_Login_Configuration is disabled, IF an API request does not carry a bearer token that the existing validation mechanism confirms as valid, THEN THE LocalServer SHALL reject the request with an authentication error (HTTP 401).
5. WHERE both Existing_Token_Auth is configured and the Local_Login_Configuration is enabled, IF an API request carries neither a valid bearer token under Existing_Token_Auth nor a valid Local_Session_Token, THEN THE LocalServer SHALL reject the request with an authentication error (HTTP 401).

### Requirement 11: Local Login Configuration

**User Story:** As a Portal_Administrator, I want to enable or disable local cached login per edge device through configuration, so that each site can choose whether local authentication applies.

#### Acceptance Criteria

1. WHEN the LocalServer component starts, THE LocalServer SHALL read the Local_Login_Configuration state (enabled or disabled) from its component configuration.
2. WHEN the Local_Login_Configuration is changed on a running edge device, THE LocalServer SHALL apply the new state to all subsequent web UI and API requests within 60 seconds of the change, without requiring reinstallation of the LocalServer component.
3. IF the Local_Login_Configuration is enabled and the Local_Credential_Cache contains no enabled accounts, THEN THE LocalServer SHALL reject each login attempt with an authentication error and SHALL log a diagnostic message indicating that no synchronized accounts are available.
4. IF the Local_Login_Configuration value is absent from or unreadable in the component configuration, THEN THE LocalServer SHALL treat the Local_Login_Configuration as disabled.

### Requirement 12: Account Creation

**User Story:** As a Portal_Administrator, I want to create new user accounts from the User_Manager, so that new users can be given portal access without leaving the portal.

#### Acceptance Criteria

1. WHEN a Portal_Administrator submits a new account with a username, an email address, and a Portal_Role, THE Portal_Backend SHALL create the account in the User_Pool with the submitted username, email address, and Portal_Role.
2. THE User_Manager SHALL offer only the defined Portal_Role values (PortalAdmin, UseCaseAdmin, DataScientist, Operator, Viewer) as the role choice for a new account.
3. WHEN THE Portal_Backend creates an account in the User_Pool, THE User_Pool SHALL deliver an invitation containing a Temporary_Password that conforms to the Password_Policy to the account's email address.
4. WHEN a newly created account signs in with its Temporary_Password, THE User_Pool SHALL require the account to set a new password conforming to the Password_Policy before granting access.
5. IF the submitted username matches an existing account in the User_Pool, THEN THE Portal_Backend SHALL reject the creation without creating or modifying any account, and THE User_Manager SHALL display an error message indicating that the username already exists.
6. IF the submitted email address does not consist of a non-empty local part, followed by an '@' separator, followed by a non-empty domain containing at least one dot, THEN THE Portal_Backend SHALL reject the creation without creating an account, and THE User_Manager SHALL display an error message identifying the email address as invalid.
7. IF the submitted account is missing a username, email address, or Portal_Role, or any of these values is empty, THEN THE Portal_Backend SHALL reject the creation without creating an account, and THE User_Manager SHALL display an error message identifying the missing field.
8. IF the submitted Portal_Role is not one of the five defined Portal_Role values (PortalAdmin, UseCaseAdmin, DataScientist, Operator, Viewer), THEN THE Portal_Backend SHALL reject the creation without creating an account.
9. IF the User_Pool operation fails during account creation, THEN THE Portal_Backend SHALL report the failure with no account or partial account record remaining in the User_Pool, and THE User_Manager SHALL display an error message indicating that the account was not created.
10. WHEN an account creation completes successfully, THE User_Manager SHALL display a confirmation indicating that an invitation with a Temporary_Password was sent to the account's email address, without displaying the Temporary_Password value, and SHALL refresh the displayed account list to include the new account.
11. WHEN an account creation completes successfully, THE Portal_Backend SHALL record the creation in the portal audit log with the acting administrator and the created account's username, email address, and Portal_Role.

### Requirement 13: Account Disable and Enable

**User Story:** As a Portal_Administrator, I want to disable and re-enable user accounts, so that I can suspend a user's access without deleting the account.

#### Acceptance Criteria

1. WHEN a Portal_Administrator initiates a disable or enable action for an account, THE User_Manager SHALL require explicit confirmation identifying the affected account by username before submitting the action.
2. WHEN a Portal_Administrator confirms a disable action for an enabled account, THE Portal_Backend SHALL mark the account as disabled in the User_Pool.
3. WHEN a Portal_Administrator confirms an enable action for a disabled account, THE Portal_Backend SHALL mark the account as enabled in the User_Pool.
4. WHILE an account is marked disabled in the User_Pool, THE User_Pool SHALL reject sign-in attempts for that account with an authentication error.
5. WHILE an account is marked disabled in the User_Pool, THE User_Pool SHALL reject requests to issue new JWT tokens for that account, including requests made through token refresh.
6. IF a confirmed disable or enable action targets an account that is already in the requested enabled/disabled state, THEN THE Portal_Backend SHALL make no change to the account and THE User_Manager SHALL refresh the displayed account list to show the account's current state.
7. IF the User_Pool operation fails during a disable or enable action, THEN THE Portal_Backend SHALL leave the account's enabled/disabled state unchanged and THE User_Manager SHALL display an error message indicating that the action failed.
8. WHEN a disable or enable action completes successfully, THE User_Manager SHALL display a confirmation identifying the affected account and refresh the displayed account list to show the account's new enabled/disabled state.
9. WHEN a disable action is rejected under Requirement 5.3 (last remaining enabled PortalAdmin account), THE Portal_Backend SHALL record an audit log entry for the rejected attempt with the acting administrator, the affected account, and the rejection reason.

### Requirement 14: Account Deletion

**User Story:** As a Portal_Administrator, I want to delete user accounts, so that accounts that are no longer needed are removed from the portal.

#### Acceptance Criteria

1. WHEN a Portal_Administrator initiates a delete action for an account, THE User_Manager SHALL require explicit confirmation identifying the affected account by username before submitting the deletion.
2. WHEN a Portal_Administrator confirms deletion of an account, THE Portal_Backend SHALL delete the account from the User_Pool.
3. IF a delete action targets the last remaining enabled PortalAdmin account, THEN THE Portal_Backend SHALL reject the deletion without modifying the account and THE User_Manager SHALL display the reason.
4. WHEN a delete action is rejected because it targets the last remaining enabled PortalAdmin account, THE Portal_Backend SHALL record an audit log entry for the rejected attempt with the acting administrator, the affected account, and the rejection reason.
5. WHEN an account is deleted from the User_Pool, THE Portal_Backend SHALL delete the account's Edge_Credential_Verifier record from portal storage.
6. IF the User_Pool operation fails during deletion, THEN THE Portal_Backend SHALL leave the account and the account's Edge_Credential_Verifier record unchanged and THE User_Manager SHALL display an error message indicating that the deletion failed.
7. WHEN an account deletion completes successfully, THE User_Manager SHALL display a confirmation identifying the deleted account and refresh the displayed account list without the deleted account.
8. WHEN an account deletion completes successfully, THE Portal_Backend SHALL record the deletion in the portal audit log with the acting administrator and the deleted account's username, email address, and Portal_Role at the time of deletion.
9. IF the Portal_Administrator cancels or dismisses the confirmation without confirming, THEN THE User_Manager SHALL NOT submit the deletion and the account SHALL remain unchanged in the User_Pool.
10. IF the Edge_Credential_Verifier record deletion fails after the account has been deleted from the User_Pool, THEN THE Portal_Backend SHALL retain the Edge_Credential_Verifier record for removal on a subsequent attempt and THE User_Manager SHALL display an error message indicating that the account was deleted but its Edge_Credential_Verifier record was not removed.
11. IF a confirmed deletion targets an account that no longer exists in the User_Pool, THEN THE Portal_Backend SHALL report the failure without modifying any account, and THE User_Manager SHALL display an error message indicating that the account was not found and refresh the displayed account list.
