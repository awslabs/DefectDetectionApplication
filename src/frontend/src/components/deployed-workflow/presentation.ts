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

/**
 * Pure presentational logic for the deployed-workflows pages. Deliberately
 * free of React/DOM so it is directly property-testable.
 */
import type { StatusIndicatorProps } from "@cloudscape-design/components";
import {
  WorkflowExecution,
  WorkflowRegistration,
} from "api/WorkflowRegistrationAPI";

export interface StatusIndicatorDescriptor {
  type: StatusIndicatorProps.Type;
  text: string;
}

/** A run can be triggered if and only if the registration is `registered`. */
export function canTrigger(registration: WorkflowRegistration): boolean {
  return registration.status === "registered";
}

/** Cloudscape status-indicator props for a registration status. */
export function registrationStatusIndicator(
  registration: WorkflowRegistration,
): StatusIndicatorDescriptor {
  switch (registration.status) {
    case "registered":
      return { type: "success", text: "Registered" };
    case "invalid":
    default:
      return { type: "error", text: "Invalid" };
  }
}

/** Cloudscape status-indicator props for an execution status. */
export function executionStatusIndicator(
  execution: WorkflowExecution,
): StatusIndicatorDescriptor {
  switch (execution.status) {
    case "pending":
      return { type: "pending", text: "Pending" };
    case "running":
      return { type: "in-progress", text: "Running" };
    case "completed":
      return { type: "success", text: "Completed" };
    case "failed":
    default:
      return { type: "error", text: "Failed" };
  }
}

/**
 * Executions sorted newest first by `startedAt` for the history table.
 * Executions that have not started yet (null `startedAt`, i.e. pending) sort
 * before started ones since they are the most recent triggers. The sort is
 * stable for ties and nulls, and the input array is not mutated.
 */
export function sortExecutions(
  executions: WorkflowExecution[],
): WorkflowExecution[] {
  const startKey = (execution: WorkflowExecution): number =>
    execution.startedAt === null ? Number.POSITIVE_INFINITY : execution.startedAt;
  // Array.prototype.sort is stable; sort a copy to avoid mutating the input.
  return [...executions].sort((a, b) => startKey(b) - startKey(a));
}

/**
 * Failure details for a failed execution; undefined for any other status.
 */
export function executionFailureDetails(
  execution: WorkflowExecution,
): { failingNodeId?: string; error?: string } | undefined {
  if (execution.status !== "failed") {
    return undefined;
  }
  return {
    ...(execution.failingNodeId !== null
      ? { failingNodeId: execution.failingNodeId }
      : {}),
    ...(execution.error !== null ? { error: execution.error } : {}),
  };
}

/** An execution is active while it is pending or running. */
export function isExecutionActive(execution: WorkflowExecution): boolean {
  return execution.status === "pending" || execution.status === "running";
}

/**
 * The details page should poll while any execution is active, and stop once
 * all executions are terminal.
 */
export function shouldPoll(executions: WorkflowExecution[]): boolean {
  return executions.some(isExecutionActive);
}
