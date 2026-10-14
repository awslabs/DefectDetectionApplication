/**
 * User Manager action modals (portal-user-manager Requirements 3.2-3.5,
 * 4.3-4.5, 5.2, 5.3, 5.7).
 *
 * - PasswordModal: password + confirm fields with a required
 *   permanent/temporary RadioGroup (no default; submit disabled until a
 *   permanence is chosen, Requirement 3.2), a client-side pre-check
 *   mirroring the user pool Password_Policy, and server policy errors
 *   surfaced verbatim (3.3). Success is reported to the parent, which
 *   shows a flashbar naming the account (3.4).
 * - ForgotPasswordModal: confirmation dialog; success reports that the
 *   temporary password was sent to the account's registered email without
 *   ever receiving the value (4.3); no-verified-email and delivery errors
 *   are shown in the modal (4.4, 4.5).
 * - RoleModal: Select limited to the five defined Portal_Role values with
 *   the account's current role preselected (5.2); success is confirmed by
 *   the parent, which re-fetches the account list (5.7); rejection reasons
 *   (including the last-PortalAdmin guard, 5.3) are shown in the modal.
 * - CreateUserModal: username, email, and role fields with the role a
 *   Select restricted to the five defined Portal_Role values (12.2);
 *   client-side pre-checks mirroring the server validation — non-empty
 *   fields and email shape — keep submit disabled until valid (12.6,
 *   12.7); server rejection reasons (duplicate username 12.5, invalid
 *   email 12.6, missing field 12.7) are surfaced verbatim in the modal.
 *   Success is reported to the parent, which shows a flashbar stating an
 *   invitation with a temporary password was sent to the account's email
 *   (never the value) and re-fetches the list (12.10).
 * - DisableEnableModal: explicit confirmation naming the affected account
 *   by username before submission (13.1); on success the parent shows a
 *   confirmation identifying the account and re-fetches the list (13.8).
 *   An already-in-requested-state response is a 200 no-op that flows
 *   through the same success path, so the modal closes and the list
 *   re-fetch shows the account's current state (13.6). Rejection reasons
 *   (last-PortalAdmin guard, 5.3) and failures (13.7) are shown in the
 *   modal.
 * - DeleteModal: explicit confirmation naming the affected account by
 *   username before submission (14.1); cancel/dismiss submits nothing
 *   (14.9); on success the parent shows a confirmation identifying the
 *   deleted account and re-fetches the list without it (14.7). The
 *   last-PortalAdmin rejection reason (14.3) and plain failures (14.6)
 *   are shown in the modal; a not-found error (14.11) and a partial
 *   verifier-cleanup error (14.10) are handed to the parent, which
 *   surfaces them in an error flashbar and refreshes the list.
 */
import { useState } from 'react';
import {
  Alert,
  Box,
  Button,
  FormField,
  Input,
  Modal,
  RadioGroup,
  Select,
  SpaceBetween,
} from '@cloudscape-design/components';
import { apiService } from '../../services/api';
import type { AdminAccount } from '../../services/api';
import { getErrorMessage } from '../../utils/errorHandling';

/** The six defined Portal_Role values (portal-user-manager Requirement 5.2, extended by dda-data-labeling Requirement 2.1). */
export const PORTAL_ROLES = [
  'PortalAdmin',
  'UseCaseAdmin',
  'DataScientist',
  'Operator',
  'Viewer',
  'DataLabeler',
] as const;

/**
 * Pure client-side Password_Policy pre-check mirroring the user pool
 * policy (AuthStack): minimum length 12, at least one lowercase letter,
 * one uppercase letter, one digit, and one symbol. Returns the list of
 * violated rules (empty when the password conforms).
 */
export function checkPasswordPolicy(password: string): string[] {
  const violations: string[] = [];
  if (password.length < 12) {
    violations.push('Password must be at least 12 characters long.');
  }
  if (!/[a-z]/.test(password)) {
    violations.push('Password must contain a lowercase letter.');
  }
  if (!/[A-Z]/.test(password)) {
    violations.push('Password must contain an uppercase letter.');
  }
  if (!/[0-9]/.test(password)) {
    violations.push('Password must contain a digit.');
  }
  if (!/[^a-zA-Z0-9]/.test(password)) {
    violations.push('Password must contain a symbol.');
  }
  return violations;
}

/**
 * Pure client-side email shape check mirroring the server validation
 * (Requirement 12.6): a non-empty local part, followed by a single '@'
 * separator, followed by a non-empty domain containing at least one dot.
 */
export function isValidCreateEmail(email: string): boolean {
  const parts = email.split('@');
  if (parts.length !== 2) {
    return false;
  }
  const [local, domain] = parts;
  return local.length > 0 && domain.length > 0 && domain.includes('.');
}

/** Per-field validation errors of a create-user request. */
export interface CreateUserFieldErrors {
  username?: string;
  email?: string;
  role?: string;
}

/**
 * Pure client-side pre-check mirroring the server's
 * `validate_create_request` (Requirements 12.6, 12.7): every field must
 * be present and non-empty, and the email must have a valid shape.
 * Returns per-field error messages; an empty object means the request is
 * valid.
 */
export function validateCreateUserRequest(fields: {
  username: string;
  email: string;
  role: string;
}): CreateUserFieldErrors {
  const errors: CreateUserFieldErrors = {};
  if (!fields.username.trim()) {
    errors.username = 'Username is required.';
  }
  if (!fields.email.trim()) {
    errors.email = 'Email address is required.';
  } else if (!isValidCreateEmail(fields.email)) {
    errors.email =
      'Enter a valid email address: a name, followed by @, followed by a domain containing a dot (e.g. name@example.com).';
  }
  if (!fields.role) {
    errors.role = 'Role is required.';
  }
  return errors;
}

interface ActionModalProps {
  account: AdminAccount;
  /** Called with the flashbar confirmation text on success. */
  onSuccess: (message: string) => void;
  onDismiss: () => void;
}

interface CreateUserModalProps {
  /** Called with the flashbar confirmation text on success. */
  onSuccess: (message: string) => void;
  onDismiss: () => void;
}

export function CreateUserModal({ onSuccess, onDismiss }: CreateUserModalProps) {
  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  // No default role: the administrator must pick one of the five defined
  // Portal_Role values before submission is possible (Requirement 12.2).
  const [role, setRole] = useState('');
  const [touched, setTouched] = useState<{
    username?: boolean;
    email?: boolean;
    role?: boolean;
  }>({});
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState('');

  // Client-side pre-checks mirror the server validation; submit stays
  // disabled until every field is valid (Requirements 12.6, 12.7).
  const fieldErrors = validateCreateUserRequest({ username, email, role });
  const isValid = Object.keys(fieldErrors).length === 0;

  const submit = async () => {
    if (!isValid || submitting) {
      return;
    }
    setServerError('');
    setSubmitting(true);
    try {
      await apiService.createAdminUser({
        username: username.trim(),
        email: email.trim(),
        role,
      });
      // The API response never carries the temporary password value; the
      // confirmation names only the invitation delivery (Requirement 12.10).
      onSuccess(
        `Account ${username.trim()} was created. An invitation with a temporary password was sent to ${email.trim()}.`
      );
    } catch (err: unknown) {
      // Server rejection reasons — duplicate username (12.5), invalid
      // email (12.6), missing field (12.7) — are shown verbatim.
      setServerError(getErrorMessage(err, 'The account was not created'));
      setSubmitting(false);
    }
  };

  return (
    <Modal
      visible
      onDismiss={onDismiss}
      header="Create user"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={submitting}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={submit}
              loading={submitting}
              // Submission requires every field to pass the client-side
              // pre-checks (Requirements 12.6, 12.7).
              disabled={!isValid}
            >
              Create user
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="l">
        {serverError && (
          <Alert type="error" header="Account not created">
            {serverError}
          </Alert>
        )}
        <Box variant="p">
          The new account receives an email invitation containing a temporary
          password and must set a new password at first sign-in.
        </Box>
        <FormField
          label="Username"
          errorText={touched.username ? fieldErrors.username : undefined}
        >
          <Input
            value={username}
            onChange={({ detail }) => setUsername(detail.value)}
            onBlur={() => setTouched((t) => ({ ...t, username: true }))}
            ariaLabel="Username"
          />
        </FormField>
        <FormField
          label="Email"
          errorText={touched.email ? fieldErrors.email : undefined}
        >
          <Input
            type="email"
            value={email}
            onChange={({ detail }) => setEmail(detail.value)}
            onBlur={() => setTouched((t) => ({ ...t, email: true }))}
            ariaLabel="Email"
          />
        </FormField>
        <FormField
          label="Role"
          errorText={touched.role ? fieldErrors.role : undefined}
        >
          <Select
            selectedOption={role ? { value: role, label: role } : null}
            onChange={({ detail }) => {
              setRole(detail.selectedOption.value ?? '');
              setTouched((t) => ({ ...t, role: true }));
            }}
            options={PORTAL_ROLES.map((value) => ({ value, label: value }))}
            placeholder="Choose a role"
            ariaLabel="Role"
          />
        </FormField>
      </SpaceBetween>
    </Modal>
  );
}

export function PasswordModal({ account, onSuccess, onDismiss }: ActionModalProps) {
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  // No default selection: the administrator must choose exactly one of
  // permanent or temporary before submission is possible (Requirement 3.2).
  const [permanence, setPermanence] = useState<'' | 'permanent' | 'temporary'>('');
  const [attempted, setAttempted] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState('');

  const policyViolations = checkPasswordPolicy(password);
  const confirmMismatch = password !== confirm;

  const submit = async () => {
    setAttempted(true);
    // Client-side policy pre-check: never send a password that the pool
    // policy would reject, or one that does not match its confirmation.
    if (!permanence || policyViolations.length > 0 || confirmMismatch) {
      return;
    }
    setServerError('');
    setSubmitting(true);
    try {
      await apiService.setAdminUserPassword(account.username, {
        password,
        permanent: permanence === 'permanent',
      });
      // Confirmation identifies the affected account (Requirement 3.4).
      onSuccess(
        permanence === 'permanent'
          ? `Password for ${account.username} was changed.`
          : `Temporary password for ${account.username} was set; the user must change it at next sign-in.`
      );
    } catch (err: unknown) {
      // Server errors — including the violated Password_Policy rule on a
      // 400 — are shown verbatim (Requirements 3.3, 3.5).
      setServerError(getErrorMessage(err, 'Password change failed'));
      setSubmitting(false);
    }
  };

  return (
    <Modal
      visible
      onDismiss={onDismiss}
      header={`Change password for ${account.username}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={submitting}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={submit}
              loading={submitting}
              // Submission requires a permanence choice (Requirement 3.2).
              disabled={!permanence}
            >
              Set password
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="l">
        {serverError && (
          <Alert type="error" header="Password change failed">
            {serverError}
          </Alert>
        )}
        <FormField
          label="New password"
          errorText={
            attempted && policyViolations.length > 0
              ? policyViolations.join(' ')
              : undefined
          }
        >
          <Input
            type="password"
            value={password}
            onChange={({ detail }) => setPassword(detail.value)}
            ariaLabel="New password"
          />
        </FormField>
        <FormField
          label="Confirm password"
          errorText={
            attempted && confirmMismatch ? 'Passwords do not match.' : undefined
          }
        >
          <Input
            type="password"
            value={confirm}
            onChange={({ detail }) => setConfirm(detail.value)}
            ariaLabel="Confirm password"
          />
        </FormField>
        <FormField
          label="Password type"
          description="Select whether the password is permanent or must be changed at the user's next sign-in."
        >
          <RadioGroup
            value={permanence || null}
            onChange={({ detail }) =>
              setPermanence(detail.value as 'permanent' | 'temporary')
            }
            items={[
              {
                value: 'permanent',
                label: 'Permanent password',
                description: 'The user can sign in with this password indefinitely.',
              },
              {
                value: 'temporary',
                label: 'Temporary password',
                description: 'The user must set a new password at next sign-in.',
              },
            ]}
          />
        </FormField>
      </SpaceBetween>
    </Modal>
  );
}

export function ForgotPasswordModal({ account, onSuccess, onDismiss }: ActionModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState('');

  const submit = async () => {
    setServerError('');
    setSubmitting(true);
    try {
      // The API never returns the temporary password value; the
      // confirmation names the delivery, not the password (Requirement 4.3).
      await apiService.sendAdminForgotPassword(account.username);
      onSuccess(
        `A temporary password for ${account.username} was sent to the account's registered email address.`
      );
    } catch (err: unknown) {
      // No-verified-email and delivery errors are surfaced in the modal
      // (Requirements 4.4, 4.5).
      setServerError(getErrorMessage(err, 'The temporary password was not sent'));
      setSubmitting(false);
    }
  };

  return (
    <Modal
      visible
      onDismiss={onDismiss}
      header={`Send temporary password to ${account.username}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={submitting}>
              Cancel
            </Button>
            <Button variant="primary" onClick={submit} loading={submitting}>
              Send temporary password
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {serverError && (
          <Alert type="error" header="Temporary password not sent">
            {serverError}
          </Alert>
        )}
        <Box variant="p">
          A temporary password will be generated and sent to the account's
          registered email address. The user must change it at next sign-in.
        </Box>
      </SpaceBetween>
    </Modal>
  );
}

export function RoleModal({ account, onSuccess, onDismiss }: ActionModalProps) {
  // Role choices are limited to the five defined Portal_Role values with
  // the account's current role preselected (Requirement 5.2).
  const [role, setRole] = useState(account.role || 'Viewer');
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState('');

  const submit = async () => {
    setServerError('');
    setSubmitting(true);
    try {
      await apiService.setAdminUserRole(account.username, role);
      // The parent shows the confirmation and re-fetches the account list
      // (Requirement 5.7).
      onSuccess(`Role for ${account.username} was changed to ${role}.`);
    } catch (err: unknown) {
      // Rejection reasons — including the last-PortalAdmin guard (5.3) —
      // are shown in the modal.
      setServerError(getErrorMessage(err, 'Role change failed'));
      setSubmitting(false);
    }
  };

  return (
    <Modal
      visible
      onDismiss={onDismiss}
      header={`Change role for ${account.username}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={submitting}>
              Cancel
            </Button>
            <Button variant="primary" onClick={submit} loading={submitting}>
              Change role
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {serverError && (
          <Alert type="error" header="Role change failed">
            {serverError}
          </Alert>
        )}
        <FormField label="Role" description={`Current role: ${account.role || 'Viewer'}`}>
          <Select
            selectedOption={{ value: role, label: role }}
            onChange={({ detail }) =>
              setRole(detail.selectedOption.value ?? role)
            }
            options={PORTAL_ROLES.map((value) => ({ value, label: value }))}
            ariaLabel="Role"
          />
        </FormField>
      </SpaceBetween>
    </Modal>
  );
}

export function DisableEnableModal({ account, onSuccess, onDismiss }: ActionModalProps) {
  // The requested action follows the account's current state: an enabled
  // account is offered a disable action, a disabled account an enable
  // action (Requirements 13.2, 13.3).
  const disabling = account.enabled;
  const actionLabel = disabling ? 'Disable' : 'Enable';
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState('');

  const submit = async () => {
    setServerError('');
    setSubmitting(true);
    try {
      // An already-in-requested-state response is a 200 no-op carrying
      // the current state; it flows through the same success path, so the
      // modal closes and the parent's list re-fetch shows the account's
      // current state (Requirement 13.6). When the response reports the
      // resulting state, the confirmation reflects it rather than the
      // requested transition.
      const response = disabling
        ? await apiService.disableAdminUser(account.username)
        : await apiService.enableAdminUser(account.username);
      const resultingEnabled = response.enabled ?? !disabling;
      // Confirmation identifies the affected account (Requirement 13.8);
      // the parent shows the flashbar and re-fetches the list.
      onSuccess(
        `Account ${account.username} is ${resultingEnabled ? 'enabled' : 'disabled'}.`
      );
    } catch (err: unknown) {
      // Rejection reasons — including the last-PortalAdmin guard (5.3) —
      // and failures (13.7) are shown in the modal.
      setServerError(
        getErrorMessage(err, `The ${actionLabel.toLowerCase()} action failed`)
      );
      setSubmitting(false);
    }
  };

  return (
    <Modal
      visible
      onDismiss={onDismiss}
      // Explicit confirmation naming the affected account by username
      // before submission (Requirement 13.1).
      header={`${actionLabel} account ${account.username}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={submitting}>
              Cancel
            </Button>
            <Button variant="primary" onClick={submit} loading={submitting}>
              {actionLabel} account
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {serverError && (
          <Alert type="error" header={`${actionLabel} failed`}>
            {serverError}
          </Alert>
        )}
        <Box variant="p">
          {disabling ? (
            <>
              Are you sure you want to disable the account{' '}
              <b>{account.username}</b>? A disabled account cannot sign in to
              the portal until it is re-enabled.
            </>
          ) : (
            <>
              Are you sure you want to enable the account{' '}
              <b>{account.username}</b>? The account will be able to sign in
              to the portal again.
            </>
          )}
        </Box>
      </SpaceBetween>
    </Modal>
  );
}

/** Classification of a failed delete action (Requirements 14.3, 14.10, 14.11). */
export type DeleteErrorKind = 'not-found' | 'partial-cleanup' | 'other';

/**
 * Pure classifier for delete-action failures:
 *
 * - 'not-found' (HTTP 404): the account no longer exists in the user
 *   pool; the error is shown and the account list refreshed (14.11).
 * - 'partial-cleanup': the account WAS deleted but its edge credential
 *   verifier record was not removed (the backend states this in the
 *   error message, D13); the error is shown and the list refreshed,
 *   since the account is gone (14.10).
 * - 'other': the deletion was rejected or failed with the account
 *   unchanged (e.g. the last-PortalAdmin guard 14.3, or a Cognito
 *   failure 14.6); shown in the modal.
 */
export function classifyDeleteError(err: unknown): DeleteErrorKind {
  const status =
    err && typeof err === 'object' && 'status' in err
      ? (err as { status?: unknown }).status
      : undefined;
  if (status === 404) {
    return 'not-found';
  }
  const message = getErrorMessage(err, '');
  if (/deleted/i.test(message) && /not removed/i.test(message)) {
    return 'partial-cleanup';
  }
  return 'other';
}

interface DeleteModalProps extends ActionModalProps {
  /**
   * Error paths after which the account list must still be refreshed:
   * a not-found deletion (14.11) and a partial verifier-cleanup failure
   * where the account itself was deleted (14.10). The parent closes the
   * modal, surfaces the message in an error flashbar, and re-fetches
   * the list.
   */
  onErrorWithRefresh: (message: string) => void;
}

export function DeleteModal({
  account,
  onSuccess,
  onErrorWithRefresh,
  onDismiss,
}: DeleteModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState('');

  const submit = async () => {
    setServerError('');
    setSubmitting(true);
    try {
      await apiService.deleteAdminUser(account.username);
      // Confirmation identifies the deleted account (Requirement 14.7);
      // the parent shows the flashbar and re-fetches the account list,
      // which no longer contains the account.
      onSuccess(`Account ${account.username} was deleted.`);
    } catch (err: unknown) {
      const kind = classifyDeleteError(err);
      if (kind === 'not-found') {
        // The account no longer exists in the user pool: show the
        // not-found error and refresh the list (Requirement 14.11).
        onErrorWithRefresh(
          `Account ${account.username} was not found; it may have already ` +
            `been deleted. The account list has been refreshed.`
        );
        return;
      }
      if (kind === 'partial-cleanup') {
        // The account was deleted but its verifier record was not
        // removed: surface the backend's message and refresh the list,
        // since the account itself is gone (Requirement 14.10).
        onErrorWithRefresh(
          getErrorMessage(
            err,
            `Account ${account.username} was deleted, but its edge ` +
              `credential record was not removed.`
          )
        );
        return;
      }
      // The account is unchanged: rejection reasons — including the
      // last-PortalAdmin guard (14.3) — and failures (14.6) are shown
      // in the modal.
      setServerError(getErrorMessage(err, 'The deletion failed'));
      setSubmitting(false);
    }
  };

  return (
    <Modal
      visible
      // Cancel/dismiss submits nothing (Requirement 14.9): the modal
      // simply closes and no API call is made.
      onDismiss={onDismiss}
      // Explicit confirmation naming the affected account by username
      // before submission (Requirement 14.1).
      header={`Delete account ${account.username}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={submitting}>
              Cancel
            </Button>
            <Button variant="primary" onClick={submit} loading={submitting}>
              Delete account
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {serverError && (
          <Alert type="error" header="Deletion failed">
            {serverError}
          </Alert>
        )}
        <Box variant="p">
          Are you sure you want to delete the account{' '}
          <b>{account.username}</b>? This permanently removes the account
          from the portal and cannot be undone.
        </Box>
      </SpaceBetween>
    </Modal>
  );
}
