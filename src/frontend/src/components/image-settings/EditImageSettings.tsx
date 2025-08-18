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
import { FormProvider, useForm } from "react-hook-form";
import { yupResolver } from "@hookform/resolvers/yup";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { Button, ContentLayout, Header } from "@cloudscape-design/components";
import { schema, SchemaType } from "./edit/schema";
import * as React from "react";
import { useContext, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { editImageSource, getImageSource } from "../../api/ImageSourceAPI";
import {
  ImageSourceConfiguration,
  RegionOfInterest,
} from "../image-source/types";
import { AppLayoutContext } from "../layout/AppLayoutContext";
import EditImageSettingsPage from "./EditImageSettingsPage";
import { NoCrop } from "../image-source/roi/RoIAnnotationImage";
import { isArvisCameraImageSource, setHashValuesInUrl } from "components/utils";
import { DynamicRouterHashKey } from "components/layout/constants";

export default function EditImageSettings(): JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  const hash = location.hash;
  const imageSourceId = useParams().imageSourceId ?? "";
  const queryClient = useQueryClient();
  const { addSuccess, addError } = useContext(AppLayoutContext);

  const getQuery = useQuery({
    queryKey: ["getImageSource", imageSourceId],
    queryFn: () => getImageSource(imageSourceId),
  });

  const imgSrcName = getQuery.data?.name || "";

  useEffect(() => {
    const nextHash = setHashValuesInUrl(hash.substring(1), {
      [DynamicRouterHashKey.IMAGE_SOURCE_NAME]: encodeURIComponent(imgSrcName)
    });
    if (hash !== nextHash) navigate(nextHash, { replace: true });
  }, [hash, imgSrcName, navigate]);

  const [cropSettings, setCropSettings] = useState<RegionOfInterest>(NoCrop);

  useEffect(() => {
    if (getQuery.data?.imageSourceConfiguration.imageCrop) {
      setCropSettings(getQuery.data?.imageSourceConfiguration.imageCrop);
    }
  }, [getQuery.data]);

  const editMutation = useMutation({
    mutationFn: (values: SchemaType) => {
      const imageSourceConfiguration: ImageSourceConfiguration = {
        gain: values.editGain,
        exposure: values.editExposure,
        processingPipeline: values.editGstreamerPipeline,
        imageCrop: cropSettings,
      };

      return editImageSource(imageSourceId, {
        // Use spread operator so that we only include if value has changed
        imageSourceConfiguration: imageSourceConfiguration,
      });
    },
    onSuccess: (data, values) => {
      const path = `/image-sources/${imageSourceId}`;
      addSuccess({
        content: (
          <>
            You successfully edited <strong>{getQuery.data?.name}</strong>.
          </>
        ),
        relevantPath: path,
      });
      navigate(path);
      // Clears cache so queries are reloaded
      queryClient.clear();
    },
    onError: (error: Error, values) => {
      addError({
        content: (
          <>
            Failed to edit <strong>{imageSourceId}</strong>. {error.message}
          </>
        ),
        action: (
          <Button
            onClick={(): void => {
              form.handleSubmit((values) => {
                editMutation.mutate(values);

                queryClient.clear();
              })();
            }}
          >
            Retry
          </Button>
        ),
      });
    },
  });

  const form = useForm({
    resolver: yupResolver(schema),
    mode: "onBlur",
    values: {
      editGain: getQuery.data?.imageSourceConfiguration.gain ?? 0,
      editExposure: getQuery.data?.imageSourceConfiguration.exposure ?? 0,
      editGstreamerPipeline:
        getQuery.data?.imageSourceConfiguration.processingPipeline ?? "",
    },
  });
  const [initialGstreamerPipeline, setInitialGstreamerPipeline] = useState("");

  useEffect(() => {
    setInitialGstreamerPipeline(
      getQuery.data?.imageSourceConfiguration.processingPipeline ?? "",
    );
  }, [getQuery.data]);

  return (
    <ContentLayout
      header={
        <Header variant="h1">Edit {getQuery.data?.name} image settings</Header>
      }
    >
      <FormProvider {...form}>
        <form
          onSubmit={form.handleSubmit((values) => {
            editMutation.mutate({
              ...values,
            });
          })}
        >
          <EditImageSettingsPage
            id={imageSourceId}
            cropSettings={cropSettings}
            initialPipelineString={initialGstreamerPipeline ?? ""}
            isLoading={getQuery.isLoading}
            isArvisCamera={isArvisCameraImageSource(getQuery.data?.type || "")}
            cameraStatus={getQuery.data?.cameraStatus?.status}
            cameraId={getQuery.data?.cameraId || ""}
            recheckCameraStatusFn={getQuery.refetch}
          />
        </form>
      </FormProvider>
    </ContentLayout>
  );
}
