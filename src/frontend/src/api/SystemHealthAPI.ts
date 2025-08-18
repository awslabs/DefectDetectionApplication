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
import axios, { AxiosResponse } from "axios";
import { APIList } from "../config/Interface";
import { SystemHealth } from "components/application-health/types";


export async function getSystemHealth(timeout?: number): Promise<SystemHealth | null> {
  /**
   * This API can be stuck at pending forever after server restart. Cancel the API call after timeout.
   * Since we call this API repeatedly with useQuery, we don't need to retry the cancelled call.
   */
  const source = axios.CancelToken.source();
  let response: AxiosResponse<SystemHealth, any> | void;
  if (timeout !== undefined && timeout > 0) {
    setTimeout(() => source.cancel(), timeout);
  }
  const endpoint = APIList.systemHealth;
  response = await axios.get<SystemHealth>(endpoint, { cancelToken: source.token });
  if (response?.data !== undefined) {
    return response.data;
  }
  return null;
}