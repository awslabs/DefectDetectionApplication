import { useState, useEffect } from 'react';
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

type LoginView = 'login' | 'new-password' | 'forgot' | 'reset-code';

export default function Login() {
  const navigate = useNavigate();
  const {
    login, completeNewPassword, forgotPassword, forgotPasswordSubmit,
    isAuthenticated, needsNewPassword, error: authError,
  } = useAuth();

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

  useEffect(() => {
    if (isAuthenticated) navigate('/dashboard');
  }, [isAuthenticated, navigate]);

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
      navigate('/dashboard');
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
