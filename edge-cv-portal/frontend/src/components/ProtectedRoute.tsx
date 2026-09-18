import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { Spinner } from '@cloudscape-design/components';
import { saveAttemptedLocation } from '../services/sessionRedirect';

export default function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading } = useAuth();
  const location = useLocation();

  if (isLoading) {
    return (
      <div
        style={{
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'center',
          minHeight: '100vh',
        }}
      >
        <Spinner size="large" />
      </div>
    );
  }

  if (!isAuthenticated) {
    // Session_Exit: remember where the user was trying to go so `Login` can
    // put them back there (Requirement 1.2). The router `state` below is
    // belt-and-braces for the in-router case; the value saved here is what
    // `Login` actually reads, so this path and the API clients' 401 path
    // behave identically (design Decision 1).
    saveAttemptedLocation(location);
    return <Navigate to="/login" replace state={{ from: location }} />;
  }

  return <>{children}</>;
}
