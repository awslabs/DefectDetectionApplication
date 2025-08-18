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
import { v4 as uuidv4 } from "uuid";
import { Alert, Button, Container, Header } from "@cloudscape-design/components";
import { useState } from "react";
import { useCookies } from "react-cookie";
import useAuth, { AUTH_STATE } from "./authHook";
import { authRequireContainerWrapperStyle, authRequireContainerHeaderStyle, authRequireContainerStyle, authRequireContainerContentStyle, urlParamsValidateAlertStyle } from "./styles";

export default function AuthRequireContent(): JSX.Element {
  const setCookie = useCookies([AUTH_STATE])[1];
  const [isRedirectButtonLoading, setIsRedirectButtonLoading] = useState(false);
  const { clientId, authEndpoint, isUrlParamsValidateFailed, setIsUrlParamsValidateFailed } = useAuth();

  return (
    <div className={authRequireContainerWrapperStyle}>
      {isUrlParamsValidateFailed && (
        <Alert
          type="error"
          className={urlParamsValidateAlertStyle}
          header="Authentication error"
          dismissible
          onDismiss={(): void => setIsUrlParamsValidateFailed(false)}>
          The authentication page returned invalid information. Try again.
        </Alert>
      )}
      <Container
        className={authRequireContainerStyle}
        header={
          <Header className={authRequireContainerHeaderStyle}>
            Authentication required
          </Header>
        }
      >
        <div className={authRequireContainerContentStyle}>
          <Button
            variant="primary"
            loading={isRedirectButtonLoading}
            onClick={(): void => {
              setIsRedirectButtonLoading(true);
              const randomAuthState = uuidv4();
              // Store OAuthState into cookie for state validating after LCA login redirect back
              setCookie(AUTH_STATE, randomAuthState, { path: "/" });
              window.location.href = `${authEndpoint}?audience=&client_id=${encodeURIComponent(clientId)}&max_age=0&nonce=&prompt=&redirect_uri=${encodeURIComponent(window.location.origin)}&response_type=token&scope=openid+offline&state=${encodeURIComponent(randomAuthState)}`
            }}
          >
            Go to authentication page
          </Button>
        </div>
      </Container>
    </div>
  );
}