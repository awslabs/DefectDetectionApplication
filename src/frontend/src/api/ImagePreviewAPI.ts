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
import { ImageSourceConfiguration } from "../components/image-source/types";
import { APIList } from "../config/Interface";
import axios from "axios";

interface GetPreviewImageRequest {
  imageSourceConfiguration?: ImageSourceConfiguration;
}

/**
 * Response should be a base64 encoded string
 */
export interface GetPreviewImageResponse {
  image: string | null;
  imageFileName?: string;
}
export async function getImagePreview(
  id: string,
  request?: GetPreviewImageRequest,
): Promise<GetPreviewImageResponse> {
  const endpoint = `${APIList.imageSourcesAPI}/${id}/preview`;
  const { data } = await axios.post<GetPreviewImageResponse>(endpoint, request ?? {});
  return data;
}
