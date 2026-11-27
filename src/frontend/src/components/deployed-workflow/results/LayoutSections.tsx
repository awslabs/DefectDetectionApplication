/*
 *  Copyright 2025 Amazon Web Services, Inc.
 *
 *  Licensed under the Apache License, Version 2.0 (the "License");
 *  you may not use this file except in compliance with the License.
 *  You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under the License is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 *  See the License for the specific language governing permissions and
 *  limitations under the License.
 */
/**
 * The run's Layout Check verdicts (car-assembly-check Requirement 7.1):
 * one section per `layout_check` node, in the order the executor evaluated
 * them, with a PASS/FAIL badge, one table row per detection of each
 * configured class (one row for a class with none), the node status detail
 * when there is one, and the instances the check ignored because they lie
 * outside the car outline.
 */
import * as React from "react";
import {
  Badge,
  Box,
  Container,
  Header,
  SpaceBetween,
  StatusIndicator,
  Table,
} from "@cloudscape-design/components";
import type { NodeStatusMap } from "api/WorkflowRegistrationAPI";
import type { IgnoredRow, LayoutSection, PieceStatus } from "../layoutVerdicts";

/** Header of the list of instances the check ignored. */
export const IGNORED_HEADER = "Ignored — outside the car outline";

const STATUS_LABELS: Record<PieceStatus, string> = {
  ok: "OK",
  misplaced: "Misplaced",
  missing: "Missing",
  duplicate: "Duplicate",
};

const NONE = "—";

/** A ratio to two decimals, or "—". */
export function formatRatio(value: number | null): string {
  return value === null ? NONE : value.toFixed(2);
}

interface PieceTableRow {
  key: string;
  label: string;
  status: PieceStatus;
  iou: number | null;
  confidence: number | null;
  id: string | null;
}

function pieceRows(section: LayoutSection): PieceTableRow[] {
  const rows: PieceTableRow[] = [];
  for (const piece of section.pieces) {
    if (piece.detections.length === 0) {
      rows.push({
        key: piece.label,
        label: piece.label,
        status: piece.status,
        iou: piece.iou,
        confidence: null,
        id: null,
      });
      continue;
    }
    piece.detections.forEach((detection, index) => {
      rows.push({
        key: `${piece.label}-${index}`,
        label: piece.label,
        status: piece.status,
        iou: detection.iou,
        confidence: detection.confidence,
        id: detection.id,
      });
    });
  }
  return rows;
}

/** The node's status detail, as a failure, a warning or plain text. */
function NodeDetail({
  nodeId,
  nodeStatus,
}: {
  nodeId: string;
  nodeStatus?: NodeStatusMap;
}): JSX.Element | null {
  const status = nodeStatus?.[nodeId];
  if (!status?.detail) {
    return null;
  }
  if (status.status === "failure") {
    return <StatusIndicator type="error">{status.detail}</StatusIndicator>;
  }
  if (status.status === "warning") {
    return <StatusIndicator type="warning">{status.detail}</StatusIndicator>;
  }
  return (
    <Box color="text-body-secondary" data-testid={`layout-detail-${nodeId}`}>
      {status.detail}
    </Box>
  );
}

function IgnoredTable({ nodeId, rows }: { nodeId: string; rows: IgnoredRow[] }): JSX.Element {
  return (
    <Table
      data-testid={`layout-ignored-${nodeId}`}
      variant="embedded"
      header={<Header variant="h3">{IGNORED_HEADER}</Header>}
      items={rows.map((row, index) => ({ ...row, index }))}
      trackBy={(row): string => String(row.index)}
      columnDefinitions={[
        { id: "class", header: "Class", cell: (row): React.ReactNode => row.classLabel },
        {
          id: "confidence",
          header: "Confidence",
          cell: (row): React.ReactNode => formatRatio(row.confidence),
        },
        { id: "id", header: "Detection ID", cell: (row): React.ReactNode => row.id ?? NONE },
        {
          id: "overlap",
          header: "Outline overlap",
          cell: (row): React.ReactNode => formatRatio(row.outlineOverlap),
        },
      ]}
    />
  );
}

function LayoutContainer({
  section,
  nodeStatus,
}: {
  section: LayoutSection;
  nodeStatus?: NodeStatusMap;
}): JSX.Element {
  return (
    <Container
      data-testid={`layout-section-${section.nodeId}`}
      header={
        <Header
          variant="h2"
          actions={
            <Badge color={section.pass ? "green" : "red"}>{section.pass ? "PASS" : "FAIL"}</Badge>
          }
        >
          {`Layout check: ${section.nodeId}`}
        </Header>
      }
    >
      <SpaceBetween size="m">
        <NodeDetail nodeId={section.nodeId} nodeStatus={nodeStatus} />
        <Table
          data-testid={`layout-pieces-${section.nodeId}`}
          variant="embedded"
          items={pieceRows(section)}
          trackBy="key"
          columnDefinitions={[
            { id: "class", header: "Class", cell: (row): React.ReactNode => row.label },
            {
              id: "status",
              header: "Status",
              cell: (row): React.ReactNode => STATUS_LABELS[row.status],
            },
            { id: "iou", header: "IoU", cell: (row): React.ReactNode => formatRatio(row.iou) },
            {
              id: "confidence",
              header: "Confidence",
              cell: (row): React.ReactNode => formatRatio(row.confidence),
            },
            { id: "id", header: "Detection ID", cell: (row): React.ReactNode => row.id ?? NONE },
          ]}
          empty={<Box textAlign="center">No classes are configured.</Box>}
        />
        {section.ignored.length > 0 && <IgnoredTable nodeId={section.nodeId} rows={section.ignored} />}
      </SpaceBetween>
    </Container>
  );
}

export default function LayoutSections({
  sections,
  nodeStatus,
}: {
  sections: LayoutSection[];
  nodeStatus?: NodeStatusMap;
}): JSX.Element {
  return (
    <SpaceBetween size="l">
      {sections.map((section) => (
        <LayoutContainer key={section.nodeId} section={section} nodeStatus={nodeStatus} />
      ))}
    </SpaceBetween>
  );
}
