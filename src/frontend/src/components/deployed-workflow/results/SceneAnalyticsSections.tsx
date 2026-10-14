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
 * The run's Scene_Analytics_Node outputs (rtsp-rtmp-stream-cameras
 * Requirement 16.3): one section per detection counter, object association
 * and event gate, grouped by kind in that order, each group in the order the
 * executor evaluated its nodes.
 *
 * A counter or association whose evaluation failed (for example, a zone on
 * a frame of unknown size) records zeros, so its node status detail is shown
 * with it rather than letting the zeros pass for a result.
 */
import {
  Badge,
  Box,
  ColumnLayout,
  Container,
  Header,
  SpaceBetween,
  StatusIndicator,
} from "@cloudscape-design/components";
import format from "date-fns/format";
import type { NodeStatusMap } from "api/WorkflowRegistrationAPI";
import { ValueWithLabel } from "Common";
import { DATE_WITHOUT_TZ } from "components/date-time-format";
import type {
  AssociationSection,
  CounterSection,
  EventGateSection,
  EventGateTransition,
  SceneAnalytics,
} from "../sceneAnalytics";

const TRANSITION_LABELS: Record<EventGateTransition, string> = {
  activated: "Activated in this run",
  cleared: "Cleared in this run",
  none: "No change",
};

/** The node's failure or warning, when its evaluation had one. */
function NodeOutcome({
  nodeId,
  nodeStatus,
}: {
  nodeId: string;
  nodeStatus?: NodeStatusMap;
}): JSX.Element | null {
  const status = nodeStatus?.[nodeId];
  if (status?.status === "failure") {
    return (
      <StatusIndicator type="error">
        {status.detail || "The node could not be evaluated."}
      </StatusIndicator>
    );
  }
  if (status?.status === "warning") {
    return (
      <StatusIndicator type="warning">
        {status.detail || "The node ran with a warning."}
      </StatusIndicator>
    );
  }
  return null;
}

function CounterContainer({
  section,
  nodeStatus,
}: {
  section: CounterSection;
  nodeStatus?: NodeStatusMap;
}): JSX.Element {
  return (
    <Container
      data-testid={`counter-section-${section.nodeId}`}
      header={<Header variant="h2">{`Detection counter: ${section.nodeId}`}</Header>}
    >
      <SpaceBetween size="m">
        <NodeOutcome nodeId={section.nodeId} nodeStatus={nodeStatus} />
        <ColumnLayout columns={4} variant="text-grid">
          <ValueWithLabel label="Total">{section.total}</ValueWithLabel>
          {section.rows.map((row) => (
            <ValueWithLabel key={row.key} label={row.label}>
              {row.count}
            </ValueWithLabel>
          ))}
        </ColumnLayout>
      </SpaceBetween>
    </Container>
  );
}

function AssociationContainer({
  section,
  nodeStatus,
}: {
  section: AssociationSection;
  nodeStatus?: NodeStatusMap;
}): JSX.Element {
  let badge: JSX.Element;
  if (section.violations > 0) {
    badge = (
      <Badge color="red">
        {section.violations === 1 ? "1 violation" : `${section.violations} violations`}
      </Badge>
    );
  } else if (section.subjects > 0) {
    badge = <Badge color="green">All compliant</Badge>;
  } else {
    badge = <Badge color="grey">No subjects</Badge>;
  }
  return (
    <Container
      data-testid={`association-section-${section.nodeId}`}
      header={
        <Header variant="h2" actions={badge}>
          {`Object association: ${section.nodeId}`}
        </Header>
      }
    >
      <SpaceBetween size="m">
        <NodeOutcome nodeId={section.nodeId} nodeStatus={nodeStatus} />
        <ColumnLayout columns={4} variant="text-grid">
          <ValueWithLabel label="Subjects">{section.subjects}</ValueWithLabel>
          <ValueWithLabel label="Compliant">{section.compliant}</ValueWithLabel>
          <ValueWithLabel label="Violations">{section.violations}</ValueWithLabel>
          {section.missing.map((row) => (
            <ValueWithLabel key={row.key} label={`Missing ${row.label}`}>
              {row.count}
            </ValueWithLabel>
          ))}
        </ColumnLayout>
        {section.violatingIds.length > 0 && (
          <Box color="text-body-secondary">
            The violating subjects are marked in Objects detected.
          </Box>
        )}
      </SpaceBetween>
    </Container>
  );
}

function EventGateContainer({ section }: { section: EventGateSection }): JSX.Element {
  const active = section.state === "active";
  return (
    <Container
      data-testid={`event-gate-section-${section.nodeId}`}
      header={
        <Header
          variant="h2"
          actions={
            <Badge color={active ? "red" : "grey"}>{active ? "Active" : "Inactive"}</Badge>
          }
        >
          {`Event gate: ${section.nodeId}`}
        </Header>
      }
    >
      <ColumnLayout columns={4} variant="text-grid">
        <ValueWithLabel label="Transition">{TRANSITION_LABELS[section.transition]}</ValueWithLabel>
        <ValueWithLabel label="Active since">
          {section.activeSince !== null ? format(section.activeSince, DATE_WITHOUT_TZ) : "-"}
        </ValueWithLabel>
        <ValueWithLabel label="Consecutive runs true">{section.consecutiveTrue}</ValueWithLabel>
        <ValueWithLabel label="Consecutive runs false">{section.consecutiveFalse}</ValueWithLabel>
      </ColumnLayout>
    </Container>
  );
}

export default function SceneAnalyticsSections({
  analytics,
  nodeStatus,
}: {
  analytics: SceneAnalytics;
  nodeStatus?: NodeStatusMap;
}): JSX.Element {
  return (
    <SpaceBetween size="l">
      {analytics.counters.map((section) => (
        <CounterContainer key={section.nodeId} section={section} nodeStatus={nodeStatus} />
      ))}
      {analytics.associations.map((section) => (
        <AssociationContainer key={section.nodeId} section={section} nodeStatus={nodeStatus} />
      ))}
      {analytics.gates.map((section) => (
        <EventGateContainer key={section.nodeId} section={section} />
      ))}
    </SpaceBetween>
  );
}
