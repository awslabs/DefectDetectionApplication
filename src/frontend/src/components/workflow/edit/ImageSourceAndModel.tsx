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

import { Alert, Button, Container, Header, SelectProps, SpaceBetween } from "@cloudscape-design/components";
import FormSelect from "../../form/FormSelect";
import { useQuery } from "@tanstack/react-query";
import { listImageSources } from "api/ImageSourceAPI";
import { UseFormReturn } from "react-hook-form";
import { SchemaType } from "./schema";

interface ImageSourceAndModelProps {
  modelOptions: SelectProps.Options;
  isLoadingModelOptions: boolean;
  form: UseFormReturn<SchemaType>;
}

export default function ImageSourceAndModel({ modelOptions, isLoadingModelOptions, form }: ImageSourceAndModelProps): JSX.Element {
  const imageSourcesQuery = useQuery({
    queryKey: ["listImageSources", "editWorkflow"],
    queryFn: async () => {
      const imageSources = await listImageSources();
      const options = imageSources.map(({ name, type, imageSourceId }) => ({
        label: name,
        description: type,
        value: imageSourceId,
      }));
      return options;
    },
  });

  const model = form.watch("model");

  return (
    <Container header={<Header variant="h2">Image source and model</Header>}>
      <SpaceBetween direction="vertical" size="l">
        <FormSelect
          name="source"
          label="Image source"
          description="The image source for this workflow."
          placeholder="Choose an image source"
          options={imageSourcesQuery.data ?? []}
          triggerVariant="option"
          empty="No image sources"
          loadingText="Loading image sources"
          statusType={imageSourcesQuery.isLoading ? "loading" : "finished"}
          stretch
        />

        <FormSelect
          name="model"
          label={(
            <span>Model - <em>optional</em></span>
          )}
          description="Model for the workflow to capture images and run inference on the image source."
          placeholder="Choose a model"
          options={modelOptions}
          empty="No models"
          loadingText="Loading models"
          statusType={isLoadingModelOptions ? "loading" : "finished"}
          stretch
          extraAction={!!model && (
            <Button
              variant="normal"
              formAction="none"
              iconName="remove"
              onClick={(): void => form.setValue("model", null)}
            />
          )}
        />

        <Alert type="info">
          Workflows without models allow only capturing images. Choose a model to run inference as well.
        </Alert>
      </SpaceBetween>
    </Container>
  );
}
