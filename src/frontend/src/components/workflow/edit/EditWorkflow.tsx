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

import {
  Header,
  SpaceBetween,
  Button,
  Form,
} from "@cloudscape-design/components";
import { yupResolver } from "@hookform/resolvers/yup";
import { useForm, FormProvider } from "react-hook-form";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import schema, { SchemaType } from "./schema";
import {
  FeatureConfigurationType,
  Rule,
  SignalType,
  WorkflowTrigger,
} from "../types";
import WorkflowDetails from "./WorkflowDetails";
import ImageSourceAndModel from "./ImageSourceAndModel";
import Inputs from "./Inputs";
import Outputs from "./outputs/Outputs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { editWorkflow, getWorkflow } from "api/WorkflowAPI";
import { useContext, useEffect } from "react";
import { AppLayoutContext } from "components/layout/AppLayoutContext";
import { formLayoutStyle } from "styles/common";
import { getWorkflowModelOptionLabel, getWorkflowModelOptionLabelWithoutVersion, setHashValuesInUrl, sortWorkflowModelOptions } from "components/utils";
import { DynamicRouterHashKey } from "components/layout/constants";
import { listFeatureConfigurations } from "api/FeatureConfigurationAPI";

export default function EditWorkflow(): JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  const hash = location.hash;
  const workflowId = useParams().workflowId ?? "";
  const queryClient = useQueryClient();
  const { addSuccess, addError } = useContext(AppLayoutContext);

  const getQuery = useQuery({
    queryKey: ["getWorkflow", workflowId],
    queryFn: () => getWorkflow(workflowId),
  });

  const { data: featureConfigurations = [], isLoading: isModelsLoading } = useQuery({
    queryKey: ["listModels", "editWorkflow"],
    queryFn: listFeatureConfigurations,
  });

  const modelOptions = featureConfigurations?.sort(sortWorkflowModelOptions)?.map((config) => ({
    label: getWorkflowModelOptionLabelWithoutVersion(config),
    value: config.modelName,
  })) || [];

  const workflowName = getQuery.data?.name || "";

  useEffect(() => {
    const nextHash = setHashValuesInUrl(hash.substring(1), {
      [DynamicRouterHashKey.WORKFLOW_NAME]: encodeURIComponent(workflowName)
    });
    if (hash !== nextHash) navigate(nextHash, { replace: true });
  }, [workflowName, hash, navigate]);

  const inputConfiguration = getQuery.data?.inputConfigurations?.[0];
  const featureConfiguration = getQuery.data?.featureConfigurations?.[0];
  const imageSource = getQuery.data?.imageSources?.[0];
  const outputConfigurations = getQuery.data?.outputConfigurations ?? [];
  const form = useForm<SchemaType>({
    resolver: yupResolver(schema),
    mode: "onSubmit",
    values: {
      name: getQuery.data?.name ?? "",
      description: getQuery.data?.description ?? "",
      source: imageSource
        ? {
          label: imageSource.name,
          value: imageSource.imageSourceId,
          description: imageSource.type,
        }
        : null,
      model: featureConfiguration?.modelName
        ? {
          value: featureConfiguration.modelName,
          label: getWorkflowModelOptionLabelWithoutVersion(featureConfiguration),
        }
        : null,
      trigger: inputConfiguration
        ? WorkflowTrigger.DigitalInput
        : WorkflowTrigger.RestApi,
      signal: inputConfiguration?.triggerState ?? SignalType.RisingEdge,
      pin: isNaN(Number(inputConfiguration?.pin))
        ? undefined
        : Number(inputConfiguration?.pin),
      debounce: inputConfiguration?.debounceTime,
      outputs: outputConfigurations.map(
        ({ signalType, pin, pulseWidth, rule }) => ({
          signal: signalType,
          pin: Number(pin),
          debounce: pulseWidth,
          rule: {
            value: rule,
          },
        }),
      ),
    },
  });

  const editMutation = useMutation({
    mutationFn: ({
      name,
      description = "",
      source,
      model,
      trigger,
      signal,
      pin,
      debounce,
      outputs = [],
    }: SchemaType) =>
      editWorkflow(workflowId, {
        name,
        description,
        featureConfigurations: !!model?.value
          ? [{
            type: FeatureConfigurationType.LFVModel,
            modelName: model.value,
          }]
          : [],
        imageSources: [{ imageSourceId: source?.value }],
        inputConfigurations:
          trigger === WorkflowTrigger.RestApi
            ? []
            : [
              {
                triggerState: signal as SignalType,
                pin: pin?.toString(),
                debounceTime: debounce,
              },
            ],
        outputConfigurations: outputs?.map(
          ({ signal, pin, debounce, rule }) => ({
            signalType: signal as SignalType,
            pin: pin.toString(),
            pulseWidth: debounce,
            rule: rule?.value as Rule,
          }),
        ),
      }),
    onSuccess: (data, values) => {
      const path = `/workflows/${workflowId}`;
      addSuccess({
        content: (
          <>
            You successfully edited <strong>{values.name}</strong>.
          </>
        ),
        relevantPath: path,
      });
      navigate(path);
      queryClient.clear();
    },
    onError: (error: Error, values) => {
      addError({
        content: (
          <>
            Failed to edit <strong>{values.name}</strong>. {error.message}
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

  return (
    <FormProvider {...form}>
      <form
        onSubmit={form.handleSubmit((values) => editMutation.mutate(values))}
        className={formLayoutStyle}
      >
        <Form
          header={<Header variant="h1">Edit workflow</Header>}
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
          {getQuery.isLoading ? (
            <WorkflowDetails isLoading={getQuery.isLoading} />
          ) : (
            <SpaceBetween direction="vertical" size="l">
              <WorkflowDetails isLoading={false} />
              <ImageSourceAndModel modelOptions={modelOptions} isLoadingModelOptions={isModelsLoading} form={form} />
              <Inputs />
              <Outputs />
            </SpaceBetween>
          )}
        </Form>
      </form>
    </FormProvider>
  );
}
