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
import { Box, Header, Table } from "@cloudscape-design/components";
import { useCollection } from "@cloudscape-design/collection-hooks";

import {
  detectionLabelSummary,
  formatBox,
  formatConfidence,
} from "../detections";
import type { RunDetection } from "../detections";

/** Empty-state message for a detection run that found nothing (R2.5). */
export const NO_OBJECTS_MESSAGE = "No objects were detected in this run.";

/** One table row: a detection plus its 0-based Detection_List position. */
interface DetectionRow extends RunDetection {
  index: number;
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
 */
export default function DetectedObjectsTable({
  detections,
}: {
  detections: RunDetection[];
}): JSX.Element {
  const rows = React.useMemo<DetectionRow[]>(
    () => detections.map((detection, index) => ({ ...detection, index })),
    [detections],
  );
  const { items, collectionProps } = useCollection(rows, { sorting: {} });
  const summary = detectionLabelSummary(detections);

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
          description={summary.length > 0 ? summary : undefined}
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
      ]}
      empty={
        <Box textAlign="center" color="inherit" data-testid="no-detected-objects">
          {NO_OBJECTS_MESSAGE}
        </Box>
      }
    />
  );
}
