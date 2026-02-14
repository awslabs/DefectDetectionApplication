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
} from '@cloudscape-design/components';
import { useAuth } from '../contexts/AuthContext';

export default function Login() {
  const navigate = useNavigate();
  const { login, isAuthenticated, error: authError } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (isAuthenticated) {
      navigate('/dashboard');
    }
  }, [isAuthenticated, navigate]);

  const handleLogin = async () => {
    setLoading(true);
    setError('');

    try {
      if (!username || !password) {
        setError('Please enter username and password');
        return;
      }
      
      await login(username, password);
      navigate('/dashboard');
    } catch (err: any) {
      setError(err.message || 'Login failed. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      style={{
        display: 'flex',
        justifyContent: 'center',
        alignItems: 'center',
        minHeight: '100vh',
        backgroundColor: '#f0f0f0',
      }}
    >
      <Container
        header={
          <Header variant="h1">
            Edge CV Admin Portal
          </Header>
        }
      >
        <SpaceBetween size="l">
          {(error || authError) && <Alert type="error">{error || authError}</Alert>}
          
          <FormField label="Username">
            <Input
              value={username}
              onChange={({ detail }) => setUsername(detail.value)}
              placeholder="Enter your username"
              onKeyDown={(e) => e.detail.key === 'Enter' && handleLogin()}
            />
          </FormField>

          <FormField label="Password">
            <Input
              value={password}
              onChange={({ detail }) => setPassword(detail.value)}
              type="password"
              placeholder="Enter your password"
              onKeyDown={(e) => e.detail.key === 'Enter' && handleLogin()}
            />
          </FormField>

          <Button
            variant="primary"
            fullWidth
            loading={loading}
            onClick={handleLogin}
          >
            Sign In
          </Button>
        </SpaceBetween>
      </Container>
    </div>
  );
}
