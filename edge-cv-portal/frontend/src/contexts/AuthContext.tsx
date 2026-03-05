import { createContext, useContext, useState, useEffect, ReactNode } from 'react';
import { Amplify } from 'aws-amplify';
import { signIn, signOut, getCurrentUser, fetchAuthSession, confirmSignIn, updatePassword, resetPassword, confirmResetPassword } from 'aws-amplify/auth';
import { User, UserRole } from '../types';

interface AuthContextType {
  user: User | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  needsNewPassword: boolean;
  login: (username: string, password: string) => Promise<void>;
  completeNewPassword: (newPassword: string, userAttributes?: Record<string, string>) => Promise<void>;
  changePassword: (oldPassword: string, newPassword: string) => Promise<void>;
  forgotPassword: (username: string) => Promise<void>;
  forgotPasswordSubmit: (username: string, code: string, newPassword: string) => Promise<void>;
  logout: () => Promise<void>;
  error: string | null;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

let amplifyConfigured = false;

async function configureAmplify() {
  if (amplifyConfigured) return;
  
  try {
    console.log('Fetching config.json...');
    const response = await fetch('/config.json');
    if (!response.ok) {
      throw new Error(`Failed to fetch config.json: ${response.status}`);
    }
    const config = await response.json();
    console.log('Config loaded:', { 
      userPoolId: config.userPoolId, 
      userPoolClientId: config.userPoolClientId?.substring(0, 5) + '...',
      region: config.region 
    });
    
    if (!config.userPoolId || !config.userPoolClientId) {
      throw new Error('Auth UserPool not configured in config.json');
    }
    
    Amplify.configure({
      Auth: {
        Cognito: {
          userPoolId: config.userPoolId,
          userPoolClientId: config.userPoolClientId,
          loginWith: {
            username: true,
            email: true,
          },
        },
      },
    });
    
    console.log('Amplify configured successfully');
    amplifyConfigured = true;
  } catch (error) {
    console.error('Failed to configure Amplify:', error);
    throw error;
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [needsNewPassword, setNeedsNewPassword] = useState(false);

  useEffect(() => {
    initAuth();
  }, []);

  const initAuth = async () => {
    try {
      await configureAmplify();
      await checkAuth();
    } catch (err) {
      console.error('Failed to initialize auth:', err);
      setIsLoading(false);
    }
  };

  const checkAuth = async () => {
    try {
      const currentUser = await getCurrentUser();
      const session = await fetchAuthSession();
      
      if (session.tokens?.idToken) {
        const idToken = session.tokens.idToken.toString();
        localStorage.setItem('idToken', idToken);
        
        // Parse user info from token
        const payload = session.tokens.idToken.payload;
        setUser({
          user_id: payload.sub as string,
          email: payload.email as string,
          username: currentUser.username,
          role: (payload['custom:role'] as UserRole) || 'Viewer',
          is_super_user: false, // Will be fetched from API
        });
      }
    } catch (err) {
      console.log('Not authenticated');
      localStorage.removeItem('idToken');
    } finally {
      setIsLoading(false);
    }
  };

  const login = async (username: string, password: string) => {
    try {
      setError(null);
      setIsLoading(true);
      
      await configureAmplify();
      const result = await signIn({ username, password });
      
      if (result.isSignedIn) {
        setNeedsNewPassword(false);
        await checkAuth();
      } else if (result.nextStep?.signInStep === 'CONFIRM_SIGN_IN_WITH_NEW_PASSWORD_REQUIRED') {
        setNeedsNewPassword(true);
        setIsLoading(false);
        // Don't throw — the UI will show the change password form
      } else {
        throw new Error(`Unexpected sign-in step: ${result.nextStep?.signInStep}`);
      }
    } catch (err: any) {
      console.error('Login error:', err);
      setError(err.message || 'Login failed');
      throw err;
    } finally {
      setIsLoading(false);
    }
  };

  const completeNewPassword = async (newPassword: string, userAttributes?: Record<string, string>) => {
    try {
      setError(null);
      setIsLoading(true);
      
      // Cognito may require user attributes (like given_name) when completing the new password challenge.
      // Pass them as the challengeResponse along with the new password.
      const result = await confirmSignIn({ 
        challengeResponse: newPassword,
        options: userAttributes ? { userAttributes } : undefined,
      });
      
      if (result.isSignedIn) {
        setNeedsNewPassword(false);
        await checkAuth();
      } else {
        throw new Error('Password change succeeded but sign-in did not complete');
      }
    } catch (err: any) {
      console.error('Change password error:', err);
      setError(err.message || 'Failed to change password');
      throw err;
    } finally {
      setIsLoading(false);
    }
  };

  const forgotPassword = async (username: string) => {
    try {
      setError(null);
      await configureAmplify();
      await resetPassword({ username });
    } catch (err: any) {
      console.error('Forgot password error:', err);
      setError(err.message || 'Failed to send reset code');
      throw err;
    }
  };

  const forgotPasswordSubmit = async (username: string, code: string, newPassword: string) => {
    try {
      setError(null);
      await confirmResetPassword({ username, confirmationCode: code, newPassword });
    } catch (err: any) {
      console.error('Reset password error:', err);
      setError(err.message || 'Failed to reset password');
      throw err;
    }
  };

  const changePassword = async (oldPassword: string, newPassword: string) => {
    try {
      setError(null);
      await updatePassword({ oldPassword, newPassword });
    } catch (err: any) {
      console.error('Change password error:', err);
      setError(err.message || 'Failed to change password');
      throw err;
    }
  };

  const logout = async () => {
    try {
      await signOut();
      setUser(null);
      localStorage.removeItem('idToken');
    } catch (err) {
      console.error('Logout error:', err);
    }
  };

  return (
    <AuthContext.Provider
      value={{
        user,
        isAuthenticated: !!user,
        isLoading,
        needsNewPassword,
        login,
        completeNewPassword,
        changePassword,
        forgotPassword,
        forgotPasswordSubmit,
        logout,
        error,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}
