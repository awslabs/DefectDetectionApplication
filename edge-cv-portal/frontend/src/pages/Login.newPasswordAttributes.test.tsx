/**
 * Unit tests for the first-login "Change Password" form in `pages/Login.tsx`.
 *
 * The user pool marks some attributes required (given_name and family_name
 * in the portal's pool). An account created without them receives a
 * NEW_PASSWORD_REQUIRED challenge that lists them as missing, and Cognito
 * rejects the new password ("Invalid attributes given, family_name is
 * missing") until every one of them is supplied with it. The form must
 * therefore render a required field for each attribute the challenge
 * reports missing, send them together with the new password, and ask for
 * nothing extra when the account's attributes are already set.
 *
 * Conventions follow Login.returnTo.test.tsx: `useNavigate` is a spy and
 * auth is driven through the mocked `useAuth` hook.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';

const { navigateMock, useAuthMock, completeNewPasswordMock } = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  useAuthMock: vi.fn(),
  completeNewPasswordMock: vi.fn(),
}));

vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => navigateMock,
}));

vi.mock('../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: useAuthMock,
}));

import Login, { requiredAttributeLabel } from './Login';

const PASSWORD = 'CorrectHorse1';

/** Mounts `Login` in the new-password challenge with the given missing attributes. */
function renderChallenge(missingAttributes: string[], authError: string | null = null) {
  useAuthMock.mockReturnValue({
    user: null,
    isAuthenticated: false,
    isLoading: false,
    needsNewPassword: true,
    missingAttributes,
    login: vi.fn(),
    completeNewPassword: completeNewPasswordMock,
    changePassword: vi.fn(),
    forgotPassword: vi.fn(),
    forgotPasswordSubmit: vi.fn(),
    logout: vi.fn(),
    error: authError,
  });
  return render(<Login />);
}

function fillPasswords(container: HTMLElement, value = PASSWORD) {
  const passwords = container.querySelectorAll('input[type="password"]');
  expect(passwords).toHaveLength(2);
  fireEvent.change(passwords[0], { target: { value } });
  fireEvent.change(passwords[1], { target: { value } });
}

function submit() {
  fireEvent.click(screen.getByText('Set New Password'));
}

beforeEach(() => {
  vi.clearAllMocks();
  completeNewPasswordMock.mockResolvedValue(undefined);
});

describe('requiredAttributeLabel', () => {
  it('names the standard name attributes the way the create form does', () => {
    expect(requiredAttributeLabel('given_name')).toBe('First name');
    expect(requiredAttributeLabel('family_name')).toBe('Last name');
  });

  it('derives a readable label for an attribute it does not know', () => {
    expect(requiredAttributeLabel('custom:department')).toBe('Department');
    expect(requiredAttributeLabel('zoneinfo')).toBe('Zoneinfo');
    expect(requiredAttributeLabel('preferred_username')).toBe('Preferred username');
  });
});

describe('Login — first-login form fields follow the challenge', () => {
  it('renders one field per attribute the challenge reports missing, replacing the single name field', () => {
    renderChallenge(['given_name', 'family_name']);

    expect(screen.getByLabelText('First name')).toBeInTheDocument();
    expect(screen.getByLabelText('Last name')).toBeInTheDocument();
    expect(screen.queryByText('Your Name')).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText('Enter your name')).not.toBeInTheDocument();
    // The banner tells the user why extra details are being asked for.
    expect(screen.getByText(/You must set a new password before continuing\./)).toHaveTextContent(
      /missing the details below/
    );
  });

  it('renders no extra field when the account already has every required attribute', () => {
    const { container } = renderChallenge([]);

    expect(screen.queryByLabelText('First name')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Last name')).not.toBeInTheDocument();
    expect(screen.getByText('You must set a new password before continuing.')).toBeInTheDocument();
    // Only the two password inputs remain.
    expect(container.querySelectorAll('input')).toHaveLength(2);
  });

  it('renders a field for an attribute outside the standard name set', () => {
    renderChallenge(['given_name', 'phone_number', 'custom:department']);

    expect(screen.getByLabelText('First name')).toBeInTheDocument();
    expect(screen.getByLabelText('Phone number')).toBeInTheDocument();
    expect(screen.getByLabelText('Department')).toBeInTheDocument();
  });
});

describe('Login — first-login submission', () => {
  it('sends every missing attribute, trimmed, together with the new password', async () => {
    const { container } = renderChallenge(['given_name', 'family_name']);

    fireEvent.change(screen.getByLabelText('First name'), { target: { value: '  Ada ' } });
    fireEvent.change(screen.getByLabelText('Last name'), { target: { value: 'Lovelace' } });
    fillPasswords(container);
    submit();

    await waitFor(() =>
      expect(completeNewPasswordMock).toHaveBeenCalledWith(PASSWORD, {
        given_name: 'Ada',
        family_name: 'Lovelace',
      })
    );
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith('/dashboard'));
  });

  it('sends no attributes when the challenge reported none missing', async () => {
    const { container } = renderChallenge([]);

    fillPasswords(container);
    submit();

    await waitFor(() =>
      expect(completeNewPasswordMock).toHaveBeenCalledWith(PASSWORD, undefined)
    );
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith('/dashboard'));
  });

  it('requires every missing attribute before submitting', async () => {
    const { container } = renderChallenge(['given_name', 'family_name']);

    fireEvent.change(screen.getByLabelText('First name'), { target: { value: 'Ada' } });
    fireEvent.change(screen.getByLabelText('Last name'), { target: { value: '   ' } });
    fillPasswords(container);
    submit();

    expect(await screen.findByText('Please enter your last name')).toBeInTheDocument();
    expect(completeNewPasswordMock).not.toHaveBeenCalled();
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it('sends a custom attribute under its own key', async () => {
    const { container } = renderChallenge(['custom:department']);

    fireEvent.change(screen.getByLabelText('Department'), { target: { value: 'Quality' } });
    fillPasswords(container);
    submit();

    await waitFor(() =>
      expect(completeNewPasswordMock).toHaveBeenCalledWith(PASSWORD, {
        'custom:department': 'Quality',
      })
    );
  });

  it("shows Cognito's error text when the challenge response is rejected", async () => {
    const cognitoMessage = 'Invalid attributes given, family_name is missing';
    completeNewPasswordMock.mockRejectedValue(new Error(cognitoMessage));
    const { container } = renderChallenge(['given_name', 'family_name']);

    fireEvent.change(screen.getByLabelText('First name'), { target: { value: 'Ada' } });
    fireEvent.change(screen.getByLabelText('Last name'), { target: { value: 'Lovelace' } });
    fillPasswords(container);
    submit();

    expect(await screen.findByText(cognitoMessage)).toBeInTheDocument();
    expect(navigateMock).not.toHaveBeenCalled();
  });
});
