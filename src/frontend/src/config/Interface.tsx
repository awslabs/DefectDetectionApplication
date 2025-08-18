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

const DEFAULT_LOCAL_SERVER_API_PORT = "5000";

function getLocalServerPort(): string {
  // currently REACT_APP_SERVER_V returns with a hard-coded "localhost" hostname (i.e. http://localhost:5000)
  // the following line maps the local server api hostname to match the hostname serving the local ui
  // for example, if local UI is invoked with a custom ip http://10.95.199.150:3000, the UI will also call the API
  // at http://10.95.199.160:5000 instead of http://localhost:5000 which will be blocked due to CORS
  // TODO: ideally we just get the port number as an environment variable directly rather than needing to
  // parse it from REACT_APP_SERVER_V
  const appServerLocation = process.env.REACT_APP_SERVER_V || "";
  try {
    return new URL(appServerLocation).port;
  } catch (e) {
    return DEFAULT_LOCAL_SERVER_API_PORT;
  }
}

const DEFAULT_LOCAL_SERVER_API_PORT_SSL = "5443";
function getLocalServerPortSSL(): string {
  const appServerLocation = process.env.REACT_APP_SERVER_V_SSL || "";
  try {
    return new URL(appServerLocation).port;
  } catch (e) {
    return DEFAULT_LOCAL_SERVER_API_PORT_SSL;
  }
}

function getLocalServerEndpoint() {
  if (window.location.protocol.includes("https")) {
    // Requires the backend server to be configured to run on 5443, which it will if authorization_settings.json is present.
    return `https://${window.location.hostname}:${getLocalServerPortSSL()}`;
  } else {
    return `http://${window.location.hostname}:${getLocalServerPort()}`;
  }
}

export const Connection = {
  ENDPOINT: getLocalServerEndpoint(),
};

export const APIList = {
  camerasAPI: `${Connection.ENDPOINT}/cameras`,
  previewImageAPI: `${Connection.ENDPOINT}/cameras/{camera_id}/preview`,
  executePipelineAPI: `${Connection.ENDPOINT}/cameras/{camera_id}/execute-pipeline`,
  listStreamsAPI: `${Connection.ENDPOINT}/streams`,
  getStreamImageAPI: `${Connection.ENDPOINT}/streams/{stream_id}/images?maxResults=1`,
  capturedImageAPI: `${Connection.ENDPOINT}/captured-images`,
  imageSourcesAPI: `${Connection.ENDPOINT}/image-sources`,
  featureConfigurations: `${Connection.ENDPOINT}/feature-configurations`,
  workflows: `${Connection.ENDPOINT}/workflows`,
  workflowCaptureTaskAPI: `${Connection.ENDPOINT}/workflows/{workflow_id}/capture-task`,
  workflowResultAPI: `${Connection.ENDPOINT}/workflows/{workflow_id}/results/{capture_id}`,
  systemHealth: `${Connection.ENDPOINT}/system-health`,
  snapshot: `${Connection.ENDPOINT}/snapshot`,
  restartDda: `${Connection.ENDPOINT}/restart-dda`,
  getDdaComponentStatus: `${Connection.ENDPOINT}/dda-component-status`,
  getStation: `${Connection.ENDPOINT}/system/station`,
  getCapture: `${Connection.ENDPOINT}/workflows/{workflow_id}/capture-details/{capture_id}`,
  getAuthConfig: `${Connection.ENDPOINT}/authorization-configurations`,
};

export const AppDescriptions = {
  captureImageDes:
    "Use this interface to capture images from a specific camera. " +
    "The images will be saved to a local folder which you can then later manually upload via the cloud interface to a specific dataset",
  processImageDes:
    "Observe results from your cameras which have an assigned model.",
  viewResiltsDes:
    "Use this interface to view the most recent result processed on a specific camera.",
  viewModelsDes: "Review the models deployed to this station.",
};
