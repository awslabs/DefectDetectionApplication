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

import { useLocation, useNavigate, useParams } from "react-router-dom";
import { ContentLayout, Header } from "@cloudscape-design/components";
import * as React from "react";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getImageSource } from "../../../api/ImageSourceAPI";
import EditRoIPage from "./EditRoIPage";
import { NoCrop } from "./RoIAnnotationImage";
import { RegionOfInterest } from "../types";
import { isArvisCameraImageSource, setHashValuesInUrl } from "components/utils";
import { DynamicRouterHashKey } from "components/layout/constants";

export default function EditRegionOfInterestContainer(): JSX.Element {
  const imageSourceId = useParams().imageSourceId ?? "";
  const location = useLocation();
  const navigate = useNavigate();
  const hash = location.hash;

  const getQuery = useQuery({
    queryKey: ["getImageSource", imageSourceId],
    queryFn: () => getImageSource(imageSourceId),
    cacheTime: 0,
  });

  const imgSrcName = getQuery.data?.name || "";

  useEffect(() => {
    const nextHash = setHashValuesInUrl(hash.substring(1), {
      [DynamicRouterHashKey.IMAGE_SOURCE_NAME]: encodeURIComponent(imgSrcName)
    });
    if (hash !== nextHash) navigate(nextHash, { replace: true });
  }, [hash, imgSrcName, navigate]);

  const [initialRegionOfInterest, setInitialRegionOfInterest] =
    useState<RegionOfInterest>(NoCrop);

  useEffect(() => {
    setInitialRegionOfInterest(
      getQuery.data?.imageSourceConfiguration.imageCrop ?? NoCrop,
    );
  }, [getQuery.data]);

  return (
    <ContentLayout
      header={
        <Header variant="h1">
          Edit {getQuery.data?.name} region of interest
        </Header>
      }
    >
      <EditRoIPage
        id={imageSourceId}
        initialImageSource={getQuery.data}
        initialRegionOfInterest={initialRegionOfInterest}
        isLoading={getQuery.isLoading}
        isArvisCamera={isArvisCameraImageSource(getQuery.data?.type || "")}
        cameraId={getQuery.data?.cameraId || ""}
        cameraStatus={getQuery.data?.cameraStatus?.status}
        recheckCameraStatusFn={getQuery.refetch}
      />
    </ContentLayout>
  );
}
