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
import { useCookies } from "react-cookie";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuthContext } from "./AuthContextProvider";
import { validateTokenAPI } from "api/AuthAPI";
import axios from "axios";

export const AUTH_COOKIE_NAME = "dda-edge-auth";
export const AUTH_STATE = "dda-edge-auth-state";

interface useAuthResponse {
  token: string;
  isValidating: boolean;
  validateLogin: () => Promise<void>;
  signout: () => Promise<void>;
  authEnabled: boolean;
  clientId: string;
  isAuthConfigLoaded: boolean;
  authEndpoint: string;
  authConfigInitError: boolean;
  initAuthConfig: () => void;
  isUrlParamsValidateFailed: boolean;
  setIsUrlParamsValidateFailed: (isUrlParamsValidateFailed: boolean) => void;
}

export default function useAuth(): useAuthResponse {
  const [cookies, setCookie, removeCookie] = useCookies([AUTH_COOKIE_NAME, AUTH_STATE]);
  const { hash, pathname } = useLocation();
  const urlSearchParams = new URLSearchParams(hash.substring(1));
  const authParam = decodeURIComponent(urlSearchParams.get("access_token") || "");
  const authStateParam = decodeURIComponent(urlSearchParams.get("state") || "");
  const { [AUTH_COOKIE_NAME]: authCookie, [AUTH_STATE]: authStateCookie } = cookies;
  // maintain token in React context so useAuth in different pages will access and control the same token and state
  // only set token when the token is validated
  const {
    token,
    setToken,
    isValidating,
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
  } = useAuthContext();

  const navigate = useNavigate();
  function validateToken(token: string): Promise<boolean> {
    return new Promise((resolve, reject) => {
      validateTokenAPI(token)
        .then(res => {
          resolve(res.isTokenValid);
        })
        .catch(() => reject(false));
    })
  }

  function setAxiosAuthHeader(token: string): void {
    axios.defaults.headers.common['Authorization'] = `Bearer ${token}`;
  }

  async function validateLogin(): Promise<void> {
    setIsUrlParamsValidateFailed(false);
    setIsValidating(true);
    let curToken = authCookie?.token;

    // check url
    if (!!authParam) {
      // validate auth state only when user tries to log into a new session using url param
      // auth state ref doc: https://developers.google.com/identity/openid-connect/openid-connect?hl=en#createxsrftoken
      if (!authStateParam || authStateParam !== authStateCookie) {
        // auth state param is invalid. stop validation
        setIsUrlParamsValidateFailed(true);
        setIsValidating(false);
      } else {
        // auth state param is valid. continue validation
        removeCookie(AUTH_STATE, { path: "/" });
        try {
          // validating url param token
          const validateRes = await validateToken(authParam);
          if (!!validateRes) {
            setAxiosAuthHeader(authParam);
            setCookie(AUTH_COOKIE_NAME, { ...authCookie, token: authParam }, { path: "/" });
            setToken(authParam);
            setIsValidating(false);
            // param token is valid, good to use. remove auth params from url
            navigate(pathname, { replace: true });
            return;
          } else {
            // url auth token is invalid.
            setIsUrlParamsValidateFailed(true);
          }
        } catch {
          // url auth token is invalid.
          setIsUrlParamsValidateFailed(true);
        }
      }
    }

    // check existing token in cookie, sign user out if cookie is invalid
    if (!curToken) {
      setToken("");
      setIsValidating(false);
      return;
    }
    validateToken(curToken)
      .then((res) => {
        if (!res) {
          // cookie token is invalid, removed cookie
          removeCookie(AUTH_COOKIE_NAME, { path: "/" });
          setToken("");
        } else {
          // cookie token is valid, good to use
          setAxiosAuthHeader(curToken);
          setToken(curToken);
        }
      })
      .finally(() => {
        setIsValidating(false);
      })
  }

  async function signout(): Promise<void> {
    removeCookie(AUTH_COOKIE_NAME, { path: "/" });
    setToken("");
    if (!!logoutEndpoint) {
      window.location.href = logoutEndpoint;
    }
  }

  return {
    token,
    isValidating,
    validateLogin,
    signout,
    authEnabled,
    clientId,
    isAuthConfigLoaded,
    authEndpoint,
    authConfigInitError,
    initAuthConfig,
    isUrlParamsValidateFailed,
    setIsUrlParamsValidateFailed,
  };
}