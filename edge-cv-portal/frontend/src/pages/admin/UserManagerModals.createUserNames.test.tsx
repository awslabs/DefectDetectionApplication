/**
 * Unit tests for the User Manager CreateUserModal's name fields.
 *
 * The user pool requires the `given_name` and `family_name` attributes. An
 * account created without them is stuck at its first-login password change
 * (Cognito rejects the NEW_PASSWORD_REQUIRED response until both are
 * supplied), so the create form collects a required First name and Last
 * name and sends them with the request; the server rejects a request
 * missing either with a 400 naming the field.
 *
 * Conventions follow UserManagerModals.test.tsx: Cloudscape createWrapper
 * test-utils, hoisted apiService mock.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { CreateUserModal, validateCreateUserRequest } from './UserManagerModals';

const { createAdminUser } = vi.hoisted(() => ({
  createAdminUser: vi.fn(),
}));

vi.mock('../../services/api', () => ({
  apiService: {
    createAdminUser,
  },
}));

const VALID_FIELDS = {
  username: 'newuser1',
  given_name: 'New',
  family_name: 'User',
  email: 'new.user@example.com',
  role: 'Operator',
};

function isDisabled(button: HTMLElement): boolean {
  return (
    button.hasAttribute('disabled') ||
    button.getAttribute('aria-disabled') === 'true'
  );
}

function type(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

function chooseRole(role: string) {
  const select = createWrapper(document.body).findSelect()!;
  select.openDropdown();
  select.selectOptionByValue(role);
}

const createButton = () =>
  screen.getByRole('button', { name: 'Create user' });

beforeEach(() => {
  vi.clearAllMocks();
});

describe('validateCreateUserRequest — name fields', () => {
  it('accepts a request carrying both names', () => {
    expect(validateCreateUserRequest(VALID_FIELDS)).toEqual({});
  });

  it.each([
    ['given_name', 'First name is required.'],
    ['family_name', 'Last name is required.'],
  ] as const)('reports a missing %s', (field, message) => {
    expect(validateCreateUserRequest({ ...VALID_FIELDS, [field]: '' })).toEqual({
      [field]: message,
    });
    // Whitespace alone is no name either.
    expect(validateCreateUserRequest({ ...VALID_FIELDS, [field]: '   ' })).toEqual({
      [field]: message,
    });
  });
});

describe('CreateUserModal — first and last name', () => {
  it('offers First name and Last name fields', () => {
    render(<CreateUserModal onSuccess={vi.fn()} onDismiss={vi.fn()} />);

    expect(screen.getByLabelText('First name')).toBeInTheDocument();
    expect(screen.getByLabelText('Last name')).toBeInTheDocument();
  });

  it('keeps submit disabled until both names are filled', () => {
    render(<CreateUserModal onSuccess={vi.fn()} onDismiss={vi.fn()} />);

    type('Username', VALID_FIELDS.username);
    type('Email', VALID_FIELDS.email);
    chooseRole(VALID_FIELDS.role);
    expect(isDisabled(createButton())).toBe(true);

    type('First name', VALID_FIELDS.given_name);
    expect(isDisabled(createButton())).toBe(true);

    type('Last name', VALID_FIELDS.family_name);
    expect(isDisabled(createButton())).toBe(false);

    fireEvent.click(createButton());
    expect(createAdminUser).toHaveBeenCalledTimes(1);
  });

  it('shows the field error once an empty name field is left', () => {
    render(<CreateUserModal onSuccess={vi.fn()} onDismiss={vi.fn()} />);

    fireEvent.blur(screen.getByLabelText('First name'));
    fireEvent.blur(screen.getByLabelText('Last name'));

    expect(screen.getByText('First name is required.')).toBeInTheDocument();
    expect(screen.getByText('Last name is required.')).toBeInTheDocument();
    expect(createAdminUser).not.toHaveBeenCalled();
  });

  it('sends given_name and family_name, trimmed, with the request', async () => {
    createAdminUser.mockResolvedValue({ message: 'ok' });
    const onSuccess = vi.fn();
    render(<CreateUserModal onSuccess={onSuccess} onDismiss={vi.fn()} />);

    type('Username', VALID_FIELDS.username);
    type('First name', '  New ');
    type('Last name', ' User  ');
    type('Email', VALID_FIELDS.email);
    chooseRole(VALID_FIELDS.role);
    fireEvent.click(createButton());

    await waitFor(() =>
      expect(createAdminUser).toHaveBeenCalledWith({
        username: VALID_FIELDS.username,
        email: VALID_FIELDS.email,
        given_name: 'New',
        family_name: 'User',
        role: VALID_FIELDS.role,
      })
    );
    expect(onSuccess).toHaveBeenCalledWith(
      expect.stringContaining(VALID_FIELDS.username)
    );
  });

  it('shows a server rejection naming a missing name field verbatim', async () => {
    const reason =
      'family_name (last name) is required: the user pool requires it and the account could not complete its first sign-in without it';
    createAdminUser.mockRejectedValue(new Error(reason));
    const onSuccess = vi.fn();
    render(<CreateUserModal onSuccess={onSuccess} onDismiss={vi.fn()} />);

    type('Username', VALID_FIELDS.username);
    type('First name', VALID_FIELDS.given_name);
    type('Last name', VALID_FIELDS.family_name);
    type('Email', VALID_FIELDS.email);
    chooseRole(VALID_FIELDS.role);
    fireEvent.click(createButton());

    expect(await screen.findByText(reason)).toBeInTheDocument();
    expect(onSuccess).not.toHaveBeenCalled();
  });
});
