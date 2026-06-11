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
import { APIList } from "../config/Interface";
import axios from "axios";
import { Camera } from "components/image-source/types";

export async function listCameras() {
  const endpoint = APIList.camerasAPI;
  const { data } = await axios.get<Camera[]>(endpoint);
  return data;
}

export async function connectCamera(cameraId: string) {
  const endpoint = `${APIList.camerasAPI}/${cameraId}/connect`;
  await axios.get<Camera[]>(endpoint);
  return cameraId;
}

export async function disconnectCamera(cameraId: string) {
  const endpoint = `${APIList.camerasAPI}/${cameraId}/disconnect`;
  await axios.get<Camera[]>(endpoint);
}

/**
 * A single adjustable camera feature, described generically so new controls
 * (read from the device's GenICam feature map) can be surfaced without
 * changing the response shape. Exposure time is reported in microseconds.
 */
export interface CameraFeatureBound {
  type: "float" | "integer" | "enumeration" | "boolean";
  min: number | null;
  max: number | null;
  increment: number | null;
  current: number | null;
  unit: string | null;
  options: string[];
}

export interface CameraFeatureBounds {
  gain?: CameraFeatureBound;
  exposure?: CameraFeatureBound;
  [feature: string]: CameraFeatureBound | undefined;
}

export async function getCameraFeatureBounds(
  cameraId: string,
): Promise<CameraFeatureBounds> {
  const endpoint = APIList.cameraFeatureBoundsAPI.replace(
    "{camera_id}",
    cameraId,
  );
  const { data } = await axios.get<CameraFeatureBounds>(endpoint);
  return data;
}