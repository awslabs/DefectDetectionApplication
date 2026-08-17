/**
 * Example-based unit tests for the User Manager action modals
 * (portal-user-manager Requirements 3.2-3.5, 4.3-4.5, 5.2, 5.3):
 *
 * - `checkPasswordPolicy`: client-side pre-check mirroring the user pool
 *   policy (min length 12, lowercase, uppercase, digit, symbol).
 * - PasswordModal: submit disabled until a permanence is chosen (3.2);
 *   client pre-check blocks non-conforming passwords before any API call;
 *   server policy errors shown verbatim (3.3); success reported with the
 *   account name (3.4); non-policy failures surfaced (3.5).
 * - ForgotPasswordModal: success confirmation says the temporary password
 *   was sent to the registered email without any password value (4.3);
 *   no-verified-email and delivery errors surfaced (4.4, 4.5).
 * - RoleModal: exactly the six defined roles with the current role
 *   preselected (5.2, extended by dda-data-labeling 2.1); rejection
 *   reasons (incl. last-PortalAdmin guard)
 *   shown in the modal (5.3); success reported for the parent to confirm
 *   and re-fetch (5.7).
 * - DeleteModal (Requirements 14.1-14.3, 14.7, 14.9-14.11): explicit
 *   confirmation naming the account; cancel submits nothing; success
 *   reported with the account name; the last-PortalAdmin rejection shown
 *   in the modal; not-found and partial verifier-cleanup errors handed
 *   to the parent for an error flashbar plus list refresh.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import {
  checkPasswordPolicy,
  classifyDeleteError,
  DeleteModal,
  ForgotPasswordModal,
  PasswordModal,
  PORTAL_ROLES,
  RoleModal,
} from './UserManagerModals';
import type { AdminAccount } from '../../services/api';

const {
  setAdminUserPassword,
  sendAdminForgotPassword,
  setAdminUserRole,
  deleteAdminUser,
} = vi.hoisted(() => ({
  setAdminUserPassword: vi.fn(),
  sendAdminForgotPassword: vi.fn(),
  setAdminUserRole: vi.fn(),
  deleteAdminUser: vi.fn(),
}));

vi.mock('../../services/api', () => ({
  apiService: {
    setAdminUserPassword,
    sendAdminForgotPassword,
    setAdminUserRole,
    deleteAdminUser,
  },
}));

const ACCOUNT: AdminAccount = {
  username: 'operator1',
  email: 'op1@example.com',
  email_verified: true,
  role: 'Operator',
  user_status: 'CONFIRMED',
  enabled: true,
  edge_capable: false,
};

const GOOD_PASSWORD = 'Sup3r-Secret-Pass!';

function isDisabled(button: HTMLElement): boolean {
  return (
    button.hasAttribute('disabled') ||
    button.getAttribute('aria-disabled') === 'true'
  );
}

function passwordInput(label: string): HTMLInputElement {
  return screen.getByLabelText(label) as HTMLInputElement;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('checkPasswordPolicy', () => {
  it('accepts a policy-conforming password', () => {
    expect(checkPasswordPolicy(GOOD_PASSWORD)).toEqual([]);
  });

  it('rejects a password shorter than 12 characters', () => {
    expect(checkPasswordPolicy('Ab1!short')).toContain(
      'Password must be at least 12 characters long.'
    );
  });

  it('requires a lowercase letter', () => {
    expect(checkPasswordPolicy('ABCDEFGH1234!')).toContain(
      'Password must contain a lowercase letter.'
    );
  });

  it('requires an uppercase letter', () => {
    expect(checkPasswordPolicy('abcdefgh1234!')).toContain(
      'Password must contain an uppercase letter.'
    );
  });

  it('requires a digit', () => {
    expect(checkPasswordPolicy('Abcdefghijk!')).toContain(
      'Password must contain a digit.'
    );
  });

  it('requires a symbol', () => {
    expect(checkPasswordPolicy('Abcdefgh1234')).toContain(
      'Password must contain a symbol.'
    );
  });

  it('reports every violated rule of the empty password', () => {
    expect(checkPasswordPolicy('')).toHaveLength(5);
  });
});

describe('PasswordModal', () => {
  function renderModal(onSuccess = vi.fn(), onDismiss = vi.fn()) {
    render(
      <PasswordModal
        account={ACCOUNT}
        onSuccess={onSuccess}
        onDismiss={onDismiss}
      />
    );
    return { onSuccess, onDismiss };
  }

  function fillPassword(value: string) {
    fireEvent.change(passwordInput('New password'), { target: { value } });
    fireEvent.change(passwordInput('Confirm password'), { target: { value } });
  }

  function choosePermanence(label: RegExp) {
    fireEvent.click(screen.getByRole('radio', { name: label }));
  }

  const submitButton = () =>
    screen.getByRole('button', { name: 'Set password' });

  it('disables submit until a permanence option is chosen (Requirement 3.2)', () => {
    renderModal();
    fillPassword(GOOD_PASSWORD);

    // No default selection: neither radio is checked and submit is disabled.
    expect(screen.getByRole('radio', { name: /Permanent password/ })).not.toBeChecked();
    expect(screen.getByRole('radio', { name: /Temporary password/ })).not.toBeChecked();
    expect(isDisabled(submitButton())).toBe(true);

    choosePermanence(/Permanent password/);
    expect(isDisabled(submitButton())).toBe(false);
  });

  it('blocks submission with client-side policy violations and no API call', () => {
    renderModal();
    fillPassword('weak');
    choosePermanence(/Permanent password/);

    fireEvent.click(submitButton());

    expect(setAdminUserPassword).not.toHaveBeenCalled();
    expect(
      screen.getByText(/Password must be at least 12 characters long\./)
    ).toBeInTheDocument();
  });

  it('blocks submission when the confirmation does not match', () => {
    renderModal();
    fireEvent.change(passwordInput('New password'), {
      target: { value: GOOD_PASSWORD },
    });
    fireEvent.change(passwordInput('Confirm password'), {
      target: { value: `${GOOD_PASSWORD}x` },
    });
    choosePermanence(/Temporary password/);

    fireEvent.click(submitButton());

    expect(setAdminUserPassword).not.toHaveBeenCalled();
    expect(screen.getByText('Passwords do not match.')).toBeInTheDocument();
  });

  it('submits with the chosen permanence and reports success naming the account (Requirements 3.1, 3.4)', async () => {
    setAdminUserPassword.mockResolvedValue({ message: 'ok' });
    const { onSuccess } = renderModal();
    fillPassword(GOOD_PASSWORD);
    choosePermanence(/Permanent password/);

    fireEvent.click(submitButton());

    await waitFor(() =>
      expect(setAdminUserPassword).toHaveBeenCalledWith('operator1', {
        password: GOOD_PASSWORD,
        permanent: true,
      })
    );
    expect(onSuccess).toHaveBeenCalledWith(
      expect.stringContaining('operator1')
    );
  });

  it('sends permanent=false for a temporary password', async () => {
    setAdminUserPassword.mockResolvedValue({ message: 'ok' });
    renderModal();
    fillPassword(GOOD_PASSWORD);
    choosePermanence(/Temporary password/);

    fireEvent.click(submitButton());

    await waitFor(() =>
      expect(setAdminUserPassword).toHaveBeenCalledWith('operator1', {
        password: GOOD_PASSWORD,
        permanent: false,
      })
    );
  });

  it('shows server policy errors verbatim in the modal (Requirements 3.3, 3.5)', async () => {
    const policyMessage =
      'Password did not conform with policy: Password must have symbol characters';
    setAdminUserPassword.mockRejectedValue(new Error(policyMessage));
    const { onSuccess } = renderModal();
    fillPassword(GOOD_PASSWORD);
    choosePermanence(/Permanent password/);

    fireEvent.click(submitButton());

    expect(await screen.findByText(policyMessage)).toBeInTheDocument();
    expect(onSuccess).not.toHaveBeenCalled();
  });
});

describe('ForgotPasswordModal', () => {
  function renderModal(onSuccess = vi.fn()) {
    render(
      <ForgotPasswordModal
        account={ACCOUNT}
        onSuccess={onSuccess}
        onDismiss={vi.fn()}
      />
    );
    return { onSuccess };
  }

  const sendButton = () =>
    screen.getByRole('button', { name: 'Send temporary password' });

  it('confirms delivery to the registered email without any password value (Requirement 4.3)', async () => {
    sendAdminForgotPassword.mockResolvedValue({ message: 'sent' });
    const { onSuccess } = renderModal();

    fireEvent.click(sendButton());

    await waitFor(() =>
      expect(sendAdminForgotPassword).toHaveBeenCalledWith('operator1')
    );
    expect(onSuccess).toHaveBeenCalledTimes(1);
    const message: string = onSuccess.mock.calls[0][0];
    expect(message).toContain('operator1');
    expect(message).toContain("registered email");
    // The response carries no password value, so the confirmation cannot
    // contain one; assert the message is only about delivery.
    expect(message.toLowerCase()).not.toMatch(/password:\s/);
  });

  it('surfaces the no-verified-email error in the modal (Requirement 4.4)', async () => {
    sendAdminForgotPassword.mockRejectedValue(
      new Error('The account has no verified email address')
    );
    const { onSuccess } = renderModal();

    fireEvent.click(sendButton());

    expect(
      await screen.findByText('The account has no verified email address')
    ).toBeInTheDocument();
    expect(onSuccess).not.toHaveBeenCalled();
  });

  it('surfaces delivery failures in the modal (Requirement 4.5)', async () => {
    sendAdminForgotPassword.mockRejectedValue(
      new Error('The temporary password was not sent: email delivery failed')
    );
    renderModal();

    fireEvent.click(sendButton());

    expect(
      await screen.findByText(
        'The temporary password was not sent: email delivery failed'
      )
    ).toBeInTheDocument();
  });
});

describe('RoleModal', () => {
  function renderModal(onSuccess = vi.fn()) {
    render(
      <RoleModal account={ACCOUNT} onSuccess={onSuccess} onDismiss={vi.fn()} />
    );
    return { onSuccess };
  }

  const changeButton = () =>
    screen.getByRole('button', { name: 'Change role' });

  // CONSCIOUS REPOINT (user-manager-datalabeler-role, design Decision 2):
  // this test was
  // ``offers exactly the five defined roles with the current role
  // preselected (Requirement 5.2)`` and pinned the five-role count that
  // dda-data-labeling Requirement 2.1 supersedes (DataLabeler is assigned/
  // revoked through the existing user administration functions). Its
  // unfixed assertion, VERBATIM (also recorded in that spec's task-2
  // preservation OUTCOME before the fix):
  //
  //     it('offers exactly the five defined roles with the current role preselected (Requirement 5.2)', () => {
  //       renderModal();
  //
  //       const select = createWrapper(document.body).findSelect()!;
  //
  //       // Current role preselected in the trigger.
  //       expect(select.findTrigger().getElement()).toHaveTextContent('Operator');
  //
  //       // Open the dropdown: exactly the five defined roles.
  //       select.openDropdown();
  //       const options = select.findDropdown().findOptions();
  //       expect(options.map((o) => o.getElement().textContent)).toEqual([
  //         ...PORTAL_ROLES,
  //       ]);
  //     });
  it('offers exactly the six defined roles with the current role preselected (Requirement 5.2, extended by dda-data-labeling Requirement 2.1)', () => {
    renderModal();

    const select = createWrapper(document.body).findSelect()!;

    // Current role preselected in the trigger.
    expect(select.findTrigger().getElement()).toHaveTextContent('Operator');

    // Open the dropdown: exactly the six defined roles (dda-data-labeling
    // Requirement 2.1 supersedes the original five-role count).
    select.openDropdown();
    const options = select.findDropdown().findOptions();
    expect(options.map((o) => o.getElement().textContent)).toEqual([
      ...PORTAL_ROLES,
    ]);
  });

  it('submits the selected role and reports success (Requirements 5.1, 5.7)', async () => {
    setAdminUserRole.mockResolvedValue({ message: 'ok' });
    const { onSuccess } = renderModal();

    const select = createWrapper(document.body).findSelect()!;
    select.openDropdown();
    select.selectOptionByValue('DataScientist');

    fireEvent.click(changeButton());

    await waitFor(() =>
      expect(setAdminUserRole).toHaveBeenCalledWith('operator1', 'DataScientist')
    );
    expect(onSuccess).toHaveBeenCalledWith(
      expect.stringContaining('DataScientist')
    );
    expect(onSuccess).toHaveBeenCalledWith(
      expect.stringContaining('operator1')
    );
  });

  it('shows the rejection reason in the modal, e.g. the last-PortalAdmin guard (Requirement 5.3)', async () => {
    const reason =
      'Cannot remove the PortalAdmin role from the last remaining enabled PortalAdmin account';
    setAdminUserRole.mockRejectedValue(new Error(reason));
    const { onSuccess } = renderModal();

    fireEvent.click(changeButton());

    expect(await screen.findByText(reason)).toBeInTheDocument();
    expect(onSuccess).not.toHaveBeenCalled();
  });
});

/** A 404 as thrown by the API client's simple error envelope path. */
function notFoundError(message = 'User not found'): Error {
  return Object.assign(new Error(message), { status: 404 });
}

describe('classifyDeleteError', () => {
  it('classifies an HTTP 404 as not-found (Requirement 14.11)', () => {
    expect(classifyDeleteError(notFoundError())).toBe('not-found');
  });

  it('classifies a partial verifier-cleanup message (Requirement 14.10)', () => {
    expect(
      classifyDeleteError(
        new Error(
          'The account was deleted but its verifier record was not removed'
        )
      )
    ).toBe('partial-cleanup');
  });

  it('classifies a rejection with the account unchanged as other (Requirement 14.3)', () => {
    expect(
      classifyDeleteError(
        Object.assign(new Error('Deletion rejected'), { status: 409 })
      )
    ).toBe('other');
    expect(classifyDeleteError(new Error('deletion failed'))).toBe('other');
    expect(classifyDeleteError(undefined)).toBe('other');
  });
});

describe('DeleteModal', () => {
  function renderModal(
    onSuccess = vi.fn(),
    onErrorWithRefresh = vi.fn(),
    onDismiss = vi.fn()
  ) {
    render(
      <DeleteModal
        account={ACCOUNT}
        onSuccess={onSuccess}
        onErrorWithRefresh={onErrorWithRefresh}
        onDismiss={onDismiss}
      />
    );
    return { onSuccess, onErrorWithRefresh, onDismiss };
  }

  const deleteButton = () =>
    screen.getByRole('button', { name: 'Delete account' });

  it('asks for explicit confirmation naming the account by username (Requirement 14.1)', () => {
    renderModal();

    expect(screen.getByText('Delete account operator1')).toBeInTheDocument();
    expect(deleteAdminUser).not.toHaveBeenCalled();
  });

  it('submits nothing on cancel (Requirement 14.9)', () => {
    const { onDismiss } = renderModal();

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(onDismiss).toHaveBeenCalledTimes(1);
    expect(deleteAdminUser).not.toHaveBeenCalled();
  });

  it('reports success identifying the deleted account for the parent to confirm and re-fetch (Requirements 14.2, 14.7)', async () => {
    deleteAdminUser.mockResolvedValue({ message: 'deleted' });
    const { onSuccess, onErrorWithRefresh } = renderModal();

    fireEvent.click(deleteButton());

    await waitFor(() =>
      expect(deleteAdminUser).toHaveBeenCalledWith('operator1')
    );
    expect(onSuccess).toHaveBeenCalledWith(
      expect.stringContaining('operator1')
    );
    expect(onErrorWithRefresh).not.toHaveBeenCalled();
  });

  it('shows the last-PortalAdmin rejection reason in the modal (Requirement 14.3)', async () => {
    const reason =
      'Cannot delete the last remaining enabled PortalAdmin account';
    deleteAdminUser.mockRejectedValue(
      Object.assign(new Error(reason), { status: 409 })
    );
    const { onSuccess, onErrorWithRefresh } = renderModal();

    fireEvent.click(deleteButton());

    expect(await screen.findByText(reason)).toBeInTheDocument();
    expect(onSuccess).not.toHaveBeenCalled();
    expect(onErrorWithRefresh).not.toHaveBeenCalled();
  });

  it('hands a not-found error to the parent for an error flashbar and list refresh (Requirement 14.11)', async () => {
    deleteAdminUser.mockRejectedValue(notFoundError());
    const { onSuccess, onErrorWithRefresh } = renderModal();

    fireEvent.click(deleteButton());

    await waitFor(() => expect(onErrorWithRefresh).toHaveBeenCalledTimes(1));
    const message: string = onErrorWithRefresh.mock.calls[0][0];
    expect(message).toContain('operator1');
    expect(message.toLowerCase()).toContain('not found');
    expect(onSuccess).not.toHaveBeenCalled();
  });

  it('hands a partial verifier-cleanup error to the parent for an error flashbar and list refresh (Requirement 14.10)', async () => {
    const partialMessage =
      'The account was deleted but its verifier record was not removed';
    deleteAdminUser.mockRejectedValue(new Error(partialMessage));
    const { onSuccess, onErrorWithRefresh } = renderModal();

    fireEvent.click(deleteButton());

    await waitFor(() =>
      expect(onErrorWithRefresh).toHaveBeenCalledWith(partialMessage)
    );
    expect(onSuccess).not.toHaveBeenCalled();
  });
});
