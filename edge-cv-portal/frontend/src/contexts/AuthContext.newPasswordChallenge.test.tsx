/**
 * Unit tests for `AuthContext`'s handling of the NEW_PASSWORD_REQUIRED
 * challenge.
 *
 * When a FORCE_CHANGE_PASSWORD account lacks attributes the user pool
 * requires, Cognito lists them in the challenge (Amplify v6 exposes them as
 * `nextStep.missingAttributes`) and rejects the new password until all of
 * them are supplied. The context must expose that list so the form can
 * collect the values, forward them with the new password, and surface
 * Cognito's error text when the response is rejected.
 *
 * `aws-amplify` and `aws-amplify/auth` are mocked; `/config.json` is served
 * by a stubbed `fetch`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, waitFor } from '@testing-library/react';
import { AuthProvider, useAuth } from './AuthContext';

const amplifyAuth = vi.hoisted(() => ({
  signIn: vi.fn(),
  signOut: vi.fn(),
  getCurrentUser: vi.fn(),
  fetchAuthSession: vi.fn(),
  confirmSignIn: vi.fn(),
  updatePassword: vi.fn(),
  resetPassword: vi.fn(),
  confirmResetPassword: vi.fn(),
}));

vi.mock('aws-amplify', () => ({ Amplify: { configure: vi.fn() } }));
vi.mock('aws-amplify/auth', () => amplifyAuth);

const NEW_PASSWORD_CHALLENGE = {
  isSignedIn: false,
  nextStep: {
    signInStep: 'CONFIRM_SIGN_IN_WITH_NEW_PASSWORD_REQUIRED',
    missingAttributes: ['given_name', 'family_name'],
  },
};

const SIGNED_IN = { isSignedIn: true, nextStep: { signInStep: 'DONE' } };

/** The latest value of the hook, captured by a probe rendered in the provider. */
let auth: ReturnType<typeof useAuth>;

function Probe() {
  auth = useAuth();
  return null;
}

async function renderProvider() {
  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>
  );
  await waitFor(() => expect(auth.isLoading).toBe(false));
}

function signedInSession() {
  amplifyAuth.getCurrentUser.mockResolvedValue({ username: 'user', userId: 'sub-1' });
  amplifyAuth.fetchAuthSession.mockResolvedValue({
    tokens: {
      idToken: {
        toString: () => 'id-token',
        payload: { sub: 'sub-1', email: 'user@example.com', 'custom:role': 'Viewer' },
      },
    },
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ userPoolId: 'pool', userPoolClientId: 'client-id', region: 'region' }),
    })
  );
  // Nobody is signed in when the provider mounts.
  amplifyAuth.getCurrentUser.mockRejectedValue(new Error('not authenticated'));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('AuthContext — NEW_PASSWORD_REQUIRED challenge', () => {
  it('exposes the attributes the challenge reports missing', async () => {
    amplifyAuth.signIn.mockResolvedValue(NEW_PASSWORD_CHALLENGE);
    await renderProvider();

    await act(() => auth.login('user', 'TemporaryPass1'));

    expect(auth.needsNewPassword).toBe(true);
    expect(auth.missingAttributes).toEqual(['given_name', 'family_name']);
    expect(auth.isAuthenticated).toBe(false);
  });

  it('exposes an empty list when the challenge names no missing attribute', async () => {
    amplifyAuth.signIn.mockResolvedValue({
      isSignedIn: false,
      nextStep: { signInStep: 'CONFIRM_SIGN_IN_WITH_NEW_PASSWORD_REQUIRED' },
    });
    await renderProvider();

    await act(() => auth.login('user', 'TemporaryPass1'));

    expect(auth.needsNewPassword).toBe(true);
    expect(auth.missingAttributes).toEqual([]);
  });

  it('sends the supplied attributes with the new password and completes sign-in', async () => {
    amplifyAuth.signIn.mockResolvedValue(NEW_PASSWORD_CHALLENGE);
    amplifyAuth.confirmSignIn.mockResolvedValue(SIGNED_IN);
    await renderProvider();
    await act(() => auth.login('user', 'TemporaryPass1'));

    signedInSession();
    await act(() =>
      auth.completeNewPassword('NewPassw0rd!', { given_name: 'Ada', family_name: 'Lovelace' })
    );

    expect(amplifyAuth.confirmSignIn).toHaveBeenCalledWith({
      challengeResponse: 'NewPassw0rd!',
      options: { userAttributes: { given_name: 'Ada', family_name: 'Lovelace' } },
    });
    expect(auth.isAuthenticated).toBe(true);
    expect(auth.needsNewPassword).toBe(false);
    expect(auth.missingAttributes).toEqual([]);
  });

  it('sends only the new password when there is nothing to supply', async () => {
    amplifyAuth.signIn.mockResolvedValue({
      isSignedIn: false,
      nextStep: { signInStep: 'CONFIRM_SIGN_IN_WITH_NEW_PASSWORD_REQUIRED', missingAttributes: [] },
    });
    amplifyAuth.confirmSignIn.mockResolvedValue(SIGNED_IN);
    await renderProvider();
    await act(() => auth.login('user', 'TemporaryPass1'));

    signedInSession();
    await act(() => auth.completeNewPassword('NewPassw0rd!'));

    expect(amplifyAuth.confirmSignIn).toHaveBeenCalledWith({
      challengeResponse: 'NewPassw0rd!',
      options: undefined,
    });
    expect(auth.isAuthenticated).toBe(true);
  });

  it("surfaces Cognito's error text when the challenge response is rejected", async () => {
    const cognitoMessage = 'Invalid attributes given, family_name is missing';
    amplifyAuth.signIn.mockResolvedValue(NEW_PASSWORD_CHALLENGE);
    amplifyAuth.confirmSignIn.mockRejectedValue(new Error(cognitoMessage));
    await renderProvider();
    await act(() => auth.login('user', 'TemporaryPass1'));

    // Caught inside the act scope: a rejection escaping `act` leaves React's
    // queued state updates unflushed, which would hide the recorded error.
    let thrown: unknown;
    await act(async () => {
      try {
        await auth.completeNewPassword('NewPassw0rd!', { given_name: 'Ada' });
      } catch (err) {
        thrown = err;
      }
    });

    // The caller (the form) still receives the rejection, and the context
    // records Cognito's message for display.
    expect(thrown).toBeInstanceOf(Error);
    expect((thrown as Error).message).toBe(cognitoMessage);
    expect(auth.error).toBe(cognitoMessage);
    expect(auth.needsNewPassword).toBe(true);
    expect(auth.missingAttributes).toEqual(['given_name', 'family_name']);
    expect(auth.isAuthenticated).toBe(false);
  });
});
