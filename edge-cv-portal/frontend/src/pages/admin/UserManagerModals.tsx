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

/** The five defined Portal_Role values (Requirement 5.2). */
export const PORTAL_ROLES = [
  'PortalAdmin',
  'UseCaseAdmin',
  'DataScientist',
  'Operator',
  'Viewer',
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

interface ActionModalProps {
  account: AdminAccount;
  /** Called with the flashbar confirmation text on success. */
  onSuccess: (message: string) => void;
  onDismiss: () => void;
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
