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
import { AuthConfigResponse, fetchAuthConfig } from "api/AuthAPI";
import { ReactNode, createContext, useContext, useEffect, useState } from "react";

interface AuthContextProps {
  token: string;
  isValidating: boolean;
  setToken: (token: string) => void;
  setIsValidating: (isValidating: boolean) => void;
  authEnabled: boolean;
  clientId: string;
  authEndpoint: string;
  logoutEndpoint: string;
  isAuthConfigLoaded: boolean;
  authConfigInitError: boolean;
  initAuthConfig: () => void;
  isUrlParamsValidateFailed: boolean;
  setIsUrlParamsValidateFailed: (isUrlParamsValidateFailed: boolean) => void;
}

const AuthContext = createContext<AuthContextProps>({
  token: "",
  isValidating: true,
  setToken: (token: string) => { },
  setIsValidating: (isValidating: boolean) => { },
  authEnabled: false,
  clientId: "",
  authEndpoint: "",
  logoutEndpoint: "",
  isAuthConfigLoaded: false,
  authConfigInitError: false,
  initAuthConfig: () => { },
  isUrlParamsValidateFailed: false,
  setIsUrlParamsValidateFailed: (isUrlParamsValidateFailed) => { },
});

export const AuthContextProvider = ({ children }: { children: ReactNode }): JSX.Element => {
  const [token, setToken] = useState("");
  const [isValidating, setIsValidating] = useState(true);
  const [authConfigInitError, setAuthConfigInitError] = useState(false);
  const [authConfig, setAuthConfig] = useState<AuthConfigResponse>();
  const [isAuthConfigLoaded, setIsAuthConfigLoaded] = useState(false);
  const [isUrlParamsValidateFailed, setIsUrlParamsValidateFailed] = useState(false);
  const { auth_enabled: authEnabled = true, auth_settings = {} } = authConfig || {};
  const {
    clientId = "",
    authorizationEndpoint: authEndpoint = "",
    logoutEndpoint = ""
  } = auth_settings || {};

  function initAuthConfig(): void {
    setIsAuthConfigLoaded(false);
    fetchAuthConfig()
      .then(res => {
        /**
         * default auth_enabled set to false in case auth config api failed,
         * so we wont block UI access for the users who didn't enable auth feature
         * for the user who enabled auth but auth config api failed, even they can access UI, they still cannot fetch anything from any working API
         */
        const { auth_enabled = false, auth_settings = {} } = res || {};
        setAuthConfigInitError(false);
        setAuthConfig({
          auth_enabled,
          auth_settings,
        })
        if (!auth_enabled) {
          setIsValidating(false);
        }
      })
      .catch(() => {
        setAuthConfigInitError(true);
      })
      .finally(() => setIsAuthConfigLoaded(true));
  }
  useEffect(() => {
    initAuthConfig();
  }, []);

  return (
    <AuthContext.Provider value={{
      token,
      isValidating,
      setToken,
      setIsValidating,
      authEnabled,
      clientId,
      isAuthConfigLoaded,
      authEndpoint,
      logoutEndpoint,
      authConfigInitError,
      initAuthConfig,
      isUrlParamsValidateFailed,
      setIsUrlParamsValidateFailed,
    }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuthContext = (): AuthContextProps => {
  return useContext(AuthContext);
};