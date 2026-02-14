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
import {
  Header,
  SpaceBetween,
  Button,
  Container,
  Form,
} from "@cloudscape-design/components";
import DetailsInput from "../details-input/DetailsInput";
import { ImageSourceType } from "../types";
import { schema, SchemaType } from "./schema";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { editImageSource, getImageSource } from "api/ImageSourceAPI";
import { useContext, useEffect } from "react";
import { AppLayoutContext } from "components/layout/AppLayoutContext";
import { FOLDER_PREFIX } from "../constants";
import FormPathInput from "components/form/FormPathInput";
import { getEditPath } from "./helpers";
import { formLayoutStyle } from "styles/common";
import { setHashValuesInUrl } from "components/utils";
import { DynamicRouterHashKey } from "components/layout/constants";

export default function EditImageSource(): JSX.Element {
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

  const editMutation = useMutation({
    mutationFn: (values: SchemaType) => {
      switch (values.type) {
        case ImageSourceType.Camera:
          return editImageSource(imageSourceId, {
            // Use spread operator so that we only include if value has changed
            ...(values.editName !== getQuery.data?.name && {
              name: values.editName,
            }),
            ...(values.editDescription !== getQuery.data?.description && {
              description: values.editDescription,
            }),
          });
        case ImageSourceType.NvidiaCSI:
          return editImageSource(imageSourceId, {
            ...(values.editName !== getQuery.data?.name && {
              name: values.editName,
            }),
            ...(values.editDescription !== getQuery.data?.description && {
              description: values.editDescription,
            }),
          });
        case ImageSourceType.Folder:
        default:
          return editImageSource(imageSourceId, {
            ...(values.editName !== getQuery.data?.name && {
              name: values.editName,
            }),
            ...(values.editDescription !== getQuery.data?.description && {
              description: values.editDescription,
            }),
            ...(FOLDER_PREFIX + values.path !== getQuery.data?.location && {
              location: FOLDER_PREFIX + values.path,
            }),
          });
      }
    },
    onSuccess: (data, values) => {
      const path = `/image-sources/${imageSourceId}`;
      addSuccess({
        content: (
          <>
            You successfully edited <strong>{values.editName}</strong>.
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
            Failed to edit <strong>{values.editName}</strong>. {error.message}
          </>
        ),
        action: (
          <Button
            onClick={(): void => {
              form.handleSubmit((values) => editMutation.mutate(values))();
            }}
          >
            Retry
          </Button>
        ),
      });
    },
  });

  const form = useForm<SchemaType>({
    resolver: yupResolver(schema),
    mode: "onSubmit",
    values: {
      editName: getQuery.data?.name ?? "",
      editDescription: getQuery.data?.description ?? "",
      path: getEditPath(getQuery.data?.location) ?? "",
      type: getQuery.data?.type ?? ImageSourceType.Camera,
    },
  });

  const type = form.watch("type");
  return (
    <FormProvider {...form}>
      <form
        onSubmit={form.handleSubmit((values) => editMutation.mutate(values))}
        className={formLayoutStyle}
      >
        <Form
          header={<Header variant="h1">Edit image source</Header>}
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                formAction="none"
                variant="link"
                onClick={(): void => navigate(-1)}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                formAction="submit"
                loading={editMutation.isLoading}
              >
                Save
              </Button>
            </SpaceBetween>
          }
        >
          <SpaceBetween direction="vertical" size="l">
            <DetailsInput namePrefix="edit" isLoading={getQuery.isLoading} />

            {type === ImageSourceType.Folder && (
              <Container>
                <FormPathInput
                  name="path"
                  prefix={FOLDER_PREFIX}
                  label={
                    <Header
                      variant="h2"
                      description="The path where the images to be used are stored."
                    >
                      Folder path
                    </Header>
                  }
                  placeholder="ImageRepository/"
                />
              </Container>
            )}
          </SpaceBetween>
        </Form>
      </form>
    </FormProvider>
  );
}
