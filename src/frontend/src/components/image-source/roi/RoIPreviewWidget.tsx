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

import * as React from "react";
import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import RoIAnnotationImage, { NoCrop } from "./RoIAnnotationImage";
import { ImageSourceConfiguration, RegionOfInterest } from "../types";
import { getImagePreview } from "../../../api/ImagePreviewAPI";
import ImagePlaceholder from "components/common/ImagePlaceholder";
import ImagePreviewError from "components/live-result/ImagePreviewError";

interface RoIPreviewWidgetProps {
  imageSourceId: string;
  imageSourceConfiguration: ImageSourceConfiguration | undefined;
}

/**
 * This component is used to render a Readonly preview of the RegionOfInterest
 */
export default function RoIPreviewWidget({
  imageSourceId,
  imageSourceConfiguration,
}: RoIPreviewWidgetProps): JSX.Element {
  const [imageToRenderBase64, setImageToRender] = useState("");
  const [imageLoadError, setImageLoadError] = useState<string | undefined>(
    undefined,
  );
  const [isImageLoading, setImageLoading] = useState(true);
  const [regionOfInterestAnnotation, setRegionOfInterestAnnotation] =
    useState<RegionOfInterest>(imageSourceConfiguration?.imageCrop || NoCrop);
  const queryClient = useQueryClient();

  const getQuery = useQuery({
    queryKey: ["previewRoIWidget", imageSourceId],
    queryFn: async () => {
      setImageLoading(true);
      if (imageSourceConfiguration) {
        return await getImagePreview(imageSourceId, {
          imageSourceConfiguration: {
            ...imageSourceConfiguration,
            imageCrop: NoCrop,
          },
        });
      }
      return { image: "" };
    },
    onSuccess: (data) => {
      if (data.image) {
        setImageToRender(data.image);
        setImageLoading(false);
        queryClient.refetchQueries(["getImageSource", imageSourceId]);
        queryClient.refetchQueries(["listImageSources"]);
      }
    },
    onError: (error: any) => {
      setImageLoadError(error?.response?.data?.message ?? "");
      setImageLoading(false);
    },
  });

  useEffect(() => {
    getQuery.refetch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [imageSourceConfiguration]);

  if (imageLoadError !== undefined) {
    return (
      <ImagePlaceholder content={<ImagePreviewError errorMsg={imageLoadError} />} />
    );
  }

  return (
    <RoIAnnotationImage
      imageSrc={`data:image/jpg;base64, ${imageToRenderBase64}`}
      isImageLoading={isImageLoading}
      readonlyMode
      setImageCropPreview={(): void => { }}
      setImageCrop={(): void => { }}
      initialRegionOfInterest={
        imageSourceConfiguration?.imageCrop || NoCrop
      }
      regionOfInterestAnnotation={regionOfInterestAnnotation}
      setRegionOfInterestAnnotation={setRegionOfInterestAnnotation}
    />
  );
}
