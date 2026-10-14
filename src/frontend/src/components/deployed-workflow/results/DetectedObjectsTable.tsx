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
import { Badge, Box, Header, Table, TableProps } from "@cloudscape-design/components";
import { useCollection } from "@cloudscape-design/collection-hooks";

import {
  detectionLabelSummary,
  formatBox,
  formatConfidence,
} from "../detections";
import type { RunDetection } from "../detections";

/** Empty-state message for a detection run that found nothing (R2.5). */
export const NO_OBJECTS_MESSAGE = "No objects were detected in this run.";

/** The badge on a detection an object association found in violation. */
export const VIOLATION_BADGE = "Violation";

/** One table row: a detection plus its 0-based Detection_List position. */
interface DetectionRow extends RunDetection {
  index: number;
  /** Whether an object association of the run found it in violation. */
  violating: boolean;
}

/**
 * The run's Detection_List as a sortable table (run-detection-visibility
 * Requirement 2): index, object label, confidence and bounding box, with the
 * count and a per-label summary in the header.
 *
 * Rows keep Detection_List order until the user sorts. The index is the
 * entry's 0-based list position, which is what `detections.N` template paths
 * and Bedrock's `crop_detection_index` refer to (design D7), so it stays
 * attached to its row under any sort.
 *
 * `violatingIds`, the Detection_IDs the run's object associations found in
 * violation (rtsp-rtmp-stream-cameras Requirement 16.3), adds a column that
 * badges those rows. Without any, the table is unchanged.
 */
export default function DetectedObjectsTable({
  detections,
  violatingIds,
}: {
  detections: RunDetection[];
  violatingIds?: ReadonlySet<string>;
}): JSX.Element {
  const highlight = !!violatingIds && violatingIds.size > 0;
  const rows = React.useMemo<DetectionRow[]>(
    () =>
      detections.map((detection, index) => ({
        ...detection,
        index,
        violating:
          highlight && detection.id !== undefined && !!violatingIds?.has(detection.id),
      })),
    [detections, highlight, violatingIds],
  );
  const { items, collectionProps } = useCollection(rows, { sorting: {} });
  const summary = detectionLabelSummary(detections);
  const violatingCount = rows.filter((row) => row.violating).length;
  const description = [
    summary,
    highlight ? `${violatingCount} in violation` : "",
  ]
    .filter((part) => part.length > 0)
    .join(" · ");
  const violationColumn: TableProps.ColumnDefinition<DetectionRow>[] = highlight
    ? [
        {
          id: "violation",
          header: "Association",
          cell: (row: DetectionRow): React.ReactNode =>
            row.violating ? <Badge color="red">{VIOLATION_BADGE}</Badge> : "-",
          sortingField: "violating",
        },
      ]
    : [];

  return (
    <Table
      {...collectionProps}
      data-testid="detected-objects-table"
      variant="container"
      items={items}
      trackBy={(row: DetectionRow): string => String(row.index)}
      header={
        <Header
          variant="h2"
          counter={`(${detections.length})`}
          description={description.length > 0 ? description : undefined}
        >
          Objects detected
        </Header>
      }
      columnDefinitions={[
        {
          id: "index",
          header: "Index",
          cell: (row: DetectionRow): React.ReactNode => row.index,
          sortingField: "index",
          width: 90,
        },
        {
          id: "label",
          header: "Object",
          cell: (row: DetectionRow): React.ReactNode => row.label,
          sortingField: "label",
        },
        {
          id: "confidence",
          header: "Confidence",
          cell: (row: DetectionRow): React.ReactNode =>
            formatConfidence(row.confidence),
          sortingField: "confidence",
        },
        {
          id: "box",
          header: "Bounding box (px)",
          cell: (row: DetectionRow): React.ReactNode => formatBox(row.box),
        },
        ...violationColumn,
      ]}
      empty={
        <Box textAlign="center" color="inherit" data-testid="no-detected-objects">
          {NO_OBJECTS_MESSAGE}
        </Box>
      }
    />
  );
}
