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
  Button,
  Cards,
  Header,
  Pagination,
  SpaceBetween,
} from "@cloudscape-design/components";
import { CAPTURED_IMAGE_PER_PAGE } from "./CaptureImage";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getCapturedImageName, getCapturedImageTime } from "./helpers";
import EmptyTable from "components/empty-table/EmptyTable";
import { useCollection } from "@cloudscape-design/collection-hooks";
import { useNavigate } from "react-router-dom";
import { listWorkflowResults } from "api/InferenceResultAPI";
import { APIList } from "config/Interface";
import { captureImageType } from "components/live-result/types";
import useAuth from "components/auth/authHook";
import { deleteWorkflowResult } from "api/WorkflowAPI";
import { CAPTURED_IMAGE_LIST_TOTAL, CAPTURED_IMAGE_REFETCH_INTERVAL } from "./constants";

interface CapturedCardsProps {
  captureResultsHref: string;
  workflowId: string;
}

export default function CapturedCards({
  captureResultsHref,
  workflowId,
}: CapturedCardsProps): JSX.Element {
  const queryClient = useQueryClient();
  const { token, authEnabled } = useAuth();

  const { data: workflowResults, isLoading: isLoadingLastCapturedImages } = useQuery({
    queryKey: ["getLastCaptureImages", workflowId],
    /**
     * Fetch all types of workflow results given that capture results can include both infer result + capture flow result
     */
    queryFn: () => listWorkflowResults({
      id: workflowId,
      page: 1,
      size: CAPTURED_IMAGE_LIST_TOTAL,
    }),
    enabled: !!workflowId,
    refetchInterval: CAPTURED_IMAGE_REFETCH_INTERVAL,
  });
  const navigate = useNavigate();

  const { items, actions, collectionProps, paginationProps } = useCollection(
    workflowResults?.results || [],
    {
      pagination: {
        pageSize: CAPTURED_IMAGE_PER_PAGE,
      },
      selection: {},
    },
  );

  const { selectedItems = [] } = collectionProps;

  const { mutate: deleteCapturedImage, isLoading: isDeletingCapturedImage } = useMutation({
    mutationFn: (captureId: string) => deleteWorkflowResult(workflowId, captureId),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["getLastCaptureImages", workflowId],
      });
      actions.setSelectedItems([]);
    },
  });

  return (
    <Cards
      {...collectionProps}
      ariaLabels={{
        itemSelectionLabel: (e, item) => `select ${item.inputImageFilePath}`,
        selectionGroupLabel: "Item selection",
      }}
      header={
        <Header
          variant="h2"
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="normal"
                disabled={selectedItems.length === 0}
                onClick={(): void => {
                  if (selectedItems.length > 0) {
                    deleteCapturedImage(selectedItems[0].captureId);
                  }
                }}
                loading={isDeletingCapturedImage}
                data-test-id="capture-delete-button"
              >
                Delete image
              </Button>
              <Button
                variant="normal"
                href={captureResultsHref}
                onClick={(e): void => {
                  e.preventDefault();
                  navigate(captureResultsHref);
                }}
                data-test-id="view-all-results-href-button"
              >
                View all capture results
              </Button>
            </SpaceBetween>

          }
        >
          Last 12 captured images
        </Header>
      }
      pagination={
        <Pagination
          {...paginationProps}
          ariaLabels={{
            nextPageLabel: "Next page",
            previousPageLabel: "Previous page",
            pageLabel: (pageNumber) => `Page ${pageNumber} of all pages`,
          }}
        />
      }
      loading={isLoadingLastCapturedImages}
      loadingText="Loading captured images"
      empty={
        <EmptyTable
          header="No captured images"
          message="No images have been captured."
        />
      }
      selectionType="single"
      cardDefinition={{
        sections: [
          {
            content: ({ captureId, inputImageFilePath }): JSX.Element => {
              const getCaptureAPI = APIList.getCapture
                .replace("{workflow_id}", workflowId)
                .replace("{capture_id}", captureId);
              return (
                <img
                  src={`${getCaptureAPI}/${captureImageType.INPUT_IMAGE}${authEnabled ? `?token=${encodeURIComponent(token)}` : ""}`}
                  alt={inputImageFilePath || ""}
                  style={{ width: "100%", height: "100%" }}
                />
              )
            },
          },
          {
            header: "File name",
            content: ({ inputImageFilePath }) => getCapturedImageName(inputImageFilePath || ""),
          },
          {
            header: "Date created",
            content: ({ inputImageFilePath }): string => getCapturedImageTime(inputImageFilePath || ""),
          },
        ],
      }}
      // Based off of default cardsPerRow from: https://cloudscape.design/components/cards/?tabId=api
      cardsPerRow={[
        {
          cards: 1,
        },
        {
          minWidth: 768,
          cards: 2,
        },
        {
          minWidth: 992,
          cards: 4,
        },
      ]}
      items={items}
    />
  );
}
