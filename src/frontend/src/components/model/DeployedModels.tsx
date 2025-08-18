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
import {
  Table,
  SpaceBetween,
  Header,
  Pagination,
  TextFilter,
} from "@cloudscape-design/components";
import { useCollection } from "@cloudscape-design/collection-hooks";
import { useQuery } from "@tanstack/react-query";

import { ModelDefaultConfigs } from "./types";
import { modelNameString } from "./helpers";
import { AppDescriptions } from "config/Interface";
import { listModels } from "api/FeatureConfigurationAPI";

import EdgeUICollectionPreferences from "components/collection-preferences/EdgeUICollectionPreferences";
import EmptyTable from "../empty-table/EmptyTable";
import { FeatureConfiguration } from "components/workflow/types";

type ModelTableItem = {
  status: string;
  name: string;
  defaultConfiguration: ModelDefaultConfigs;
};

type ModelTableHeaderProps = {
  numAllItems: number;
};

export default function DeployedModels(): JSX.Element {
  const listQuery = useQuery({
    queryKey: ["listModels"],
    queryFn: async () => {
      const models = await listModels();
      const modelItems: ModelTableItem[] = models.map(
        (model: FeatureConfiguration) => {
          return {
            name: model.modelName,
            status: model.status || "",
            defaultConfiguration: model.defaultConfiguration,
          };
        },
      );
      return modelItems;
    },
  });

  const defaultPageSize = 10;
  const [pageSize, setPageSize] = React.useState(defaultPageSize);

  const models = listQuery.data ?? [];
  const { items, collectionProps, filterProps, paginationProps } =
    useCollection(models, {
      pagination: { pageSize: pageSize },
      filtering: {
        empty: (
          <EmptyTable
            header="No deployed models"
            message="No deployed models to display."
          />
        ),
      },
      sorting: {
        defaultState: {
          sortingColumn: {
            sortingField: "friendlyName",
          },
        },
      },
    });

  return (
    <Table
      loading={listQuery.isFetching}
      loadingText="Loading deployed models"
      header={<ModelTableHeader numAllItems={models.length} />}
      columnDefinitions={[
        {
          id: "friendlyName",
          header: "Name",
          cell: (item): string =>
            item?.defaultConfiguration?.modelAlias || item?.name,
          sortingField: "friendlyName",
          sortingComparator: (a, b): number => {
            return (a.defaultConfiguration.modelAlias || "") >
              (b.defaultConfiguration.modelAlias || "")
              ? 1
              : -1;
          },
        },
        {
          id: "modelStatus",
          header: "Status",
          cell: (item): string => item?.status || "-",
          sortingField: "modelStatus"
        },
        {
          id: "name",
          header: "ID",
          cell: (item): string => item?.name || "-",
          sortingField: "name",
        },
      ]}
      items={items}
      filter={
        <TextFilter
          {...filterProps}
          filteringPlaceholder="Search deployed models"
          filteringAriaLabel="Search deployed models"
        />
      }
      pagination={<Pagination {...paginationProps} />}
      preferences={
        <EdgeUICollectionPreferences
          collectionTypeLabel="models"
          pageSize={pageSize}
          setPageSize={setPageSize}
        />
      }
      variant="full-page"
      {...collectionProps}
    />
  );
}

function ModelTableHeader(props: ModelTableHeaderProps): JSX.Element {
  // TODO: Add breadcrumb group
  // https://sim.amazon.com/issues/DD-14606
  return (
    <SpaceBetween size="xl">
      <Header
        variant="h1"
        description={AppDescriptions.viewModelsDes}
        counter={`(${props.numAllItems})`}
      >
        Deployed models
      </Header>
    </SpaceBetween>
  );
}
