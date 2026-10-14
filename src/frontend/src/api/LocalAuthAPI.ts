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
import axios from "axios";
import { APIList } from "../config/Interface";

/** Response of the unauthenticated `GET /local-auth/status` endpoint. */
export interface LocalAuthStatusResponse {
  localLoginEnabled: boolean;
}

/**
 * Response of `POST /local-auth/login`.
 * `expiresAt` is an epoch timestamp in seconds (issuance + 12 h).
 */
export interface LocalLoginResponse {
  token: string;
  expiresAt: number;
  role: string;
  username: string;
}

/**
 * Fetch whether Local_Login is enabled on this device. Unauthenticated;
 * drives the LoginGate (design D8).
 */
export async function fetchLocalAuthStatus(): Promise<LocalAuthStatusResponse> {
  const { data } = await axios.get<LocalAuthStatusResponse>(
    APIList.getLocalAuthStatus,
  );
  return data;
}

/**
 * Authenticate against the Local_Credential_Cache.
 * Rejections: uniform 401 for any credential failure (Requirement 8.3),
 * 403 when local login is disabled (Requirement 9.5).
 */
export async function localLoginAPI(
  username: string,
  password: string,
): Promise<LocalLoginResponse> {
  const { data } = await axios.post<LocalLoginResponse>(
    APIList.postLocalAuthLogin,
    { username, password },
  );
  return data;
}
