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
  Box,
  ContentLayout,
  SpaceBetween,
  Alert,
} from "@cloudscape-design/components";
import { getWorkflow } from "api/WorkflowAPI";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import DetailsHeader from "./DetailsHeader";
import DetailsContainer from "./DetailsContainer";
import InputsContainer from "./InputsContainer";
import OutputPathContainer from "./OutputPathContainer";
import OutputsContainer from "./OutputsContainer";
import { ImageSourceType } from "components/image-source/types";
import DownloadResultModal from "./DownloadResultModal";
import { useEffect, useState } from "react";
import { DynamicRouterHashKey } from "components/layout/constants";
import { setHashValuesInUrl } from "components/utils";
import DeleteWorkflowModal from "../delete/DeleteWorkflowModal";

export default function WorkflowDetails(): JSX.Element {
  const workflowId = useParams().workflowId ?? "";
  const [showDownloadModal, setShowDownloadModal] = useState(false);
  const [deleteWorkflowModalVisible, setDeleteWorkflowModalVisible] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();

  const editWorkflowUrl = `/workflows/${workflowId}/edit`;

  const workflow = useQuery({
    queryKey: ["getWorkflow", workflowId],
    queryFn: () => getWorkflow(workflowId),
  });

  const workflowName = workflow.data?.name || "";
  const hash = location.hash;

  useEffect(() => {
    const nextHash = setHashValuesInUrl(hash.substring(1), {
      [DynamicRouterHashKey.WORKFLOW_NAME]: encodeURIComponent(workflowName)
    });
    if (hash !== nextHash) navigate(nextHash, { replace: true });
  }, [workflowName, hash, navigate]);

  const onClickDownload = (): void => {
    setShowDownloadModal(true)
  }

  const onClickDelete = (): void => {
    setDeleteWorkflowModalVisible(true)
  }

  if (workflow.data !== undefined && !workflow.data?.imageSources) {
    return (
      <ContentLayout
        disableOverlap
        header={
          <DetailsHeader
            workflowName={workflowName}
            editWorkflowUrl={editWorkflowUrl}
            onClickDelete={onClickDelete}
          />
        }
      >
        <Box padding={{ top: "xl" }}>
          <SpaceBetween size="xl">
            <Alert>
              This workflow isn't configured. Choose Edit workflow to configure
              the workflow.
            </Alert>
          </SpaceBetween>
        </Box>
        <DeleteWorkflowModal
          workflowId={workflowId}
          workflowName={workflowName}
          isVisible={deleteWorkflowModalVisible}
          onCancel={(): void => setDeleteWorkflowModalVisible(false)}
        />
      </ContentLayout>
    );
  }

  const workflowDescription = workflow.data?.description || "-";
  const workflowLastUpdateTime = workflow.data?.lastUpdatedTime || 0;
  const imageSource = workflow.data?.imageSources[0];
  const imageSourceId = imageSource?.imageSourceId || "-";
  const imageSourceName = imageSource?.name || "-";
  const isFolderSrc = imageSource?.type === ImageSourceType.Folder;
  const modelName =
    workflow.data?.featureConfigurations?.[0]?.defaultConfiguration?.modelAlias ||
    workflow.data?.featureConfigurations?.[0]?.modelName ||
    "-";
  const modelVersion = workflow.data?.featureConfigurations?.[0]?.defaultConfiguration?.modelVersion || "";
  const inputConfigurations = workflow.data?.inputConfigurations || [];
  const outputConfigurations = workflow.data?.outputConfigurations || [];
  const workflowOutputPath = workflow.data?.workflowOutputPath || "undefined";

  return (
    <ContentLayout
      disableOverlap
      header={
        <DetailsHeader
          workflowName={workflowName}
          editWorkflowUrl={editWorkflowUrl}
          onClickDelete={onClickDelete}
        />
      }
    >
      <Box padding={{ top: "xl" }}>
        <SpaceBetween size="xl">
          <DetailsContainer
            workflowName={workflowName}
            workflowDescription={workflowDescription}
            lastUpdateTime={workflowLastUpdateTime}
            imageSourceId={imageSourceId}
            imageSourceName={imageSourceName}
            modelName={modelName}
            workflowId={workflowId}
            modelVersion={modelVersion}
          />

          <InputsContainer
            inputConfigurations={inputConfigurations}
            isFolderSrc={isFolderSrc}
          />

          <OutputsContainer outputConfigurations={outputConfigurations} />

          <OutputPathContainer workflowOutputPath={workflowOutputPath} onClickDownload={onClickDownload} />
        </SpaceBetween>
      </Box>
      <DownloadResultModal
        visible={showDownloadModal}
        onClose={(): void => setShowDownloadModal(false)}
        workflowId={workflowId}
      />
      <DeleteWorkflowModal
        workflowId={workflowId}
        workflowName={workflowName}
        isVisible={deleteWorkflowModalVisible}
        onCancel={(): void => setDeleteWorkflowModalVisible(false)}
      />
    </ContentLayout>
  );
}
