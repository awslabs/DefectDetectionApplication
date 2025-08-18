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
import { WorkflowsItem, WorkflowsHeaderProps } from "./types";
import format from "date-fns/format";
import {
  Button,
  Header,
  Link,
  Pagination,
  SpaceBetween,
  Table,
  TextFilter,
} from "@cloudscape-design/components";
import { useCollection } from "@cloudscape-design/collection-hooks";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { FeatureConfigurationType } from "../types";
import { createWorkflow, listWorkflows } from "api/WorkflowAPI";

import { DATE_TZ_OFFSET, DATE_WITHOUT_TZ } from "components/date-time-format";
import EdgeUICollectionPreferences from "components/collection-preferences/EdgeUICollectionPreferences";
import EmptyTable from "components/empty-table/EmptyTable";
import { useNavigate } from "react-router-dom";
import { AppLayoutContext } from "components/layout/AppLayoutContext";

export default function ListWorkflows(): JSX.Element {
  const listQuery = useQuery({
    queryKey: ["listWorkflows"],
    queryFn: async () => {
      const workflows = await listWorkflows();
      const workflowItems = workflows.map((workflow) => {
        const imageSource = workflow.imageSources?.[0];
        const modelConfigs = workflow.featureConfigurations?.filter(
          (config: any) => config.type === FeatureConfigurationType.LFVModel,
        );
        const modelConfig = modelConfigs?.[0];
        const workflowsItem: WorkflowsItem = {
          workflowId: workflow.workflowId,
          name: workflow.name,
          description: workflow.description,
          imageSource: imageSource,
          model:
            modelConfig?.defaultConfiguration?.modelAlias
            || modelConfig?.modelName
            || "",
          lastUpdateTime: format(workflow.lastUpdatedTime, DATE_WITHOUT_TZ),
        };
        return workflowsItem;
      });
      return workflowItems;
    },
  });

  const defaultPageSize = 10;
  const [pageSize, setPageSize] = React.useState(defaultPageSize);
  const [selectedItems, setSelectedItems] = React.useState([]);

  const workflows = listQuery.data ?? [];
  const { items, paginationProps, filterProps, collectionProps } =
    useCollection(workflows, {
      pagination: { pageSize: pageSize },
      filtering: {
        empty: (
          <EmptyTable
            header="No workflows"
            message="No workflows to display."
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
        defaultSelectedItems: selectedItems,
      },
    });

  const timezoneLabel = format(new Date(), DATE_TZ_OFFSET);
  const navigate = useNavigate();
  return (
    <Table
      wrapLines
      loading={listQuery.isFetching}
      loadingText="Loading workflows"
      selectionType="single"
      header={
        <WorkflowsHeader
          totalItemCount={workflows.length}
          selectedItems={collectionProps.selectedItems}
        />
      }
      columnDefinitions={[
        {
          id: "name",
          header: "Name",
          cell: (item: WorkflowsItem): React.ReactNode => {
            const url = `/workflows/${item.workflowId}`;
            return (
              (
                <Link
                  href={url}
                  onFollow={(event): void => {
                    event.preventDefault();
                    navigate(url);
                  }}
                >
                  {item.name}
                </Link>
              ) || "-"
            );
          },
          sortingField: "name",
        },
        {
          id: "description",
          header: "Description",
          cell: (item: WorkflowsItem) => item.description || "-",
          sortingField: "description",
        },
        {
          id: "imageSource",
          header: "Image source",
          cell: (item: WorkflowsItem) =>
            item.imageSource ? (
              <Link
                href={`/image-sources/${item.imageSource.imageSourceId}`}
                onFollow={(event): void => {
                  event.preventDefault();
                  navigate(`/image-sources/${item.imageSource?.imageSourceId}`);
                }}
              >
                {item.imageSource.name}
              </Link>
            ) : (
              "-"
            ),
          sortingField: "imageSource",
          sortingComparator: (a, b) => {
            return (a.imageSource?.name || "") > (b.imageSource?.name || "")
              ? 1
              : -1;
          },
        },
        {
          id: "model",
          header: "Model",
          cell: (item: WorkflowsItem) => item.model || "-",
          sortingField: "model",
        },
        {
          id: "workflowId",
          header: "ID",
          cell: (item: WorkflowsItem) => item.workflowId || "-",
          sortingField: "workflowId",
        },
        {
          id: "lastUpdateTime",
          header: `Date modified ${timezoneLabel}`,
          cell: (item: WorkflowsItem) => item.lastUpdateTime || "-",
          sortingField: "lastUpdateTime",
        },
      ]}
      items={items}
      filter={
        <TextFilter
          {...filterProps}
          filteringPlaceholder="Search workflows"
          filteringAriaLabel="Search workflows"
        />
      }
      pagination={<Pagination {...paginationProps} />}
      preferences={
        <EdgeUICollectionPreferences
          collectionTypeLabel="workflows"
          pageSize={pageSize}
          setPageSize={setPageSize}
        />
      }
      variant="full-page"
      {...collectionProps}
    />
  );
}

function WorkflowsHeader(props: WorkflowsHeaderProps): JSX.Element {
  const editWorkflowUrl =
    props.selectedItems.length > 0 &&
    `/workflows/${props.selectedItems[0].workflowId}/edit`;
  const navigate = useNavigate();

  const queryClient = useQueryClient();
  const { addSuccess, addError } = React.useContext(AppLayoutContext);
  const addMutation = useMutation({
    mutationFn: () => createWorkflow(),
    onSuccess: (data) => {
      const path = `/workflows`;
      addSuccess({
        content: (
          <>
            You successfully created <strong>{data}</strong>.
          </>
        ),
        relevantPath: path,
      });
      navigate(path);
      queryClient.clear();
    },
    onError: () => {
      addError({
        content: (
          <>
            Failed to create workflow.
          </>
        ),
        action: (
          <Button onClick={(): void => addMutation.mutate()}>Retry</Button>
        ),
      });
    },
  });

  return (
    <Header
      variant="h1"
      actions={
        <SpaceBetween direction="horizontal" size="xs">
          <Button
            onClick={(): void => addMutation.mutate()}
          >Add workflow
          </Button>
          <Button
            variant="primary"
            onClick={(): void => navigate(editWorkflowUrl || "")}
            disabled={!editWorkflowUrl}
          >
            Edit workflow
          </Button>
        </SpaceBetween>
      }
      description={
        <>
          Manage how image sources are mapped to models and the corresponding
          output logic.
          <br />
          Workflows are created and deleted in the cloud interface. You can
          configure your workflows here.
        </>
      }
      counter={`(${props.totalItemCount})`}
    >
      Workflows
    </Header>
  );
}
