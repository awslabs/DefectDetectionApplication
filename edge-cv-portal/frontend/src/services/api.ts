/**
 * API service for making HTTP requests to the backend
 */
import { getConfig } from '../config';
import { UseCase, Device, User, S3Bucket } from '../types';
import type { Capture } from '../components/ResultsViewer';
import type {
  NodeTypeDescriptor,
  TestDataset,
  TestDatasetCompletedFile,
  TestDatasetUploadInitiation,
  WorkflowDefinition,
  WorkflowGenerationResult,
  WorkflowSummary,
  WorkflowTestRun,
  WorkflowTestRunDetail,
  WorkflowValidationRun,
  WorkflowValidationStatus,
} from '../pages/workflows/types';
import type {
  CameraMutationResponse,
  CameraSourceMutationBody,
  DeviceCameraConflictsResponse,
  DeviceCamerasResponse,
} from '../pages/workflows/cameraReference';
import type { CameraBindingContext } from '../pages/deployments/cameraBindings';
import type {
  BuildBranchesResponse,
  BuildJob,
  BuildJobsPage,
  BuildLogsPage,
  SubmitBuildRequest,
  SubmitBuildResponse,
} from '../pages/builds/types';
import type {
  CreateSyntheticSessionBody,
  SyntheticApprovalBody,
  SyntheticGenerateBody,
  SyntheticGenerateResponse,
  SyntheticIntegrateResponse,
  SyntheticModelsResponse,
  SyntheticPromptTemplateResponse,
  SyntheticRetrainBody,
  SyntheticSession,
  SyntheticSessionDetailResponse,
  SyntheticSessionSummary,
} from '../pages/synthetic/types';
import { beginRequest, endRequest } from './loadingBus';

/**
 * Error thrown for API failures. Workflow Manager endpoints use the
 * structured error envelope {error: {code, message, details}}; `code`
 * and `details` are carried through so callers can act on them (e.g.
 * the deployment ids of a rejected workflow delete, Requirement 5.6).
 */
export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status?: number,
    public readonly code?: string,
    public readonly details?: Record<string, unknown>
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

// Code_Assistant API types (custom-node-code-assist).

/** Runtime entry-point contract of the node type being edited. */
export type CodeAssistContract =
  | 'process_frame'
  | 'process_frame_or_handle'
  | 'frame_hook'
  | 'produce_frame';

/** One `POST /code-assist` request body. */
export interface CodeAssistRequest {
  usecase_id: string;
  surface: 'workflow-builder' | 'node-designer';
  contract: CodeAssistContract;
  /** 1..4,000 chars with at least one non-whitespace character. */
  prompt: string;
  /** Present iff the editor holds a non-whitespace character (2.6, 2.10). */
  current_code?: string;
  context?: {
    nodeType?: string;
    parameters?: { name: string; param_type: string; description?: string }[];
  };
}

/** Successful code-assist result; nothing is persisted server-side. */
export interface CodeAssistResponse {
  code: string;
  notes: string;
  model_id: string;
  contract: CodeAssistContract;
}

// User admin types (portal-user-manager).

/**
 * One Cognito account as returned by `GET /api/v1/admin/users`
 * (portal-user-manager Requirement 2.1). `role` is the Portal_Role from
 * the `custom:role` attribute (default `Viewer`); `user_status` is the
 * Cognito status (e.g. CONFIRMED, FORCE_CHANGE_PASSWORD); `edge_capable`
 * is true when a credential verifier has been captured for the account,
 * making it usable for local edge login.
 */
export interface AdminAccount {
  username: string;
  email: string;
  email_verified: boolean;
  role: string;
  user_status: string;
  enabled: boolean;
  edge_capable: boolean;
}

/** Response of `GET /api/v1/admin/users` (user_admin.py). */
export interface AdminUsersResponse {
  users: AdminAccount[];
  total_count: number;
}

/**
 * One edge device row of `GET /api/v1/admin/edge-sync/devices`
 * (portal-user-manager Requirement 7.4): the devices table joined with
 * the `dda-portal-account-sync` sync-state table. `lastSyncStatus` /
 * `lastSyncAt` are absent for devices that have never been synced;
 * `failureReason` accompanies a `failed` status (e.g. "device
 * unreachable").
 */
export interface EdgeSyncDevice {
  device_id: string;
  lastSyncStatus?: 'pending' | 'in_progress' | 'success' | 'failed' | null;
  /** Epoch milliseconds of the last successful sync. */
  lastSyncAt?: number | null;
  pendingChanges?: boolean;
  failureReason?: string | null;
}

// Build infrastructure configuration types
// (portal-build-fleet-and-workflow-gates Requirement 9).

/**
 * Effective build infrastructure configuration as returned by
 * `GET /build-config`: stored values merged over the documented defaults
 * (arm64 m6g.4xlarge, x86_64 m6i.4xlarge, 100 GB, us-east-1, 4 h,
 * spot off, source_ref null = repository default branch).
 */
export interface BuildInfrastructureConfig {
  arm64_instance_type: string;
  x86_64_instance_type: string;
  volume_size_gb: number;
  region: string;
  max_runtime_hours: number;
  use_spot_for_ephemeral: boolean;
  /** null means "the repository's default branch". */
  source_ref: string | null;
  /**
   * Operator-controlled repository the submission form defaults to
   * (build-source-selection Req 1.5); the documented default is the
   * DDA repository.
   */
  default_repository: string;
}

/**
 * One per-parameter rejection from `PUT /build-config`
 * (CONFIG_INVALID `details.errors`, Requirement 9.5): the failed
 * validation rule, the invalid parameter, and a user-readable message.
 */
export interface BuildConfigValidationError {
  rule: string;
  parameter?: string;
  message: string;
}

/** One applied change recorded by `PUT /build-config` (Requirement 9.4). */
export interface BuildConfigChange {
  parameter: string;
  prior_value: unknown;
  new_value: unknown;
}

/**
 * Partial configuration update for `PUT /build-config`. Values are sent
 * as entered (the backend validates and rejects atomically with
 * per-parameter errors); null reverts a field to its documented default.
 */
export type BuildInfrastructureConfigUpdate = Partial<
  Record<keyof BuildInfrastructureConfig, string | number | boolean | null>
>;

/** Response of `GET /api/v1/admin/edge-sync/devices` (user_admin.py). */
export interface EdgeSyncDevicesResponse {
  devices: EdgeSyncDevice[];
  count: number;
}

// Build fleet types (portal-build-fleet-and-workflow-gates).

/**
 * EC2 lifecycle state of a Dedicated_Build_Server — exactly the six
 * states of Requirement 6.1.
 */
export type BuildServerLifecycleState =
  | 'pending'
  | 'running'
  | 'stopping'
  | 'stopped'
  | 'shutting-down'
  | 'terminated';

/** CPU architecture of a Dedicated_Build_Server (Requirement 6.5). */
export type BuildServerArchitecture = 'arm64' | 'x86_64';

/**
 * Ubuntu LTS release of a Dedicated_Build_Server host: 22.04 is the
 * default (existing behavior); 24.04 (noble) is the JetPack 7 (JP7)
 * build host and is arm64 only (jetpack7-support design §10).
 */
export type BuildServerUbuntuVersion = '22.04' | '24.04';

/**
 * Marker of an accepted fleet action that has not yet reached its
 * expected lifecycle state (build_fleet.py, Requirement 6.11). The
 * dispatcher reports the action failed when `deadline` (epoch ms)
 * passes before `expected_state` is observed.
 */
export interface BuildServerPendingAction {
  action: 'launch' | 'start' | 'stop' | 'terminate';
  requested_by: string;
  /** Epoch milliseconds. */
  requested_at: number;
  /** Epoch milliseconds; 10 minutes after the action was accepted. */
  deadline: number;
  expected_state: BuildServerLifecycleState;
}

/**
 * One Dedicated_Build_Server as returned by `GET /build-servers`
 * (build_fleet.py, Requirement 6.1): name, instance identifier,
 * instance type, CPU architecture, lifecycle state (reconciled live
 * against EC2), the running Build_Job when one exists, and the time of
 * the last state change.
 */
export interface BuildServer {
  server_id: string;
  name: string;
  instance_id: string;
  instance_type: string;
  cpu_architecture: BuildServerArchitecture;
  /**
   * Ubuntu release the server was launched with; absent on servers
   * launched before the JP7 (24.04) option existed — those are 22.04
   * hosts.
   */
  ubuntu_version?: BuildServerUbuntuVersion;
  lifecycle_state: BuildServerLifecycleState;
  /** Present iff a Build_Job is currently running on the server. */
  running_build_job_id?: string | null;
  /** Epoch milliseconds of the last lifecycle state change. */
  last_state_change_at?: number | null;
  pending_action?: BuildServerPendingAction | null;
  created_by?: string;
  created_at?: number;
  terminated_at?: number | null;
}

/** Response of `GET /build-servers` (build_fleet.py). */
export interface BuildServersResponse {
  servers: BuildServer[];
}

// vLLM model registration types (vllm-triton-inference).

/**
 * One validation finding from `POST /api/v1/models/vllm` (model_import.py,
 * Requirements 1.1/1.9/1.10/1.11): a 400 response carries the complete
 * finding list, each naming the offending field and value.
 */
export interface VllmRegistrationFinding {
  field: string;
  value: unknown;
  reason: string;
}

/** One engine setting of `GET /api/v1/models/vllm/engine-spec`. */
export interface VllmEngineSettingSpec {
  default: string | number | boolean;
  type: 'string' | 'number' | 'integer' | 'boolean';
  accepted_values?: string[];
  range?: string;
  description: string;
}

/** Response of `GET /api/v1/models/vllm/engine-spec` (model_import.py). */
export interface VllmEngineSpec {
  description: string;
  settings: Record<string, VllmEngineSettingSpec>;
  source: {
    description: string;
    huggingface_model_id: { type: string; format: string; example: string };
    s3_model_artifact: { type: string; format: string; example: string };
  };
}

/**
 * The publish write-back map on a vLLM_Model_Record
 * (greengrass_publish.py task 4.2): carries the component identity and
 * the supported Target_Architecture set shown on the model detail view
 * (Requirement 3.8).
 */
export interface VllmPublishedComponent {
  component_name: string;
  component_version: string;
  supported_architectures: string[];
  runtime: string;
  component_arns: Record<string, string>;
  published_at: number;
}

// vLLM engine-configuration types (vllm-sizing-and-packaging-errors).

/**
 * The resolved Engine_Configuration stored on a vLLM_Model_Record
 * (dtype, gpu_memory_utilization, max_model_len, tensor_parallel_size,
 * enforce_eager). Returned on the model detail response (Requirement 1.2)
 * and by the engine-configuration update endpoint (Requirement 2.4).
 */
export type VllmEngineConfiguration = Record<string, string | number | boolean>;

/**
 * One per-architecture finding of a preflight Fit_Check evaluation.
 *
 * The first five fields are the ORIGINAL contract and are unchanged in name
 * and type. Everything below them is the ADDITIVE term breakdown behind
 * `fits` that `vllm_fit_check.FitFinding` gained in
 * `jp6-vllm-kv-cache-oom-regression` (design Decision 2, File 7), declared
 * **optional** on purpose: findings serialized before that change omit the
 * fields entirely, and every existing consumer reads only `message` — which
 * already states each term with its number — so absence must keep rendering
 * exactly as today (Requirements 2.1, 2.3, 3.1).
 */
export interface VllmFitCheckFinding {
  arch: string;
  fits: boolean;
  budget_bytes: number;
  required_bytes: number;
  message: string;
  /** Estimated on-GPU weight size charged against the budget. */
  weights_bytes?: number;
  /** Estimated PyTorch activation/profiling peak — an ESTIMATE, not measured. */
  activation_bytes?: number;
  /** Serving-margin floor reserved for the KV cache. */
  kv_floor_bytes?: number;
  /** Memory co-resident consumers hold on the shared (unified) device. */
  co_tenancy_bytes?: number;
  /**
   * Ceiling on `gpu_memory_utilization` for this architecture; `null` when
   * no cap is known (the backend omits rather than invents one).
   */
  fraction_cap?: number | null;
  /** Effective `limit_mm_per_prompt.image` the activation term scales with. */
  images_per_prompt?: number;
  /** Which conditions failed: `'budget'` | `'co_tenancy'`. */
  failed_conditions?: string[];
  /** Soft warnings on a verdict: `'thin_margin'` | `'near_cap'`. */
  warnings?: string[];
}

/**
 * Non-blocking Fit_Check result carried on registration and
 * engine-configuration update responses (Requirements 3.4, 3.5).
 */
export interface VllmFitCheckResult {
  status: 'passed' | 'warnings' | 'unverified';
  estimate: {
    total_bytes: number;
    method: string;
    detail: string;
  } | null;
  findings: VllmFitCheckFinding[];
  message?: string;
}

/**
 * Response of `PUT /api/v1/models/vllm/{training_id}/engine-configuration`
 * (model_import.py update_vllm_engine_configuration): the complete updated
 * configuration, a re-package/publish notice, and a fit-check result
 * (Requirements 2.4, 3.5). A 400 rejection carries the finding list as
 * `details.findings` on the thrown ApiError (Requirement 2.2).
 */
export interface VllmEngineConfigurationUpdateResponse {
  training_id: string;
  engine_configuration: VllmEngineConfiguration;
  notice: string;
  fit_check: VllmFitCheckResult;
}

// Station Quick Setup types (station-quick-setup).

/** Lifecycle state of a Device_Registration (Setup_Status). */
export type SetupStatus =
  | 'pending'
  | 'in_progress'
  | 'completed'
  | 'expired'
  | 'failed';

/**
 * The portal-side record of a pending Station as returned by the
 * device-registration routes (station-quick-setup Requirements 1.1, 6.3).
 * Token material is never included — only the token expiry is surfaced.
 */
export interface DeviceRegistration {
  registration_id: string;
  usecase_id: string;
  device_name: string;
  device_group: string;
  status: SetupStatus;
  created_by: string;
  created_at: number;
  updated_at: number;
  /** Epoch seconds; <= creation/regeneration time + 90 min. */
  token_expires_at: number;
  /** Present when status is `failed`; truncated to <=1024 chars. */
  error_summary?: string;
}

/** Body of `POST /device-registrations` (Requirement 1.1). */
export interface RegisterDeviceInput {
  device_name: string;
  device_group: string;
  usecase_id: string;
}

/**
 * Response of `POST /device-registrations` and
 * `POST /device-registrations/{id}/command`: the persisted registration
 * plus the one-line Setup_Command embedding the Setup_Token and the token
 * expiry (station-quick-setup Requirements 2.1, 2.5).
 */
export interface RegistrationWithCommand {
  registration: DeviceRegistration;
  setup_command: string;
  token_expires_at: number;
}

/** Response of `GET /device-registrations` (Requirement 6.3). */
export interface DeviceRegistrationsResponse {
  registrations: DeviceRegistration[];
  count: number;
}

/**
 * Response of `GET /device-registrations/thing-groups`: existing IoT Thing
 * Group names from the Use_Case account for Device_Group selection
 * (station-quick-setup Requirement 1.7).
 */
export interface ThingGroupsResponse {
  thing_groups: string[];
  count: number;
}

// DDA labeling types (dda-data-labeling).

/** One Labeling_Team member (dda-data-labeling Requirement 3.8). */
export interface LabelingTeamMember {
  user_id: string;
  email: string;
  added_at?: number;
  added_by?: string;
}

/**
 * One Labeling_Team as returned by `GET /labeling-teams?usecase_id=`,
 * including the current member list with identities and emails
 * (dda-data-labeling Requirement 3.8).
 */
export interface LabelingTeam {
  team_id: string;
  usecase_id: string;
  team_name: string;
  members: LabelingTeamMember[];
  created_at?: number;
  created_by?: string;
}

/**
 * Per-member submitted/remaining counts on a DDA job detail
 * (dda-data-labeling Requirement 11.2).
 */
export interface LabelingMemberProgress {
  user_id: string;
  email?: string;
  submitted: number;
  remaining: number;
}

/**
 * A terminally failed notification recipient recorded on the job
 * (dda-data-labeling Requirement 6.4).
 */
export interface LabelingNotificationFailure {
  email: string;
  reason: string;
}

/**
 * One job row of `GET /labeler/jobs`: a job in which the caller holds at
 * least one unsubmitted Task_Assignment, with submitted/remaining counts
 * (dda-data-labeling Requirements 2.4, 7.10).
 */
export interface LabelerJobSummary {
  job_id: string;
  job_name: string;
  task_type: string;
  label_set: string[];
  submitted_count: number;
  remaining_count: number;
  withheld_count?: number;
  instructions?: string;
}

/** A classless-or-classed region/box of a Pre_Label or annotation. */
export interface DdaBoundingBox {
  /** Label_Set class; null for unclassified SAM proposals. */
  class: string | null;
  left: number;
  top: number;
  width: number;
  height: number;
}

/** RLE-encoded mask region keyed to a Label_Set class. */
export interface DdaMaskRegion {
  /** Label_Set class; null for unclassified SAM proposals. */
  class: string | null;
  /** Run-length-encoded bitmap of the region. */
  rle: number[];
}

/**
 * Modality-tagged annotation payload used for both Pre_Labels returned by
 * the labeler APIs and submissions sent to `POST /labeler/tasks/{id}/submit`
 * (dda-data-labeling Requirements 7.3–7.5, 7.7, 8.3).
 */
export interface DdaAnnotation {
  /** Binary_Classification selection: 'normal' | 'anomaly'. */
  label?: string;
  /** Object_Detection boxes (pixel coordinates within image bounds). */
  boxes?: DdaBoundingBox[];
  /** Semantic_Segmentation regions (RLE-encoded, label-indexed). */
  regions?: DdaMaskRegion[];
  /** Image pixel dimensions the regions/boxes are expressed against. */
  image_width?: number;
  image_height?: number;
}

/**
 * Response of `GET /labeler/jobs/{jobId}/next`: the next presentable
 * unsubmitted Task_Assignment with a 15-minute presigned image URL,
 * Pre_Label (when available), instructions and example-image URLs, or a
 * completion payload when zero presentable tasks remain
 * (dda-data-labeling Requirements 7.1, 7.2, 7.11, 8.3, 12.6).
 */
export interface LabelerNextTaskResponse {
  /** True when the labeler has no presentable unsubmitted tasks left. */
  complete: boolean;
  task?: {
    task_id: string;
    job_id: string;
    image_url: string;
    /** Epoch seconds when the presigned image URL expires. */
    image_url_expires_at?: number;
    prelabel?: DdaAnnotation;
    prelabel_status?: string;
  };
  task_type?: string;
  label_set?: string[];
  instructions?: string;
  good_example_urls?: string[];
  bad_example_urls?: string[];
  submitted_count: number;
  remaining_count: number;
  withheld_count?: number;
}

/**
 * One per-image row of `GET /labeling/{id}/review`: the auto-labeled
 * result or failed status plus the current accept/reject decision
 * (dda-data-labeling Requirements 9.5, 9.6, 9.10).
 */
export interface ReviewItem {
  task_id: string;
  image_key: string;
  image_url?: string;
  status: 'succeeded' | 'failed';
  annotation?: DdaAnnotation;
  autolabel_error?: string;
  decision?: 'accepted' | 'rejected';
}

/** Response of `GET /labeling/{id}/review` (paginated). */
export interface ReviewResponse {
  job_id: string;
  items: ReviewItem[];
  count: number;
  next_token?: string;
  review_finalized?: boolean;
}

// Asynchronous workflow generation types (workflow-manager-gaps).

/**
 * 202 response of `POST /workflows/generate`
 * (workflow_generator.submit_generation, workflow-manager-gaps
 * Requirements 1.1, 1.7, 1.8): the accepted Generation_Job and the
 * effective Chat_Session identifier. `session_id` is freshly minted when
 * the request carried none or an unresolvable one, so callers adopt it
 * for follow-up prompts.
 */
export interface WorkflowGenerationSubmission {
  job_id: string;
  session_id: string;
  usecase_id: string;
  status: 'pending';
}

/**
 * A Generation_Job that has not reached a terminal state: the status
 * endpoint returns only the job identity and state, never a partial
 * result (workflow-manager-gaps Requirement 2.8).
 */
export interface WorkflowGenerationJobInProgress {
  job_id: string;
  status: 'pending' | 'running';
}

/**
 * A succeeded Generation_Job: the synchronous endpoint's exact payload
 * (`WorkflowGenerationResult`) embedded field-for-field beside the job
 * identity (workflow-manager-gaps Requirement 2.2).
 */
export interface WorkflowGenerationJobSucceeded extends WorkflowGenerationResult {
  job_id: string;
  status: 'succeeded';
}

/**
 * Resolved states of `GET /workflows/generate/{job_id}`, discriminated
 * on `status`. The third state — a failed Generation_Job — never
 * resolves: the backend replays the originating Error_Envelope with its
 * original non-2xx HTTP status (workflow-manager-gaps Requirement 2.3),
 * so it surfaces as a thrown `ApiError` carrying that status, code,
 * message, and details (e.g. GENERATION_TIMEOUT, GENERATION_REJECTED,
 * GENERATION_VALIDATION_INCOMPLETE, GENERATION_ABNORMAL_TERMINATION).
 */
export type WorkflowGenerationJobStatus =
  | WorkflowGenerationJobInProgress
  | WorkflowGenerationJobSucceeded;

class ApiService {
  private get baseUrl(): string {
    return getConfig().apiUrl;
  }

  private async request<T>(
    endpoint: string,
    options: RequestInit = {}
  ): Promise<T> {
    const token = localStorage.getItem('idToken');
    
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...(options.headers as Record<string, string>),
    };

    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }

    // Track this request globally so the app-wide activity bar shows while any
    // API call is in flight.
    beginRequest();
    try {
      const response = await fetch(`${this.baseUrl}${endpoint}`, {
        ...options,
        headers,
      });

      if (!response.ok) {
        // If 401, token might be expired - redirect to login
        if (response.status === 401) {
          console.error('Authentication failed - token may be expired');
          localStorage.removeItem('idToken');
          // Redirect to login page
          if (window.location.pathname !== '/login') {
            window.location.href = '/login';
          }
        }
        
        const error = await response.json().catch(() => ({ error: 'Request failed' }));
        // Structured error envelope: {error: {code, message, details}}
        if (error.error && typeof error.error === 'object') {
          throw new ApiError(
            error.error.message || `HTTP ${response.status}`,
            response.status,
            error.error.code,
            error.error.details
          );
        }
        // Simple error envelope {error: string, ...}: carry the HTTP
        // status so callers can branch on it (e.g. the delete-account
        // not-found path, portal-user-manager Requirement 14.11). The
        // full parsed body rides along as `details` so callers can read
        // sibling fields (e.g. the vLLM registration `findings` list).
        throw new ApiError(
          error.error || `HTTP ${response.status}`,
          response.status,
          undefined,
          error
        );
      }

      return response.json();
    } catch (err: any) {
      // Structured API errors carry code/details; re-throw untouched.
      if (err instanceof ApiError) {
        throw err;
      }
      // Handle Amplify/AWS errors that have a complex structure
      if (err && typeof err === 'object' && 'message' in err) {
        if (typeof err.message === 'string') {
          throw new Error(err.message);
        }
      }
      // Handle errors with nested structure
      if (err && typeof err === 'object' && 'errors' in err && Array.isArray(err.errors)) {
        const messages = err.errors.map((e: any) => e?.message || String(e)).join(', ');
        throw new Error(messages || 'Request failed');
      }
      // Re-throw if it's already an Error
      if (err instanceof Error) {
        throw err;
      }
      // Fallback
      throw new Error('Request failed');
    } finally {
      endRequest();
    }
  }

  // Auth endpoints
  async getCurrentUser(): Promise<{ user: User }> {
    return this.request<{ user: User }>('/auth/me');
  }

  // User admin endpoints (portal-user-manager) — PortalAdmin only.

  /**
   * All portal user accounts from the Cognito user pool
   * (`user_admin.py`, portal-user-manager Requirement 2.1).
   */
  async listAdminUsers(): Promise<AdminUsersResponse> {
    return this.request<AdminUsersResponse>('/admin/users');
  }

  /**
   * Create a new portal user account (`user_admin.py`, portal-user-manager
   * Requirement 12.1). Cognito emails the invitation with a temporary
   * password itself — the value never appears in the response (12.10).
   * A 409 indicates the username already exists (12.5); a 400 identifies
   * the invalid email or missing field (12.6, 12.7).
   */
  async createAdminUser(body: {
    username: string;
    email: string;
    role: string;
  }): Promise<{ message: string }> {
    return this.request<{ message: string }>('/admin/users', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  }

  /**
   * Set a new password on an account with the selected permanence
   * (`user_admin.py`, portal-user-manager Requirements 3.1, 3.2). A 400
   * response carries the violated Password_Policy rule verbatim (3.3).
   */
  async setAdminUserPassword(
    username: string,
    body: { password: string; permanent: boolean }
  ): Promise<{ message: string }> {
    return this.request<{ message: string }>(
      `/admin/users/${encodeURIComponent(username)}/password`,
      { method: 'POST', body: JSON.stringify(body) }
    );
  }

  /**
   * Trigger the forgot-password flow: a temporary password is generated
   * and emailed to the account's registered address; the value is never
   * returned to the client (portal-user-manager Requirements 4.1, 4.3).
   * A 400 response indicates the account has no verified email (4.4).
   */
  async sendAdminForgotPassword(username: string): Promise<{ message: string }> {
    return this.request<{ message: string }>(
      `/admin/users/${encodeURIComponent(username)}/forgot-password`,
      { method: 'POST' }
    );
  }

  /**
   * Change an account's Portal_Role (`user_admin.py`, portal-user-manager
   * Requirement 5.1). A 409 response carries the rejection reason, e.g.
   * the last-PortalAdmin guard (5.3).
   */
  async setAdminUserRole(
    username: string,
    role: string
  ): Promise<{ message: string }> {
    return this.request<{ message: string }>(
      `/admin/users/${encodeURIComponent(username)}/role`,
      { method: 'PUT', body: JSON.stringify({ role }) }
    );
  }

  /**
   * Disable an account (`user_admin.py`, portal-user-manager Requirement
   * 13.2). An already-disabled account is a 200 no-op returning the
   * current state (13.6). A 409 response carries the rejection reason,
   * e.g. the last-PortalAdmin guard (5.3, 13.9); other failures are a 502
   * with the state unchanged (13.7).
   */
  async disableAdminUser(
    username: string
  ): Promise<{ message: string; enabled?: boolean }> {
    return this.request<{ message: string; enabled?: boolean }>(
      `/admin/users/${encodeURIComponent(username)}/disable`,
      { method: 'POST' }
    );
  }

  /**
   * Enable an account (`user_admin.py`, portal-user-manager Requirement
   * 13.3). An already-enabled account is a 200 no-op returning the
   * current state (13.6); other failures are a 502 with the state
   * unchanged (13.7).
   */
  async enableAdminUser(
    username: string
  ): Promise<{ message: string; enabled?: boolean }> {
    return this.request<{ message: string; enabled?: boolean }>(
      `/admin/users/${encodeURIComponent(username)}/enable`,
      { method: 'POST' }
    );
  }

  /**
   * Delete an account (`user_admin.py`, portal-user-manager Requirement
   * 14.2). A 409 carries the rejection reason, e.g. the last-PortalAdmin
   * guard (14.3); a 404 means the account no longer exists in the user
   * pool (14.11); a partial verifier-cleanup failure returns an error
   * stating the account was deleted but its verifier record was not
   * removed (14.10); other failures leave the account unchanged (14.6).
   */
  async deleteAdminUser(username: string): Promise<{ message: string }> {
    return this.request<{ message: string }>(
      `/admin/users/${encodeURIComponent(username)}`,
      { method: 'DELETE' }
    );
  }

  /**
   * Edge devices with their per-device account-sync state: last sync
   * status, last sync timestamp, and whether changes are pending
   * (`user_admin.py`, portal-user-manager Requirement 7.4).
   */
  async listEdgeSyncDevices(): Promise<EdgeSyncDevicesResponse> {
    return this.request<EdgeSyncDevicesResponse>('/admin/edge-sync/devices');
  }

  /**
   * Stage the selected accounts for sync to an edge device and trigger
   * an immediate sync attempt (`user_admin.py`, portal-user-manager
   * Requirement 7.1).
   */
  async syncEdgeDevice(
    deviceId: string,
    usernames: string[]
  ): Promise<{ message: string }> {
    return this.request<{ message: string }>(
      `/admin/edge-sync/devices/${encodeURIComponent(deviceId)}`,
      { method: 'POST', body: JSON.stringify({ usernames }) }
    );
  }

  // Build fleet endpoints (portal-build-fleet-and-workflow-gates).

  /**
   * The Dedicated_Build_Server fleet list with live EC2 lifecycle state
   * reconciliation (`build_fleet.py`, Requirement 6.1).
   */
  async listBuildServers(): Promise<BuildServersResponse> {
    return this.request<BuildServersResponse>('/build-servers');
  }

  /**
   * Launch a new Dedicated_Build_Server of the selected CPU architecture
   * (`build_fleet.py`, Requirement 6.5). PortalAdmin only (6.7); a 400
   * identifies the missing name or invalid architecture. An omitted
   * `ubuntu_version` means 22.04; 24.04 (the JP7 build host) is
   * accepted for arm64 only (jetpack7-support design §10).
   */
  async launchBuildServer(body: {
    name: string;
    architecture: BuildServerArchitecture;
    ubuntu_version?: BuildServerUbuntuVersion;
  }): Promise<{ server: BuildServer }> {
    return this.request<{ server: BuildServer }>('/build-servers', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  }

  /**
   * Start a stopped Dedicated_Build_Server (`build_fleet.py`,
   * Requirements 6.2, 6.10). PortalAdmin only (6.7); a 409 identifies
   * the server's current lifecycle state when start is not permitted.
   */
  async startBuildServer(serverId: string): Promise<{ server: BuildServer }> {
    return this.request<{ server: BuildServer }>(
      `/build-servers/${encodeURIComponent(serverId)}/start`,
      { method: 'POST' }
    );
  }

  /**
   * Stop a running Dedicated_Build_Server with no running Build_Job
   * (`build_fleet.py`, Requirements 6.3, 6.4, 6.10). PortalAdmin only
   * (6.7); a 409 identifies the current lifecycle state or the running
   * Build_Job when stop is not permitted.
   */
  async stopBuildServer(serverId: string): Promise<{ server: BuildServer }> {
    return this.request<{ server: BuildServer }>(
      `/build-servers/${encodeURIComponent(serverId)}/stop`,
      { method: 'POST' }
    );
  }

  /**
   * Terminate a Dedicated_Build_Server. The request body must echo the
   * server's exact name as `confirm` — anything else performs no
   * termination and leaves the server unchanged (`build_fleet.py`,
   * Requirements 6.6, 6.12). PortalAdmin only (6.7); a 409 identifies a
   * running Build_Job or a lifecycle state that forbids termination.
   */
  async terminateBuildServer(
    serverId: string,
    confirm: string
  ): Promise<{ server: BuildServer }> {
    return this.request<{ server: BuildServer }>(
      `/build-servers/${encodeURIComponent(serverId)}`,
      { method: 'DELETE', body: JSON.stringify({ confirm }) }
    );
  }

  // UseCase endpoints
  async listUseCases(): Promise<{ usecases: UseCase[]; count: number }> {
    return this.request<{ usecases: UseCase[]; count: number }>('/usecases');
  }

  // List S3 buckets in the current (portal) account - used by Onboard New Use Case
  async listS3Buckets(): Promise<{ buckets: S3Bucket[]; count: number }> {
    return this.request<{ buckets: S3Bucket[]; count: number }>('/s3-buckets');
  }

  // Create a new S3 bucket in the current (portal) account with default settings
  async createS3Bucket(data: { name: string; region?: string }): Promise<{ bucket: S3Bucket }> {
    return this.request<{ bucket: S3Bucket }>('/s3-buckets', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  // Dataset endpoints
  async listDatasets(params: {
    usecase_id: string;
    prefix?: string;
    max_depth?: number;
  }): Promise<{
    datasets: Array<{
      prefix: string;
      image_count: number;
      last_modified: string | null;
      has_subdirectories: boolean;
    }>;
    bucket: string;
    base_prefix: string;
  }> {
    const queryParams = new URLSearchParams({
      usecase_id: params.usecase_id,
      ...(params.prefix && { prefix: params.prefix }),
      ...(params.max_depth && { max_depth: params.max_depth.toString() }),
    });
    return this.request(`/datasets?${queryParams}`);
  }

  async countImages(params: {
    usecase_id: string;
    prefix: string;
  }): Promise<{
    prefix: string;
    image_count: number;
    sample_images: Array<{
      key: string;
      size: number;
      last_modified: string;
    }>;
    bucket: string;
  }> {
    return this.request('/datasets/count', {
      method: 'POST',
      body: JSON.stringify(params),
    });
  }

  async getImagePreview(params: {
    usecase_id: string;
    prefix: string;
    limit?: number;
    offset?: number;
  }): Promise<{
    prefix: string;
    bucket: string;
    total_found: number;
    offset: number;
    limit: number;
    has_more: boolean;
    images: Array<{
      key: string;
      filename: string;
      size: number;
      last_modified: string;
      presigned_url: string;
    }>;
    expires_in_seconds: number;
  }> {
    const queryParams = new URLSearchParams({
      usecase_id: params.usecase_id,
      prefix: params.prefix,
      ...(params.limit && { limit: params.limit.toString() }),
      ...(params.offset !== undefined && { offset: params.offset.toString() }),
    });
    return this.request(`/datasets/preview?${queryParams}`);
  }

  // Captures endpoint (inference-results Results_Viewer)
  // Mirrors getImagePreview: presigned-URL pattern against the inference-results
  // bucket, returning parsed capture metadata (detection typing + Detections_Block)
  // and presigned URLs for the source / overlay / mask artifacts.
  async getCaptures(params: {
    usecase_id: string;
    prefix: string;
    device_id?: string;
    limit?: number;
  }): Promise<{
    captures: Capture[];
    bucket: string;
    prefix: string;
    total_found: number;
    expires_in_seconds: number;
  }> {
    const queryParams = new URLSearchParams({
      usecase_id: params.usecase_id,
      prefix: params.prefix,
      ...(params.device_id && { device_id: params.device_id }),
      ...(params.limit && { limit: params.limit.toString() }),
    });
    return this.request(`/captures?${queryParams}`);
  }

  async getUseCase(id: string): Promise<{ usecase: UseCase }> {
    return this.request<{ usecase: UseCase }>(`/usecases/${id}`);
  }

  async createUseCase(data: Partial<UseCase>): Promise<{ usecase: UseCase; message: string }> {
    return this.request<{ usecase: UseCase; message: string }>('/usecases', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async verifyRole(roleArn: string, externalId?: string): Promise<{
    status: string;
    account_id?: string;
    assumed_role?: string;
    error?: string;
  }> {
    return this.request('/usecases/verify-role', {
      method: 'POST',
      body: JSON.stringify({ role_arn: roleArn, external_id: externalId }),
    });
  }

  async updateUseCase(id: string, data: Partial<UseCase>): Promise<{ message: string }> {
    return this.request<{ message: string }>(`/usecases/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    });
  }

  async deleteUseCase(id: string): Promise<{ message: string }> {
    return this.request<{ message: string }>(`/usecases/${id}`, {
      method: 'DELETE',
    });
  }

  // Shared Components endpoints
  async provisionSharedComponents(usecaseId: string, componentVersion?: string): Promise<{
    usecase_id: string;
    components: Array<{
      component_name: string;
      component_version?: string;
      component_arn?: string;
      platform: string;
      status: string;
      error?: string;
    }>;
    policy_updated: boolean;
    message: string;
  }> {
    return this.request('/shared-components/provision', {
      method: 'POST',
      body: JSON.stringify({
        usecase_id: usecaseId,
        ...(componentVersion && { component_version: componentVersion }),
      }),
    });
  }

  async listAvailableSharedComponents(): Promise<{
    components: Array<{
      component_name: string;
      description: string;
      platform: string;
      platforms: string[];
      source: string;
      latest_version: string;
    }>;
    count: number;
  }> {
    return this.request('/shared-components/available');
  }

  async listSharedComponents(usecaseId: string): Promise<{
    usecase_id: string;
    components: Array<{
      component_name: string;
      component_version: string;
      component_arn: string;
      platform: string;
      status: string;
      update_available?: boolean;
      latest_version?: string;
    }>;
    count: number;
    latest_version: string;
  }> {
    return this.request(`/shared-components?usecase_id=${usecaseId}`);
  }

  async getSharedComponentsStatus(): Promise<{
    usecases: Array<{
      usecase_id: string;
      usecase_name: string;
      account_id: string;
      needs_update: boolean;
      shared_components_provisioned: boolean;
      components: Array<{
        component_name: string;
        current_version: string;
        latest_version: string;
        update_available: boolean;
        status: string;
      }>;
    }>;
    total_usecases: number;
    usecases_needing_update: number;
    latest_version: string;
  }> {
    return this.request('/shared-components/status');
  }

  async updateAllSharedComponents(params?: {
    version?: string;
    usecase_ids?: string[];
  }): Promise<{
    message: string;
    target_version: string;
    results: Array<{
      usecase_id: string;
      usecase_name: string;
      status: string;
      error?: string;
      components?: Array<{
        component_name: string;
        status: string;
        error?: string;
      }>;
    }>;
    success_count: number;
    failed_count: number;
  }> {
    return this.request('/shared-components/update-all', {
      method: 'POST',
      body: JSON.stringify(params || {}),
    });
  }

  // Device endpoints
  async listDevices(usecaseId: string): Promise<{ devices: Device[]; count: number }> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request<{ devices: Device[]; count: number }>(`/devices?usecase_id=${usecaseId}`);
  }

  async getDevice(id: string, usecaseId: string): Promise<{ device: Device }> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request<{ device: Device }>(`/devices/${id}?usecase_id=${usecaseId}`);
  }

  /**
   * Record portal-managed device attributes on the Devices table: the
   * Test_Device flag and/or the DDA Target_Architecture the deployment
   * architecture gates check. UseCaseAdmin (node-designer:manage) only —
   * the backend enforces the permission.
   */
  async updateDeviceFlags(
    deviceId: string,
    usecaseId: string,
    updates: { test_device?: boolean; target_architecture?: string | null }
  ): Promise<{
    device_id: string;
    usecase_id: string;
    test_device: boolean;
    target_architecture: string | null;
  }> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request(`/devices/${deviceId}`, {
      method: 'PUT',
      body: JSON.stringify({ usecase_id: usecaseId, ...updates }),
    });
  }

  /**
   * The device's Camera_Registry entries with computed staleness and the
   * device's IoT connectivity status (camera-registry-sync Requirements
   * 1.3, 7.1). Devices that never completed a synchronization return
   * `state: "never-synced"` rather than a bare empty list.
   */
  async getDeviceCameras(deviceId: string, usecaseId: string): Promise<DeviceCamerasResponse> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request<DeviceCamerasResponse>(
      `/devices/${deviceId}/cameras?usecase_id=${usecaseId}`
    );
  }

  /**
   * The device's recorded camera-sync conflict events, newest first
   * (camera-registry-sync Requirement 6.3).
   */
  async getDeviceCameraConflicts(
    deviceId: string,
    usecaseId: string
  ): Promise<DeviceCameraConflictsResponse> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request<DeviceCameraConflictsResponse>(
      `/devices/${deviceId}/cameras/conflicts?usecase_id=${usecaseId}`
    );
  }

  /** Create a portal-managed Camera_Source (Operator, Requirement 5.1). */
  async createDeviceCamera(
    deviceId: string,
    usecaseId: string,
    body: CameraSourceMutationBody
  ): Promise<CameraMutationResponse> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request<CameraMutationResponse>(
      `/devices/${deviceId}/cameras?usecase_id=${usecaseId}`,
      { method: 'POST', body: JSON.stringify(body) }
    );
  }

  /** Update a portal-managed Camera_Source (Operator, Requirement 5.1). */
  async updateDeviceCamera(
    deviceId: string,
    cameraSourceId: string,
    usecaseId: string,
    body: CameraSourceMutationBody
  ): Promise<CameraMutationResponse> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request<CameraMutationResponse>(
      `/devices/${deviceId}/cameras/${encodeURIComponent(cameraSourceId)}?usecase_id=${usecaseId}`,
      { method: 'PUT', body: JSON.stringify(body) }
    );
  }

  /** Pending-delete a portal-managed Camera_Source (Operator). */
  async deleteDeviceCamera(
    deviceId: string,
    cameraSourceId: string,
    usecaseId: string
  ): Promise<CameraMutationResponse> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request<CameraMutationResponse>(
      `/devices/${deviceId}/cameras/${encodeURIComponent(cameraSourceId)}?usecase_id=${usecaseId}`,
      { method: 'DELETE' }
    );
  }

  /**
   * Re-issue a conflict's overridden portal version as a new pending
   * change (Operator, Requirement 6.4).
   */
  async reapplyCameraConflict(
    deviceId: string,
    conflictId: string,
    usecaseId: string
  ): Promise<CameraMutationResponse> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request<CameraMutationResponse>(
      `/devices/${deviceId}/cameras/conflicts/${encodeURIComponent(conflictId)}/reapply?usecase_id=${usecaseId}`,
      { method: 'POST' }
    );
  }

  /**
   * On-demand refresh: pulls the device's registry shadow through the
   * same reducer as the ingest path and returns the refreshed inventory.
   */
  async refreshDeviceCameras(
    deviceId: string,
    usecaseId: string
  ): Promise<DeviceCamerasResponse> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request<DeviceCamerasResponse>(
      `/devices/${deviceId}/cameras/refresh?usecase_id=${usecaseId}`,
      { method: 'POST' }
    );
  }

  // Device Logs endpoints
  async getDeviceLogGroups(deviceId: string, usecaseId: string): Promise<{
    device_id: string;
    log_groups: Array<{
      log_group_name: string;
      component_type: 'system' | 'user';
      component_name: string;
      creation_time?: number;
      stored_bytes: number;
      retention_days?: number;
    }>;
    count: number;
  }> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request(`/devices/${deviceId}/logs?usecase_id=${usecaseId}`);
  }

  async getDeviceLogs(
    deviceId: string,
    componentName: string,
    usecaseId: string,
    params?: {
      start_time?: number;
      end_time?: number;
      limit?: number;
      next_token?: string;
      filter_pattern?: string;
    }
  ): Promise<{
    device_id: string;
    component_name: string;
    log_group_name: string;
    logs: Array<{
      timestamp: number;
      message: string;
      log_stream_name: string;
      ingestion_time?: number;
    }>;
    count: number;
    start_time: number;
    end_time: number;
    next_token?: string;
  }> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    const queryParams = new URLSearchParams({ usecase_id: usecaseId });
    if (params?.start_time) queryParams.set('start_time', params.start_time.toString());
    if (params?.end_time) queryParams.set('end_time', params.end_time.toString());
    if (params?.limit) queryParams.set('limit', params.limit.toString());
    if (params?.next_token) queryParams.set('next_token', params.next_token);
    if (params?.filter_pattern) queryParams.set('filter_pattern', params.filter_pattern);
    
    return this.request(`/devices/${deviceId}/logs/${encodeURIComponent(componentName)}?${queryParams}`);
  }

  async analyzeLogs(
    deviceId: string,
    usecaseId: string,
    params?: {
      hours?: number;
    }
  ): Promise<{
    analysis: {
      device_id: string;
      analysis_timestamp: string;
      issues_detected: number;
      critical_count: number;
      high_count: number;
      medium_count: number;
      low_count: number;
      issues: Array<{
        issue_id: string;
        title: string;
        severity: 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW';
        likely_causes: string[];
        recommended_actions: string[];
        prevention_tips: string[];
      }>;
      next_steps: string[];
    };
  }> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    const queryParams = new URLSearchParams({ usecase_id: usecaseId });
    if (params?.hours) queryParams.set('hours', params.hours.toString());
    
    return this.request(
      `/devices/${deviceId}/logs/analyze?${queryParams}`,
      { method: 'POST' }
    );
  }

  // SSH tunnel (AWS IoT Secure Tunneling) endpoints
  async getSshTunnelStatus(deviceId: string, usecaseId: string): Promise<{
    device_id: string;
    enabled: boolean;
    component_version?: string | null;
    device_arch?: string | null;
    // Max SecureTunneling version deployable to this arch (e.g. '1.1.3' on
    // JP5, where >= 2.0.0 is GLIBC-incompatible); null when uncapped.
    secure_tunneling_max_version?: string | null;
  }> {
    return this.request(`/devices/${deviceId}/ssh-tunnel?usecase_id=${usecaseId}`);
  }

  async setSshTunnel(deviceId: string, usecaseId: string, enabled: boolean, osUser?: string): Promise<{
    device_id: string;
    enabled: boolean;
    os_user?: string | null;
    deployment_id?: string;
    message: string;
  }> {
    return this.request(`/devices/${deviceId}/ssh-tunnel?usecase_id=${usecaseId}`, {
      method: 'POST',
      body: JSON.stringify({ enabled, osUser }),
    });
  }

  async openSshTunnel(deviceId: string, usecaseId: string, lifetimeMinutes?: number): Promise<{
    device_id: string;
    tunnel_id: string;
    region: string;
    source_access_token: string;
    lifetime_minutes: number;
    message: string;
  }> {
    const qs = new URLSearchParams({ usecase_id: usecaseId });
    if (lifetimeMinutes) qs.set('lifetime_minutes', String(lifetimeMinutes));
    return this.request(`/devices/${deviceId}/ssh-tunnel/open?${qs}`, { method: 'POST' });
  }

  // Device registration endpoints (station-quick-setup)

  /**
   * Register a new device from the portal, returning the created
   * Device_Registration together with the one-line Setup_Command and its
   * token expiry (station-quick-setup Requirement 1.1). Requires the
   * manage-devices permission — the backend enforces it. A 400 identifies
   * each missing/invalid field (1.2, 1.9); a 409 identifies a conflicting
   * device name (1.3).
   */
  async registerDevice(
    input: RegisterDeviceInput
  ): Promise<RegistrationWithCommand> {
    return this.request<RegistrationWithCommand>('/device-registrations', {
      method: 'POST',
      body: JSON.stringify(input),
    });
  }

  /**
   * List the Device_Registrations for a Use_Case with their Setup_Status
   * and token expiry, never token material (station-quick-setup
   * Requirement 6.3).
   */
  async listDeviceRegistrations(
    usecaseId: string
  ): Promise<DeviceRegistrationsResponse> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request<DeviceRegistrationsResponse>(
      `/device-registrations?usecase_id=${encodeURIComponent(usecaseId)}`
    );
  }

  /**
   * List the existing IoT Thing Group names in the Use_Case account for
   * Device_Group selection (station-quick-setup Requirement 1.7).
   */
  async listThingGroups(usecaseId: string): Promise<ThingGroupsResponse> {
    if (!usecaseId) {
      throw new Error('usecase_id is required');
    }
    return this.request<ThingGroupsResponse>(
      `/device-registrations/thing-groups?usecase_id=${encodeURIComponent(usecaseId)}`
    );
  }

  /**
   * Regenerate the Setup_Command for a Device_Registration, invalidating
   * any prior Setup_Token and returning an updated command (station-quick-
   * setup Requirements 2.5, 6.4). A 4xx indicates the registration is
   * already `completed` (2.8).
   */
  async regenerateSetupCommand(
    registrationId: string
  ): Promise<RegistrationWithCommand> {
    return this.request<RegistrationWithCommand>(
      `/device-registrations/${encodeURIComponent(registrationId)}/command`,
      { method: 'POST' }
    );
  }

  /**
   * Delete a non-completed Device_Registration, invalidating its
   * Setup_Token (station-quick-setup Requirement 6.6). A 4xx indicates the
   * registration is `completed` and cannot be deleted (6.9).
   */
  async deleteDeviceRegistration(
    registrationId: string
  ): Promise<{ message: string }> {
    return this.request<{ message: string }>(
      `/device-registrations/${encodeURIComponent(registrationId)}`,
      { method: 'DELETE' }
    );
  }

  // Training endpoints
  async listTrainingJobs(usecaseId?: string): Promise<{ jobs: any[]; count: number }> {
    const query = usecaseId ? `?usecase_id=${usecaseId}` : '';
    return this.request<{ jobs: any[]; count: number }>(`/training${query}`);
  }

  async getTrainingJob(id: string): Promise<any> {
    return this.request<any>(`/training/${id}`);
  }

  // Workteams endpoints
  async listWorkteams(usecaseId: string): Promise<{
    workteams: Array<{
      name: string;
      arn: string;
      description: string;
      member_count: number;
    }>;
    count: number;
  }> {
    const queryParams = new URLSearchParams({ usecase_id: usecaseId });
    return this.request(`/workteams?${queryParams}`);
  }

  // Labeling endpoints
  async listLabelingJobs(params: {
    usecase_id: string;
    status?: string;
  }): Promise<{
    jobs: Array<{
      job_id: string;
      job_name: string;
      status: string;
      task_type: string;
      image_count: number;
      labeled_objects?: number;
      progress_percent?: number;
      created_at: number;
      updated_at: number;
    }>;
    count: number;
  }> {
    const queryParams = new URLSearchParams({
      usecase_id: params.usecase_id,
      ...(params.status && { status: params.status }),
    });
    return this.request(`/labeling?${queryParams}`);
  }

  async createLabelingJob(data: {
    usecase_id: string;
    job_name: string;
    dataset_prefix: string;
    task_type: string;
    /**
     * Mandatory Labeling_Backend discriminator (dda-data-labeling
     * Requirement 1.1): 'GroundTruth' submits through the existing
     * SageMaker flow unchanged; 'DDA' creates a portal-native job.
     */
    labeling_backend: 'DDA' | 'GroundTruth';
    // Ground Truth fields (labeling_backend='GroundTruth').
    label_categories?: string[];
    workforce_arn?: string;
    instructions?: string;
    num_workers_per_object?: number;
    task_time_limit?: number;
    mask_prefix?: string;
    enable_automated_labeling?: boolean;
    // DDA fields (labeling_backend='DDA', dda-data-labeling
    // Requirements 4.1-4.4, 8.1, 9.2).
    label_set?: string[];
    team_id?: string;
    example_images?: { good: string[]; bad: string[] };
    auto_label?: { enabled: boolean; model: string };
    skip_verification?: boolean;
    bedrock_model_id?: string;
    per_label_prompts?: Record<string, string>;
  }): Promise<{
    job_id: string;
    sagemaker_job_name?: string;
    status: string;
    message: string;
  }> {
    return this.request('/labeling', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async getLabelingJob(jobId: string): Promise<{
    job: {
      job_id: string;
      usecase_id: string;
      job_name: string;
      sagemaker_job_name: string;
      status: string;
      task_type: string;
      dataset_prefix: string;
      image_count: number;
      label_categories: string[];
      labeled_objects?: number;
      human_labeled?: number;
      machine_labeled?: number;
      failed_objects?: number;
      progress_percent?: number;
      manifest_s3_uri: string;
      output_s3_uri: string;
      output_manifest_s3_uri?: string;
      workforce_arn: string;
      created_at: number;
      created_by: string;
      updated_at: number;
      completed_at?: number;
      failure_reason?: string;
      console_url?: string;
      worker_portal_url?: string;
      // DDA job fields (labeling_backend='DDA', dda-data-labeling
      // Requirements 5.4, 6.4, 6.6, 11.1, 11.2, 11.10).
      labeling_backend?: 'DDA' | 'GroundTruth';
      label_set?: string[];
      team_id?: string;
      submitted_count?: number;
      member_progress?: LabelingMemberProgress[];
      unassigned_count?: number;
      blocked?: boolean;
      notifications_skipped?: boolean;
      notification_failures?: LabelingNotificationFailure[];
      skip_verification?: boolean;
      review_ready?: boolean;
      stopped_at?: number;
    };
  }> {
    return this.request(`/labeling/${jobId}`);
  }

  async getLabelingJobManifest(jobId: string): Promise<{
    manifest_uri: string;
    job_id: string;
  }> {
    return this.request(`/labeling/${jobId}/manifest`);
  }

  // DDA labeling endpoints (dda-data-labeling).

  /**
   * List the Labeling_Teams scoped to a Use_Case with each team's member
   * identities and emails (dda-data-labeling Requirement 3.8).
   */
  async listLabelingTeams(usecaseId: string): Promise<{
    teams: LabelingTeam[];
    count: number;
  }> {
    const queryParams = new URLSearchParams({ usecase_id: usecaseId });
    return this.request(`/labeling-teams?${queryParams}`);
  }

  /**
   * Create a Labeling_Team scoped to a Use_Case (dda-data-labeling
   * Requirements 3.1, 3.2). A 4xx indicates a name validation failure.
   */
  async createLabelingTeam(data: {
    usecase_id: string;
    team_name: string;
  }): Promise<{ team: LabelingTeam; message?: string }> {
    return this.request('/labeling-teams', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  /**
   * Add a Data_Labeler user to a Labeling_Team (dda-data-labeling
   * Requirements 3.3–3.5). A 4xx indicates a missing Data_Labeler role or
   * duplicate membership.
   */
  async addTeamMember(
    teamId: string,
    userId: string
  ): Promise<{ message: string }> {
    return this.request(
      `/labeling-teams/${encodeURIComponent(teamId)}/members`,
      {
        method: 'POST',
        body: JSON.stringify({ user_id: userId }),
      }
    );
  }

  /**
   * Remove a member from a Labeling_Team; the member's unsubmitted
   * Task_Assignments in InProgress jobs are reassigned server-side
   * (dda-data-labeling Requirements 3.6, 5.3, 5.4).
   */
  async removeTeamMember(
    teamId: string,
    userId: string
  ): Promise<{ message: string }> {
    return this.request(
      `/labeling-teams/${encodeURIComponent(teamId)}/members/${encodeURIComponent(userId)}`,
      { method: 'DELETE' }
    );
  }

  /**
   * Delete a Labeling_Team (dda-data-labeling Requirement 3.1). A 409
   * indicates the team is referenced by an InProgress labeling job and
   * cannot be deleted yet.
   */
  async deleteLabelingTeam(teamId: string): Promise<{ message: string }> {
    return this.request(`/labeling-teams/${encodeURIComponent(teamId)}`, {
      method: 'DELETE',
    });
  }

  /**
   * Stop an InProgress DDA Labeling_Job, retaining submitted annotations
   * (dda-data-labeling Requirements 11.4, 11.5, 11.9). A 4xx indicates the
   * job is not InProgress; a 5xx indicates the job was not stopped.
   */
  async stopLabelingJob(jobId: string): Promise<{
    job_id: string;
    status: string;
    stopped_at?: number;
    message?: string;
  }> {
    return this.request(`/labeling/${encodeURIComponent(jobId)}/stop`, {
      method: 'POST',
    });
  }

  /**
   * List the jobs in which the caller holds at least one unsubmitted
   * Task_Assignment, with submitted/remaining counts (dda-data-labeling
   * Requirements 2.4, 7.10). Empty list when none exist.
   */
  async getLabelerJobs(): Promise<{
    jobs: LabelerJobSummary[];
    count: number;
  }> {
    return this.request('/labeler/jobs');
  }

  /**
   * Fetch the caller's next presentable unsubmitted Task_Assignment for a
   * job — presigned image URL, Pre_Label when available, instructions and
   * example-image URLs — or the completion payload when none remain
   * (dda-data-labeling Requirements 7.1, 7.2, 8.3, 12.6).
   */
  async getNextTask(jobId: string): Promise<LabelerNextTaskResponse> {
    return this.request(`/labeler/jobs/${encodeURIComponent(jobId)}/next`);
  }

  /**
   * Submit the annotation for a Task_Assignment, marking it submitted
   * (dda-data-labeling Requirements 7.7–7.9, 11.8). A 4xx indicates an
   * incomplete annotation or a Stopped job; a 5xx means the annotation was
   * not saved and the task remains unsubmitted.
   */
  async submitTask(
    taskId: string,
    annotation: DdaAnnotation
  ): Promise<{
    message: string;
    submitted_count?: number;
    remaining_count?: number;
  }> {
    return this.request(`/labeler/tasks/${encodeURIComponent(taskId)}/submit`, {
      method: 'POST',
      body: JSON.stringify({ annotation }),
    });
  }

  /**
   * Record that a Task_Assignment's image could not be retrieved or
   * rendered; the task is withheld from labeling (dda-data-labeling
   * Requirement 7.12).
   */
  async reportPresentationFailure(
    taskId: string,
    reason: string
  ): Promise<{ message: string }> {
    return this.request(
      `/labeler/tasks/${encodeURIComponent(taskId)}/presentation-failure`,
      {
        method: 'POST',
        body: JSON.stringify({ reason }),
      }
    );
  }

  /**
   * Fetch a fresh 15-minute presigned URL for a task's image after the
   * prior URL expires; client-side annotation state is untouched
   * (dda-data-labeling Requirement 12.7).
   */
  async refreshTaskImageUrl(taskId: string): Promise<{
    image_url: string;
    image_url_expires_at?: number;
  }> {
    return this.request(
      `/labeler/tasks/${encodeURIComponent(taskId)}/image-url`
    );
  }

  /**
   * Fetch the paginated Admin_Review results for a Skip_Verification_Mode
   * job: every dataset image with its auto-labeled result or failed status
   * and current decision (dda-data-labeling Requirement 9.5).
   */
  async getReview(
    jobId: string,
    nextToken?: string
  ): Promise<ReviewResponse> {
    const query = nextToken
      ? `?next_token=${encodeURIComponent(nextToken)}`
      : '';
    return this.request(
      `/labeling/${encodeURIComponent(jobId)}/review${query}`
    );
  }

  /**
   * Batch-save accept/reject decisions for Admin_Review results; decisions
   * remain mutable until the review is finalized (dda-data-labeling
   * Requirement 9.6).
   */
  async saveReviewDecisions(
    jobId: string,
    decisions: Record<string, 'accepted' | 'rejected'>
  ): Promise<{ message: string }> {
    return this.request(
      `/labeling/${encodeURIComponent(jobId)}/review/decisions`,
      {
        method: 'POST',
        body: JSON.stringify({ decisions }),
      }
    );
  }

  /**
   * Finalize the Admin_Review, triggering manifest generation over exactly
   * the accepted results (dda-data-labeling Requirements 9.7–9.9). A 4xx
   * indicates undecided results or zero accepted results.
   */
  async finalizeReview(jobId: string): Promise<{
    job_id: string;
    status?: string;
    message?: string;
  }> {
    return this.request(
      `/labeling/${encodeURIComponent(jobId)}/review/finalize`,
      { method: 'POST' }
    );
  }

  async transformManifest(data: {
    usecase_id: string;
    source_manifest_uri: string;
    output_manifest_uri?: string;
    task_type?: 'classification' | 'segmentation';
  }): Promise<{
    message: string;
    transformed_manifest_uri: string;
    stats: {
      total_entries: number;
      transformed: number;
      skipped: number;
      errors: string[];
    };
    detected_attributes: {
      label_attr: string;
      metadata_attr: string;
    };
    dda_attributes: {
      label: string;
      metadata: string;
    };
    sample_entry: any;
  }> {
    return this.request('/training/transform-manifest', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async createTrainingJob(data: {
    usecase_id: string;
    model_source?: string;
    model_name: string;
    model_version: string;
    model_type: string;
    dataset_manifest_s3: string;
    instance_type: string;
    max_runtime_seconds?: number;
    hyperparameters?: Record<string, any>;
    auto_compile?: boolean;
    compilation_targets?: string[];
  }): Promise<{ training_job_id: string; message: string }> {
    return this.request<{ training_job_id: string; message: string }>('/training', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async getTrainingLogs(id: string, nextToken?: string): Promise<{ 
    training_id: string;
    training_job_name: string;
    logs: Array<{
      timestamp: number;
      message: string;
      ingestionTime?: number;
    }>;
    nextForwardToken?: string;
    nextBackwardToken?: string;
    message?: string;
  }> {
    const query = nextToken ? `?nextToken=${nextToken}` : '';
    return this.request(`/training/${id}/logs${query}`);
  }

  async downloadTrainingLogs(id: string): Promise<string> {
    const token = localStorage.getItem('idToken');
    const headers: Record<string, string> = {};
    
    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }

    const response = await fetch(`${this.baseUrl}/training/${id}/logs/download`, {
      headers,
    });

    if (!response.ok) {
      throw new Error(`Failed to download logs: ${response.statusText}`);
    }

    return response.text();
  }

  async stopTrainingJob(id: string): Promise<{ message: string }> {
    return this.request<{ message: string }>(`/training/${id}/stop`, {
      method: 'POST',
    });
  }

  // Compilation endpoints
  async startCompilation(trainingId: string, targets: string[]): Promise<{
    training_id: string;
    compilation_jobs: Array<{
      target: string;
      compilation_job_name: string;
      compilation_job_arn: string;
      status: string;
    }>;
    message: string;
  }> {
    return this.request(`/training/${trainingId}/compile`, {
      method: 'POST',
      body: JSON.stringify({ targets }),
    });
  }

  async getCompilationStatus(trainingId: string): Promise<{
    training_id: string;
    compilation_jobs: Array<{
      target: string;
      compilation_job_name: string;
      compilation_job_arn: string;
      status: string;
      compiled_model_s3?: string;
      failure_reason?: string;
      error?: string;
    }>;
  }> {
    return this.request(`/training/${trainingId}/compile`);
  }

  // Packaging endpoints
  async startPackaging(
    trainingId: string,
    targets?: string[],
    autoTriggered?: boolean,
    options?: { signal?: AbortSignal }
  ): Promise<{
    training_id: string;
    packaged_components: Array<{
      target: string;
      component_package_s3?: string;
      status: string;
      error?: string;
    }>;
    message: string;
    component_creation_triggered?: boolean;
  }> {
    return this.request(`/training/${trainingId}/package`, {
      method: 'POST',
      // auto_triggered chains packaging -> greengrass publish (component creation).
      body: JSON.stringify({ targets, auto_triggered: autoTriggered }),
      // Client-side abort/timeout support only; the request body is unchanged.
      signal: options?.signal,
    });
  }

  // Greengrass publish endpoints
  async publishGreengrassComponent(
    trainingId: string,
    componentName: string,
    componentVersion: string,
    friendlyName?: string,
    targets?: string[],
    options?: { signal?: AbortSignal }
  ): Promise<{
    training_id: string;
    component_name: string;
    component_version: string;
    published_components: Array<{
      target: string;
      platform: string;
      component_name: string;
      component_version: string;
      component_arn?: string;
      status: string;
      error?: string;
    }>;
    message: string;
  }> {
    return this.request(`/training/${trainingId}/publish`, {
      method: 'POST',
      body: JSON.stringify({
        component_name: componentName,
        component_version: componentVersion,
        friendly_name: friendlyName,
        targets,
      }),
      // Client-side abort/timeout support only; the request body is unchanged.
      signal: options?.signal,
    });
  }

  // Pre-labeled datasets endpoints
  async listPreLabeledDatasets(usecaseId: string): Promise<{
    datasets: Array<{
      dataset_id: string;
      usecase_id: string;
      name: string;
      description?: string;
      manifest_s3_uri: string;
      image_count: number;
      label_attribute: string;
      label_stats: Record<string, number>;
      task_type: string;
      created_at: number;
      created_by: string;
      updated_at: number;
    }>;
    count: number;
  }> {
    const queryParams = new URLSearchParams({ usecase_id: usecaseId });
    return this.request(`/datasets/pre-labeled?${queryParams}`);
  }

  async getPreLabeledDataset(datasetId: string): Promise<{
    dataset: {
      dataset_id: string;
      usecase_id: string;
      name: string;
      description?: string;
      manifest_s3_uri: string;
      image_count: number;
      label_attribute: string;
      label_stats: Record<string, number>;
      task_type: string;
      created_at: number;
      created_by: string;
      updated_at: number;
    };
  }> {
    return this.request(`/datasets/pre-labeled/${datasetId}`);
  }

  async createPreLabeledDataset(data: {
    usecase_id: string;
    name: string;
    description?: string;
    manifest_s3_uri: string;
    task_type: string;
    label_attribute: string;
    image_count: number;
    label_stats: Record<string, number>;
    created_by: string;
  }): Promise<{
    dataset_id: string;
    message: string;
  }> {
    return this.request('/datasets/pre-labeled', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async deletePreLabeledDataset(datasetId: string): Promise<{ message: string }> {
    return this.request<{ message: string }>(`/datasets/pre-labeled/${datasetId}`, {
      method: 'DELETE',
    });
  }

  async validateManifest(data: {
    usecase_id: string;
    manifest_s3_uri: string;
  }): Promise<{
    valid: boolean;
    errors: string[];
    warnings: string[];
    stats: {
      total_images: number;
      task_type: string;
      label_distribution: Record<string, number>;
      sample_entries: any[];
    };
  }> {
    return this.request('/datasets/validate-manifest', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async browseS3Bucket(usecaseId: string, prefix: string = ''): Promise<{
    bucket: string;
    current_prefix: string;
    breadcrumbs: Array<{ name: string; prefix: string }>;
    folders: Array<{
      name: string;
      prefix: string;
      type: 'folder';
    }>;
    files: Array<{
      name: string;
      key: string;
      size: number;
      size_mb: number;
      last_modified: string;
      type: 'file' | 'manifest' | 'image';
      s3_uri: string;
    }>;
    folder_count: number;
    file_count: number;
  }> {
    const queryParams = new URLSearchParams({
      usecase_id: usecaseId,
      prefix: prefix,
    });
    return this.request(`/datasets/pre-labeled/browse?${queryParams}`);
  }

  // Component endpoints
  async listComponents(params: {
    usecase_id: string;
    scope?: 'PRIVATE' | 'PUBLIC';
    search?: string;
    sort_by?: 'component_name' | 'creation_timestamp';
    sort_order?: 'asc' | 'desc';
  }): Promise<{
    components: Array<{
      arn: string;
      component_name: string;
      latest_version: {
        arn: string;
        componentName: string;
        componentVersion: string;
        creationTimestamp: string;
        description?: string;
        status: string;
        platforms: Array<{
          name?: string;
          attributes?: Record<string, string>;
        }>;
      };
      description: string;
      publisher: string;
      creation_timestamp: string;
      status: string;
      platforms: Array<{
        name?: string;
        attributes?: Record<string, string>;
      }>;
      tags: Record<string, string>;
      component_type: string;
      deployment_info: {
        total_deployments: number;
        active_deployments: number;
        deployed_devices: string[];
        device_count: number;
      };
    }>;
    total_count: number;
  }> {
    const queryParams = new URLSearchParams({
      usecase_id: params.usecase_id,
      ...(params.scope && { scope: params.scope }),
      ...(params.search && { search: params.search }),
      ...(params.sort_by && { sort_by: params.sort_by }),
      ...(params.sort_order && { sort_order: params.sort_order }),
    });
    return this.request(`/components?${queryParams}`);
  }

  async getComponent(arn: string, usecaseId: string): Promise<{
    arn: string;
    component_name: string;
    description: string;
    publisher: string;
    creation_timestamp: string;
    status: string;
    platforms: Array<{
      name?: string;
      attributes?: Record<string, string>;
    }>;
    tags: Record<string, string>;
    component_type: string;
    versions: Array<{
      arn: string;
      componentName: string;
      componentVersion: string;
      creationTimestamp: string;
      description?: string;
      status: string;
      platforms: Array<{
        name?: string;
        attributes?: Record<string, string>;
      }>;
    }>;
    deployment_info: {
      total_deployments: number;
      active_deployments: number;
      deployed_devices: string[];
      device_count: number;
    };
    recipe: {
      RecipeFormatVersion: string;
      ComponentName: string;
      ComponentVersion: string;
      ComponentType: string;
      ComponentPublisher?: string;
      ComponentConfiguration?: {
        DefaultConfiguration?: Record<string, any>;
      };
      ComponentDependencies?: Record<string, {
        VersionRequirement: string;
        DependencyType: string;
      }>;
      Manifests?: Array<{
        Platform: {
          name?: string;
          attributes?: Record<string, string>;
        };
        Lifecycle?: Record<string, {
          Script?: string;
          Timeout?: number;
          requiresPrivilege?: boolean;
          runWith?: {
            posixUser?: string;
            windowsUser?: string;
          };
        }>;
        Artifacts?: Array<{
          Uri: string;
          Digest?: string;
          Algorithm?: string;
          Unarchive?: string;
          Permission?: {
            Read?: string;
            Execute?: string;
          };
        }>;
      }>;
      Lifecycle?: Record<string, any>;
    };
  }> {
    const queryParams = new URLSearchParams({ usecase_id: usecaseId });
    return this.request(`/components/${encodeURIComponent(arn)}?${queryParams}`);
  }

  async deleteComponent(arn: string, usecaseId: string): Promise<{ message: string }> {
    const queryParams = new URLSearchParams({ usecase_id: usecaseId });
    return this.request<{ message: string }>(`/components/${encodeURIComponent(arn)}?${queryParams}`, {
      method: 'DELETE',
    });
  }

  // Deployment endpoints
  async listDeployments(usecaseId: string): Promise<{
    deployments: Array<{
      deployment_id: string;
      deployment_name: string;
      target_arn: string;
      revision_id: string;
      deployment_status: string;
      is_latest_for_target: boolean;
      creation_timestamp: string;
      usecase_id: string;
    }>;
    count: number;
  }> {
    return this.request(`/deployments?usecase_id=${usecaseId}`);
  }

  async getDeployment(deploymentId: string, usecaseId: string): Promise<{
    deployment: {
      deployment_id: string;
      deployment_name: string;
      target_arn: string;
      revision_id: string;
      deployment_status: string;
      iot_job_id: string;
      iot_job_arn: string;
      is_latest_for_target: boolean;
      creation_timestamp: string;
      components: Array<{
        component_name: string;
        component_version: string;
        configuration_update: Record<string, unknown>;
      }>;
      deployment_policies: Record<string, unknown>;
      tags: Record<string, string>;
      usecase_id: string;
    };
  }> {
    return this.request(`/deployments/${deploymentId}?usecase_id=${usecaseId}`);
  }

  async getTargetDeployment(params: {
    usecase_id: string;
    target_device?: string;
    target_thing_group?: string;
  }): Promise<{
    existing_deployment: null | {
      deployment_id: string;
      deployment_name: string;
      target_arn: string;
      deployment_status: string;
      revision_id: string;
      creation_timestamp: string;
      components: Array<{
        component_name: string;
        component_version: string;
      }>;
    };
    group_member_conflicts?: Array<{
      device: string;
      deployment_id: string;
      deployment_name: string;
      deployment_status: string;
    }>;
  }> {
    const qs = new URLSearchParams({ usecase_id: params.usecase_id });
    if (params.target_device) qs.set('target_device', params.target_device);
    if (params.target_thing_group) qs.set('target_thing_group', params.target_thing_group);
    return this.request(`/deployments?${qs.toString()}`);
  }

  async createDeployment(data: {
    usecase_id: string;
    deployment_name?: string;
    components: Array<{
      component_name: string;
      component_version: string;
    }>;
    target_devices?: string[];
    target_thing_group?: string;
    rollout_config?: {
      auto_rollback?: boolean;
      timeout_seconds?: number;
    };
  }): Promise<{
    deployment_id: string;
    iot_job_id: string;
    iot_job_arn: string;
    message: string;
    components?: Array<{
      component_name: string;
      component_version: string;
    }>;
    auto_included?: Array<{
      component_name: string;
      component_version: string;
      reason: string;
    }>;
    is_revision?: boolean;
    superseded_deployment_id?: string | null;
  }> {
    return this.request('/deployments', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async cancelDeployment(deploymentId: string, usecaseId: string): Promise<{
    message: string;
    deployment_id: string;
  }> {
    return this.request(`/deployments/${deploymentId}?usecase_id=${usecaseId}`, {
      method: 'DELETE',
    });
  }

  /**
   * Deploy-time Camera_Binding context for the CreateDeployment binding
   * matrix (camera-registry-sync Requirements 8.1, 8.5, 8.9): for each
   * Camera_Input_Node of the workflow version and each target device,
   * the device's registered Camera_Sources as binding options with
   * hint-matching pre-selection. `binding_required: false` skips the
   * matrix step entirely.
   */
  async getCameraBindingContext(params: {
    usecase_id: string;
    workflow_id: string;
    workflow_version?: number;
    target_devices?: string[];
    target_thing_group?: string;
  }): Promise<CameraBindingContext> {
    const qs = new URLSearchParams({
      view: 'binding-context',
      usecase_id: params.usecase_id,
      workflow_id: params.workflow_id,
    });
    if (params.workflow_version !== undefined) {
      qs.set('workflow_version', String(params.workflow_version));
    }
    if (params.target_devices && params.target_devices.length > 0) {
      qs.set('target_devices', params.target_devices.join(','));
    }
    if (params.target_thing_group) {
      qs.set('target_thing_group', params.target_thing_group);
    }
    return this.request(`/deployments?${qs.toString()}`);
  }

  /**
   * Deploy a packaged Workflow_Component (component_type: workflow) with
   * optional deploy-time Camera_Bindings and confirmed warning ids
   * (camera-registry-sync Requirements 8.2, 8.5, 9.3). Rejections carry
   * structured codes: 409 CAMERA_BINDINGS_INVALID {errors, warnings},
   * 409 CAMERA_WARNINGS_UNCONFIRMED {warnings}, 503 REGISTRY_UNAVAILABLE,
   * and 502 BINDING_DELIVERY_FAILED.
   */
  async createWorkflowDeployment(data: {
    usecase_id: string;
    workflow_id: string;
    workflow_version?: number;
    target_devices?: string[];
    target_thing_group?: string;
    deployment_name?: string;
    rollout_config?: {
      auto_rollback?: boolean;
      timeout_seconds?: number;
    };
    camera_bindings?: Record<
      string,
      Record<string, { cameraSourceId: string } | { override: Record<string, unknown> }>
    >;
    confirmed_warnings?: string[];
  }): Promise<{
    deployment_id: string;
    iot_job_id: string;
    iot_job_arn: string;
    workflow_id: string;
    workflow_version: number;
    component_name: string;
    component_version: string;
    target_arn: string;
    target_devices: string[];
    target_thing_group?: string | null;
    is_revision: boolean;
    superseded_deployment_id?: string | null;
    camera_bindings_delivered: boolean;
    message: string;
  }> {
    return this.request('/deployments', {
      method: 'POST',
      body: JSON.stringify({ component_type: 'workflow', ...data }),
    });
  }

  async createDeploymentFromComponent(data: {
    usecase_id: string;
    component_arn: string;
    component_version: string;
    target_devices?: string[];
    target_groups?: string[];
    rollout_strategy: 'all-at-once' | 'canary' | 'percentage';
    rollout_config?: {
      canarySize?: number;
      canaryPercentage?: number;
      failureThreshold?: number;
    };
  }): Promise<{
    deployment_id: string;
    message: string;
  }> {
    return this.request('/deployments', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  // Data Management endpoints
  async listDataBuckets(usecaseId: string): Promise<{
    buckets: Array<{
      name: string;
      creation_date?: string;
      region: string;
      tags?: Record<string, string>;
      is_configured?: boolean;
    }>;
    current_data_bucket: string | null;
    target_account?: string;
    has_data_account_role?: boolean;
    message?: string;
  }> {
    return this.request(`/usecases/${usecaseId}/data/buckets`);
  }

  async createDataBucket(usecaseId: string, data: {
    bucket_name: string;
    region?: string;
    enable_versioning?: boolean;
    encryption?: string;
  }): Promise<{
    bucket_name: string;
    region: string;
    arn: string;
    created: boolean;
    versioning_enabled: boolean;
    encryption: string;
  }> {
    return this.request(`/usecases/${usecaseId}/data/buckets`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async listDataFolders(usecaseId: string, params: {
    bucket?: string;
    prefix?: string;
  }): Promise<{
    bucket: string;
    prefix: string;
    folders: Array<{ name: string; path: string }>;
    files: Array<{
      name: string;
      key: string;
      size: number;
      last_modified: string;
    }>;
    is_truncated: boolean;
  }> {
    const queryParams = new URLSearchParams();
    if (params.bucket) queryParams.set('bucket', params.bucket);
    if (params.prefix) queryParams.set('prefix', params.prefix);
    const query = queryParams.toString() ? `?${queryParams}` : '';
    return this.request(`/usecases/${usecaseId}/data/folders${query}`);
  }

  async createDataFolder(usecaseId: string, data: {
    bucket?: string;
    folder_path: string;
  }): Promise<{
    bucket: string;
    folder_path: string;
    created: boolean;
  }> {
    return this.request(`/usecases/${usecaseId}/data/folders`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async getUploadUrl(usecaseId: string, data: {
    bucket?: string;
    key: string;
    content_type?: string;
    expires_in?: number;
  }): Promise<{
    upload_url: string;
    bucket: string;
    key: string;
    expires_in: number;
  }> {
    return this.request(`/usecases/${usecaseId}/data/upload-url`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async getBatchUploadUrls(usecaseId: string, data: {
    bucket?: string;
    prefix?: string;
    files: Array<{ filename: string; content_type?: string }>;
    expires_in?: number;
  }): Promise<{
    bucket: string;
    prefix: string;
    uploads: Array<{
      filename: string;
      key: string;
      upload_url: string;
      content_type: string;
      error?: string;
    }>;
    expires_in: number;
  }> {
    return this.request(`/usecases/${usecaseId}/data/batch-upload-urls`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async configureDataAccount(usecaseId: string, data: {
    data_account_id?: string;
    data_account_role_arn?: string;
    data_account_external_id?: string;
    data_s3_bucket?: string;
    data_s3_prefix?: string;
  }): Promise<{
    message: string;
    usecase_id: string;
  }> {
    return this.request(`/usecases/${usecaseId}/data/configure`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  // User Roles / Team Management endpoints
  async listUsecaseUsers(usecaseId: string): Promise<{
    users: Array<{
      user_id: string;
      roles: Array<{
        usecase_id: string;
        role: string;
        assigned_at?: number;
        assigned_by?: string;
      }>;
    }>;
    total_count: number;
  }> {
    return this.request(`/users?usecase_id=${usecaseId}`);
  }

  async assignUserRole(data: {
    user_id: string;
    usecase_id: string;
    role: string;
  }): Promise<{
    message: string;
    user_id: string;
    usecase_id: string;
    role: string;
  }> {
    return this.request('/users/assign-role', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async removeUserRole(userId: string, usecaseId: string): Promise<{
    message: string;
    user_id: string;
    usecase_id: string;
  }> {
    return this.request(`/users/${userId}/roles/${usecaseId}`, {
      method: 'DELETE',
    });
  }

  // Model Import (BYOM) endpoints
  async getModelFormatSpec(): Promise<{
    description: string;
    format: string;
    framework: string;
    required_structure: Record<string, {
      description: string;
      required_fields?: Record<string, string>;
      example?: string;
      notes?: string;
    }>;
    validation_rules: string[];
    supported_compilation_targets: string[];
  }> {
    return this.request('/models/format-spec');
  }

  // Model Registry endpoints
  async listModels(params: {
    usecase_id: string;
    stage?: 'candidate' | 'staging' | 'production';
    source?: 'trained' | 'imported' | 'marketplace';
  }): Promise<{
    models: Array<{
      model_id: string;
      usecase_id: string;
      name: string;
      version: string;
      stage: 'candidate' | 'staging' | 'production';
      source: string;
      training_job_id: string;
      model_type: string;
      metrics: Record<string, number>;
      artifact_s3?: string;
      component_arns: Record<string, string>;
      deployed_devices: string[];
      created_by: string;
      created_at: number;
      updated_at: number;
      description?: string;
      compilation_status?: string;
      packaging_status?: string;
    }>;
    count: number;
    usecase_id: string;
  }> {
    const queryParams = new URLSearchParams({
      usecase_id: params.usecase_id,
      ...(params.stage && { stage: params.stage }),
      ...(params.source && { source: params.source }),
    });
    return this.request(`/models?${queryParams}`);
  }

  async getModel(modelId: string): Promise<{
    model: {
      model_id: string;
      usecase_id: string;
      name: string;
      version: string;
      stage: 'candidate' | 'staging' | 'production';
      source: string;
      training_job_id: string;
      training_job_name?: string;
      model_type: string;
      description?: string;
      metrics: Record<string, number>;
      artifact_s3?: string;
      component_arns: Record<string, string>;
      deployed_devices: string[];
      created_by: string;
      created_at: number;
      updated_at: number;
      completed_at?: number;
      promoted_at?: number;
      promoted_by?: string;
      compilation_status?: string;
      compilation_jobs?: Array<{
        target: string;
        status: string;
        compiled_model_s3?: string;
      }>;
      packaging_status?: string;
      packaged_components?: Array<{
        target: string;
        status: string;
        component_package_s3?: string;
        supported_architectures?: string[];
      }>;
      published_component?: VllmPublishedComponent;
      // Stored Engine_Configuration, present for vLLM records only
      // (models.py get_model, Requirement 1.2).
      engine_configuration?: VllmEngineConfiguration | null;
      validation_result?: Record<string, unknown>;
      hyperparameters?: Record<string, unknown>;
      instance_type?: string;
      dataset_manifest_s3?: string;
    };
  }> {
    return this.request(`/models/${modelId}`);
  }

  async updateModelStage(modelId: string, stage: 'candidate' | 'staging' | 'production'): Promise<{
    model_id: string;
    previous_stage: string;
    stage: string;
    message: string;
  }> {
    return this.request(`/models/${modelId}/stage`, {
      method: 'PUT',
      body: JSON.stringify({ stage }),
    });
  }

  async deleteModel(modelId: string): Promise<{
    model_id: string;
    message: string;
  }> {
    return this.request(`/models/${modelId}`, {
      method: 'DELETE',
    });
  }

  async validateModel(data: {
    usecase_id: string;
    model_s3_uri: string;
  }): Promise<{
    valid: boolean;
    model_s3_uri?: string;
    metadata?: {
      image_width: number;
      image_height: number;
      input_shape: number[];
      model_type: string;
      pt_file: string;
      framework: string;
      framework_version: string;
    };
    files_found?: string[];
    warnings?: string[];
    error?: string;
    details?: string[];
  }> {
    return this.request('/models/validate', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async importModel(data: {
    usecase_id: string;
    model_name: string;
    model_version: string;
    model_s3_uri: string;
    description?: string;
    auto_compile?: boolean;
    compilation_targets?: string[];
  }): Promise<{
    training_id: string;
    model_name: string;
    model_version: string;
    status: string;
    source: string;
    validation_result: {
      valid: boolean;
      metadata: {
        image_width: number;
        image_height: number;
        input_shape: number[];
        model_type: string;
        pt_file: string;
        framework: string;
        framework_version: string;
      };
      files_found: string[];
      warnings: string[];
    };
    message: string;
    auto_compile_triggered: boolean;
  }> {
    return this.request('/models/import', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  // vLLM model registration endpoints (vllm-triton-inference)

  /**
   * Register a vLLM_Model_Record (Requirements 1.1, 1.2). Exactly one of
   * `huggingface_model_id` / `s3_model_artifact` must be supplied (the API
   * enforces the XOR); a 400 rejection carries the complete finding list
   * as `details.findings` on the thrown ApiError.
   */
  async registerVllmModel(data: {
    usecase_id: string;
    model_name: string;
    model_version: string;
    huggingface_model_id?: string;
    s3_model_artifact?: string;
    engine_configuration?: Record<string, string | number | boolean>;
    description?: string;
  }): Promise<{
    training_id: string;
    publish_eligible: boolean;
    labeling_steps: number;
    training_steps: number;
  }> {
    return this.request('/models/vllm', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  /** Documented vLLM engine settings, defaults, and accepted ranges. */
  async getVllmEngineSpec(): Promise<VllmEngineSpec> {
    return this.request('/models/vllm/engine-spec');
  }

  /**
   * Update the stored Engine_Configuration of a registered vLLM model
   * (vllm-sizing-and-packaging-errors, Requirements 2.1–2.4). Supplied
   * settings are validated against the registration rules and overlaid
   * onto the stored configuration; a 400 rejection carries the per-field
   * finding list as `details.findings` on the thrown ApiError. The change
   * takes effect only after the model is packaged and published again.
   */
  async updateVllmEngineConfiguration(
    trainingId: string,
    engineConfiguration: VllmEngineConfiguration
  ): Promise<VllmEngineConfigurationUpdateResponse> {
    return this.request(`/models/vllm/${trainingId}/engine-configuration`, {
      method: 'PUT',
      body: JSON.stringify({ engine_configuration: engineConfiguration }),
    });
  }

  // Model Converter endpoints (Smart Import)
  async getSupportedModelTypes(): Promise<{
    model_types: Record<string, {
      description: string;
      output_format: string;
    }>;
    common_dimensions: Record<string, number[]>;
    supported_frameworks: string[];
    framework_versions: string[];
  }> {
    return this.request('/models/types');
  }

  async inspectModel(data: {
    usecase_id: string;
    model_s3_uri: string;
  }): Promise<{
    model_s3_uri: string;
    inspection_result: {
      type: string;
      is_state_dict?: boolean;
      is_jit?: boolean;
      is_full_model?: boolean;
      layers?: string[];
      total_layers?: number;
      input_channels?: number;
      num_classes?: number;
      architecture_hints: string[];
      suggested_type?: string;
      error?: string;
      // ONNX auto-detected attributes (from graph input/output shapes).
      detection_arch?: string;
      input_width?: number | null;
      input_height?: number | null;
      num_outputs?: number;
      input_shapes?: (number | null)[][];
      output_shapes?: (number | null)[][];
    };
    supported_model_types: Record<string, {
      description: string;
      output_format: string;
    }>;
  }> {
    return this.request('/models/inspect', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async convertModel(data: {
    usecase_id: string;
    model_s3_uri: string;
    model_name: string;
    model_type: string;
    image_width: number;
    image_height: number;
    num_classes?: number;
    class_names?: string[];
    auto_import?: boolean;
    // 'pytorch' (legacy .pt/DLR) or 'onnx' (pluggable ONNX Runtime engine).
    export_format?: string;
    // Object-detection decode thresholds (only used for object_detection).
    score_threshold?: number;
    iou_threshold?: number;
    // Object-detection decoder family: 'yolo' (single tensor + NMS) or
    // 'rf_detr' (DETR-family, two tensors, NMS-free top-k). Only used when
    // model_type === 'object_detection'.
    detection_arch?: string;
  }): Promise<{
    converted_model_s3_uri: string;
    model_name: string;
    model_type: string;
    input_shape: number[];
    model_info: {
      type: string;
      architecture_hints: string[];
      suggested_type?: string;
    };
    message: string;
    import_result?: {
      training_id: string;
      message: string;
    };
    training_id?: string;
    import_error?: string;
  }> {
    return this.request('/models/convert', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  // Audit Logs endpoints
  async getAuditLogs(params?: {
    usecase_id?: string;
    action?: string;
    user_id?: string;
    start_time?: number;
    end_time?: number;
    limit?: number;
    next_token?: string;
  }): Promise<{
    logs: Array<{
      event_id: string;
      timestamp: number;
      user_id: string;
      usecase_id?: string;
      action: string;
      resource_type: string;
      resource_id: string;
      result: string;
      details?: Record<string, any>;
    }>;
    count: number;
    scanned_count: number;
    next_token?: string;
    available_actions: string[];
    is_admin?: boolean;
  }> {
    const queryParams = new URLSearchParams();
    if (params?.usecase_id) queryParams.set('usecase_id', params.usecase_id);
    if (params?.action) queryParams.set('action', params.action);
    if (params?.user_id) queryParams.set('user_id', params.user_id);
    if (params?.start_time) queryParams.set('start_time', params.start_time.toString());
    if (params?.end_time) queryParams.set('end_time', params.end_time.toString());
    if (params?.limit) queryParams.set('limit', params.limit.toString());
    if (params?.next_token) queryParams.set('next_token', params.next_token);
    
    const query = queryParams.toString() ? `?${queryParams}` : '';
    return this.request(`/audit-logs${query}`);
  }

  // Data Accounts endpoints
  async listDataAccounts(): Promise<{
    data_accounts: Array<{
      data_account_id: string;
      name: string;
      description?: string;
      role_arn: string;
      external_id: string;
      region: string;
      status: string;
      created_at: number;
      created_by: string;
      updated_at: number;
      connection_test?: {
        status: string;
        message: string;
      };
      last_tested_at?: number;
    }>;
    count: number;
  }> {
    return this.request('/data-accounts');
  }

  async getDataAccount(accountId: string): Promise<{
    data_account: {
      data_account_id: string;
      name: string;
      description?: string;
      role_arn: string;
      external_id: string;
      region: string;
      status: string;
      created_at: number;
      created_by: string;
      updated_at: number;
      connection_test?: {
        status: string;
        message: string;
      };
      last_tested_at?: number;
    };
  }> {
    return this.request(`/data-accounts/${accountId}`);
  }

  async createDataAccount(data: {
    data_account_id: string;
    name: string;
    description?: string;
    role_arn: string;
    external_id: string;
    region: string;
  }): Promise<{
    data_account_id: string;
    message: string;
  }> {
    return this.request('/data-accounts', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  async updateDataAccount(accountId: string, data: {
    name?: string;
    description?: string;
    role_arn?: string;
    external_id?: string;
    region?: string;
  }): Promise<{
    data_account_id: string;
    message: string;
  }> {
    return this.request(`/data-accounts/${accountId}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    });
  }

  async deleteDataAccount(accountId: string): Promise<{
    message: string;
  }> {
    return this.request(`/data-accounts/${accountId}`, {
      method: 'DELETE',
    });
  }

  async testDataAccountConnection(accountId: string): Promise<{
    result: {
      status: string;
      message: string;
      error?: string;
    };
  }> {
    return this.request(`/data-accounts/${accountId}/test`, {
      method: 'POST',
    });
  }

  // Bedrock_Configuration endpoints (workflow-manager Requirement 10.6).
  // No dedicated settings route exists in API Gateway, so the configuration
  // rides the PortalAdmin-only /data-accounts/{id} routes with the reserved
  // id 'bedrock-configuration' (handled by the data_accounts Lambda).
  async getBedrockConfiguration(): Promise<{
    bedrock_configuration: {
      model_id: string;
      region: string;
      max_tokens: number;
      // null means "unset": the sampling parameter is omitted at invocation.
      temperature: number | null;
      top_p: number | null;
      timeout_seconds: number;
    };
    defaults: Record<string, string | number | null>;
    max_timeout_seconds: number;
  }> {
    return this.request('/data-accounts/bedrock-configuration');
  }

  // Invokable model options (inference profiles + on-demand foundation
  // models) for the settings-page model dropdown. An empty list with a
  // 'permissions' hint means the backend lacks the bedrock list
  // permissions and the UI should fall back to free-text entry.
  async getBedrockModels(): Promise<{
    models: { id: string; label: string }[];
    region: string;
    permissions?: string;
  }> {
    return this.request('/data-accounts/bedrock-configuration/models');
  }

  async updateBedrockConfiguration(config: {
    model_id?: string;
    region?: string;
    max_tokens?: number;
    // An explicit null unsets the sampling parameter (the backend merges
    // provided keys, so omitting the key keeps the current value).
    temperature?: number | null;
    top_p?: number | null;
    timeout_seconds?: number;
  }): Promise<{
    message: string;
    bedrock_configuration: {
      model_id: string;
      region: string;
      max_tokens: number;
      temperature: number | null;
      top_p: number | null;
      timeout_seconds: number;
    };
  }> {
    return this.request('/data-accounts/bedrock-configuration', {
      method: 'PUT',
      body: JSON.stringify(config),
    });
  }

  // Build infrastructure configuration endpoints (build_config.py,
  // portal-build-fleet-and-workflow-gates Requirement 9). GET returns the
  // effective configuration with documented defaults applied per field
  // (builds:read); PUT is PortalAdmin-only and applies a partial update
  // atomically — an invalid update is rejected in full with code
  // CONFIG_INVALID and per-parameter errors in details.errors
  // (Requirements 9.1, 9.5).
  async getBuildConfig(): Promise<{ config: BuildInfrastructureConfig }> {
    return this.request('/build-config');
  }

  async updateBuildConfig(config: BuildInfrastructureConfigUpdate): Promise<{
    config: BuildInfrastructureConfig;
    changes: BuildConfigChange[];
  }> {
    return this.request('/build-config', {
      method: 'PUT',
      body: JSON.stringify({ config }),
    });
  }

  // Component Configuration endpoints
  async getComponentConfigurationSchema(componentName: string): Promise<{
    component_name: string;
    displayName: string;
    description: string;
    parameters: Record<string, {
      name: string;
      type: 'string' | 'number' | 'boolean' | 'select';
      default: any;
      description: string;
      required: boolean;
      validation?: { min?: number; max?: number };
      options?: Array<{ label: string; value: any }>;
      envVar?: string;
    }>;
  }> {
    const queryParams = new URLSearchParams({ component_name: componentName });
    return this.request(`/components/schema?${queryParams}`);
  }

  async configureComponent(data: {
    component_name: string;
    usecase_id: string;
    configuration: Record<string, any>;
    target_devices: string[];
    deployment_name?: string;
  }): Promise<{
    status: string;
    deployment_id: string;
    component_name: string;
    configuration: Record<string, any>;
    environment_variables: Record<string, string>;
    target_devices: string[];
    message: string;
  }> {
    return this.request('/components/configure', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  // Workflow Manager endpoints
  // Node type catalog for the Workflow_Builder Node_Palette (camelCase
  // wire form of workflow_core.catalog, Requirement 2.8).
  async getWorkflowNodeCatalog(
    usecaseId?: string
  ): Promise<{ nodeTypes: NodeTypeDescriptor[] }> {
    // With usecase_id the Use_Case's registered Custom_Node_Types are
    // merged in (test/prod backed only); without it the endpoint serves
    // the built-in catalog unchanged.
    const query = usecaseId
      ? `?usecase_id=${encodeURIComponent(usecaseId)}`
      : '';
    return this.request(`/workflows/node-catalog${query}`);
  }

  // Workflow_Store API (workflows.py): CRUD, versioning, duplication
  // (Requirements 5.1, 5.2, 5.4, 5.5, 5.7).
  async listWorkflows(usecaseId?: string): Promise<{ workflows: WorkflowSummary[]; count: number }> {
    const query = usecaseId ? `?usecase_id=${encodeURIComponent(usecaseId)}` : '';
    return this.request(`/workflows${query}`);
  }

  // Create a workflow as version 1 (Requirement 5.1).
  async createWorkflow(data: {
    usecase_id: string;
    name: string;
    definition: WorkflowDefinition;
    description?: string;
  }): Promise<{ workflow: WorkflowSummary; version: number }> {
    return this.request('/workflows', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  // Open/load a stored definition, latest version by default (Requirement 5.4).
  async getWorkflow(
    workflowId: string,
    version?: number
  ): Promise<{
    workflow: WorkflowSummary;
    version: number;
    validation_status?: WorkflowValidationStatus;
    definition: WorkflowDefinition;
  }> {
    const query = version !== undefined ? `?version=${version}` : '';
    return this.request(`/workflows/${encodeURIComponent(workflowId)}${query}`);
  }

  // Save changes as a new version; prior versions are retained (Requirement 5.2).
  async updateWorkflow(
    workflowId: string,
    data: { definition: WorkflowDefinition; name?: string; description?: string }
  ): Promise<{ workflow: WorkflowSummary; version: number }> {
    return this.request(`/workflows/${encodeURIComponent(workflowId)}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    });
  }

  // Metadata-only rename of a workflow's display name (workflows.py
  // rename_workflow, workflow-manager-gaps Requirements 5.1, 5.2, 5.7).
  // No definition is sent and no new version is allocated: only `name`
  // and `updated_at` change on the workflow record. Rejections raise
  // ApiError with the structured envelope: 400 INVALID_NAME
  // (empty/whitespace-only or > 128 characters), the existing 403 RBAC
  // envelope, or the uniform 404 (Requirements 5.3-5.5).
  async renameWorkflow(
    workflowId: string,
    name: string
  ): Promise<{ workflow: WorkflowSummary }> {
    return this.request(`/workflows/${encodeURIComponent(workflowId)}/name`, {
      method: 'PATCH',
      body: JSON.stringify({ name }),
    });
  }

  // Delete a workflow and its versions; rejected with 409 and the
  // referencing deployment ids when active deployments exist (5.5, 5.6).
  async deleteWorkflow(workflowId: string): Promise<{ workflow_id: string; message: string }> {
    return this.request(`/workflows/${encodeURIComponent(workflowId)}`, {
      method: 'DELETE',
    });
  }

  // Duplicate under a new name (Requirement 5.7).
  async duplicateWorkflow(
    workflowId: string,
    data: { name?: string; description?: string } = {}
  ): Promise<{ workflow: WorkflowSummary; version: number }> {
    return this.request(`/workflows/${encodeURIComponent(workflowId)}/duplicate`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  // Version history, newest first (Requirement 5.2).
  async listWorkflowVersions(workflowId: string): Promise<{
    workflow_id: string;
    latest_version: number;
    versions: Array<{
      version: number;
      created_at?: number;
      created_by?: string;
      validation_status?: WorkflowValidationStatus;
      component_arn?: string | null;
    }>;
    count: number;
  }> {
    return this.request(`/workflows/${encodeURIComponent(workflowId)}/versions`);
  }

  // Run all backend Workflow_Validator checks on a stored version and
  // return the complete findings list (Requirements 4.8, 4.9).
  async validateWorkflow(workflowId: string, version?: number): Promise<WorkflowValidationRun> {
    return this.request(`/workflows/${encodeURIComponent(workflowId)}/validate`, {
      method: 'POST',
      body: JSON.stringify(version !== undefined ? { version } : {}),
    });
  }

  // Compile, assemble, upload, and register a Workflow_Component
  // (dda.workflow.{id}) for the selected target architectures so the
  // workflow becomes deployable from the Create Deployment screen
  // (workflow_packaging.py, POST /workflows/{id}/package,
  // Requirements 7.1-7.5, 11.5, 13.3). `version` defaults to the
  // workflow's latest version. Gate rejections (unsupported/LLM arch,
  // plugin lifecycle/arch, packaging failure) raise ApiError with the
  // structured envelope.
  async packageWorkflow(
    workflowId: string,
    data: { architectures: string[]; version?: number }
  ): Promise<{
    workflow_id: string;
    version: number;
    component_name: string;
    component_version: string;
    component_arn: string;
    architectures: string[];
    artifacts: Record<string, string>;
  }> {
    return this.request(`/workflows/${encodeURIComponent(workflowId)}/package`, {
      method: 'POST',
      body: JSON.stringify(
        data.version !== undefined
          ? { architectures: data.architectures, version: data.version }
          : { architectures: data.architectures }
      ),
    });
  }

  // Prompt-based workflow generation via the configured Bedrock model,
  // asynchronous submit/poll transport (workflow_generator.py
  // submit_generation, workflow-manager-gaps Requirements 1.1, 1.7, 1.8).
  // Accepted submissions return 202 with the Generation_Job id to poll
  // via getWorkflowGenerationJob; the generation itself (Bedrock
  // invocation, Generation_Gate, session persistence) runs in a
  // background worker. `session_id` continues an existing chat session;
  // `current_definition` is the canvas snapshot so follow-up prompts
  // modify rather than regenerate. Synchronous rejections (missing
  // fields, INVALID_TEMPERATURE, RBAC, USECASE_NOT_FOUND,
  // GENERATION_NOT_STARTED) raise ApiError with the structured envelope
  // and create no job (Requirements 1.3, 1.4).
  // `temperature` (0..1) overrides the configured model temperature for
  // this invocation only; omitted = use the configured value.
  async generateWorkflow(data: {
    usecase_id: string;
    prompt: string;
    session_id?: string;
    current_definition?: WorkflowDefinition;
    temperature?: number;
  }): Promise<WorkflowGenerationSubmission> {
    return this.request('/workflows/generate', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  // Poll one Generation_Job (workflow_generator.get_generation_job,
  // workflow-manager-gaps Requirements 2.1, 2.2, 2.8). Resolves with the
  // in-progress state ({job_id, status}) or, on success, the job identity
  // plus the synchronous endpoint's exact WorkflowGenerationResult
  // payload. A failed job rejects with an ApiError replaying the
  // originating Error_Envelope and HTTP status verbatim (Requirement
  // 2.3); an unknown, removed, or inaccessible job id rejects with the
  // uniform 404 JOB_NOT_FOUND envelope (Requirements 2.4, 2.10).
  async getWorkflowGenerationJob(jobId: string): Promise<WorkflowGenerationJobStatus> {
    return this.request(`/workflows/generate/${encodeURIComponent(jobId)}`);
  }

  // Code_Assistant endpoint (custom-node-code-assist): synchronous,
  // stateless code generation for one custom Python node module.
  // Failures surface as ApiError with the envelope's code/details so
  // describeCodeAssistError can categorize them (Requirements 5.1-5.3).
  async codeAssist(request: CodeAssistRequest): Promise<CodeAssistResponse> {
    return this.request('/code-assist', {
      method: 'POST',
      body: JSON.stringify(request),
    });
  }

  // Workflow_Test_Runner endpoints (workflow_testing.py, Requirement 12)
  // Test_Datasets scoped to the Use_Case (Requirement 12.2).
  async listTestDatasets(usecaseId?: string): Promise<{ datasets: TestDataset[]; count: number }> {
    const query = usecaseId ? `?usecase_id=${encodeURIComponent(usecaseId)}` : '';
    return this.request(`/test-datasets${query}`);
  }

  // Initiate a dataset upload: declares the file set and returns presigned
  // multipart upload URLs. No dataset record is written until finalize
  // verifies the uploaded content (Requirements 12.3, 12.11).
  async createTestDataset(data: {
    usecase_id: string;
    name: string;
    description?: string;
    files: Array<{ name: string; size: number; content_type?: string }>;
  }): Promise<TestDatasetUploadInitiation> {
    return this.request('/test-datasets', {
      method: 'POST',
      body: JSON.stringify({ action: 'initiate', ...data }),
    });
  }

  // Finalize a dataset upload: completes the multipart uploads and commits
  // the Test_Dataset record after server-side verification (12.3, 12.11).
  async finalizeTestDataset(data: {
    usecase_id: string;
    dataset_id: string;
    name: string;
    description?: string;
    files: TestDatasetCompletedFile[];
  }): Promise<{ dataset: TestDataset }> {
    return this.request('/test-datasets', {
      method: 'POST',
      body: JSON.stringify({ action: 'finalize', ...data }),
    });
  }

  // Start a test run of a stored workflow version against a Test_Dataset
  // (Requirement 12.4; latest version when omitted). `simulated_inference`
  // configures the outcome injected for simulation-stubbed model
  // inference nodes — the model itself is not executed in the cloud
  // sandbox (Requirement 12.6); the backend defaults it when omitted.
  async startTestRun(
    workflowId: string,
    data: {
      dataset_id: string;
      version?: number;
      simulated_inference?: { is_anomalous: boolean; confidence: number };
    }
  ): Promise<{ test_run: WorkflowTestRun }> {
    return this.request(`/workflows/${encodeURIComponent(workflowId)}/test-runs`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  // Test runs of one workflow, newest first.
  async listTestRuns(
    workflowId: string
  ): Promise<{ test_runs: WorkflowTestRun[]; count: number }> {
    return this.request(`/workflows/${encodeURIComponent(workflowId)}/test-runs`);
  }

  // Test run status plus the per-node results {nodeId, status, outputs,
  // stubActivity, error} produced so far (Requirements 12.7, 12.10).
  async getTestRun(testRunId: string): Promise<WorkflowTestRunDetail> {
    return this.request(`/test-runs/${encodeURIComponent(testRunId)}`);
  }

  // Build fleet endpoints (portal-build-fleet-and-workflow-gates).

  /**
   * Submit a build request: one Build_Job is created per selected
   * Build_Target in request order (Req 1.1, 1.2, 1.3, 2.1).
   */
  async submitBuild(data: SubmitBuildRequest): Promise<SubmitBuildResponse> {
    return this.request('/builds', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  /**
   * Branches of the selected repository with its default branch
   * identified, for the submission form's branch dropdown
   * (build-source-selection Req 3.1). Failures carry a distinct error
   * code per condition (REPOSITORY_NOT_FOUND, REPOSITORY_FORBIDDEN,
   * DISCOVERY_RATE_LIMITED, DISCOVERY_TIMEOUT, DISCOVERY_UPSTREAM_ERROR,
   * REPOSITORY_EMPTY) in the standard envelope; the form falls back to
   * manual ref entry. The configured default repository comes from the
   * existing getBuildConfig() read (`default_repository`, Req 1.5).
   */
  async listBuildBranches(repository: string): Promise<BuildBranchesResponse> {
    const query = new URLSearchParams({ repository });
    return this.request(`/build-branches?${query.toString()}`);
  }

  /**
   * One page of the 90-day Build_Job history, most recent first
   * (Req 4.7). Pass the returned nextToken to fetch the next page.
   */
  async listBuilds(params?: {
    limit?: number;
    nextToken?: string;
  }): Promise<BuildJobsPage> {
    const query = new URLSearchParams();
    if (params?.limit) query.set('limit', String(params.limit));
    if (params?.nextToken) query.set('nextToken', params.nextToken);
    const qs = query.toString();
    return this.request(`/builds${qs ? `?${qs}` : ''}`);
  }

  /** Build_Job detail (Req 4.3). */
  async getBuild(buildJobId: string): Promise<{ job: BuildJob }> {
    return this.request(`/builds/${encodeURIComponent(buildJobId)}`);
  }

  /**
   * One CloudWatch Logs page of the job's build log (Req 4.4).
   * CloudWatch returns the same nextToken when the page is exhausted;
   * keep polling that token for new output of a running build.
   */
  async getBuildLogs(
    buildJobId: string,
    params?: { limit?: number; nextToken?: string }
  ): Promise<BuildLogsPage> {
    const query = new URLSearchParams();
    if (params?.limit) query.set('limit', String(params.limit));
    if (params?.nextToken) query.set('nextToken', params.nextToken);
    const qs = query.toString();
    return this.request(
      `/builds/${encodeURIComponent(buildJobId)}/logs${qs ? `?${qs}` : ''}`
    );
  }

  /**
   * Cancel a queued or running Build_Job (Req 4.5, 4.6); terminal jobs
   * are rejected with 409 (Req 4.8).
   */
  async cancelBuild(buildJobId: string): Promise<{ job: BuildJob }> {
    return this.request(`/builds/${encodeURIComponent(buildJobId)}/cancel`, {
      method: 'POST',
    });
  }

  /**
   * Retry an interrupted Build_Job: creates a new Build_Job with the
   * same Build_Target and execution mode plus a retry_of reference
   * (Req 3.6).
   */
  async retryBuild(buildJobId: string): Promise<{ job: BuildJob }> {
    return this.request(`/builds/${encodeURIComponent(buildJobId)}/retry`, {
      method: 'POST',
    });
  }

  // Synthetic defect data generation endpoints
  // (synthetic-defect-data-generation, synthetic_data.py). All routes are
  // RBAC-gated server-side to Data_Scientist_Access (Req 9.1, 9.2).

  /**
   * The Model_Catalog available in the portal region with capability
   * flags (Req 1.1). When empty, `guidance` identifies the Bedrock
   * model-access configuration needed (Req 1.3).
   */
  async listSyntheticModels(usecaseId: string): Promise<SyntheticModelsResponse> {
    const query = new URLSearchParams({ usecase_id: usecaseId });
    return this.request(`/synthetic/models?${query.toString()}`);
  }

  /**
   * The stored Prompt_Template for the Use_Case/Object_Type/Defect_Type
   * key, or the default template (with `is_default: true`) when none is
   * stored (Req 2.2, 2.3).
   */
  async getSyntheticPromptTemplate(params: {
    usecase_id: string;
    object_type: string;
    defect_type: string;
  }): Promise<SyntheticPromptTemplateResponse> {
    const query = new URLSearchParams(params);
    return this.request(`/synthetic/prompt-templates?${query.toString()}`);
  }

  /** Persist an edited Prompt_Template for the key (Req 2.1, 2.4). */
  async putSyntheticPromptTemplate(body: {
    usecase_id: string;
    object_type: string;
    defect_type: string;
    template_text: string;
  }): Promise<SyntheticPromptTemplateResponse> {
    return this.request('/synthetic/prompt-templates', {
      method: 'PUT',
      body: JSON.stringify(body),
    });
  }

  /** Create a Generation_Session (persisted + audited, Req 10.1, 9.4). */
  async createSyntheticSession(
    body: CreateSyntheticSessionBody
  ): Promise<{ session: SyntheticSession }> {
    return this.request('/synthetic/sessions', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  }

  /** Generation_Sessions of a Use_Case with status + creation time (Req 10.4). */
  async listSyntheticSessions(
    usecaseId: string
  ): Promise<{ sessions: SyntheticSessionSummary[]; count: number }> {
    const query = new URLSearchParams({ usecase_id: usecaseId });
    return this.request(`/synthetic/sessions?${query.toString()}`);
  }

  /**
   * Full session state: META plus previews with presigned thumbnail URLs
   * and per-preview resolved prompt text (Req 10.2, 5.2, 5.6).
   */
  async getSyntheticSession(
    sessionId: string
  ): Promise<SyntheticSessionDetailResponse> {
    return this.request(`/synthetic/sessions/${encodeURIComponent(sessionId)}`);
  }

  /** Update model selection, sources/classification, params (Req 1.2, 3.2-3.4). */
  async patchSyntheticSession(
    sessionId: string,
    body: Partial<CreateSyntheticSessionBody>
  ): Promise<{ session: SyntheticSession }> {
    return this.request(`/synthetic/sessions/${encodeURIComponent(sessionId)}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    });
  }

  /**
   * Start (or re-start with an edited prompt) asynchronous preview
   * generation; returns 202 with the task count (Req 5.1, 5.3). A 400
   * carries `unresolved_placeholders` when the template has placeholder
   * variables missing from the context (Req 2.6).
   */
  async generateSyntheticPreviews(
    sessionId: string,
    body: SyntheticGenerateBody = {}
  ): Promise<SyntheticGenerateResponse> {
    return this.request(
      `/synthetic/sessions/${encodeURIComponent(sessionId)}/generate`,
      { method: 'POST', body: JSON.stringify(body) }
    );
  }

  /** Set the approval state for listed preview ids or all (Req 6.1, 6.2). */
  async setSyntheticPreviewApproval(
    sessionId: string,
    body: SyntheticApprovalBody
  ): Promise<{ updated: number; approval_state: string }> {
    return this.request(
      `/synthetic/sessions/${encodeURIComponent(sessionId)}/previews/approval`,
      { method: 'POST', body: JSON.stringify(body) }
    );
  }

  /**
   * Integrate the approved previews: upload, auto-annotate, and append to
   * the Data_Manifest; returns the manifest URI and appended record count
   * (Req 6.3-6.6, 7.1-7.8). Any failure leaves the manifest untouched.
   */
  async integrateSyntheticSession(
    sessionId: string,
    body: { target_dataset_prefix?: string; target_manifest_key?: string } = {}
  ): Promise<SyntheticIntegrateResponse> {
    return this.request(
      `/synthetic/sessions/${encodeURIComponent(sessionId)}/integrate`,
      { method: 'POST', body: JSON.stringify(body) }
    );
  }

  /**
   * Create a training job through the existing Training_Subsystem with
   * dataset_manifest_s3 pre-populated from the integration result and the
   * originating generation_session_id recorded (Req 8.1-8.3).
   */
  async retrainSyntheticSession(
    sessionId: string,
    body: SyntheticRetrainBody
  ): Promise<{ training_job_id: string; message: string }> {
    return this.request(
      `/synthetic/sessions/${encodeURIComponent(sessionId)}/retrain`,
      { method: 'POST', body: JSON.stringify(body) }
    );
  }

  // Manifest Validator endpoints
  async manifestValidator(data: {
    action: 'validate' | 'transform' | 'fix_timestamps' | 'validate_and_transform';
    manifestPath: string;
    usecaseId: string;
    outputPath?: string;
  }): Promise<any> {
    return this.request('/manifest-validator', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }
}

export const apiService = new ApiService();
export default apiService;
