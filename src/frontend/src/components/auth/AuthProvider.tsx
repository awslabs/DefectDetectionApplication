/*
 *
 * Copyright 2025 Amazon Web Services, Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 *  You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under the License is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 */
import { ReactNode, useEffect, useRef } from "react";
import useAuth from "./authHook";
import { Alert, Button, Spinner } from "@cloudscape-design/components";
import { useLocation } from "react-router-dom";
import AuthRequireContent from "./AuthRequireContent";
import { authConfigErrorAlertStyle, authRequireContainerWrapperStyle } from "./styles";
import AuthLayout from "components/layout/AuthLayout";

interface AuthProviderProps {
  children: ReactNode;
}

function AuthConfigErrorAlert({ onRetry }: { onRetry: () => void }): JSX.Element {
  return (
    <div className={authRequireContainerWrapperStyle}>
      <Alert
        className={authConfigErrorAlertStyle}
        type="error"
        header="Unable to load authentication configuration"
        action={
          <Button onClick={onRetry}>
            Retry
          </Button>
        }
      >
        The service was unable to load this station's authentication configuration and received a 500 internal server error.
        This error can occur regardless if you have activated authentication.
        If this issue persists you should contact the person responsible for this station.
      </Alert>
    </div>
  );
}

export default function AuthProvider({ children }: AuthProviderProps): JSX.Element {

  const { token, isValidating, validateLogin, authEnabled, isAuthConfigLoaded, authConfigInitError, initAuthConfig } = useAuth();
  const { pathname } = useLocation();
  const initAuthCompleted = useRef(false);

  useEffect(() => {
    // Validate login token on page change
    if (isAuthConfigLoaded && authEnabled) {
      // only validate login when auth is enabled
      validateLogin();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pathname, isAuthConfigLoaded, authEnabled]);

  useEffect(() => {
    if (isValidating || !authEnabled) return;
    if (!!token) {
      initAuthCompleted.current = true;
    }
  }, [token, isValidating, authEnabled]);

  if (!isAuthConfigLoaded) {
    return (
      <AuthLayout>
        <Spinner size="big" />
      </AuthLayout>
    );
  }
  if (authConfigInitError) {
    return (
      <AuthLayout>
        <AuthConfigErrorAlert onRetry={initAuthConfig} />
      </AuthLayout>
    );
  }
  if (authEnabled) {
    if (isValidating && !initAuthCompleted.current) {
      return (
        <AuthLayout>
          <Spinner size="big" />
        </AuthLayout>
      );
    }
    if (!token) {
      return (
        <AuthLayout>
          <AuthRequireContent />
        </AuthLayout>
      )
    }
  }
  return <>{children}</>
}