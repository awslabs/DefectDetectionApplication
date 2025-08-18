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
import { ImageSourcesHeaderProps } from "./types";
import { ImageSource } from "../types";
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
import { useQuery } from "@tanstack/react-query";

import { listImageSources } from "api/ImageSourceAPI";
import { DATE_TZ_OFFSET, DATE_WITHOUT_TZ } from "components/date-time-format";
import EdgeUICollectionPreferences from "components/collection-preferences/EdgeUICollectionPreferences";
import EmptyTable from "components/empty-table/EmptyTable";
import { useNavigate } from "react-router-dom";
import CopyButton from "components/common/CopyButton";
import { getPathForImageSource } from "./helpers";

const DEFAULT_PAGE_SIZE = 10;

export default function ListImageSources(): JSX.Element {
  const navigate = useNavigate();
  const listQuery = useQuery({
    queryKey: ["listImageSources"],
    queryFn: async () => {
      return await listImageSources();
    },
  });
  const [pageSize, setPageSize] = React.useState(DEFAULT_PAGE_SIZE);

  const { data: imageSources = [] } = listQuery || {};
  const { items, paginationProps, filterProps, collectionProps } =
    useCollection(imageSources, {
      pagination: { pageSize: pageSize },
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
    });

  const timezoneLabel = format(new Date(), DATE_TZ_OFFSET);
  return (
    <Table
      wrapLines
      loading={listQuery.isFetching}
      loadingText="Loading image sources"
      header={
        <ImageSourcesHeader
          totalItemCount={imageSources.length}
        />
      }
      columnDefinitions={[
        {
          id: "name",
          header: "Name",
          cell: (item: ImageSource): React.ReactNode => {
            const url = `/image-sources/${item.imageSourceId}`;
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
          id: "path",
          header: "Path",
          cell: (item: ImageSource): React.ReactNode => {
            const path = getPathForImageSource(item);
            return (
              <CopyButton
                onCopy={(): void => {
                  navigator.clipboard.writeText(path);
                }}
                content={<span>{path}</span>}
              />
            );
          },
          sortingField: "",
          sortingComparator: (imgSourceA, imgSourceB): number => {
            const pathA = getPathForImageSource(imgSourceA);
            const pathB = getPathForImageSource(imgSourceB);
            if (pathA < pathB) {
              return -1;
            }
            if (pathA > pathB) {
              return 1;
            }
            return 0;
          },
        },
        {
          id: "description",
          header: "Description",
          cell: (item: ImageSource) => item.description || "-",
          sortingField: "description",
        },
        {
          id: "cameraId",
          header: "Camera name",
          width: "160px",
          cell: (item: ImageSource) => item.cameraId || "-",
          sortingField: "cameraId",
        },
        {
          id: "type",
          header: "Type",
          cell: (item: ImageSource) => item.type || "-",
          sortingField: "type",
        },
        {
          id: "lastUpdateTime",
          header: `Date modified ${timezoneLabel}`,
          width: "250px",
          cell: (item: ImageSource) =>
            item.lastUpdateTime
              ? format(item.lastUpdateTime, DATE_WITHOUT_TZ)
              : "-",
          sortingField: "lastUpdateTime",
        },
      ]}
      items={items}
      filter={
        <TextFilter
          {...filterProps}
          filteringPlaceholder="Search image sources"
          filteringAriaLabel="Search image sources"
          data-test-id="image-sources-search-box"
        />
      }
      pagination={<Pagination {...paginationProps} />}
      preferences={
        <EdgeUICollectionPreferences
          collectionTypeLabel="image sources"
          pageSize={pageSize}
          setPageSize={setPageSize}
        />
      }
      variant="full-page"
      {...collectionProps}
    />
  );
}

function ImageSourcesHeader({
  totalItemCount,
}: ImageSourcesHeaderProps): JSX.Element {
  const navigate = useNavigate();
  return (
    <Header
      variant="h1"
      actions={
        <SpaceBetween direction="horizontal" size="s">
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