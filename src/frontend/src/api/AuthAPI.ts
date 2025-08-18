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
import { Station } from "components/station/types";

export interface AuthConfigResponse {
  auth_enabled?: boolean;
  auth_settings?: {
    clientId?: string;
    authorizationEndpoint?: string;
    logoutEndpoint?: string;
  };
}

export async function validateTokenAPI(token: string): Promise<{ isTokenValid: boolean }> {
  const endpoint = APIList.getStation;
  return new Promise((resolve) => {
    axios.get<Station>(endpoint, { headers: { Authorization: `Bearer ${token || ""}` } })
      .then(() => resolve({ isTokenValid: true }))
      .catch((err) => {
        resolve({ isTokenValid: err?.response?.status !== 401 })
      })
  })
}

export async function fetchAuthConfig(): Promise<AuthConfigResponse> {
  const endpoint = APIList.getAuthConfig;
  const { data } = await axios.get<AuthConfigResponse>(endpoint);
  return data;
}