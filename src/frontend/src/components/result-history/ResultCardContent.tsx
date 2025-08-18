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
import Cards from "@cloudscape-design/components/cards";
import Box from "@cloudscape-design/components/box";
import Header from "@cloudscape-design/components/header";
import Pagination from "@cloudscape-design/components/pagination";
import CollectionPreferences from "@cloudscape-design/components/collection-preferences";
import PropertyFilter from "@cloudscape-design/components/property-filter";
import {
  Button,
  ButtonDropdown,
  ExpandableSection,
  Icon,
  Link,
  PropertyFilterProps,
  SpaceBetween,
  TextContent,
} from "@cloudscape-design/components";
import { HistoryResultPageType, ImageCaptureResultType, InferenceResultHistory, ResultTableActionType, ResultTableFeedbackType } from "./types";
import {
  getDownloadUrl,
  listWorkflowResults,
  updateInferenceResults,
} from "api/InferenceResultAPI";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  convertTimestampToLocalTime,
  getFileName,
} from "components/live-result/helpers";
import OutputImageInCard from "./OutputImageInCard";
import { AnomalyLabel, deleteWorkflowResult } from "api/WorkflowAPI";
import AnomalyLabels from "components/live-result/AnomalyLabels";
import ColoredInferenceBox from "./ColoredInferenceBox";
import {
  PropertyFilterToken,
  useCollection,
} from "@cloudscape-design/collection-hooks";
import { Workflow } from "components/workflow/types";
import { css } from "@emotion/css";
import {
  NUMBER_CARDS_PER_ROW,
  RESULT_DEFAULT_PAGE_SIZE,
  filteringOptions,
  filteringProperties,
  pageSizeOption,
} from "./ResultCardFilterConfig";
import {
  colorTextStatusError,
  colorTextStatusSuccess,
} from "@cloudscape-design/design-tokens";
import DownloadModal from "./DownloadModal";
import EditNotesModal from "./EditNotesModal";
import { AppLayoutContext } from "components/layout/AppLayoutContext";
import { getResultHistoryFilterParams } from "./utils";
import useAuth from "components/auth/authHook";
import { APIList } from "config/Interface";
import { AxiosError } from "axios";
import { TableTypes } from "components/layout/constants";
import Divider from "components/common/Divider";
import { noteEditIconInlineStyle } from "./styles";
import { secondaryTextColorStyle } from "styles/common";
import { PredictionType } from "components/image-source/types";
import DismissibleAlert from "components/common/DismissibleAlert";

interface ResultCardContentProps {
  workflowId: string;
  workflow: Workflow;
  historyResultPageType: HistoryResultPageType;
}

export default function ResultCardContent({
  workflowId,
  workflow,
  historyResultPageType,
}: ResultCardContentProps): JSX.Element {
  const isInferenceResultPage = historyResultPageType === HistoryResultPageType.INFERENCE_RESULT;
  // Set to undefined for Capture page as capture page contains both result type so doesn't need filter
  const resultTypeFilter: ImageCaptureResultType | undefined = isInferenceResultPage ? ImageCaptureResultType.INFERENCE : undefined
  const tableType = isInferenceResultPage ? TableTypes.RESULT_HISTORY : TableTypes.CAPTURE_HISTORY;
  const workflowName = workflow.name;
  const queryClient = useQueryClient();
  const {
    addError,
    tablesInfo: {
      [tableType]: resultHistoryTableInfo
    },
    setTableInfo,
    setTableTypePref,
    tablesPref: {
      [tableType]: resultHistoryTablePref
    },
  } = React.useContext(AppLayoutContext);
  const { token, authEnabled } = useAuth();
  const { pageSize = RESULT_DEFAULT_PAGE_SIZE } = resultHistoryTablePref || {};
  const { [workflowId]: curWorkflowResultTableInfo } = resultHistoryTableInfo || {};
  const {
    pageIdx: currentPageIndex = 1,
    filters = {
      tokens: [],
      operation: "and",
    } as PropertyFilterProps.Query,
  } = curWorkflowResultTableInfo || {};
  const [selectedItems, setSelectedItems] = React.useState<
    InferenceResultHistory[]
  >([]);
  const [selectedItemsSet, setSelectedItemsSet] = React.useState<Set<string>>(
    new Set()
  );
  const [showDownloadModal, setShowDownloadModal] = React.useState(false);
  const [isDownloadPending, setIsDownloadPending] = React.useState(false);
  const [selectedNoteEditItems, setSelectedNoteEditItems] = React.useState<InferenceResultHistory[]>([]);
  const filterParamsString = getResultHistoryFilterParams(filters.tokens).join(
    "&"
  );

  React.useEffect(() => {
    // clear state on workflow id change
    setSelectedItems([]);
    setSelectedItemsSet(new Set());
  }, [workflowId]);

  const { data: inferenceResults, isLoading: isLoadingTable } = useQuery({
    queryKey: [
      "getInferenceResults",
      workflowId,
      currentPageIndex,
      pageSize,
      filterParamsString,
    ],
    queryFn: () =>
      listWorkflowResults({
        id: workflowId,
        page: currentPageIndex,
        size: pageSize,
        captureType: resultTypeFilter,
        filterParamsStr: filterParamsString
      }),
    cacheTime: 0, // we want to fetch the latest data all the time
    enabled: !!workflowId,
  });

  const { data: totalItemCountRes } = useQuery({
    queryKey: ["getInferenceTotalCount", workflowId],
    queryFn: () => listWorkflowResults({
      id: workflowId,
      page: 1,
      size: 1,
      captureType: resultTypeFilter,
    }),
    cacheTime: 0, // we want to fetch the latest data all the time
    enabled: !!workflowId,
  });

  function refetchTableItems(): void {
    queryClient.refetchQueries(["getInferenceResults"]);
    queryClient.refetchQueries(["getInferenceTotalCount"]);
  }

  const onBatchUpdateSuccess = (): void => {
    setSelectedItems([]);
    setSelectedItemsSet(new Set());
    refetchTableItems();
  }

  const { mutate: batchMarkFlagStatus, isLoading: isMarkingFlagStatus } =
    useMutation({
      mutationFn: ({
        resultIds,
        mark,
      }: {
        resultIds: string[];
        mark: boolean;
      }) => updateInferenceResults(workflowId, resultIds, { flagForReview: mark }),
      onSuccess: onBatchUpdateSuccess,
      onError: (_, { mark }) => {
        if (mark) {
          addError({
            header: "Failed to flag for review",
            content: "Unable to mark these results as flagged.",
          });
        } else {
          addError({
            header: "Failed to unflag for review",
            content: "Unable to mark these results as unflagged.",
          });
        }
      },
    });

  const {
    mutateAsync: asyncBatchMarkDownloadStatus,
    mutate: batchMarkDownloadStatus,
    isLoading: isMarkingDownloadStatus,
  } = useMutation({
    mutationFn: ({ resultIds, mark }: { resultIds: string[]; mark: boolean }) =>
      updateInferenceResults(workflowId, resultIds, { downloaded: mark }),
    onSuccess: onBatchUpdateSuccess,
    onError: (_, { mark }) => {
      if (mark) {
        addError({
          header: "Failed to mark as downloaded",
          content: "Unable to mark these results as downloaded.",
        });
      } else {
        addError({
          header: "Failed to unmark as downloaded",
          content: "Unable to unmark these results as downloaded.",
        });
      }
    },
  });

  const {
    mutate: batchUpdateTextNote,
    isLoading: isUpdatingTextNote,
  } = useMutation({
    mutationFn: ({ resultIds, textNote }: { resultIds: string[]; textNote: string }) =>
      updateInferenceResults(workflowId, resultIds, { textNote }),
    onSuccess: (_, { resultIds }) => {
      refetchTableItems();
      setSelectedNoteEditItems([]);
      if (resultIds.length > 1) {
        // clear selected items after batch update 
        setSelectedItems([]);
        setSelectedItemsSet(new Set());
      }
    },
    onError: (_) => {
      addError({
        header: "Failed to save edited notes",
        content: "Unable to save the edited notes."
      });
    },
  });

  const {
    mutate: batchFeedback,
    isLoading: isUpdatingVerification,
  } = useMutation({
    mutationFn: ({ resultIds, feedback }: { resultIds: string[]; feedback: PredictionType }) =>
      updateInferenceResults(workflowId, resultIds, { humanClassification: feedback }),
    onSuccess: onBatchUpdateSuccess,
    onError: (_, { feedback }) => {
      addError({
        header: "Failed to save human feedback",
        content: feedback === PredictionType.Normal
          ? "Unable to save the human feedback as normal."
          : "Unable to save the human feedback as anomaly."
      });
    },
  });

  const { mutate: deleteResult, isLoading: isDeletingResult } = useMutation({
    mutationFn: (captureId: string) => deleteWorkflowResult(workflowId, captureId),
    onSuccess: onBatchUpdateSuccess,
    onError: (_) => {
      addError({
        header: "Failed to delete result",
        content: "Unable to delete the selected result.",
      });
    },
  });


  const isUpdating = isMarkingFlagStatus || isMarkingDownloadStatus || isUpdatingTextNote || isUpdatingVerification;
  const [downloadPendingCallback, setDownloadPendingCallback] = React.useState<NodeJS.Timeout | null>(null);
  const { mutate: download, isLoading: isDownloading } = useMutation({
    mutationFn: async ({
      resultIds,
      markAsDownloaded,
    }: {
      resultIds: string[];
      markAsDownloaded: boolean;
    }) => {
      const result = await getDownloadUrl(workflowId, resultIds);
      if (markAsDownloaded) {
        try {
          await asyncBatchMarkDownloadStatus({ resultIds, mark: true });
        } catch {
          // we have handled the error in mutation onError callback
          // here its just to catch the error from the promise
        }
      }
      const link = document.createElement("a");
      link.href = `${APIList.workflows}/${workflowId}/results/export?captureIdPath=${result.captureIdPath}${authEnabled ? `token=${encodeURIComponent(token)}` : ""}`;
      link.click();
      return { resultIds };
    },
    onError: (err?: AxiosError) => {
      setShowDownloadModal(false);
      if (err?.response?.status === 413) {
        addError({
          header: "Failed to download",
          content: (err.response?.data as any)?.message || "",
        });
      } else {
        addError({
          content: "Failed to download images.",
        });
      }
    },
    onSuccess: ({ resultIds }) => {
      /**
       * API will process and generate the zip file before download start, so the api will be pending for a while
       * since we open the api link by clicking on the <a> tag, we are not able to track the api pending status,
       * so we will set a waiting time here dynamically based on the files user selected. i.e. 500ms per 10 files
       */
      setIsDownloadPending(true);
      setDownloadPendingCallback(setTimeout(
        () => {
          setIsDownloadPending(false);
          setShowDownloadModal(false);
        },
        500 * (resultIds.length / 10)
      ));
    },
  });

  const selectCurrentPageItems = (): void => {
    const nextSelectedItems =
      currentPageItems.reduce((prev, item) => {
        if (!selectedItemsSet.has(item.captureId)) {
          selectedItemsSet.add(item.captureId);
          prev.push(item);
        }
        return prev;
      }, selectedItems) || [];
    setSelectedItems([...nextSelectedItems]);
  };

  const { results: currentPageItems = [], total: filteredTotalItemCount = 0 } =
    inferenceResults || {};
  const { total: overallItemCount = 0 } = totalItemCountRes || {};

  const { items, paginationProps, propertyFilterProps } = useCollection(
    currentPageItems,
    {
      propertyFiltering: {
        filteringProperties: filteringProperties(historyResultPageType),
      },
      pagination: {
        pageSize,
      },
      selection: {},
    }
  );

  const disableAction = selectedItems.length === 0;
  const selectedNotesEditItemsCount = selectedNoteEditItems.length;
  return (
    <SpaceBetween direction="vertical" size="l">
      {
        !isInferenceResultPage && (
          <DismissibleAlert type="info">
            Capture results contain images captured via inference and capture workflows.
          </DismissibleAlert>
        )
      }
      <Cards
        data-test-id="workflow-result-list"
        loading={isLoadingTable}
        onSelectionChange={({ detail: { selectedItems = [] } }): void => {
          setSelectedItems(selectedItems);
          setSelectedItemsSet(
            new Set(selectedItems.map((item) => item.captureId))
          );
        }}
        selectedItems={selectedItems}
        ariaLabels={{
          itemSelectionLabel: (_e, t) => `select ${t.captureId}`,
          selectionGroupLabel: "Item selection",
        }}
        cardDefinition={{
          header: (item: InferenceResultHistory) => (
            <SpaceBetween
              direction="horizontal"
              size="xs"
              alignItems="start"
              className={css`
                flex-wrap: nowrap !important;
              `}
            >
              {item.flagForReview && (
                <div
                  className={css`
                    color: ${colorTextStatusError};
                  `}
                >
                  <Icon size="big" name="flag" />
                </div>
              )}
              <div
                className={css`
                  a {
                    word-break: break-word;
                  }
                `}
              >
                <Link fontSize="heading-m">
                  {getFileName(item.inputImageFilePath || "")}
                </Link>
              </div>
            </SpaceBetween>
          ),
          sections: [
            {
              id: "image",
              content: (item) => (
                <OutputImageInCard
                  workflowId={item.workflowId}
                  captureId={item.captureId}
                  resultsList={currentPageItems}
                  historyResultPageType={historyResultPageType}
                />
              ),
            },
            ...(isInferenceResultPage ? [
              {
                id: "result date",
                header: "Result date",
                content: (item: InferenceResultHistory) =>
                  convertTimestampToLocalTime(item.inferenceCreationTime),
              },
              {
                id: "prediction",
                content: (item: InferenceResultHistory) =>
                  <div data-test-id="result-card-prediction-section">
                    <ColoredInferenceBox
                      humanFeedbackRequired={item.humanReviewRequired}
                      prediction={item.prediction}
                      humanFeedback={item.humanClassification}
                    />
                  </div>
                ,
              },
              {
                id: "label",
                content: (item: InferenceResultHistory) => (
                  <SpaceBetween direction="vertical" size="xs">
                    <ExpandableSection
                      variant="footer"
                      headerText={
                        "Anomaly labels (" + (item.anomalyLabels?.length ?? 0) + ")"
                      }
                    >
                      {item.anomalyLabels ? (
                        item.anomalyLabels.map((label: AnomalyLabel) => {
                          return (
                            <AnomalyLabels
                              key={label["hex-color"]}
                              labelInfo={label}
                            />
                          );
                        })
                      ) : (
                        <span className={secondaryTextColorStyle}>No anomalies labels</span>
                      )}
                    </ExpandableSection>
                    <Divider />
                  </SpaceBetween>
                ),
              },
            ] : [
              {
                id: "capture date",
                header: "Capture date",
                content: (item: InferenceResultHistory) =>
                (
                  <SpaceBetween direction="vertical" size="xs">
                    {convertTimestampToLocalTime(item.inferenceCreationTime)}
                    <Divider />
                  </SpaceBetween>
                )
                ,
              },
            ]),
            {
              id: "note",
              content: (item) => (
                <SpaceBetween direction="vertical" size="xs">
                  <div data-test-id="workflow-result-note-content">
                    {/* Tried to put the button in headerActions prop of ExpandableSection, 
                        but headerActions only work with "container" type of ExpandableSection, 
                        so here we float the button to the right to match the UX design */}
                    <Button
                      variant="link"
                      iconName="edit"
                      className={noteEditIconInlineStyle}
                      onClick={(): void => setSelectedNoteEditItems([item])}
                      data-test-id="workflow-result-note-edit-button"
                    />
                    <ExpandableSection
                      headerText={<span data-test-id="workflow-result-note-expandable-header">Notes</span>}
                      variant="footer"
                    >
                      <span
                        data-test-id="workflow-result-note-expandable-content"
                        className={secondaryTextColorStyle}
                      >
                        {item.textNote || ""}
                      </span>
                    </ExpandableSection>
                  </div>
                  <Divider />
                </SpaceBetween>
              )
            },
            {
              id: "downloaded",
              content: (item) => (
                <TextContent>
                  <SpaceBetween size="xxs" direction="horizontal">
                    <Icon name="download" />
                    <b>{"Download status"}</b>
                    {item.downloaded ? (
                      <span
                        className={css`
                          color: ${colorTextStatusSuccess};
                        `}
                      >
                        Downloaded
                      </span>
                    ) : (
                      <span>Not downloaded</span>
                    )}
                  </SpaceBetween>
                </TextContent>
              ),
            },
          ],
        }}
        cardsPerRow={[
          { cards: NUMBER_CARDS_PER_ROW.NARROW },
          { minWidth: 500, cards: NUMBER_CARDS_PER_ROW.NORMAL },
          { minWidth: 1000, cards: NUMBER_CARDS_PER_ROW.WIDE },
          { minWidth: 1200, cards: NUMBER_CARDS_PER_ROW.EX_WIDE },
        ]}
        items={items}
        loadingText="Loading resources"
        selectionType="multi"
        trackBy="captureId"
        empty={
          <Box margin={{ vertical: "xs" }} textAlign="center" color="inherit">
            <b>No resources</b>
          </Box>
        }
        filter={
          <PropertyFilter
            {...propertyFilterProps}
            query={filters}
            onChange={(event): void => {
              const tokenMap = {} as { [key: string]: PropertyFilterToken };
              event.detail.tokens.forEach((token) => {
                if (token.propertyKey) {
                  tokenMap[token.propertyKey] = token;
                }
              });
              setTableInfo({
                tableType,
                tableId: workflowId,
                tableInfo: {
                  pageIdx: 1,
                  filters: {
                    ...event.detail, tokens: Object.values(tokenMap)
                  }
                }
              });
              setSelectedItems([]);
              setSelectedItemsSet(new Set());
            }}
            i18nStrings={{
              filteringAriaLabel: "Find results",
              filteringPlaceholder: "Find results",
              groupPropertiesText: "Properties",
              clearFiltersText: "Clear filters",
              operationAndText: "and",
              operationOrText: "or",
              applyActionText: "Apply",
            }}
            tokenLimit={2}
            countText={`${filteredTotalItemCount} ${filteredTotalItemCount === 1 ? "match" : "matches"}`}
            expandToViewport
            hideOperations
            filteringOptions={filteringOptions(historyResultPageType)}
          />
        }
        header={
          <Header
            counter={
              selectedItems.length > 0
                ? `(${selectedItems.length}/${overallItemCount})`
                : `(${overallItemCount})`
            }
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <ButtonDropdown
                  onItemClick={({ detail }): void => {
                    switch (detail.id) {
                      case ResultTableActionType.MARK_AS_DOWNLOADED:
                        batchMarkDownloadStatus({
                          resultIds: selectedItems.map(
                            (item) => item.captureId
                          ),
                          mark: true,
                        });
                        break;
                      case ResultTableActionType.UNMARK_AS_DOWNLOADED:
                        batchMarkDownloadStatus({
                          resultIds: selectedItems.map(
                            (item) => item.captureId
                          ),
                          mark: false,
                        });
                        break;
                      case ResultTableActionType.FLAG_FOR_REVIEW:
                        batchMarkFlagStatus({
                          resultIds: selectedItems.map((item) => item.captureId),
                          mark: true,
                        });
                        break;
                      case ResultTableActionType.REMOVE_REVIEW_FLAG:
                        batchMarkFlagStatus({
                          resultIds: selectedItems.map((item) => item.captureId),
                          mark: false,
                        });
                        break;
                      case ResultTableActionType.SELECT_PAGE:
                        selectCurrentPageItems();
                        break;
                      default:
                        break;
                    }
                  }}
                  loading={isUpdating}
                  items={[
                    {
                      text: "Select page",
                      id: ResultTableActionType.SELECT_PAGE,
                    },
                    {
                      text: "Mark as downloaded",
                      id: ResultTableActionType.MARK_AS_DOWNLOADED,
                      disabled: disableAction,
                    },
                    {
                      text: "Unmark as downloaded",
                      id: ResultTableActionType.UNMARK_AS_DOWNLOADED,
                      disabled: disableAction,
                    },
                    {
                      text: "Flag for review",
                      id: ResultTableActionType.FLAG_FOR_REVIEW,
                      disabled: disableAction,
                    },
                    {
                      text: "Remove review flag",
                      id: ResultTableActionType.REMOVE_REVIEW_FLAG,
                      disabled: disableAction,
                    }
                  ]}
                >
                  Actions
                </ButtonDropdown>
                <Button
                  disabled={disableAction}
                  variant="normal"
                  onClick={(): void => setSelectedNoteEditItems([...selectedItems])}
                  data-test-id="workflow-result-batch-edit-notes-button"
                >
                  Edit notes
                </Button>
                {
                  isInferenceResultPage ? (
                    <ButtonDropdown
                      onItemClick={({ detail }): void => {
                        const resultIds = selectedItems.map(item => item.captureId);
                        switch (detail.id) {
                          case ResultTableFeedbackType.NORMAL:
                            batchFeedback({
                              resultIds,
                              feedback: PredictionType.Normal,
                            });
                            break;
                          case ResultTableFeedbackType.ANOMALY:
                            batchFeedback({
                              resultIds,
                              feedback: PredictionType.Anomaly,
                            });
                            break;
                          default:
                            break;
                        }
                      }}
                      loading={isUpdating}
                      items={[
                        {
                          text: "Normal",
                          id: ResultTableFeedbackType.NORMAL,
                          disabled: disableAction,
                        },
                        {
                          text: "Anomaly",
                          id: ResultTableFeedbackType.ANOMALY,
                          disabled: disableAction,
                        },
                      ]}
                      data-test-id="feedback-action"
                    >
                      Feedback
                    </ButtonDropdown>
                  ) : (
                    <Button
                      variant="normal"
                      disabled={disableAction || isUpdating || selectedItems.length !== 1}
                      data-test-id="workflow-result-batch-delete-button"
                      loading={isDeletingResult}
                      onClick={(): void => {
                        deleteResult(selectedItems[0].captureId);
                      }}
                    >
                      Delete
                    </Button>
                  )
                }

                <Button
                  disabled={disableAction || isUpdating}
                  variant="primary"
                  onClick={(): void => setShowDownloadModal(true)}
                >
                  Download results
                </Button>
              </SpaceBetween>
            }
          >
            {workflowName} results
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
            currentPageIndex={currentPageIndex}
            onChange={({ detail: { currentPageIndex } }): void => {
              setTableInfo({
                tableType,
                tableId: workflowId,
                tableInfo: {
                  pageIdx: currentPageIndex
                }
              });
            }}
            pagesCount={
              filteredTotalItemCount > 0
                ? Math.ceil(filteredTotalItemCount / pageSize)
                : 1
            }
          />
        }
        preferences={
          <CollectionPreferences
            title="Preferences"
            confirmLabel="Confirm"
            cancelLabel="Cancel"
            preferences={{ pageSize }}
            pageSizePreference={{
              title: "Page size",
              options: pageSizeOption,
            }}
            onConfirm={({ detail: { pageSize } }): void => {
              setTableTypePref({
                tableType,
                pageSize: pageSize!,
              });
              setTableInfo({
                tableType,
                tableInfo: {
                  pageIdx: 1
                },
              });
            }}
          />
        }
      />
      <DownloadModal
        showModal={showDownloadModal}
        onClose={(): void => {
          setShowDownloadModal(false);
          // clear the pending status on modal close, so it won't block user from retriggering download in case previous download pending time is too long
          setIsDownloadPending(false);
          if (downloadPendingCallback) {
            clearTimeout(downloadPendingCallback)
          }
        }}
        onDownload={(markAsDownloaded): void => {
          download({
            resultIds: selectedItems.map((item) => item.captureId),
            markAsDownloaded,
          });
        }}
        isDownloading={isDownloading || isDownloadPending}
      />
      <EditNotesModal
        showModal={selectedNotesEditItemsCount > 0}
        onClose={(): void => setSelectedNoteEditItems([])}
        description={
          <span>
            {
              selectedNotesEditItemsCount === 1
                ? `${selectedNotesEditItemsCount} result selected - `
                : `${selectedNotesEditItemsCount} results selected`
            }
            {
              selectedNotesEditItemsCount === 1 &&
              <b>{getFileName(selectedNoteEditItems[0].inputImageFilePath || "")}</b>
            }
          </span>
        }
        showHasNoteAlert={!!(selectedNotesEditItemsCount > 1 && selectedNoteEditItems.find(item => (item.textNote || "")?.length > 0))}
        initialNotes={selectedNotesEditItemsCount === 1 ? (selectedNoteEditItems[0].textNote || "") : ""}
        onSave={(textNote): void => {
          batchUpdateTextNote({
            resultIds: selectedNoteEditItems.map(
              (item) => item.captureId
            ),
            textNote,
          });
        }}
        isSaving={isUpdatingTextNote}
      />
    </SpaceBetween>
  );
}
