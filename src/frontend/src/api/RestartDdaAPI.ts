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
import { getDdaComponentStatus } from "./getDdaComponentStatusAPI";
import { HEALTH_PAGE_API_TIMEOUT } from "components/application-health/constants";

const DDA_COMPONENT_STATUS_UNHEALTHY = "UNHEALTHY"

export async function restartDdaApplication() {
  const endpoint = APIList.restartDda;
  await axios.post<void>(endpoint);

  console.log("Start DDA application")
  // Wait for the application greengrass components to restart completely.
  let ddaComponentStatus = await getDdaComponentStatus(HEALTH_PAGE_API_TIMEOUT)

  while (ddaComponentStatus === DDA_COMPONENT_STATUS_UNHEALTHY) {
    await new Promise(res => setTimeout(res, 1000));
    try {
      ddaComponentStatus = await getDdaComponentStatus(HEALTH_PAGE_API_TIMEOUT);
    } catch (error) {
      // Local server will be down for sometime when restarting, network error is expected.
      if (axios.isAxiosError(error)) {
        if (error.response) {
          // The request was made and the server responded with a status code that falls out of the range of 2xx
          console.error(error);
          ddaComponentStatus = DDA_COMPONENT_STATUS_UNHEALTHY;
        } else if (error.request) {
          // The request was made but no response was received
          console.log(error);
        } else {
          // Something happened in setting up the request that triggered an Error
          console.error(error);
          ddaComponentStatus = DDA_COMPONENT_STATUS_UNHEALTHY;
        }
      } else {
        console.error(error);
        ddaComponentStatus = DDA_COMPONENT_STATUS_UNHEALTHY;
      }
    }
  }
}