import { useState, useEffect, useRef, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Container,
  Header,
  SpaceBetween,
  Button,
  FormField,
  Input,
  Alert,
  Link,
} from '@cloudscape-design/components';
import { useAuth } from '../contexts/AuthContext';
import { takeAttemptedLocation } from '../services/sessionRedirect';

type LoginView = 'login' | 'new-password' | 'forgot' | 'reset-code';

export default function Login() {
  const navigate = useNavigate();
  const {
    login, completeNewPassword, forgotPassword, forgotPasswordSubmit,
    isAuthenticated, needsNewPassword, error: authError, user,
  } = useAuth();

  // DataLabeler-only users land on the labeler workspace instead of the
  // dashboard (dda-data-labeling Req 2.2); every other role keeps the
  // historical /dashboard landing (Req 2.8). This stays the fallback for when
  // there is no remembered location to return to.
  const postLoginLanding = user?.role === 'DataLabeler' ? '/labeler' : '/dashboard';

  const [view, setView] = useState<LoginView>('login');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [givenName, setGivenName] = useState('');
  const [resetCode, setResetCode] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [successMsg, setSuccessMsg] = useState('');

  // The remembered Attempted_Location, consumed at most once per mount.
  // `undefined` means "not consumed yet"; `null` means "nothing usable was
  // remembered". Both post-sign-in navigations (the `isAuthenticated` effect
  // and the new-password handler) go through `resolveDestination`, so whichever
  // fires first they agree on one destination and cannot navigate to two
  // different places (design Decision 5).
  const rememberedRef = useRef<string | null | undefined>(undefined);

  const resolveDestination = useCallback(() => {
    if (rememberedRef.current === undefined) {
      // Read + clear + validate in one operation: a later ordinary sign-in
      // cannot resurrect a stale page and a role-guard bounce cannot
      // re-restore one (Requirements 2.4, 2.5). Unsafe or over-long values are
      // discarded by the validator, falling back to the landing page
      // (Requirement 3.2).
      rememberedRef.current = takeAttemptedLocation();
    }
    return rememberedRef.current ?? postLoginLanding;
  }, [postLoginLanding]);

  useEffect(() => {
    // Return the user to where the session ended when a valid location was
    // remembered by a Session_Exit; otherwise the historical landing page
    // (Requirements 2.1, 2.3). An already-authenticated visit to /login still
    // redirects immediately, exactly as today (Requirement 2.6).
    if (isAuthenticated) navigate(resolveDestination());
  }, [isAuthenticated, resolveDestination, navigate]);

  useEffect(() => {
    if (needsNewPassword) setView('new-password');
  }, [needsNewPassword]);

  const handleLogin = async () => {
    setLoading(true);
    setError('');
    try {
      if (!username || !password) { setError('Please enter username and password'); return; }
      await login(username, password);
    } catch (err: any) {
      if (!needsNewPassword) setError(err.message || 'Login failed.');
    } finally { setLoading(false); }
  };

  const handleNewPassword = async () => {
    setLoading(true);
    setError('');
    try {
      if (!newPassword || !confirmPassword) { setError('Please fill in all fields'); return; }
      if (newPassword !== confirmPassword) { setError('Passwords do not match'); return; }
      if (newPassword.length < 8) { setError('Password must be at least 8 characters'); return; }
      if (!givenName.trim()) { setError('Please enter your name'); return; }
      await completeNewPassword(newPassword, { given_name: givenName.trim() });
      // Same restoration through the new-password challenge (Requirement 2.2).
      navigate(resolveDestination());
    } catch (err: any) { setError(err.message || 'Failed to set password.'); }
    finally { setLoading(false); }
  };

  const handleForgotPassword = async () => {
    setLoading(true);
    setError('');
    try {
      if (!username) { setError('Enter your username first'); return; }
      await forgotPassword(username);
      setView('reset-code');
      setSuccessMsg('Verification code sent to your email.');
    } catch (err: any) { setError(err.message || 'Failed to send code.'); }
    finally { setLoading(false); }
  };

  const handleResetSubmit = async () => {
    setLoading(true);
    setError('');
    setSuccessMsg('');
    try {
      if (!resetCode || !newPassword || !confirmPassword) { setError('All fields required'); return; }
      if (newPassword !== confirmPassword) { setError('Passwords do not match'); return; }
      if (newPassword.length < 8) { setError('Password must be at least 8 characters'); return; }
      await forgotPasswordSubmit(username, resetCode, newPassword);
      setSuccessMsg('Password reset. You can now sign in.');
      setView('login');
      setPassword('');
    } catch (err: any) { setError(err.message || 'Failed to reset password.'); }
    finally { setLoading(false); }
  };

  return (
    <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '100vh', backgroundColor: '#f0f0f0' }}>
      <Container
        header={
          <Header variant="h1">
            {view === 'login' && 'Defect Detection Application'}
            {view === 'new-password' && 'Change Password'}
            {view === 'forgot' && 'Forgot Password'}
            {view === 'reset-code' && 'Reset Password'}
          </Header>
        }
      >
        <SpaceBetween size="l">
          {(error || authError) && <Alert type="error">{error || authError}</Alert>}
          {successMsg && <Alert type="success">{successMsg}</Alert>}

          {view === 'login' && (
            <>
              <FormField label="Username">
                <Input value={username} onChange={({ detail }) => setUsername(detail.value)}
                  placeholder="Enter your username" onKeyDown={(e) => e.detail.key === 'Enter' && handleLogin()} />
              </FormField>
              <FormField label="Password">
                <Input value={password} onChange={({ detail }) => setPassword(detail.value)}
                  type="password" placeholder="Enter your password" onKeyDown={(e) => e.detail.key === 'Enter' && handleLogin()} />
              </FormField>
              <Button variant="primary" fullWidth loading={loading} onClick={handleLogin}>Sign In</Button>
              <Link onFollow={() => { setError(''); setView('forgot'); }}>Forgot password?</Link>
            </>
          )}

          {view === 'new-password' && (
            <>
              <Alert type="info">You must set a new password before continuing.</Alert>
              <FormField label="Your Name">
                <Input value={givenName} onChange={({ detail }) => setGivenName(detail.value)}
                  placeholder="Enter your name" />
              </FormField>
              <FormField label="New Password">
                <Input type="password" value={newPassword} onChange={({ detail }) => setNewPassword(detail.value)}
                  onKeyDown={(e) => e.detail.key === 'Enter' && handleNewPassword()} />
              </FormField>
              <FormField label="Confirm New Password">
                <Input type="password" value={confirmPassword} onChange={({ detail }) => setConfirmPassword(detail.value)}
                  onKeyDown={(e) => e.detail.key === 'Enter' && handleNewPassword()} />
              </FormField>
              <Button variant="primary" fullWidth loading={loading} onClick={handleNewPassword}>Set New Password</Button>
            </>
          )}

          {view === 'forgot' && (
            <>
              <Alert type="info">Enter your username and we'll send a verification code to your email.</Alert>
              <FormField label="Username">
                <Input value={username} onChange={({ detail }) => setUsername(detail.value)}
                  placeholder="Enter your username" onKeyDown={(e) => e.detail.key === 'Enter' && handleForgotPassword()} />
              </FormField>
              <Button variant="primary" fullWidth loading={loading} onClick={handleForgotPassword}>Send Reset Code</Button>
              <Link onFollow={() => { setError(''); setView('login'); }}>Back to sign in</Link>
            </>
          )}

          {view === 'reset-code' && (
            <>
              <FormField label="Verification Code">
                <Input value={resetCode} onChange={({ detail }) => setResetCode(detail.value)} placeholder="Enter code from email" />
              </FormField>
              <FormField label="New Password">
                <Input type="password" value={newPassword} onChange={({ detail }) => setNewPassword(detail.value)} />
              </FormField>
              <FormField label="Confirm New Password">
                <Input type="password" value={confirmPassword} onChange={({ detail }) => setConfirmPassword(detail.value)}
                  onKeyDown={(e) => e.detail.key === 'Enter' && handleResetSubmit()} />
              </FormField>
              <Button variant="primary" fullWidth loading={loading} onClick={handleResetSubmit}>Reset Password</Button>
              <Link onFollow={() => { setError(''); setView('login'); }}>Back to sign in</Link>
            </>
          )}
        </SpaceBetween>
      </Container>
    </div>
  );
}
