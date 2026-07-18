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
import axios from "axios";
import { Connection } from "config/Interface";

// Endpoint constants are built locally from the read-only Connection export
// so config/Interface.tsx stays untouched.
const REGISTRATIONS_ENDPOINT = `${Connection.ENDPOINT}/workflows/registrations`;
const EXECUTIONS_ENDPOINT = `${Connection.ENDPOINT}/workflows/executions`;

export type RegistrationStatus = "registered" | "invalid";
export type ExecutionStatus = "pending" | "running" | "completed" | "failed";

export interface WorkflowRegistration {
  registrationId: string;
  workflowId: string;
  version: string;
  arch: string;
  artifactPath: string;
  status: RegistrationStatus;
  /** Epoch seconds. */
  registeredAt: number;
  /** Present only when status is not "registered". */
  invalidReason?: string;
}

export interface WorkflowExecution {
  executionId: string;
  registrationId: string;
  status: ExecutionStatus;
  /** Epoch seconds; null until the execution starts. */
  startedAt: number | null;
  /** Epoch seconds; null until the execution finishes. */
  finishedAt: number | null;
  failingNodeId: string | null;
  error: string | null;
}

export interface WorkflowRegistrationDetails extends WorkflowRegistration {
  executions: WorkflowExecution[];
}

export async function listWorkflowRegistrations(): Promise<
  WorkflowRegistration[]
> {
  const { data } = await axios.get<WorkflowRegistration[]>(
    REGISTRATIONS_ENDPOINT,
  );
  return data;
}

export async function getWorkflowRegistration(
  id: string,
): Promise<WorkflowRegistrationDetails> {
  const { data } = await axios.get<WorkflowRegistrationDetails>(
    `${REGISTRATIONS_ENDPOINT}/${id}`,
  );
  return data;
}

export async function triggerWorkflowRegistration(
  id: string,
): Promise<WorkflowExecution> {
  const { data } = await axios.post<WorkflowExecution>(
    `${REGISTRATIONS_ENDPOINT}/${id}/trigger`,
  );
  return data;
}

export async function getWorkflowExecution(
  id: string,
): Promise<WorkflowExecution> {
  const { data } = await axios.get<WorkflowExecution>(
    `${EXECUTIONS_ENDPOINT}/${id}`,
  );
  return data;
}
