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
import Form from "@cloudscape-design/components/form";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import { Alert, Pagination, Table } from "@cloudscape-design/components";
import { useNavigate } from "react-router-dom";
import { useCollection } from "@cloudscape-design/collection-hooks";
import { FormProvider, useForm } from "react-hook-form";
import { yupResolver } from "@hookform/resolvers/yup";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { listCameras } from "api/CameraAPI";
import { createImageSource } from "api/ImageSourceAPI";
import EmptyTable from "components/empty-table/EmptyTable";
import FormRadioGroup from "components/form/FormRadioGroup";
import DetailsInput from "../details-input/DetailsInput";
import { Camera, ImageSourceType } from "../types";
import { SchemaType, schema } from "./schema";
import { useContext, useState } from "react";
import { AppLayoutContext } from "components/layout/AppLayoutContext";
import { FOLDER_PREFIX } from "../constants";
import FormPathInput from "components/form/FormPathInput";
import { formLayoutStyle } from "styles/common";

export default function AddImageSource(): JSX.Element {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { addSuccess, addError } = useContext(AppLayoutContext);

  const [selectedItems, setSelectedItems] = useState<Camera[]>([]);
  const listQuery = useQuery({
    queryKey: ["listCameras"],
    queryFn: () => listCameras(),
    onSuccess: (data) => {
      if (data.length > 0 && selectedItems.length === 0) {
        setSelectedItems([data[0]]);
      }
    },
  });
  const cameras = listQuery.data ?? [];

  const {
    items,
    paginationProps,
    // Extract out ref so we don't pass it into <FormTable>
    collectionProps: { ref, ...collectionProps },
  } = useCollection(cameras, {
    sorting: {},
    pagination: {},
  });

  const createMutation = useMutation({
    mutationFn: (values: SchemaType) => {
      switch (values.type) {
        case ImageSourceType.Camera:
          return createImageSource({
            type: ImageSourceType.Camera,
            name: values.cameraName ?? "",
            description: values.cameraDescription,
            cameraId: selectedItems[0]?.id ?? "",
          });
        case ImageSourceType.Folder:
        default:
          return createImageSource({
            type: ImageSourceType.Folder,
            name: values.folderName ?? "",
            description: values.folderDescription,
            // Hardcoding prefix to location
            location: FOLDER_PREFIX + values.path ?? "",
          });
      }
    },
    onSuccess: (data, values) => {
      const path = `/image-sources/${data.imageSourceId}`;
      addSuccess({
        content: (
          <>
            You successfully added{" "}
            <strong>
              {values.type === ImageSourceType.Camera
                ? values.cameraName
                : values.folderName}
            </strong>
            .
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
            Failed to add{" "}
            <strong>
              {values.type === ImageSourceType.Camera
                ? values.cameraName
                : values.folderName}
            </strong>
            . {error.message}
          </>
        ),
        action: (
          <Button
            onClick={(): void => {
              form.handleSubmit((values) => createMutation.mutate(values))();
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
      type: ImageSourceType.Camera,
      cameraName: cameras.length > 0 ? cameras[0].id : "",
    },
  });

  const type = form.watch("type");
  return (
    <FormProvider {...form}>
      <form
        onSubmit={form.handleSubmit((values) => createMutation.mutate(values))}
        className={formLayoutStyle}
      >
        <Form
          header={<Header variant="h1">Add image source</Header>}
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
                loading={createMutation.isLoading}
                formAction="submit"
                disabled={
                  type === ImageSourceType.Camera && cameras.length === 0
                }
              >
                Save
              </Button>
            </SpaceBetween>
          }
        >
          <SpaceBetween direction="vertical" size="l">
            <Container>
              <FormRadioGroup
                name="type"
                label={
                  <Header
                    variant="h2"
                    description="Set up a camera or a folder repository of images."
                  >
                    Type
                  </Header>
                }
                items={[
                  { value: ImageSourceType.Camera, label: "Camera" },
                  { value: ImageSourceType.Folder, label: "Folder" },
                ]}
              />
            </Container>

            {type === ImageSourceType.Camera && (
              <>
                <Container>
                  <Table
                    {...collectionProps}
                    variant="embedded"
                    loading={listQuery.isFetching}
                    loadingText="Loading cameras"
                    header={
                      <Header
                        variant="h2"
                        actions={
                          <Button
                            formAction="none"
                            iconName="refresh"
                            onClick={(): void => {
                              listQuery.refetch();
                            }}
                            loading={listQuery.isFetching}
                          >
                            Rediscover cameras
                          </Button>
                        }
                      >
                        Cameras discovered
                      </Header>
                    }
                    items={items}
                    columnDefinitions={[
                      {
                        id: "id",
                        header: "Name",
                        cell: (item) => item.id,
                        sortingField: "id",
                      },
                    ]}
                    pagination={
                      paginationProps.pagesCount > 1 && (
                        <Pagination {...paginationProps} />
                      )
                    }
                    selectionType="single"
                    selectedItems={selectedItems}
                    onSelectionChange={(event): void => {
                      form.setValue(
                        "cameraName",
                        event.detail.selectedItems[0].id,
                        {
                          shouldValidate: true,
                        },
                      );
                      setSelectedItems(event.detail.selectedItems);
                    }}
                    empty={
                      <EmptyTable
                        header="No cameras discovered"
                        message="No cameras to display."
                        action={
                          <Button
                            formAction="none"
                            iconName="refresh"
                            onClick={(): void => {
                              listQuery.refetch();
                            }}
                          >
                            Rediscover cameras
                          </Button>
                        }
                      />
                    }
                  />
                </Container>

                {cameras.length > 0 || listQuery.isLoading ? (
                  <DetailsInput namePrefix="camera" isLoading={false} />
                ) : (
                  <Alert type="error" header="No cameras discovered">
                    To use a camera as an image source, the camera must be
                    discoverable. Make sure the camera is on the same network as
                    this station.
                  </Alert>
                )}
              </>
            )}

            {type === ImageSourceType.Folder && (
              <>
                <Container>
                  <FormPathInput
                    stretch
                    name="path"
                    prefix={FOLDER_PREFIX}
                    label={
                      <Header
                        variant="h2"
                        description="The path where source images are stored."
                      >
                        Folder path
                      </Header>
                    }
                    placeholder="ImageRepository/"
                  />
                </Container>
                <DetailsInput namePrefix="folder" isLoading={false} />
              </>
            )}
          </SpaceBetween>
        </Form>
      </form>
    </FormProvider>
  );
}
