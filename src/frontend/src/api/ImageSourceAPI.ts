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
import {
  ImageSourceType,
  ImageSource,
  ImageSourceConfiguration,
} from "components/image-source/types";
import { APIList } from "config/Interface";

interface CreateCameraImageSourceRequest {
  type: ImageSourceType.Camera;
  name: string;
  description?: string;
  cameraId: string;
}
interface CreateFolderImageSourceRequest {
  type: ImageSourceType.Folder;
  name: string;
  description?: string;
  location: string;
}
interface CreateNvidiaCSIImageSourceRequest {
  type: ImageSourceType.NvidiaCSI;
  name: string;
  description?: string;
}
interface CreateImageSourceResponse {
  imageSourceId: string;
}
export async function createImageSource(
  request: CreateCameraImageSourceRequest | CreateFolderImageSourceRequest | CreateNvidiaCSIImageSourceRequest,
): Promise<CreateImageSourceResponse> {
  const endpoint = APIList.imageSourcesAPI;
  const { data } = await axios.post<CreateImageSourceResponse>(
    endpoint,
    request,
  );
  return data;
}

export async function getImageSource(id: string): Promise<ImageSource> {
  const endpoint = `${APIList.imageSourcesAPI}/${id}`;
  const { data } = await axios.get<ImageSource>(endpoint);
  return data;
}

export async function listImageSources(): Promise<ImageSource[]> {
  const endpoint = APIList.imageSourcesAPI;
  const { data } = await axios.get<ImageSource[]>(endpoint);
  return data;
}

interface EditCameraImageSourceRequest {
  name?: string;
  description?: string;
  imageSourceConfiguration?: ImageSourceConfiguration;
}
interface EditFolderImageSourceRequest {
  name?: string;
  description?: string;
  location?: string;
}
interface EditImageSourceResponse {
  imageSourceId: string;
}

export async function editImageSource(
  id: string,
  request: EditCameraImageSourceRequest | EditFolderImageSourceRequest,
): Promise<EditImageSourceResponse> {
  const endpoint = `${APIList.imageSourcesAPI}/${id}`;
  const { data } = await axios.patch<EditImageSourceResponse>(
    endpoint,
    request,
  );
  return data;
}

export async function deleteImageSource(id: string) {
  const endpoint = `${APIList.imageSourcesAPI}/${id}`;
  await axios.delete<void>(endpoint);
}
