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
/**
 * LoginGate (portal-user-manager, Requirements 8.1, 8.8, 9.2; design D8).
 *
 * Wraps the whole app. Behavior:
 * - Fetches `GET /local-auth/status` on mount.
 * - Local login enabled and no unexpired Local_Session_Token in
 *   sessionStorage: renders the login screen (posting to
 *   `POST /local-auth/login`) and blocks every other view (8.1, 8.8).
 * - Local login disabled: renders the app directly — no login screen, no
 *   prompt (9.2).
 * - On successful login the token is stored in sessionStorage and attached
 *   as `Authorization: Bearer` to all API calls; any API 401 clears the
 *   token and returns to the login screen (token expiry mid-session).
 */
import axios, { AxiosError } from "axios";
import { ReactNode, useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Container,
  Form,
  FormField,
  Header,
  Input,
  SpaceBetween,
  Spinner,
} from "@cloudscape-design/components";
import AuthLayout from "components/layout/AuthLayout";
import {
  fetchLocalAuthStatus,
  localLoginAPI,
  LocalLoginResponse,
} from "api/LocalAuthAPI";
import { APIList } from "config/Interface";
import {
  LocalSession,
  clearLocalSessionAuthHeader,
  clearStoredSession,
  loadStoredSession,
  setLocalSessionAuthHeader,
  storeSession,
} from "./localSession";
import {
  authConfigErrorAlertStyle,
  authRequireContainerContentStyle,
  authRequireContainerHeaderStyle,
  authRequireContainerStyle,
  authRequireContainerWrapperStyle,
} from "./styles";

type GateStatus = "loading" | "error" | "enabled" | "disabled";

const UNIFORM_LOGIN_FAILURE_MESSAGE = "Invalid username or password.";
const LOGIN_UNAVAILABLE_MESSAGE =
  "Unable to reach the login service. Try again.";

interface LoginScreenProps {
  onSuccess: (session: LocalSession) => void;
  /** Called when the server reports local login is disabled (403). */
  onDisabled: () => void;
}

export function LocalLoginScreen({
  onSuccess,
  onDisabled,
}: LoginScreenProps): JSX.Element {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [errorText, setErrorText] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  async function submit(): Promise<void> {
    if (!username || !password || isSubmitting) return;
    setIsSubmitting(true);
    setErrorText("");
    try {
      const response: LocalLoginResponse = await localLoginAPI(
        username,
        password,
      );
      const session = storeSession(response);
      setLocalSessionAuthHeader(session.token);
      onSuccess(session);
    } catch (err) {
      const status = (err as AxiosError)?.response?.status;
      if (status === 401) {
        // Uniform failure: same message whether the username exists,
        // the password is wrong, or the account is locked out (8.3).
        setErrorText(UNIFORM_LOGIN_FAILURE_MESSAGE);
      } else if (status === 403) {
        // Local login was disabled while the screen was open (9.5);
        // hand control back to the gate to re-evaluate the status.
        onDisabled();
      } else {
        setErrorText(LOGIN_UNAVAILABLE_MESSAGE);
      }
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <AuthLayout>
      <div className={authRequireContainerWrapperStyle}>
        <Container
          className={authRequireContainerStyle}
          header={
            <Header className={authRequireContainerHeaderStyle}>
              Sign in
            </Header>
          }
        >
          <div className={authRequireContainerContentStyle}>
            <form
              style={{ width: "100%" }}
              onSubmit={(event): void => {
                event.preventDefault();
                void submit();
              }}
            >
              <Form
                errorText={errorText}
                actions={
                  <Button
                    variant="primary"
                    formAction="submit"
                    loading={isSubmitting}
                    disabled={!username || !password}
                  >
                    Sign in
                  </Button>
                }
              >
                <SpaceBetween direction="vertical" size="m">
                  <FormField label="Username">
                    <Input
                      value={username}
                      autoFocus
                      autoComplete="username"
                      onChange={({ detail }): void => setUsername(detail.value)}
                      ariaLabel="Username"
                    />
                  </FormField>
                  <FormField label="Password">
                    <Input
                      value={password}
                      type="password"
                      autoComplete="current-password"
                      onChange={({ detail }): void => setPassword(detail.value)}
                      ariaLabel="Password"
                    />
                  </FormField>
                </SpaceBetween>
              </Form>
            </form>
          </div>
        </Container>
      </div>
    </AuthLayout>
  );
}

export default function LoginGate({
  children,
}: {
  children: ReactNode;
}): JSX.Element {
  const [status, setStatus] = useState<GateStatus>("loading");
  const [session, setSession] = useState<LocalSession | null>(null);

  const initStatus = useCallback((): void => {
    setStatus("loading");
    fetchLocalAuthStatus()
      .then(({ localLoginEnabled }) => {
        if (localLoginEnabled) {
          // Reuse an unexpired token from sessionStorage so a reload does
          // not force a re-login (8.2: tokens are valid for 12 hours).
          const stored = loadStoredSession();
          if (stored) {
            setLocalSessionAuthHeader(stored.token);
          }
          setSession(stored);
          setStatus("enabled");
        } else {
          // Disabled: the app renders directly, no login screen or
          // prompt (9.2).
          setStatus("disabled");
        }
      })
      .catch(() => setStatus("error"));
  }, []);

  useEffect(() => {
    initStatus();
  }, [initStatus]);

  const handleUnauthorized = useCallback((activeToken: string): void => {
    clearStoredSession();
    clearLocalSessionAuthHeader(activeToken);
    setSession(null);
  }, []);

  // While a local session is active, any API 401 (expired, revoked, or
  // disabled account, 8.5-8.7) clears the token and returns to the login
  // screen.
  useEffect(() => {
    if (status !== "enabled" || !session) return;
    const activeToken = session.token;
    const interceptorId = axios.interceptors.response.use(
      (response) => response,
      (error: AxiosError) => {
        const isLoginCall = error?.config?.url === APIList.postLocalAuthLogin;
        if (error?.response?.status === 401 && !isLoginCall) {
          handleUnauthorized(activeToken);
        }
        return Promise.reject(error);
      },
    );
    return (): void => {
      axios.interceptors.response.eject(interceptorId);
    };
  }, [status, session, handleUnauthorized]);

  if (status === "loading") {
    return (
      <AuthLayout>
        <Spinner size="big" />
      </AuthLayout>
    );
  }

  if (status === "error") {
    return (
      <AuthLayout>
        <div className={authRequireContainerWrapperStyle}>
          <Alert
            className={authConfigErrorAlertStyle}
            type="error"
            header="Unable to load login configuration"
            action={<Button onClick={initStatus}>Retry</Button>}
          >
            The service was unable to determine whether local login is
            enabled on this station. If this issue persists, contact the
            person responsible for this station.
          </Alert>
        </div>
      </AuthLayout>
    );
  }

  if (status === "enabled" && !session) {
    return (
      <LocalLoginScreen
        onSuccess={(newSession): void => setSession(newSession)}
        onDisabled={initStatus}
      />
    );
  }

  return <>{children}</>;
}
