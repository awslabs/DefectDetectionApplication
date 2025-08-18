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
import { GroupedImageSourcesHeaderProps } from "./types";
import { ImageSource, ImageSourceType, CameraStatus, WorkflowTriggerType } from "../types";
import format from "date-fns/format";
import {
  Button,
  Header,
  Link,
  SpaceBetween,
  TextFilter,
  StatusIndicator,
  StatusIndicatorProps,
  ContentLayout,
} from "@cloudscape-design/components";
import { AppLayoutContext } from "components/layout/AppLayoutContext";
import { useQuery, useMutation } from "@tanstack/react-query";

import { listImageSources } from "api/ImageSourceAPI";
import { connectCamera, disconnectCamera } from "api/CameraAPI";
import { DATE_TZ_OFFSET, DATE_WITHOUT_TZ } from "components/date-time-format";
import EmptyTable from "components/empty-table/EmptyTable";
import { useNavigate } from "react-router-dom";
import CopyButton from "components/common/CopyButton";
import { getPathForImageSource } from "./helpers";
import RelatedTable from "components/common/RelatedTable/table/RelatedTableComponent";
import { useTreeCollection } from "components/common/RelatedTable/hooks/useTreeCollection";
import ConfirmDisconnectModal, { FilteredWorkflowTableItem } from "./ConfirmDisconnectModal";
import { css } from "@emotion/css";
import { filterWorkflows } from "api/WorkflowAPI";
import { Workflow } from "components/workflow/types";
import { CAMERA_CONNECTING_NOTIFICATION_ID } from "../constants";

interface RelatedTableItem {
  entityId: string;
  parentId?: string;
  isParent?: boolean;
}

type ImageSourceItem = ImageSource & RelatedTableItem;

/**
 * This component is not in use
 */
export default function GroupListImageSources(): JSX.Element {
  const navigate = useNavigate();
  const listQuery = useQuery({
    queryKey: ["listImageSources"],
    queryFn: async () => {
      return await listImageSources();
    },
    cacheTime: 0, // we want to fetch the latest data all the time
  });
  const { data: imageSources = [], isFetching } = listQuery
  // TODO: add back after updating group item pagination design
  // const defaultPageSize = 10;
  // const [pageSize, setPageSize] = React.useState(defaultPageSize);
  const [showModal, setShowModal] = React.useState<boolean>(false);
  const [showModalError, setShowModalError] = React.useState<boolean>(false);
  const [filteredWorkflows, setFilteredWorkflows] = React.useState<FilteredWorkflowTableItem[]>();
  const { addSuccess, addError, addNotification, removeNotification } = React.useContext(AppLayoutContext);

  const imageSourceNodes: ImageSourceItem[] = React.useMemo(() => (imageSources)?.map(item => ({ ...item, parentId: item.cameraId, entityId: item.imageSourceId })), [imageSources]);
  const cameraNodes: ImageSourceItem[] = React.useMemo(() => {
    const uniqueCameraMap = (imageSources).reduce((prev: Map<string, ImageSourceItem>, cur: ImageSource) => {
      let { cameraId = "" } = cur || {};
      if (!cameraId) {
        return prev;
      }
      if (!prev.has(cameraId)) {
        prev.set(cameraId, {
          ...cur,
          name: cameraId,
          imageCapturePath: "-",
          description: "-",
          lastUpdateTime: 0,
          entityId: cameraId,
          isParent: true,
        });
      }
      return prev;
    }, new Map() as Map<string, ImageSourceItem>);
    return Array.from(uniqueCameraMap.values());
  }, [imageSources])
  const allNodes = React.useMemo(() => {
    return [...imageSourceNodes, ...cameraNodes]
  }, [imageSourceNodes, cameraNodes]);

  const timezoneLabel = format(new Date(), DATE_TZ_OFFSET);

  const columnDefinitions = [
    {
      id: "name",
      header: "Name",
      cell: (item: ImageSourceItem): React.ReactNode => {
        if (item.isParent) {
          return item.name;
        }
        const url = `/image-sources/${item.imageSourceId}`;
        return (
          <div className={!item.isParent && item.type === ImageSourceType.Camera ? "camera-table-child-node" : ""}>
            {
              item.name ? (
                <Link
                  href={url}
                  onFollow={(event): void => {
                    event.preventDefault();
                    navigate(url);
                  }}
                >
                  {item.name}
                </Link>
              ) : "-"
            }
          </div>
        )
      },
      sortingField: "name",
    },
    {
      id: "path",
      header: "Path",
      cell: (item: ImageSourceItem): React.ReactNode => {
        if (item.isParent) {
          return "-";
        }
        const path = getPathForImageSource(item);
        if (item)
          return (
            <CopyButton
              onCopy={(): void => {
                navigator.clipboard.writeText(path);
              }}
              content={<span>{path}</span>}
            />
          );
      },
    },
    {
      id: "description",
      header: "Description",
      cell: (item: ImageSourceItem) => item.description || "-",
      sortingField: "description",
    },
    {
      id: "connectionStatus",
      header: "Connection status",
      cell: (item: ImageSourceItem) => {
        if (!item.isParent || item.type !== ImageSourceType.Camera) return "-"
        const cameraStatus = item.cameraStatus?.status
        let statusType: StatusIndicatorProps.Type | undefined;
        if (!!cameraStatus) {
          switch (cameraStatus) {
            case CameraStatus.Connected:
              statusType = "success";
              break;
            case CameraStatus.Disconnected:
              statusType = "error";
              break;
            default:
              statusType = undefined;
          }
        }
        if (!!statusType) {
          return <StatusIndicator type={statusType}>{cameraStatus}</StatusIndicator>;
        } else {
          return "-";
        }
      }
    },
    {
      id: "type",
      header: "Type",
      cell: (item: ImageSourceItem) => item.type || "-",
      sortingField: "type",
    },
    {
      id: "lastUpdateTime",
      header: `Date modified ${timezoneLabel}`,
      cell: (item: ImageSourceItem) =>
        item.lastUpdateTime
          ? format(item.lastUpdateTime, DATE_WITHOUT_TZ)
          : "-",
      sortingField: "lastUpdateTime",
    },
  ];

  const { items, filterProps, collectionProps, expandNode } = useTreeCollection(
    allNodes,
    {
      keyPropertyName: "entityId",
      parentKeyPropertyName: "parentId",
      columnDefinitions,
      // TODO: add back after updating group item pagination design
      // pagination: { pageSize },
      filtering: {
        empty: (
          <EmptyTable
            header="No image sources"
            message="No image sources to display."
            action={
              <Button onClick={(): void => navigate("/image-sources/add")}>
                Add image source
              </Button>
            }
          />
        ),
      },
      sorting: {
        defaultState: {
          sortingColumn: {
            sortingField: "name",
          },
        },
      },
      selection: {
        trackBy: "entityId",
      },
      expandedPropertyName: "cameraId"
    },
  )
  const { selectedItems = [] } = collectionProps;

  const connectMutation = useMutation({
    mutationFn: (cameraId: string) => connectCamera(cameraId),
    onSuccess: (cameraId: string) => {
      removeNotification(CAMERA_CONNECTING_NOTIFICATION_ID);
      listQuery.refetch()
      addSuccess({
        content: (
          <>
            You successfully connected <strong>{cameraId}</strong>.
          </>
        ),
      });
    },
    onMutate: (cameraId: string) => {
      addNotification({
        content: (
          <>
            Connecting to <strong>{cameraId}</strong>.
          </>
        ),
        id: CAMERA_CONNECTING_NOTIFICATION_ID,
        loading: true,
      });
    },
    onError: (error: Error, cameraId) => {
      removeNotification(CAMERA_CONNECTING_NOTIFICATION_ID);
      addError({
        header: (
          <>
            Failed to connect to {cameraId}.
          </>
        ),
        content: (
          <>
            Verify that your camera is powered on and not already connected to another station or device.
          </>
        ),
        action: (
          <Button
            onClick={(): void => {
              connectMutation.mutate(cameraId);
            }}
          >
            Retry
          </Button>
        ),
      });
    },
  },
  );

  const [isConnecting, setIsConnecting] = React.useState(false);
  React.useEffect(() => {
    setIsConnecting(connectMutation.isLoading);
  }, [connectMutation.isLoading]);

  const filterRowCss = css`
  display: flex;
  justify-content: space-between;
  .searchBox
  {
    flex-grow:5;
  }
  .pageControl
  {
    display: flex;
  }
  margin-bottom: -40px;
  margin-top: 10px;
  `;

  const tableWrap = css`
  margin-top: 40px;
  `
  const disconnectMutation = useMutation({
    mutationFn: (cameraId: string) => disconnectCamera(cameraId),
    onSuccess: () => {
      listQuery.refetch();
    },
  });

  const { mutate: getFilteredWorkflows, } = useMutation({
    mutationFn: async ({ cameraId }: { cameraId: string }) => {
      const workflows = await filterWorkflows(cameraId);
      const workflowItems: FilteredWorkflowTableItem[] = workflows.map(
        (workflow: Workflow) => {
          const triggerType = workflow.inputConfigurations.length > 0
            ? WorkflowTriggerType.DigitalInput
            : WorkflowTriggerType.RESTAPI
          return {
            imageSourceName: workflow.imageSources[0].name,
            workflowName: workflow.name,
            trigger: triggerType,
          };
        },
      );
      return workflowItems;
    },
    onSuccess: (data, { cameraId }) => {
      setShowModalError(false);
      if (data.length > 0) {
        setFilteredWorkflows(data)
        setShowModal(true)
      } else {
        disconnectMutation.mutate(cameraId);
      }
    },
    onError: (error: Error) => {
      setFilteredWorkflows([]);
      setShowModalError(true);
      setShowModal(true);
    },
    cacheTime: 0,
  });


  return (
    <>
      <ContentLayout
        header={
          <>
            <ImageSourcesHeader
              totalItemCount={imageSourceNodes.length}
              selectedImageSourceType={selectedItems[0]?.type}
              selectedImageSourceCameraId={selectedItems[0]?.cameraId}
              selectedImageSourceCameraStatus={selectedItems[0]?.cameraStatus?.status}
              onDisconnect={(cameraId: string) => {
                getFilteredWorkflows({ cameraId });
              }}
              onConnect={(cameraId: string) => {
                connectMutation.mutate(cameraId);
              }}
              isConnecting={isConnecting}
              selectedIsParent={selectedItems[0]?.isParent}
            />
            <div className={filterRowCss}>
              <div className="searchBox">
                <TextFilter
                  {...filterProps}
                  filteringPlaceholder="Search image sources"
                  filteringAriaLabel="Search image sources"
                />
              </div>
              {/* TODO: add back after updating group item pagination design */}
              {/* <div className="pageControl">
                <Pagination {...paginationProps} />
                <EdgeUICollectionPreferences
                  collectionTypeLabel="image sources"
                  pageSize={pageSize}
                  setPageSize={setPageSize}
                />
              </div> */}
            </div>
          </>
        }
      >
        <div className={tableWrap}>
          <RelatedTable
            wrapLines
            loading={isFetching}
            loadingText="Loading image sources"
            selectionType="single"
            expandChildren={expandNode}
            items={items}
            columnDefinitions={columnDefinitions}
            trackBy="entityId"
            {...collectionProps}
            variant="borderless"
          />
        </div>
      </ContentLayout>
      <ConfirmDisconnectModal
        isVisible={!!showModal}
        cameraId={!!showModal ? (selectedItems[0]?.cameraId || "") : ""}
        onCancel={(): void => setShowModal(false)}
        onCameraDisconnect={(): void => { listQuery.refetch(); }}
        filteredWorkflows={filteredWorkflows}
        showError={showModalError}
      />

      {/* TODO: hide the selector for camera imgsrcs
      <style> 
        {`
          .awsui_row_wih1l_v4054_301:has(.camera-table-child-node) .awsui_styled-circle-border_1mabk_vy609_146,
          .awsui_row_wih1l_v4054_301:has(.camera-table-child-node) .awsui_label_1ut8b_bbxfi_97{
            display: none;
            pointer-events: none;
          }
        `}
      </style> */}

    </>
  );
}

function ImageSourcesHeader({
  totalItemCount,
  selectedImageSourceType,
  selectedImageSourceCameraId,
  selectedImageSourceCameraStatus,
  selectedIsParent,
  onConnect,
  onDisconnect,
  isConnecting,
}: GroupedImageSourcesHeaderProps): JSX.Element {
  const navigate = useNavigate();
  const isCameraFlag = (
    selectedImageSourceType === ImageSourceType.Camera
  );
  return (
    <Header
      variant="h1"
      actions={
        <SpaceBetween direction="horizontal" size="s">
          <Button
            variant="normal"
            onClick={() => {
              onDisconnect?.(selectedImageSourceCameraId || "");
            }}
            disabled={!selectedIsParent || !isCameraFlag || selectedImageSourceCameraStatus !== CameraStatus.Connected}
          >
            Disconnect
          </Button>
          <Button
            variant="normal"
            onClick={() => {
              onConnect?.(selectedImageSourceCameraId || "");
            }}
            disabled={!selectedIsParent || !isCameraFlag || selectedImageSourceCameraStatus === CameraStatus.Connected}
            loading={isConnecting}
          >
            Connect
          </Button>
          <Button
            variant="primary"
            onClick={(): void => navigate("/image-sources/add")}
          >
            Add image source
          </Button>
        </SpaceBetween>
      }
      description="Manage the image sources configured on this station."
      counter={`(${totalItemCount})`}
    >
      Image sources
    </Header>
  );
}
