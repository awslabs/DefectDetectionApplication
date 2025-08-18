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
import { isMock } from "../Common";
import { mockGetPreviewImage } from "./MockAPIs";
import { APIList } from "../config/Interface";

export interface LastCapturedImage {
  path: string;
  image: string;
}
interface DeleteCapturedImageResponse {
  imageFilePath: string;
}
interface PreviewImageResponse {
  image: string;
}

export async function getLastCaptureImages(
  path: string,
): Promise<LastCapturedImage[]> {
  const endpoint = `${APIList.capturedImageAPI}?path=${path}`;
  const { data } = await axios.get<LastCapturedImage[]>(endpoint);
  return data;
}

export async function deleteCapturedImage(
  path: string,
): Promise<DeleteCapturedImageResponse> {
  const endpoint = `${APIList.capturedImageAPI}?filePath=${path}`;
  const { data } = await axios.delete<DeleteCapturedImageResponse>(endpoint);
  return data;
}

interface CaptureImageRequest {
  filePrefix?: string;
}
export async function captureImage(id: string, prefix?: string): Promise<void> {
  const endpoint = `${APIList.imageSourcesAPI}/${id}/capture`;
  const request: CaptureImageRequest = {
    ...(prefix && { filePrefix: prefix }),
  };
  await axios.post<void>(endpoint, request);
  return;
}

export async function previewImage(id: string): Promise<PreviewImageResponse> {
  const endpoint = `${APIList.imageSourcesAPI}/${id}/preview`;
  const { data } = await axios.post<PreviewImageResponse>(endpoint);
  return data;
}

// TODO: delete following functions
export function getImageFromCamera(selectedCameraContext: any): any {
  if (isMock()) return mockGetPreviewImage();

  const cameraId = selectedCameraContext.selectedCamera.value;
  let previewAPI = APIList.previewImageAPI.replace(
    new RegExp("{camera_id}", "g"),
    cameraId,
  );
  // Workaround for bypass browser cache
  previewAPI += "?newTime=" + Date.now();
  console.log("preview: ", previewAPI);
  selectedCameraContext.setPreviewImageSrc(previewAPI);
  return previewAPI;
  //TODO: Throw error message when cannot load preview image
}