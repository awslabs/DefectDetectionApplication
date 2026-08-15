/**
 * Types for the synthetic defect data generation workspace
 * (synthetic-defect-data-generation). Mirrors the backend contracts of
 * `synthetic_data.py` / `synthetic_core.py`.
 */

/** Capability flags of one Model_Catalog entry (Req 1.1, 4.3). */
export interface SyntheticModelCapabilities {
  text_to_image: boolean;
  inpainting: boolean;
  image_variation: boolean;
  seed: boolean;
  cfg_scale: boolean;
}

/** One Generation_Model of the Model_Catalog (`GET /synthetic/models`). */
export interface SyntheticModel {
  model_id: string;
  display_name: string;
  capabilities: SyntheticModelCapabilities;
  max_images_per_call: number;
  randomization_defaults: { seed: number | null; cfg_scale: number | null };
}

/**
 * Response of `GET /synthetic/models`. `guidance` is present exactly when
 * `models` is empty and identifies the Bedrock model-access configuration
 * needed to enable at least one Generation_Model (Req 1.3).
 */
export interface SyntheticModelsResponse {
  models: SyntheticModel[];
  guidance?: string;
}

/** Response of the prompt-template get/put endpoints (Req 2.2-2.4). */
export interface SyntheticPromptTemplateResponse {
  template_text: string;
  object_type: string;
  defect_type: string;
  /** True when no stored template exists and the default was returned. */
  is_default: boolean;
}

/** A Source_Image reference stored on the session. */
export interface SyntheticSourceImage {
  key: string;
  bucket?: string;
  width?: number;
  height?: number;
}

export type SyntheticSessionStatus =
  | 'draft'
  | 'generating'
  | 'awaiting_review'
  | 'approved'
  | 'integrated'
  | 'failed';

export type SyntheticSourceClass = 'defect' | 'normal';

export type SyntheticApprovalState = 'pending' | 'approved' | 'rejected';

/** Generation parameters persisted on the session META (Req 4.1, 4.3). */
export interface SyntheticGenerationParams {
  variation_count?: number;
  seed?: number | null;
  cfg_scale?: number | null;
}

/** Result of a completed integration run (Req 7.6). */
export interface SyntheticIntegrationResult {
  manifest_uri: string;
  appended_count: number;
  at: number;
}

/** Generation_Session META as returned by the session endpoints. */
export interface SyntheticSession {
  session_id: string;
  usecase_id: string;
  status: SyntheticSessionStatus;
  generation_model_id?: string | null;
  object_type?: string | null;
  defect_type?: string | null;
  prompt_template_text?: string | null;
  resolved_prompt?: string | null;
  source_class?: SyntheticSourceClass | null;
  source_images?: SyntheticSourceImage[];
  generation_params?: SyntheticGenerationParams;
  generation_pass?: number;
  /** Persisted plan of the last generate call; its length is the task count. */
  generation_plan?: unknown[];
  target_dataset_prefix?: string | null;
  target_manifest_key?: string | null;
  last_failure?: { reason: string; at: number } | null;
  integration_result?: SyntheticIntegrationResult | null;
  created_by?: string;
  created_at: number;
  updated_at?: number;
}

/** One row of `GET /synthetic/sessions` (status + creation time, Req 10.4). */
export interface SyntheticSessionSummary {
  session_id: string;
  status: SyntheticSessionStatus;
  created_at: number;
  object_type?: string | null;
  defect_type?: string | null;
  generation_model_id?: string | null;
}

/** One Preview_Image item of `GET /synthetic/sessions/{id}` (Req 5.2, 5.6). */
export interface SyntheticPreview {
  preview_id: string;
  source_image_key?: string;
  variation_index?: number;
  generation_pass?: number;
  staging_key?: string;
  generation_method?: 'inpainting' | 'image_variation';
  mask_region?: { left: number; top: number; width: number; height: number } | null;
  /** Exact prompt text sent to the model for this preview (Req 5.6). */
  resolved_prompt?: string;
  seed?: number;
  status: 'completed' | 'failed';
  failure_reason?: string;
  approval_state?: SyntheticApprovalState;
  /** Presigned staging URL, present for completed previews (Req 5.2). */
  thumbnail_url?: string | null;
  created_at?: number;
}

/** Response of `GET /synthetic/sessions/{id}` (Req 10.2). */
export interface SyntheticSessionDetailResponse {
  session: SyntheticSession;
  previews: SyntheticPreview[];
}

/** Body of `POST /synthetic/sessions` (Req 10.1). */
export interface CreateSyntheticSessionBody {
  usecase_id: string;
  generation_model_id?: string;
  object_type?: string;
  defect_type?: string;
  prompt_template_text?: string;
  source_class?: SyntheticSourceClass;
  source_images?: SyntheticSourceImage[];
  generation_params?: SyntheticGenerationParams;
  target_dataset_prefix?: string;
  target_manifest_key?: string;
}

/** Body of `POST /synthetic/sessions/{id}/generate` (Req 5.3). */
export interface SyntheticGenerateBody {
  /** Edited prompt for regeneration; takes precedence over the stored one. */
  prompt_template_text?: string;
  generation_model_id?: string;
  defect_type?: string;
  source_class?: SyntheticSourceClass;
  source_images?: SyntheticSourceImage[];
  variation_count?: number;
  generation_params?: SyntheticGenerationParams;
  /** Regeneration scope (Req 5.3): all | source_image | preview. */
  scope?: 'all' | 'source_image' | 'preview';
  source_image_key?: string;
  preview_id?: string;
}

/** 202 response of the generate endpoint (Req 5.1). */
export interface SyntheticGenerateResponse {
  session_id: string;
  status: 'generating';
  generation_pass: number;
  task_count: number;
}

/** Body of `POST /synthetic/sessions/{id}/previews/approval` (Req 6.1, 6.2). */
export interface SyntheticApprovalBody {
  approval_state: SyntheticApprovalState;
  preview_ids?: string[];
  all?: boolean;
}

/** Response of the integrate endpoint (Req 7.6). */
export interface SyntheticIntegrateResponse {
  manifest_uri: string;
  appended_count: number;
  session_id: string;
  status: 'integrated';
}

/**
 * Body of `POST /synthetic/sessions/{id}/retrain` — the existing
 * Training_Subsystem contract; `dataset_manifest_s3` and
 * `generation_session_id` are pre-populated server-side from the
 * integration result (Req 8.1-8.3).
 */
export interface SyntheticRetrainBody {
  model_name: string;
  model_version?: string;
  model_type?: string;
  model_source?: string;
  instance_type?: string;
  max_runtime_seconds?: number;
  dataset_manifest_s3?: string;
}
