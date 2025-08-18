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

interface DdaComponentStatus {
  status: string;
}

export async function getDdaComponentStatus(timeout?: number): Promise<string> {
  /**
   * This API can be stuck at pending forever after server restart. Cancel the API call after timeout and retry it.
   */
  const source = axios.CancelToken.source();
  let response: AxiosResponse<DdaComponentStatus, any> | void;
  if (timeout !== undefined && timeout > 0) {
    setTimeout(() => source.cancel(), timeout);
  }
  const endpoint = APIList.getDdaComponentStatus;
  return new Promise(async (resolve, reject) => {
    response = await axios.get<DdaComponentStatus>(endpoint, { cancelToken: source.token })
      .catch((error) => {
        if (axios.isCancel(error)) {
          resolve(getDdaComponentStatus(timeout));
        } else {
          reject("")
        }
      });
    if (response?.data !== undefined) {
      resolve(response.data.status);
    }
  })
}