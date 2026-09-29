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
  StreamCredentials,
  StreamHealth,
  StreamSettings,
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
interface CreateICamImageSourceRequest {
  type: ImageSourceType.ICam;
  name: string;
  description?: string;
}
/**
 * An RTSP/RTMP camera (rtsp-rtmp-stream-cameras Requirement 4.1): the
 * Stream_URL goes in `location`. `credentials` is send-only; the API never
 * returns it.
 */
export interface CreateStreamImageSourceRequest {
  type: ImageSourceType.RTSP | ImageSourceType.RTMP;
  name: string;
  description?: string;
  location: string;
  streamSettings: StreamSettings;
  credentials?: StreamCredentials;
}
interface CreateImageSourceResponse {
  imageSourceId: string;
}
export async function createImageSource(
  request: CreateCameraImageSourceRequest | CreateFolderImageSourceRequest | CreateNvidiaCSIImageSourceRequest | CreateICamImageSourceRequest | CreateStreamImageSourceRequest,
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
/**
 * An RTSP/RTMP camera update: settings merge over the stored ones, omitted
 * credentials keep the stored ones, and `clearCredentials` removes them.
 */
export interface EditStreamImageSourceRequest {
  name?: string;
  description?: string;
  location?: string;
  streamSettings?: StreamSettings;
  credentials?: StreamCredentials;
  clearCredentials?: boolean;
}
interface EditImageSourceResponse {
  imageSourceId: string;
}

export async function editImageSource(
  id: string,
  request: EditCameraImageSourceRequest | EditFolderImageSourceRequest | EditStreamImageSourceRequest,
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

/** The outcome of a stream camera connection test (Requirement 4.3). */
export interface StreamConnectionTestResult {
  ok: boolean;
  /** The failure category, null on success. */
  category: string | null;
  /** A redacted, human-readable outcome. */
  message: string;
  streamHealth: StreamHealth;
  /** Base64 JPEG of the first frame through the pipeline, on success. */
  image?: string | null;
  imageError?: string | null;
}

/** The backend answers within 20 s; allow for the network on top. */
export const STREAM_CONNECTION_TEST_TIMEOUT_MS = 30_000;

export async function testStreamConnection(
  id: string,
): Promise<StreamConnectionTestResult> {
  const endpoint = `${APIList.imageSourcesAPI}/${id}/test-connection`;
  const { data } = await axios.post<StreamConnectionTestResult>(
    endpoint,
    undefined,
    { timeout: STREAM_CONNECTION_TEST_TIMEOUT_MS },
  );
  return data;
}

export async function getStreamHealth(id: string): Promise<StreamHealth> {
  const endpoint = `${APIList.imageSourcesAPI}/${id}/stream-health`;
  const { data } = await axios.get<StreamHealth>(endpoint);
  return data;
}
