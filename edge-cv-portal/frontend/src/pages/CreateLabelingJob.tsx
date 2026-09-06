import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Container,
  Header,
  Wizard,
  FormField,
  Input,
  Select,
  SelectProps,
  SpaceBetween,
  Box,
  Alert,
  Textarea,
  Button,
  Checkbox,
  RadioGroup,
  Toggle,
  FileUpload,
} from '@cloudscape-design/components';
import { useNavigate, useLocation, useSearchParams } from 'react-router-dom';
import { S3Dataset } from '../types';
import {
  apiService,
  ApiError,
  LabelingTeam,
  PreviewFewShotExample,
} from '../services/api';
import { useUsecase } from '../contexts/UsecaseContext';
import { useAuth } from '../contexts/AuthContext';
import S3Browser from '../components/S3Browser';
import PromptTuningPreview, {
  TOKEN_BUDGET_RANGE_TEXT,
  parseTokenBudget,
} from '../components/labeling/PromptTuningPreview';
import type { LabelingModality } from '../components/labeling/AnnotationCanvas';
import {
  DRAFT_SAVE_DEBOUNCE_MS,
  canResumePreviewRun,
  clearLabelingJobDraft,
  draftsEquivalent,
  exampleRefDisplayName,
  mergedExampleRefs,
  readLabelingJobDraft,
  writeLabelingJobDraft,
} from './labelingJobDraft';
import type {
  LabelingJobDraft,
  PreviewRunReference,
} from './labelingJobDraft';
import { validateS3Uri } from '../utils/s3Validation';
import { getErrorMessage, scrollToTop } from '../utils/errorHandling';

interface LocationState {
  dataset?: S3Dataset;
}

// DDA Labeling_Backend constants (dda-data-labeling Requirements 1.1, 4.x).
type LabelingBackend = 'DDA' | 'GroundTruth';

/** Fixed Label_Set for Binary_Classification (Requirement 4.3). */
export const FIXED_CLASSIFICATION_LABEL_SET = ['normal', 'anomaly'];
const MAX_LABELS = 10;
const MAX_LABEL_LENGTH = 64;
const MAX_INSTRUCTIONS_LENGTH = 5000;
const MAX_EXAMPLE_IMAGES = 10;
const EXAMPLE_IMAGE_TYPES = ['image/jpeg', 'image/png'];
const MAX_DDA_JOB_NAME_LENGTH = 63;

/**
 * Auto_Labeler model/modality compatibility matrix, enforced client-side
 * at job creation (dda-data-labeling Requirement 8.8; the backend
 * re-validates). SAM is class-agnostic geometry — Segmentation and
 * ObjectDetection only; Bedrock vision models answer classification and
 * detection prompts — Classification and ObjectDetection only;
 * prompt-guided LLM models return coordinate guidance convertible to any
 * modality (llm-auto-labeling Requirement 1.3).
 */
export const SAM_MODALITIES = ['Segmentation', 'ObjectDetection'];
export const BEDROCK_MODALITIES = ['Classification', 'ObjectDetection'];
export const LLM_MODALITIES = [
  'Classification',
  'Segmentation',
  'ObjectDetection',
];
/**
 * Grounded-SAM produces classified geometry (text-prompted boxes, and
 * masks for segmentation) — Segmentation and ObjectDetection only, never
 * Classification (grounded-sam-autolabel Requirements 1.1, 1.2, 1.3).
 */
export const GROUNDED_SAM_MODALITIES = ['Segmentation', 'ObjectDetection'];

/**
 * Prompt_Override length limit for the Grounded-SAM family, judged on the
 * raw entered value (grounded-sam-autolabel Requirement 2.6; the backend
 * re-validates).
 */
export const MAX_PROMPT_OVERRIDE_LENGTH = 256;

/** Detection_Prompt length limit (llm-auto-labeling Requirements 2.1, 2.2). */
export const MAX_DETECTION_PROMPT_LENGTH = 2000;

/**
 * Model_Image_Limit fallback when the model catalog carries no
 * `image_limit` for the selected model — the same default the backend
 * resolves (llm-autolabel-prompt-tuning Requirement 7.1). The backend
 * stays authoritative for what is actually attached; this only drives
 * the wizard's attach/omit hint.
 */
export const MODEL_IMAGE_LIMIT_DEFAULT = 20;

/**
 * Model_Token_Limit_Default — the Token_Budget_Selection pre-fill when the
 * model catalog carries no `token_limit` for the selected model, matching
 * the backend's resolver default (llm-model-token-and-image-sizing
 * Requirements 3.1, 3.2). The backend stays authoritative for the
 * Effective_Token_Budget; this only seeds the control.
 */
export const MODEL_TOKEN_LIMIT_DEFAULT = 10000;

/**
 * Attached/omitted Few_Shot_Example split for `total` stored example
 * images under a Model_Image_Limit of `limit`
 * (llm-autolabel-prompt-tuning Requirements 7.2, 7.4, 7.5). One image
 * slot is always reserved for the target image, so a limit of 1 attaches
 * nothing.
 */
export function fewShotAttachmentCounts(
  total: number,
  limit: number
): { attached: number; omitted: number } {
  const usable = Math.max(0, (Number.isInteger(limit) && limit >= 1
    ? limit
    : MODEL_IMAGE_LIMIT_DEFAULT) - 1);
  const attached = Math.min(total, usable);
  return { attached, omitted: total - attached };
}

/**
 * Image_Input_Capability filter for the auto-label families: a model is
 * excluded only when positively known to lack image input
 * (`image_input === false`); unknown capability (field absent) is
 * included (llm-model-picker-search-and-image-filter Requirements 2.1,
 * 2.2).
 */
export function isImageCapableModel(m: { image_input?: boolean }): boolean {
  return m.image_input !== false;
}

/**
 * The ordered Few_Shot_Example set for the uploaded example image
 * references: good examples in upload order first, then bad examples,
 * each carrying its designation and its position *within* that
 * designation — the shape persisted with the Labeling_Job record
 * (llm-autolabel-prompt-tuning Requirement 6.4).
 */
export function fewShotExamplesFromRefs(exampleImages: {
  good: string[];
  bad: string[];
}): PreviewFewShotExample[] {
  return [
    ...exampleImages.good.map((ref, position) => ({
      ref,
      designation: 'good' as const,
      position,
    })),
    ...exampleImages.bad.map((ref, position) => ({
      ref,
      designation: 'bad' as const,
      position,
    })),
  ];
}

/** True when the auto-label model value is usable with the modality. */
export function isAutoLabelModelCompatible(
  modelValue: string,
  taskType: string
): boolean {
  if (modelValue === 'sam') return SAM_MODALITIES.includes(taskType);
  if (modelValue === 'grounded-sam')
    return GROUNDED_SAM_MODALITIES.includes(taskType);
  if (modelValue.startsWith('llm:')) return LLM_MODALITIES.includes(taskType);
  if (modelValue.startsWith('bedrock:'))
    return BEDROCK_MODALITIES.includes(taskType);
  return false;
}

/**
 * Every task-type option either branch can offer, for reconstructing a
 * restored Setup_Draft's selection by value before the branch-dependent
 * option list re-renders (labeling-setup-session-recovery Requirement
 * 3.2); an unknown value restores as no selection. The rendered options
 * stay branch-filtered (`taskTypeOptions`).
 */
const ALL_TASK_TYPE_OPTIONS: SelectProps.Option[] = [
  { label: 'Image Classification', value: 'Classification' },
  { label: 'Semantic Segmentation', value: 'Segmentation' },
  { label: 'Object Detection', value: 'ObjectDetection' },
];

const fileUploadI18nStrings = {
  uploadButtonText: (multiple: boolean) =>
    multiple ? 'Choose files' : 'Choose file',
  dropzoneText: (multiple: boolean) =>
    multiple ? 'Drop files to upload' : 'Drop file to upload',
  removeFileAriaLabel: (index: number) => `Remove file ${index + 1}`,
  limitShowFewer: 'Show fewer files',
  limitShowMore: 'Show more files',
  errorIconAriaLabel: 'Error',
};

/** Per-file JPEG/PNG errors for an example uploader (Requirement 4.4). */
function exampleFileErrors(files: File[]): (string | null)[] {
  return files.map((file) =>
    EXAMPLE_IMAGE_TYPES.includes(file.type)
      ? null
      : 'Example images must be JPEG or PNG'
  );
}

export default function CreateLabelingJob() {
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const { user } = useAuth();
  const { selectedUsecaseId, setSelectedUsecaseId } = useUsecase();
  const useCaseIdFromUrl = searchParams.get('usecase_id');
  const preselectedDataset = (location.state as LocationState)?.dataset;

  // Skip_Verification is admin-only (dda-data-labeling Requirement 9.1).
  const isAdmin =
    user?.role === 'UseCaseAdmin' || user?.role === 'PortalAdmin';

  const [activeStepIndex, setActiveStepIndex] = useState(0);
  const [useCases, setUseCases] = useState<any[]>([]);
  const [selectedUseCase, setSelectedUseCase] = useState<any>(null);
  const [jobName, setJobName] = useState('');
  const [description, setDescription] = useState('');
  const [datasetS3Uri, setDatasetS3Uri] = useState(preselectedDataset?.prefix || '');
  const [maskPrefix, setMaskPrefix] = useState('');
  const [taskType, setTaskType] = useState<SelectProps.Option | null>(null);
  const [workforceType, setWorkforceType] = useState<SelectProps.Option | null>({
    label: 'Private',
    value: 'private',
  });
  const [labelCategories, setLabelCategories] = useState('');
  const [instructions, setInstructions] = useState('');
  const [enableAutomatedLabeling, setEnableAutomatedLabeling] = useState(false);
  const [workteams, setWorkteams] = useState<any[]>([]);
  const [selectedWorkteam, setSelectedWorkteam] = useState<SelectProps.Option | null>(null);
  const [loadingWorkteams, setLoadingWorkteams] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState('');
  const [showBrowseModal, setShowBrowseModal] = useState(false);

  // Labeling_Backend selection — required before submit (Requirement 1.1).
  const [labelingBackend, setLabelingBackend] = useState<LabelingBackend | ''>('');

  // DDA branch state (Requirements 4.1-4.4, 8.1, 9.2).
  const [labelingTeams, setLabelingTeams] = useState<LabelingTeam[]>([]);
  const [loadingTeams, setLoadingTeams] = useState(false);
  const [selectedTeam, setSelectedTeam] = useState<SelectProps.Option | null>(null);
  const [ddaLabels, setDdaLabels] = useState<string[]>(['']);
  const [ddaInstructions, setDdaInstructions] = useState('');
  const [goodExampleFiles, setGoodExampleFiles] = useState<File[]>([]);
  const [badExampleFiles, setBadExampleFiles] = useState<File[]>([]);
  const [autoLabelEnabled, setAutoLabelEnabled] = useState(false);
  const [autoLabelModel, setAutoLabelModel] = useState('');
  // Detection_Prompt for prompt-guided LLM auto-labeling
  // (llm-auto-labeling Requirements 2.1, 2.2).
  const [detectionPrompt, setDetectionPrompt] = useState('');
  // Prompt_Override entries for the Grounded-SAM family, keyed by label
  // name; every entry is optional and the label name itself is the
  // default text prompt (grounded-sam-autolabel Requirement 2.1).
  const [groundedSamPromptOverrides, setGroundedSamPromptOverrides] =
    useState<Record<string, string>>({});
  const [skipVerification, setSkipVerification] = useState(false);
  const [skipVerificationModelId, setSkipVerificationModelId] = useState('');
  const [perLabelPrompts, setPerLabelPrompts] = useState<Record<string, string>>({});
  // Few_Shot_Option — per-job, disabled by default, offered only for the
  // prompt-guided LLM family (llm-autolabel-prompt-tuning Req 6.1, 10.5).
  const [fewShotEnabled, setFewShotEnabled] = useState(false);
  // Downscale_Setting — Downscale_Off (`null`) by default, offered only for
  // the prompt-guided LLM family; lifted here so the value chosen in the
  // Prompt_Tuning_Preview rides into job submission
  // (llm-model-token-and-image-sizing Req 5.1, 5.2, 5.7).
  const [downscaleMaxEdge, setDownscaleMaxEdge] = useState<number | null>(
    null
  );
  // Token_Budget_Selection as entered — pre-filled from the selected
  // model's catalog `token_limit` and replaced on every model change;
  // empty omits the value from submission
  // (llm-model-token-and-image-sizing Req 3.1, 3.2, 3.10).
  const [tokenBudget, setTokenBudget] = useState('');
  const [bedrockModels, setBedrockModels] = useState<
    {
      id: string;
      label: string;
      image_limit?: number;
      token_limit?: number;
      // Image_Input_Capability tri-state: absent = unknown, never exclude
      // (llm-model-picker-search-and-image-filter Req 1.4, 2.2).
      image_input?: boolean;
    }[]
  >([]);
  const [bedrockModelsUnavailable, setBedrockModelsUnavailable] = useState(false);

  // --- Setup_Draft recovery state (labeling-setup-session-recovery) -------
  // Restored_Example_References still present in the form, per designation
  // (Requirement 4.1).
  const [restoredExampleRefs, setRestoredExampleRefs] = useState<{
    good: string[];
    bad: string[];
  }>({ good: [], bad: [] });
  // Write-through mirrors of the Prompt_Tuning_Preview's Sample_Selection
  // and most recently started Preview_Run, fed by the preview's callbacks
  // and serialized into the draft (Requirement 2.4).
  const [previewSelectedKeys, setPreviewSelectedKeys] = useState<string[]>([]);
  const [previewRunRef, setPreviewRunRef] =
    useState<PreviewRunReference | null>(null);
  // The pending Restore_Offer; rendered while it matches the selected use
  // case (Requirement 3.1).
  const [draftOffer, setDraftOffer] = useState<{
    usecaseId: string;
    draft: LabelingJobDraft;
  } | null>(null);
  // React key for the preview: a restore bumps it so the preview remounts
  // with initialSelectedKeys and resumeRun applied deterministically.
  const [previewRestoreNonce, setPreviewRestoreNonce] = useState(0);
  // Once-per-use-case draft read guard (the TestPanel resumedUsecaseRef
  // pattern; Requirement 3.1).
  const draftReadUsecases = useRef<Set<string>>(new Set());
  // Team id parked by a restore until the team list has loaded (Req 3.4).
  const pendingTeamRestoreRef = useRef<string | null>(null);
  // resumeRun for the next preview mount; cleared once a new run starts.
  const restoredPreviewRunRef = useRef<PreviewRunReference | null>(null);
  // Use case the restored example refs were restored under — they are
  // discarded when the selection moves away from it (Requirement 4.5).
  const restoredRefsUsecaseRef = useRef<string | null>(null);
  // Set after successful creation so no debounced write re-creates the
  // draft before navigation (Requirement 6.1).
  const draftClearedRef = useRef(false);

  // Load use cases on mount
  useEffect(() => {
    const loadUseCases = async () => {
      try {
        console.log('Loading use cases...');
        const data = await apiService.listUseCases();
        console.log('Use cases loaded:', data);
        setUseCases(data.usecases || []);
        
        // If use case ID is in URL, select that one
        if (useCaseIdFromUrl && data.usecases) {
          const useCaseFromUrl = data.usecases.find((uc: any) => uc.usecase_id === useCaseIdFromUrl);
          if (useCaseFromUrl) {
            console.log('Auto-selecting use case from URL:', useCaseFromUrl);
            setSelectedUseCase(useCaseFromUrl);
            setSelectedUsecaseId(useCaseFromUrl.usecase_id);
            return;
          }
        }
        
        // Use saved selection from context
        if (selectedUsecaseId && data.usecases) {
          const saved = data.usecases.find((uc: any) => uc.usecase_id === selectedUsecaseId);
          if (saved) {
            console.log('Auto-selecting use case from context:', saved);
            setSelectedUseCase(saved);
            return;
          }
        }
        
        // Otherwise auto-select first use case
        if (data.usecases && data.usecases.length > 0) {
          console.log('Auto-selecting first use case:', data.usecases[0]);
          setSelectedUseCase(data.usecases[0]);
          setSelectedUsecaseId(data.usecases[0].usecase_id);
        }
      } catch (err) {
        console.error('Failed to load use cases:', err);
        setError('Failed to load use cases');
      }
    };
    loadUseCases();
  }, [useCaseIdFromUrl, selectedUsecaseId, setSelectedUsecaseId]);

  // Setup_Draft read: once per resolved use case per mount, a stored
  // draft raises the Restore_Offer; no draft leaves rendering unchanged
  // (labeling-setup-session-recovery Requirements 3.1, 3.8).
  useEffect(() => {
    const usecaseId: string | undefined = selectedUseCase?.usecase_id;
    if (!usecaseId || draftReadUsecases.current.has(usecaseId)) return;
    draftReadUsecases.current.add(usecaseId);
    const draft = readLabelingJobDraft(usecaseId);
    if (draft !== null) {
      setDraftOffer({ usecaseId, draft });
    }
  }, [selectedUseCase]);

  // Restored_Example_References never cross use cases: moving away from
  // the use case they were restored under discards them (Requirement 4.5).
  useEffect(() => {
    if (
      restoredRefsUsecaseRef.current !== null &&
      selectedUseCase?.usecase_id !== restoredRefsUsecaseRef.current
    ) {
      restoredRefsUsecaseRef.current = null;
      setRestoredExampleRefs({ good: [], bad: [] });
    }
  }, [selectedUseCase]);

  // Load SageMaker workteams when use case changes (GroundTruth branch).
  useEffect(() => {
    const loadWorkteams = async () => {
      if (!selectedUseCase) return;
      
      setLoadingWorkteams(true);
      try {
        console.log('Loading workteams for use case:', selectedUseCase.usecase_id);
        const data = await apiService.listWorkteams(selectedUseCase.usecase_id);
        console.log('Workteams loaded:', data);
        setWorkteams(data.workteams || []);
        // Auto-select first workteam
        if (data.workteams && data.workteams.length > 0) {
          setSelectedWorkteam({
            label: data.workteams[0].name,
            value: data.workteams[0].name,
            description: data.workteams[0].description,
          });
        }
      } catch (err) {
        console.error('Failed to load workteams:', err);
        // Don't set error here, just log it - workteams might not be set up yet
      } finally {
        setLoadingWorkteams(false);
      }
    };
    loadWorkteams();
  }, [selectedUseCase]);

  // Load DDA Labeling_Teams for the use case (Requirement 4.1; teams come
  // from GET /labeling-teams scoped to the Use_Case).
  useEffect(() => {
    if (labelingBackend !== 'DDA' || !selectedUseCase) return;
    let cancelled = false;
    setLoadingTeams(true);
    setSelectedTeam(null);
    apiService
      .listLabelingTeams(selectedUseCase.usecase_id)
      .then((data) => {
        if (!cancelled) setLabelingTeams(data.teams || []);
      })
      .catch((err) => {
        // Teams may not exist yet; the validation surfaces the requirement.
        console.error('Failed to load labeling teams:', err);
        if (!cancelled) setLabelingTeams([]);
      })
      .finally(() => {
        if (!cancelled) setLoadingTeams(false);
      });
    return () => {
      cancelled = true;
    };
  }, [labelingBackend, selectedUseCase]);

  // Complete a restore's parked team selection once the use case's team
  // list has settled: the parked id is selected when present in the loaded
  // list and consumed either way, leaving the team unselected when the id
  // is gone (labeling-setup-session-recovery Requirement 3.4).
  useEffect(() => {
    if (loadingTeams) return;
    const pendingTeamId = pendingTeamRestoreRef.current;
    if (pendingTeamId === null) return;
    pendingTeamRestoreRef.current = null;
    const team = labelingTeams.find((t) => t.team_id === pendingTeamId);
    if (team) {
      setSelectedTeam({ label: team.team_name, value: team.team_id });
    }
  }, [labelingTeams, loadingTeams]);

  // Load the Bedrock models available to the Portal for the auto-label
  // model select and the Skip_Verification model select (Requirements
  // 8.1, 9.2). On failure the UI degrades to free-text entry.
  useEffect(() => {
    if (labelingBackend !== 'DDA') return;
    let cancelled = false;
    apiService
      .getBedrockModels()
      .then((response) => {
        if (cancelled) return;
        setBedrockModels(response.models || []);
        setBedrockModelsUnavailable((response.models || []).length === 0);
      })
      .catch(() => {
        if (cancelled) return;
        setBedrockModels([]);
        setBedrockModelsUnavailable(true);
      });
    return () => {
      cancelled = true;
    };
  }, [labelingBackend]);

  // Ground Truth supports only Classification/Segmentation; clear an
  // ObjectDetection selection when switching back (Requirement 1.2 — the
  // GroundTruth path submits exactly as before).
  useEffect(() => {
    if (labelingBackend === 'GroundTruth' && taskType?.value === 'ObjectDetection') {
      setTaskType(null);
    }
  }, [labelingBackend, taskType]);

  // The model the Token_Budget_Selection was last pre-filled for, so the
  // budget is replaced exactly when the model selection changes and any
  // other re-run of the compatibility effect leaves an entered value
  // alone (llm-model-token-and-image-sizing Req 3.2).
  const budgetPrefillModelRef = useRef('');

  // Enforce the model/modality matrix client-side: drop an auto-label
  // model selection that is incompatible with the chosen modality
  // (Requirement 8.8).
  useEffect(() => {
    if (autoLabelModel && taskType?.value &&
        !isAutoLabelModelCompatible(autoLabelModel, taskType.value as string)) {
      setAutoLabelModel('');
    }
    // The Few_Shot_Option belongs to the `llm:` family only: any other
    // selection (including a cleared one) submits it disabled
    // (llm-autolabel-prompt-tuning Req 6.9, 10.5).
    if (!autoLabelModel.startsWith('llm:')) {
      setFewShotEnabled(false);
      // The sizing values belong to the `llm:` family only, so leaving it
      // returns both to their defaults and `sam`, `bedrock:` and no-model
      // states submit neither (llm-model-token-and-image-sizing Req 3.2,
      // 5.2, 10.4, 10.6).
      setDownscaleMaxEdge(null);
      setTokenBudget('');
      budgetPrefillModelRef.current = '';
    } else if (budgetPrefillModelRef.current !== autoLabelModel) {
      // A newly selected `llm:` model replaces the shown budget with the
      // model's catalog `token_limit` (falling back to 10000), discarding
      // the previous model's value and touching nothing else — the
      // Detection_Prompt, Label_Set, selected samples, Few_Shot_Option and
      // Downscale_Setting stay as they are
      // (llm-model-token-and-image-sizing Req 3.1, 3.2).
      budgetPrefillModelRef.current = autoLabelModel;
      const catalogTokenLimit = bedrockModels.find(
        (m) => m.id === autoLabelModel.slice('llm:'.length)
      )?.token_limit;
      setTokenBudget(String(catalogTokenLimit ?? MODEL_TOKEN_LIMIT_DEFAULT));
    }
  }, [taskType, autoLabelModel, bedrockModels]);

  const isDda = labelingBackend === 'DDA';
  const modality = (taskType?.value as string) || '';
  const isClassification = modality === 'Classification';

  // Effective Label_Set: fixed normal/anomaly for Binary_Classification
  // (Requirement 4.3), otherwise the editor rows (Requirement 4.2).
  const trimmedDdaLabels = ddaLabels.map((l) => l.trim()).filter((l) => l);
  const effectiveLabelSet = isClassification
    ? FIXED_CLASSIFICATION_LABEL_SET
    : trimmedDdaLabels;

  // Auto_Labeler options per the compatibility matrix (Requirements 8.1, 8.8).
  // The prompt-guided LLM entries (`llm:<id>`, llm-auto-labeling
  // Requirements 1.1, 1.2) are grouped apart from the plain Bedrock
  // entries so both modes stay reachable for the same catalog model.
  const samAutoLabelOptions: SelectProps.Option[] = SAM_MODALITIES.includes(
    modality
  )
    ? [{ label: 'Segment Anything (SAM)', value: 'sam' }]
    : [];
  // Grounded_SAM_Entry: a static entry beside SAM, offered for exactly
  // the Grounded-SAM modalities and never for Classification
  // (grounded-sam-autolabel Requirements 1.1, 1.2, 7.2).
  const groundedSamAutoLabelOptions: SelectProps.Option[] =
    GROUNDED_SAM_MODALITIES.includes(modality)
      ? [{ label: 'Grounded-SAM (text-prompted)', value: 'grounded-sam' }]
      : [];
  // The auto-label families offer only the models not positively known to
  // be text-only; everything else (catalog-unavailable detection, per-model
  // lookups, the Skip_Verification select) keeps reading the raw
  // `bedrockModels` (llm-model-picker-search-and-image-filter Req 2.1,
  // 2.2, 4.2, 4.6). `filteringTags` carries the bare catalog id so
  // type-to-search matches it by contract (Req 3.2).
  const imageCapableModels = bedrockModels.filter(isImageCapableModel);
  const bedrockAutoLabelOptions: SelectProps.Option[] =
    BEDROCK_MODALITIES.includes(modality)
      ? imageCapableModels.map((m) => ({
          label: `Bedrock: ${m.label}`,
          value: `bedrock:${m.id}`,
          filteringTags: [m.id],
        }))
      : [];
  const llmAutoLabelOptions: SelectProps.Option[] = LLM_MODALITIES.includes(
    modality
  )
    ? imageCapableModels.map((m) => ({
        label: `${m.label} (prompt-guided)`,
        value: `llm:${m.id}`,
        filteringTags: [m.id],
      }))
    : [];
  const flatAutoLabelOptions: SelectProps.Option[] = [
    ...samAutoLabelOptions,
    ...groundedSamAutoLabelOptions,
    ...bedrockAutoLabelOptions,
    ...llmAutoLabelOptions,
  ];
  const autoLabelOptions: SelectProps.Options = [
    ...samAutoLabelOptions,
    ...groundedSamAutoLabelOptions,
    ...(bedrockAutoLabelOptions.length > 0
      ? [{ label: 'Bedrock vision models', options: bedrockAutoLabelOptions }]
      : []),
    ...(llmAutoLabelOptions.length > 0
      ? [
          {
            label: 'Prompt-guided LLM models',
            options: llmAutoLabelOptions,
          },
        ]
      : []),
  ];
  const isLlmAutoLabelModel = autoLabelModel.startsWith('llm:');

  // Few_Shot_Option surfaces only alongside a prompt-guided LLM model
  // (Req 1.2, 6.1, 10.5).
  const showFewShotControls = autoLabelEnabled && isLlmAutoLabelModel;

  // Model_Image_Limit of the selected model, from the catalog payload with
  // the shared fallback (Req 7.1). The attach/omit counts recompute
  // whenever the model or either example list changes (Req 7.5).
  const selectedModelImageLimit = isLlmAutoLabelModel
    ? bedrockModels.find((m) => m.id === autoLabelModel.slice('llm:'.length))
        ?.image_limit ?? MODEL_IMAGE_LIMIT_DEFAULT
    : MODEL_IMAGE_LIMIT_DEFAULT;
  // Combined per-designation example counts: Restored_Example_References
  // still in the form plus newly staged files. These feed the
  // per-designation limits, the few-shot at-least-one rule, the
  // attach/omit hint, the review step, and the preview's example-count
  // props; with no restore they equal the file counts exactly
  // (labeling-setup-session-recovery Requirement 4.2).
  const combinedGoodExampleCount =
    restoredExampleRefs.good.length + goodExampleFiles.length;
  const combinedBadExampleCount =
    restoredExampleRefs.bad.length + badExampleFiles.length;
  const exampleImageCount = combinedGoodExampleCount + combinedBadExampleCount;
  const { attached: fewShotAttachedCount, omitted: fewShotOmittedCount } =
    fewShotAttachmentCounts(exampleImageCount, selectedModelImageLimit);

  // Dataset prefix for the Prompt_Tuning_Preview listing and sample scope:
  // the wizard's dataset S3 URI with the bucket stripped, the same
  // derivation job submission applies (llm-autolabel-prompt-tuning Req
  // 2.1). Empty until the URI is a well-formed `s3://bucket/prefix`, which
  // leaves the preview's listing idle rather than calling the API.
  const datasetPrefix = useMemo(() => {
    const match = datasetS3Uri.trim().match(/^s3:\/\/[^/]+\/(.+)$/);
    return match ? match[1] : '';
  }, [datasetS3Uri]);

  const taskTypeOptions = [
    { label: 'Image Classification', value: 'Classification' },
    { label: 'Semantic Segmentation', value: 'Segmentation' },
    // Object detection is a DDA-only modality; the Ground Truth path is
    // unchanged (Requirement 1.2).
    ...(isDda ? [{ label: 'Object Detection', value: 'ObjectDetection' }] : []),
  ];

  const workforceOptions = [
    { label: 'Private', value: 'private' },
    { label: 'Public (Mechanical Turk)', value: 'public' },
    { label: 'Vendor', value: 'vendor' },
  ];

  /**
   * Apply a restored Setup_Draft to the Wizard_Setup_State — every
   * Requirement 2.1 field, set synchronously in the offer's Restore
   * action so the wizard's reactive effects run over the restored values
   * as one consistent update (labeling-setup-session-recovery Req 3.2).
   */
  const applyDraftRestore = (draft: LabelingJobDraft) => {
    setActiveStepIndex(draft.activeStepIndex);
    setLabelingBackend(draft.labelingBackend);
    setJobName(draft.jobName);
    setDescription(draft.description);
    setDatasetS3Uri(draft.datasetS3Uri);
    setMaskPrefix(draft.maskPrefix);
    setTaskType(
      ALL_TASK_TYPE_OPTIONS.find((o) => o.value === draft.taskTypeValue) ??
        null
    );
    setWorkforceType(
      workforceOptions.find((o) => o.value === draft.workforceTypeValue) ??
        null
    );
    setLabelCategories(draft.labelCategories);
    setInstructions(draft.gtInstructions);
    setEnableAutomatedLabeling(draft.enableAutomatedLabeling);
    setDdaLabels(draft.ddaLabels);
    setDdaInstructions(draft.ddaInstructions);
    setAutoLabelEnabled(draft.autoLabelEnabled);
    // The recorded selection value restores verbatim, independent of the
    // capability-filtered picker's current entries (Req 2.2, 3.5).
    setAutoLabelModel(draft.autoLabelModel);
    setDetectionPrompt(draft.detectionPrompt);
    // Prompt_Override entries restore exactly as saved; a pre-feature
    // draft (field absent) restores with zero entries
    // (grounded-sam-autolabel Requirements 6.2, 6.3).
    setGroundedSamPromptOverrides(draft.groundedSamPromptOverrides ?? {});
    setFewShotEnabled(draft.fewShotEnabled);
    setDownscaleMaxEdge(draft.downscaleMaxEdge);
    setTokenBudget(draft.tokenBudget);
    // Skip_Verification_Configuration is admin-only: a non-admin restores
    // it disabled and empty, everything else applies (Req 3.6).
    if (isAdmin) {
      setSkipVerification(draft.skipVerification);
      setSkipVerificationModelId(draft.skipVerificationModelId);
      setPerLabelPrompts(draft.perLabelPrompts);
    } else {
      setSkipVerification(false);
      setSkipVerificationModelId('');
      setPerLabelPrompts({});
    }

    // Defuse the token-budget pre-fill: the compatibility effect replaces
    // the budget whenever budgetPrefillModelRef disagrees with the model,
    // so pre-marking it presents the restored budget instead (Req 3.3).
    budgetPrefillModelRef.current = draft.autoLabelModel;

    // Team re-selection (Req 3.4): the teams-loading effect nulls the
    // selection when it starts, so the draft's team id is parked and the
    // follow-up effect selects it once the list has loaded. When no
    // reload will run (the backend selection is unchanged DDA and loading
    // has settled), resolve against the already-loaded list immediately.
    const pendingTeamId = draft.selectedTeam?.teamId ?? null;
    const teamsSettled =
      draft.labelingBackend === labelingBackend &&
      labelingBackend === 'DDA' &&
      !loadingTeams;
    if (pendingTeamId !== null && teamsSettled) {
      const team = labelingTeams.find((t) => t.team_id === pendingTeamId);
      setSelectedTeam(
        team ? { label: team.team_name, value: team.team_id } : null
      );
      pendingTeamRestoreRef.current = null;
    } else {
      pendingTeamRestoreRef.current = pendingTeamId;
      setSelectedTeam(null);
    }

    // Restored example refs (Req 4.1) and the preview surface (Req 2.4,
    // 5.5, 5.7). An out-of-window Preview_Run_Reference is dropped
    // silently — no poll, no error (Req 5.5).
    setRestoredExampleRefs(draft.exampleRefs);
    restoredRefsUsecaseRef.current = draft.usecaseId;
    setPreviewSelectedKeys(draft.previewSelectedKeys);
    const resumableRun = canResumePreviewRun(draft.previewRun, Date.now())
      ? draft.previewRun
      : null;
    restoredPreviewRunRef.current = resumableRun;
    setPreviewRunRef(resumableRun);

    // Remount the preview so initialSelectedKeys and resumeRun apply
    // deterministically, then resolve the offer — saving resumes (Req 3.2).
    setPreviewRestoreNonce((n) => n + 1);
    setDraftOffer(null);
  };

  /**
   * Resolve the Restore_Offer by discarding: the stored draft is removed
   * and no other state changes (labeling-setup-session-recovery Req 3.7).
   */
  const handleDraftDiscard = () => {
    if (draftOffer === null) return;
    clearLabelingJobDraft(draftOffer.usecaseId);
    setDraftOffer(null);
  };

  /**
   * Restored_Example_Reference chips under a FileUpload field: one named,
   * individually removable chip per restored ref of the designation
   * (labeling-setup-session-recovery Requirement 4.1). Removal is
   * reflected in the next draft write through state.
   */
  const restoredExampleChips = (kind: 'good' | 'bad') =>
    restoredExampleRefs[kind].length > 0 ? (
      <SpaceBetween size="xxs">
        {restoredExampleRefs[kind].map((ref, idx) => (
          <div key={`${idx}-${ref}`} data-testid="restored-example-chip">
            <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
              <Box>{exampleRefDisplayName(ref)}</Box>
              <Button
                iconName="close"
                variant="inline-icon"
                ariaLabel={`Remove restored example ${exampleRefDisplayName(ref)}`}
                onClick={() =>
                  setRestoredExampleRefs((current) => ({
                    good:
                      kind === 'good'
                        ? current.good.filter((_, i) => i !== idx)
                        : current.good,
                    bad:
                      kind === 'bad'
                        ? current.bad.filter((_, i) => i !== idx)
                        : current.bad,
                  }))
                }
              />
            </SpaceBetween>
          </div>
        ))}
      </SpaceBetween>
    ) : null;

  /**
   * Validate the DDA labeling-setup step (Requirements 4.1-4.4, 8.1, 8.8,
   * 9.1, 9.2). Returns an error message, or null when valid.
   */
  const validateDdaSetup = (): string | null => {
    if (skipVerification && !isAdmin) {
      return 'Skip verification is restricted to administrators';
    }
    if (!skipVerification && !selectedTeam) {
      return 'A labeling team is required';
    }
    if (!isClassification) {
      if (trimmedDdaLabels.length === 0) {
        return 'Provide at least one label (up to 10)';
      }
      if (trimmedDdaLabels.length > MAX_LABELS) {
        return `The label set supports at most ${MAX_LABELS} labels`;
      }
      const tooLong = trimmedDdaLabels.find((l) => l.length > MAX_LABEL_LENGTH);
      if (tooLong) {
        return `Label "${tooLong.slice(0, 32)}…" exceeds ${MAX_LABEL_LENGTH} characters`;
      }
      if (new Set(trimmedDdaLabels).size !== trimmedDdaLabels.length) {
        return 'Label names must be distinct';
      }
    }
    if (ddaInstructions.length > MAX_INSTRUCTIONS_LENGTH) {
      return `Instructions exceed ${MAX_INSTRUCTIONS_LENGTH.toLocaleString()} characters`;
    }
    if (combinedGoodExampleCount > MAX_EXAMPLE_IMAGES) {
      return `At most ${MAX_EXAMPLE_IMAGES} good example images are allowed`;
    }
    if (combinedBadExampleCount > MAX_EXAMPLE_IMAGES) {
      return `At most ${MAX_EXAMPLE_IMAGES} bad example images are allowed`;
    }
    const badTyped = [...goodExampleFiles, ...badExampleFiles].find(
      (f) => !EXAMPLE_IMAGE_TYPES.includes(f.type)
    );
    if (badTyped) {
      return `Example image "${badTyped.name}" must be JPEG or PNG`;
    }
    if (autoLabelEnabled) {
      if (!autoLabelModel) {
        return 'Select an auto-label model';
      }
      if (!isAutoLabelModelCompatible(autoLabelModel, modality)) {
        return 'The selected auto-label model does not support this task type';
      }
      // Prompt_Override gating for the Grounded-SAM family: every entry
      // is optional, but none may exceed the length limit, judged on the
      // raw value (grounded-sam-autolabel Requirement 2.6; the backend
      // re-validates).
      if (autoLabelModel === 'grounded-sam') {
        const overlongLabel = effectiveLabelSet.find(
          (label) =>
            (groundedSamPromptOverrides[label] || '').length >
            MAX_PROMPT_OVERRIDE_LENGTH
        );
        if (overlongLabel !== undefined) {
          return `The text prompt for label "${overlongLabel}" exceeds ${MAX_PROMPT_OVERRIDE_LENGTH} characters`;
        }
      }
      // Detection_Prompt gating for the prompt-guided LLM family:
      // emptiness is judged on the trimmed prompt, length on the raw one
      // (llm-auto-labeling Requirements 2.1, 2.2; the backend re-validates).
      if (autoLabelModel.startsWith('llm:')) {
        if (!detectionPrompt.trim()) {
          return 'A detection prompt is required for prompt-guided auto-labeling';
        }
        if (detectionPrompt.length > MAX_DETECTION_PROMPT_LENGTH) {
          return `The detection prompt exceeds ${MAX_DETECTION_PROMPT_LENGTH.toLocaleString()} characters`;
        }
        // The Few_Shot_Option is meaningless without an example image
        // (llm-autolabel-prompt-tuning Req 6.2; the backend re-validates).
        if (fewShotEnabled && exampleImageCount === 0) {
          return 'At least one example image is required for the few-shot examples option';
        }
        // Token_Budget_Selection: a non-empty entry must be a whole number
        // in the accepted range. Rejection issues no creation request and
        // retains every entered value (llm-model-token-and-image-sizing
        // Req 3.3; the backend re-validates).
        if (parseTokenBudget(tokenBudget) === null) {
          return `The output token budget must be a whole number from ${TOKEN_BUDGET_RANGE_TEXT}`;
        }
      }
    }
    if (skipVerification) {
      if (!skipVerificationModelId.trim()) {
        return 'Skip verification requires a Bedrock model';
      }
      const missingPrompt = effectiveLabelSet.find(
        (label) => !(perLabelPrompts[label] || '').trim()
      );
      if (missingPrompt !== undefined) {
        return `Skip verification requires a prompt for label "${missingPrompt}"`;
      }
    }
    return null;
  };

  const validateStep = (stepIndex: number): boolean => {
    switch (stepIndex) {
      case 0: // Labeling backend selection (Requirement 1.1)
        if (!labelingBackend) {
          setError('Select a labeling backend');
          return false;
        }
        return true;
      case 1: // Job Configuration
        if (!jobName.trim()) {
          setError('Job Name is required');
          return false;
        }
        if (isDda && jobName.trim().length > MAX_DDA_JOB_NAME_LENGTH) {
          setError(`Job Name must be at most ${MAX_DDA_JOB_NAME_LENGTH} characters`);
          return false;
        }
        if (!selectedUseCase) {
          setError('Use Case is required');
          return false;
        }
        return true;
      case 2: // Dataset Selection
        if (!datasetS3Uri.trim()) {
          setError('S3 URI is required');
          return false;
        }
        if (!datasetS3Uri.startsWith('s3://')) {
          setError('S3 URI must start with s3://');
          return false;
        }
        return true;
      case 3: // Task Configuration
        if (!taskType) {
          setError('Task Type is required');
          return false;
        }
        if (!isDda) {
          const categories = labelCategories.split(',').map(c => c.trim()).filter(c => c);
          if (categories.length === 0) {
            setError('Please provide at least one label category');
            return false;
          }
        }
        return true;
      case 4: // Workforce (GroundTruth) or DDA labeling setup
        if (isDda) {
          const ddaError = validateDdaSetup();
          if (ddaError) {
            setError(ddaError);
            return false;
          }
          return true;
        }
        if (!workforceType) {
          setError('Workforce Type is required');
          return false;
        }
        if (workforceType.value === 'private' && !selectedWorkteam) {
          setError('Workteam is required for private workforce');
          return false;
        }
        return true;
      default:
        return true;
    }
  };

  const selectDatasetFromBrowser = (s3Uri: string) => {
    setDatasetS3Uri(s3Uri);
    setShowBrowseModal(false);
  };

  /**
   * Upload the good/bad example images through the portal's existing
   * presigned-PUT pattern (batch upload URLs + browser PUT, as in
   * DataManagement) before job submission, returning the stored S3 URIs
   * (Requirement 4.4). Throws on any failed upload so no job is created
   * with dangling example references.
   */
  const uploadExampleImages = useCallback(async (): Promise<{
    good: string[];
    bad: string[];
  }> => {
    if (goodExampleFiles.length === 0 && badExampleFiles.length === 0) {
      return { good: [], bad: [] };
    }
    const jobSlug = jobName.trim().toLowerCase().replace(/[^a-z0-9-]+/g, '-') || 'job';
    const basePrefix = `labeling-examples/${jobSlug}-${Date.now()}`;

    const uploadKind = async (files: File[], kind: 'good' | 'bad'): Promise<string[]> => {
      if (files.length === 0) return [];
      const named = files.map((file, idx) => ({
        file,
        filename: `${idx}-${file.name.replace(/[^A-Za-z0-9._-]/g, '_')}`,
      }));
      const response = await apiService.getBatchUploadUrls(selectedUseCase.usecase_id, {
        prefix: `${basePrefix}/${kind}`,
        files: named.map(({ file, filename }) => ({
          filename,
          content_type: file.type || 'application/octet-stream',
        })),
      });
      const uris: string[] = [];
      for (const { file, filename } of named) {
        const info = response.uploads.find((u) => u.filename === filename);
        if (!info || info.error || !info.upload_url) {
          throw new Error(
            `Could not get an upload URL for example image "${file.name}"` +
              (info?.error ? `: ${info.error}` : '')
          );
        }
        const put = await fetch(info.upload_url, {
          method: 'PUT',
          body: file,
          headers: { 'Content-Type': info.content_type },
        });
        if (!put.ok) {
          throw new Error(`Uploading example image "${file.name}" failed (HTTP ${put.status})`);
        }
        uris.push(`s3://${response.bucket}/${info.key}`);
      }
      return uris;
    };

    return {
      good: await uploadKind(goodExampleFiles, 'good'),
      bad: await uploadKind(badExampleFiles, 'bad'),
    };
  }, [goodExampleFiles, badExampleFiles, jobName, selectedUseCase]);

  /**
   * Identity of the current example file set: two uploads of the same
   * files never happen, and changing any file invalidates the cache.
   */
  const exampleFilesKey = useMemo(
    () =>
      JSON.stringify(
        [goodExampleFiles, badExampleFiles].map((files) =>
          files.map((f) => [f.name, f.size, f.type, f.lastModified])
        )
      ),
    [goodExampleFiles, badExampleFiles]
  );

  const exampleUploadCache = useRef<{
    key: string;
    uris: { good: string[]; bad: string[] };
  } | null>(null);

  /**
   * Upload the example images at most once per file set and hand back the
   * stored S3 URIs. A Preview_Run with Few_Shot_Examples and the job
   * submission therefore reference the *same* uploaded objects
   * (llm-autolabel-prompt-tuning Req 6.6): whichever runs first pays for
   * the upload, the other reuses the cached references. Editing either
   * example list invalidates the cache, so the next call uploads the new
   * set. Throws on any failed upload, naming the file.
   *
   * The returned set is the Merged_Example_Refs — Restored_Example_
   * References first, newly uploaded refs after, per designation — so the
   * preview request and the job submission consume restored refs with
   * zero changes to their builders, and only newly staged files are
   * uploaded (labeling-setup-session-recovery Req 4.3). The cache keeps
   * holding the raw upload result only.
   */
  const ensureExampleImagesUploaded = useCallback(async (): Promise<{
    good: string[];
    bad: string[];
  }> => {
    const cached = exampleUploadCache.current;
    if (cached && cached.key === exampleFilesKey) {
      return mergedExampleRefs(restoredExampleRefs, cached.uris);
    }
    const uris = await uploadExampleImages();
    exampleUploadCache.current = { key: exampleFilesKey, uris };
    return mergedExampleRefs(restoredExampleRefs, uris);
  }, [exampleFilesKey, uploadExampleImages, restoredExampleRefs]);

  /**
   * Assemble the Setup_Draft from the live Wizard_Setup_State — every
   * Requirement 2.1 field (labeling-setup-session-recovery). `exampleRefs`
   * carries the Merged_Example_Refs as of now: the restored refs still in
   * the form followed by the Current_Upload_Refs — the upload cache's URIs
   * only while its identity key matches the currently staged files, so a
   * draft never carries refs of a file set the Job_Creator has since
   * changed (Req 2.3). `savedAtMs` is stamped by writeLabelingJobDraft.
   */
  const buildDraft = useCallback((): LabelingJobDraft => {
    const cache = exampleUploadCache.current;
    const cacheCurrent = cache !== null && cache.key === exampleFilesKey;
    return {
      version: 1,
      savedAtMs: 0,
      usecaseId: selectedUseCase?.usecase_id ?? '',
      activeStepIndex,
      labelingBackend,
      jobName,
      description,
      datasetS3Uri,
      maskPrefix,
      taskTypeValue: (taskType?.value as string | undefined) ?? '',
      workforceTypeValue: (workforceType?.value as string | undefined) ?? '',
      labelCategories,
      gtInstructions: instructions,
      enableAutomatedLabeling,
      ddaLabels,
      ddaInstructions,
      selectedTeam:
        selectedTeam !== null
          ? {
              teamId: (selectedTeam.value as string | undefined) ?? '',
              teamName: selectedTeam.label ?? '',
            }
          : null,
      autoLabelEnabled,
      autoLabelModel,
      detectionPrompt,
      groundedSamPromptOverrides,
      fewShotEnabled,
      downscaleMaxEdge,
      tokenBudget,
      skipVerification,
      skipVerificationModelId,
      perLabelPrompts,
      exampleRefs: mergedExampleRefs(
        restoredExampleRefs,
        cacheCurrent ? cache.uris : { good: [], bad: [] }
      ),
      previewSelectedKeys,
      previewRun: previewRunRef,
    };
  }, [
    selectedUseCase,
    activeStepIndex,
    labelingBackend,
    jobName,
    description,
    datasetS3Uri,
    maskPrefix,
    taskType,
    workforceType,
    labelCategories,
    instructions,
    enableAutomatedLabeling,
    ddaLabels,
    ddaInstructions,
    selectedTeam,
    autoLabelEnabled,
    autoLabelModel,
    detectionPrompt,
    groundedSamPromptOverrides,
    fewShotEnabled,
    downscaleMaxEdge,
    tokenBudget,
    skipVerification,
    skipVerificationModelId,
    perLabelPrompts,
    restoredExampleRefs,
    previewSelectedKeys,
    previewRunRef,
    exampleFilesKey,
  ]);

  // Pristine_State draft, captured once per entry context: the first
  // render's state is exactly the mount's initial values, including a
  // dataset preselected via navigation state
  // (labeling-setup-session-recovery Requirement 1.2).
  const pristineDraftRef = useRef<LabelingJobDraft | null>(null);
  if (pristineDraftRef.current === null) {
    pristineDraftRef.current = buildDraft();
  }

  // Debounced Setup_Draft capture (labeling-setup-session-recovery Req
  // 1.1): a burst of changes produces one write under the selected use
  // case's key. Skipped while no use case is resolved, while a
  // Restore_Offer for this use case is unresolved (Req 1.3 — new input
  // must not clobber the draft being offered), after a successful
  // creation (Req 6.1), and while the state still equals the mount's
  // Pristine_State (Req 1.2 — visiting the page never creates a draft).
  useEffect(() => {
    if (!selectedUseCase) return;
    const usecaseId: string = selectedUseCase.usecase_id;
    if (draftOffer !== null && draftOffer.usecaseId === usecaseId) return;
    if (draftClearedRef.current) return;
    const timer = window.setTimeout(() => {
      if (draftClearedRef.current) return;
      const draft = buildDraft();
      const pristine = pristineDraftRef.current;
      // The comparison ignores the stamped usecaseId: a use case
      // resolving is metadata, not Job_Creator input.
      if (
        pristine !== null &&
        draftsEquivalent(draft, { ...pristine, usecaseId: draft.usecaseId })
      ) {
        return;
      }
      writeLabelingJobDraft(usecaseId, draft);
    }, DRAFT_SAVE_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [buildDraft, selectedUseCase, draftOffer]);

  const handleSubmit = async () => {
    setCreating(true);
    setError('');
    try {
      // Validate all steps before submission; jump to the first invalid step
      // so the user sees the relevant fields alongside the error message.
      for (let i = 0; i < 5; i++) {
        if (!validateStep(i)) {
          setActiveStepIndex(i);
          setCreating(false);
          window.scrollTo({ top: 0, behavior: 'smooth' });
          return;
        }
      }

      if (!selectedUseCase) {
        setError('Please select a use case');
        setCreating(false);
        return;
      }

      // Validate S3 URI format
      if (!datasetS3Uri.match(/^s3:\/\/[^/]+\/.+$/)) {
        setError('Invalid S3 URI format. Expected: s3://bucket/prefix');
        setCreating(false);
        return;
      }

      // Extract prefix from S3 URI (everything after bucket name)
      const prefixMatch = datasetS3Uri.match(/^s3:\/\/[^/]+\/(.+)$/);
      const prefix = prefixMatch ? prefixMatch[1] : '';

      if (labelingBackend === 'DDA') {
        // DDA branch (Requirements 1.3, 4.1-4.4, 8.1, 9.2): upload the
        // example images first, then submit with labeling_backend='DDA'.
        const exampleImages = await ensureExampleImagesUploaded();
        const promptsForLabels: Record<string, string> = {};
        if (skipVerification) {
          for (const label of effectiveLabelSet) {
            promptsForLabels[label] = (perLabelPrompts[label] || '').trim();
          }
        }

        // Token_Budget_Selection for the `llm:` family: a number when the
        // control holds one (validateDdaSetup already rejected an invalid
        // non-empty entry), `undefined` when the control is empty so the
        // key is omitted (llm-model-token-and-image-sizing Req 3.6, 3.10).
        const tokenBudgetSelection = parseTokenBudget(tokenBudget);

        await apiService.createLabelingJob({
          usecase_id: selectedUseCase.usecase_id,
          job_name: jobName.trim(),
          dataset_prefix: prefix,
          task_type: modality,
          labeling_backend: 'DDA',
          label_set: effectiveLabelSet,
          instructions: ddaInstructions || undefined,
          example_images: exampleImages,
          // Few_Shot_Option as it stands in the form at submission time,
          // regardless of what any completed Preview_Run used (Req 5.5).
          // A non-`llm:` selection always submits it disabled (Req 6.9);
          // designations and positions match the persisted shape (Req 6.4).
          few_shot:
            autoLabelEnabled && isLlmAutoLabelModel && fewShotEnabled
              ? {
                  enabled: true,
                  examples: fewShotExamplesFromRefs(exampleImages),
                }
              : { enabled: false, examples: [] },
          ...(skipVerification
            ? {
                skip_verification: true,
                bedrock_model_id: skipVerificationModelId.trim(),
                per_label_prompts: promptsForLabels,
              }
            : { team_id: selectedTeam?.value as string }),
          ...(autoLabelEnabled && autoLabelModel
            ? {
                auto_label: {
                  enabled: true,
                  model: autoLabelModel,
                  // The Detection_Prompt travels character-for-character as
                  // entered (llm-auto-labeling Requirement 2.5), only for
                  // the prompt-guided LLM family.
                  ...(autoLabelModel.startsWith('llm:')
                    ? {
                        detection_prompt: detectionPrompt,
                        // Downscale_Setting as it stands in the form; a
                        // blank select submits `null` (Downscale_Off), and
                        // `sam`/`bedrock:` selections submit nothing
                        // (llm-model-token-and-image-sizing Req 5.2, 5.7,
                        // 10.4).
                        downscale_max_edge: downscaleMaxEdge,
                        // The Token_Budget_Selection travels only when the
                        // control is non-empty — validated in range above —
                        // so an empty field omits the key and the budget
                        // resolves from the Model_Token_Limits and the
                        // default (Req 3.3, 3.6, 3.10).
                        ...(typeof tokenBudgetSelection === 'number'
                          ? { token_budget: tokenBudgetSelection }
                          : {}),
                      }
                    : {}),
                  // Prompt_Overrides for the Grounded-SAM family: exactly
                  // the entries non-empty after trimming whose label is in
                  // the submitted Label_Set, each raw value transmitted
                  // character-for-character; the key is omitted entirely
                  // when none survive, and every other family attaches no
                  // key (grounded-sam-autolabel Requirements 2.3, 2.8).
                  ...(autoLabelModel === 'grounded-sam'
                    ? (() => {
                        const entries = effectiveLabelSet
                          .filter(
                            (l) =>
                              (groundedSamPromptOverrides[l] || '').trim() !==
                              ''
                          )
                          .map(
                            (l) =>
                              [l, groundedSamPromptOverrides[l]] as [
                                string,
                                string,
                              ]
                          );
                        return entries.length > 0
                          ? { prompt_overrides: Object.fromEntries(entries) }
                          : {};
                      })()
                    : {}),
                },
              }
            : {}),
        });

        // Successful creation removes the Setup_Draft before navigating;
        // the flag stops any pending debounced write from re-creating it
        // (labeling-setup-session-recovery Requirement 6.1).
        draftClearedRef.current = true;
        clearLabelingJobDraft(selectedUseCase.usecase_id);

        navigate('/labeling');
        return;
      }

      // Ground Truth branch — submits exactly as before (Requirement 1.2),
      // now carrying the explicit labeling_backend discriminator.

      // Parse label categories
      const categories = labelCategories.split(',').map(c => c.trim()).filter(c => c);

      // Build workforce ARN from selected workteam
      if (workforceType?.value === 'private' && !selectedWorkteam) {
        setError('Please select a workteam for private workforce');
        setCreating(false);
        return;
      }
      
      const workforceArn = workforceType?.value === 'private' && selectedWorkteam
        ? `arn:aws:sagemaker:us-east-1:${selectedUseCase.account_id}:workteam/private-crowd/${selectedWorkteam.value}`
        : '';
      
      if (!workforceArn) {
        setError('Invalid workforce configuration');
        setCreating(false);
        return;
      }

      // Extract mask prefix if provided
      let maskPrefixValue: string | undefined;
      if (maskPrefix.trim()) {
        const maskPrefixMatch = maskPrefix.match(/^s3:\/\/[^/]+\/(.+)$/);
        maskPrefixValue = maskPrefixMatch ? maskPrefixMatch[1] : maskPrefix;
      }

      await apiService.createLabelingJob({
        usecase_id: selectedUseCase.usecase_id,
        job_name: jobName,
        dataset_prefix: prefix,
        task_type: taskType?.value as string,
        labeling_backend: 'GroundTruth',
        label_categories: categories,
        workforce_arn: workforceArn,
        instructions: instructions || undefined,
        num_workers_per_object: 1,
        task_time_limit: 600,
        mask_prefix: maskPrefixValue,
        enable_automated_labeling: enableAutomatedLabeling,
      });

      // Successful creation removes the Setup_Draft before navigating
      // (labeling-setup-session-recovery Requirement 6.1).
      draftClearedRef.current = true;
      clearLabelingJobDraft(selectedUseCase.usecase_id);

      navigate('/labeling');
    } catch (err) {
      // The DDA create endpoint reports each rejected parameter in a
      // `validation_errors` list alongside the generic top-level error
      // (dda-data-labeling Req 4.9). The parsed body rides along as
      // ApiError.details, so surface the specific messages instead of
      // just "Labeling job validation failed".
      const details = err instanceof ApiError ? err.details : undefined;

      const validationErrors = Array.isArray(details?.validation_errors)
        ? (details.validation_errors as Array<{ message?: string }>)
            .map((e) => e?.message)
            .filter((m): m is string => Boolean(m))
        : [];

      setError(
        validationErrors.length
          ? validationErrors.join(' • ')
          : getErrorMessage(err, 'Failed to create labeling job. Please try again.')
      );
      console.error('Failed to create labeling job:', err);
      scrollToTop();
    } finally {
      setCreating(false);
    }
  };

  // --- Step contents ------------------------------------------------------

  // Required first step: Labeling_Backend choice with exactly two options
  // (Requirement 1.1).
  const backendStep = {
    title: 'Labeling Backend',
    description: 'Choose how this job is executed',
    content: (
      <FormField
        label="Labeling backend"
        description="The engine that executes this labeling job"
        constraintText="Required"
      >
        <RadioGroup
          value={labelingBackend || null}
          onChange={({ detail }) =>
            setLabelingBackend(detail.value as LabelingBackend)
          }
          items={[
            {
              value: 'DDA',
              label: 'DDA Data Labeling System',
              description:
                'Portal-native labeling with private labeling teams, optional model-assisted pre-labeling, and direct DDA manifest output',
            },
            {
              value: 'GroundTruth',
              label: 'SageMaker Ground Truth',
              description:
                'The existing SageMaker workflow using Ground Truth work teams and the worker portal. ' +
                'Note: no longer accessible to new users as of July 30, 2026 — ' +
                'we recommend the DDA Data Labeling System instead.',
            },
          ]}
        />
      </FormField>
    ),
  };

  const jobConfigStep = {
    title: 'Job Configuration',
    description: 'Basic job information',
    content: (
      <SpaceBetween size="l">
        <FormField
          label="Job Name"
          description="A unique name for this labeling job"
          constraintText={isDda ? 'Required, 1-63 characters' : 'Required'}
        >
          <Input
            value={jobName}
            onChange={({ detail }) => setJobName(detail.value)}
            placeholder="e.g., Defect Detection - Batch 1"
          />
        </FormField>

        <FormField
          label="Description"
          description="Optional description of this labeling job"
        >
          <Textarea
            value={description}
            onChange={({ detail }) => setDescription(detail.value)}
            placeholder="Describe the purpose of this labeling job..."
            rows={3}
          />
        </FormField>

        {useCaseIdFromUrl ? (
          <FormField
            label="Use Case"
            description="The use case this job belongs to"
          >
            <Input
              value={selectedUseCase?.name || ''}
              disabled
              readOnly
            />
          </FormField>
        ) : (
          <FormField
            label="Use Case"
            description="The use case this job belongs to"
            constraintText="Required"
          >
            <Select
              selectedOption={
                selectedUseCase
                  ? {
                      label: selectedUseCase.name,
                      value: selectedUseCase.usecase_id,
                    }
                  : null
              }
              onChange={({ detail }) => {
                const useCase = useCases.find(
                  (uc) => uc.usecase_id === detail.selectedOption.value
                );
                setSelectedUseCase(useCase);
              }}
              options={useCases.map((uc) => ({
                label: uc.name,
                value: uc.usecase_id,
              }))}
              placeholder="Select a use case"
              selectedAriaLabel="Selected"
            />
          </FormField>
        )}
      </SpaceBetween>
    ),
  };

  const datasetStep = {
    title: 'Dataset Selection',
    description: 'Choose the dataset to label',
    content: (
      <SpaceBetween size="l">
        <FormField
          label="S3 URI"
          description="The S3 location containing images to label"
          constraintText="Required"
          errorText={validateS3Uri(datasetS3Uri)}
        >
          <SpaceBetween direction="horizontal" size="xs">
            <Input
              value={datasetS3Uri}
              onChange={({ detail }) => setDatasetS3Uri(detail.value)}
              placeholder="e.g., s3://my-bucket/raw-images/production-line-1/"
            />
            <Button onClick={() => setShowBrowseModal(true)} disabled={!selectedUseCase}>
              Browse S3
            </Button>
          </SpaceBetween>
        </FormField>

        {preselectedDataset && (
          <Alert type="info">
            Dataset preselected: {preselectedDataset.image_count.toLocaleString()} images
          </Alert>
        )}
      </SpaceBetween>
    ),
  };

  const taskConfigStep = {
    title: 'Task Configuration',
    description: 'Configure the labeling task',
    content: (
      <SpaceBetween size="l">
        <FormField
          label="Task Type"
          description="The type of labeling task"
          constraintText="Required"
        >
          <Select
            selectedOption={taskType}
            onChange={({ detail }) => setTaskType(detail.selectedOption)}
            options={taskTypeOptions}
            placeholder="Select task type"
          />
        </FormField>

        {!isDda && (
          <>
            {taskType?.value === 'Segmentation' && (
              <FormField
                label="Mask Prefix (Optional)"
                description="S3 location containing segmentation masks for this task"
                errorText={validateS3Uri(maskPrefix)}
              >
                <Input
                  value={maskPrefix}
                  onChange={({ detail }) => setMaskPrefix(detail.value)}
                  placeholder="e.g., s3://my-bucket/masks/production-line-1/"
                />
              </FormField>
            )}

            <FormField
              label="Label Categories"
              description="Comma-separated list of label categories"
              constraintText="Required"
              info={
                <Box>
                  For anomaly detection, the first category should be "normal" (non-defect) 
                  and subsequent categories should be defect types. This ensures correct 
                  label encoding where 0=normal, 1+=anomaly.
                </Box>
              }
            >
              <Input
                value={labelCategories}
                onChange={({ detail }) => setLabelCategories(detail.value)}
                placeholder="e.g., normal, defect"
              />
            </FormField>
            
            {labelCategories && (
              <Alert type="info">
                <Box>
                  <strong>Label Order Preview:</strong>
                  <ul style={{ marginTop: '8px', marginBottom: 0 }}>
                    {labelCategories.split(',').map((cat, idx) => (
                      <li key={idx}>
                        <strong>{idx}</strong> = {cat.trim()}
                        {idx === 0 && ' (should be normal/non-defect)'}
                        {idx > 0 && ' (anomaly/defect)'}
                      </li>
                    ))}
                  </ul>
                </Box>
              </Alert>
            )}

            <FormField
              label="Labeling Instructions"
              description="Instructions for workers performing the labeling"
            >
              <Textarea
                value={instructions}
                onChange={({ detail }) => setInstructions(detail.value)}
                placeholder="Provide clear instructions for labeling workers..."
                rows={5}
              />
            </FormField>

            <FormField
              label="Automated labeling (optional)"
              description="Use SageMaker Ground Truth active learning to auto-label a portion of your data and reduce human labeling effort. Only supported for built-in task types."
            >
              <Checkbox
                checked={enableAutomatedLabeling}
                onChange={({ detail }) => setEnableAutomatedLabeling(detail.checked)}
              >
                Enable automated data labeling (active learning)
              </Checkbox>
            </FormField>
          </>
        )}

        {isDda && (
          <Alert type="info">
            Labels, instructions, example images, and auto-labeling for the
            DDA Data Labeling System are configured in the next step.
          </Alert>
        )}
      </SpaceBetween>
    ),
  };

  // GroundTruth workforce step — unchanged (Requirement 1.2).
  const workforceStep = {
    title: 'Workforce Configuration',
    description: 'Configure the labeling workforce',
    content: (
      <SpaceBetween size="l">
        <FormField
          label="Workforce Type"
          description="The type of workforce to use for labeling"
          constraintText="Required"
        >
          <Select
            selectedOption={workforceType}
            onChange={({ detail }) => setWorkforceType(detail.selectedOption)}
            options={workforceOptions}
          />
        </FormField>

        {workforceType?.value === 'private' && (
          <>
            <Alert type="info">
              Private workforce requires a pre-configured work team in SageMaker Ground Truth.
            </Alert>
            
            <FormField
              label="Workteam"
              description="Select the workteam to use for labeling"
              constraintText="Required"
            >
              <Select
                selectedOption={selectedWorkteam}
                onChange={({ detail }) => setSelectedWorkteam(detail.selectedOption)}
                options={workteams.map((wt) => ({
                  label: wt.name,
                  value: wt.name,
                  description: wt.description || `${wt.member_count} members`,
                }))}
                placeholder={loadingWorkteams ? 'Loading workteams...' : 'Select a workteam'}
                disabled={loadingWorkteams || workteams.length === 0}
                empty={workteams.length === 0 ? 'No workteams found. Please create a workteam in SageMaker Ground Truth.' : undefined}
                selectedAriaLabel="Selected"
              />
            </FormField>
          </>
        )}

        {workforceType?.value === 'public' && (
          <Alert type="warning">
            Public workforce (Mechanical Turk) may incur additional costs and requires
            careful review of labeled data.
          </Alert>
        )}
      </SpaceBetween>
    ),
  };

  // DDA labeling setup — replaces the Workforce step for the DDA backend
  // (Requirements 4.1-4.4, 8.1, 9.1, 9.2).
  const ddaSetupStep = {
    title: 'Labeling Setup',
    description: 'Team, labels, instructions, and auto-labeling',
    content: (
      <SpaceBetween size="l">
        <FormField
          label="Labeling team"
          description="The private labeling team this job is assigned to"
          constraintText={skipVerification ? 'Not required when skip verification is enabled' : 'Required'}
        >
          <Select
            selectedOption={selectedTeam}
            onChange={({ detail }) => setSelectedTeam(detail.selectedOption)}
            options={labelingTeams.map((team) => ({
              label: team.team_name,
              value: team.team_id,
              description: `${team.members.length} member${team.members.length === 1 ? '' : 's'}`,
            }))}
            placeholder={loadingTeams ? 'Loading teams...' : 'Select a labeling team'}
            disabled={skipVerification || loadingTeams || labelingTeams.length === 0}
            empty="No labeling teams found for this use case. Create one on the Labeling Teams page."
            selectedAriaLabel="Selected"
          />
        </FormField>

        {!taskType && (
          <Alert type="info">
            Select a task type in the previous step to configure labels and
            auto-labeling.
          </Alert>
        )}

        {isClassification ? (
          <FormField
            label="Label set"
            description="Binary classification uses a fixed label set"
          >
            <Alert type="info">
              Each image is labeled <strong>normal</strong> or <strong>anomaly</strong>.
            </Alert>
          </FormField>
        ) : taskType ? (
          <FormField
            label="Label set"
            description={`1-${MAX_LABELS} distinct class names, up to ${MAX_LABEL_LENGTH} characters each`}
            constraintText="Required"
          >
            <SpaceBetween size="xs">
              {ddaLabels.map((label, idx) => (
                <SpaceBetween key={idx} direction="horizontal" size="xs">
                  <Input
                    value={label}
                    onChange={({ detail }) =>
                      setDdaLabels((current) =>
                        current.map((l, i) => (i === idx ? detail.value : l))
                      )
                    }
                    placeholder={`Label ${idx + 1}`}
                    ariaLabel={`Label ${idx + 1}`}
                  />
                  <Button
                    iconName="remove"
                    variant="icon"
                    ariaLabel={`Remove label ${idx + 1}`}
                    disabled={ddaLabels.length === 1}
                    onClick={() =>
                      setDdaLabels((current) => current.filter((_, i) => i !== idx))
                    }
                  />
                </SpaceBetween>
              ))}
              <Button
                iconName="add-plus"
                disabled={ddaLabels.length >= MAX_LABELS}
                onClick={() => setDdaLabels((current) => [...current, ''])}
              >
                Add label
              </Button>
            </SpaceBetween>
          </FormField>
        ) : null}

        <FormField
          label="Labeling instructions"
          description="Shown to labelers beside every image"
          constraintText={`Up to ${MAX_INSTRUCTIONS_LENGTH.toLocaleString()} characters (${ddaInstructions.length.toLocaleString()} used)`}
          errorText={
            ddaInstructions.length > MAX_INSTRUCTIONS_LENGTH
              ? `Instructions exceed ${MAX_INSTRUCTIONS_LENGTH.toLocaleString()} characters`
              : undefined
          }
        >
          <Textarea
            value={ddaInstructions}
            onChange={({ detail }) => setDdaInstructions(detail.value)}
            placeholder="Explain exactly what to label and how..."
            rows={5}
          />
        </FormField>

        <FormField
          label="Good examples (optional)"
          description="Up to 10 JPEG/PNG images showing correct labeling"
          errorText={
            combinedGoodExampleCount > MAX_EXAMPLE_IMAGES
              ? `At most ${MAX_EXAMPLE_IMAGES} good example images are allowed`
              : undefined
          }
        >
          <SpaceBetween size="xs">
            <FileUpload
              value={goodExampleFiles}
              onChange={({ detail }) => setGoodExampleFiles(detail.value)}
              multiple
              accept="image/jpeg,image/png"
              showFileThumbnail
              fileErrors={exampleFileErrors(goodExampleFiles)}
              constraintText="JPEG or PNG"
              i18nStrings={fileUploadI18nStrings}
            />
            {restoredExampleChips('good')}
          </SpaceBetween>
        </FormField>

        <FormField
          label="Bad examples (optional)"
          description="Up to 10 JPEG/PNG images showing labeling mistakes to avoid"
          errorText={
            combinedBadExampleCount > MAX_EXAMPLE_IMAGES
              ? `At most ${MAX_EXAMPLE_IMAGES} bad example images are allowed`
              : undefined
          }
        >
          <SpaceBetween size="xs">
            <FileUpload
              value={badExampleFiles}
              onChange={({ detail }) => setBadExampleFiles(detail.value)}
              multiple
              accept="image/jpeg,image/png"
              showFileThumbnail
              fileErrors={exampleFileErrors(badExampleFiles)}
              constraintText="JPEG or PNG"
              i18nStrings={fileUploadI18nStrings}
            />
            {restoredExampleChips('bad')}
          </SpaceBetween>
        </FormField>

        <FormField
          label="Model-assisted pre-labeling"
          description="Pre-label each image so labelers only approve or correct"
        >
          <Toggle
            checked={autoLabelEnabled}
            onChange={({ detail }) => setAutoLabelEnabled(detail.checked)}
          >
            Enable auto-labeling assist
          </Toggle>
        </FormField>

        {autoLabelEnabled && (
          <FormField
            label="Auto-label model"
            description="SAM supports segmentation and object detection; Bedrock models support classification and object detection; prompt-guided LLM models support all three; Grounded-SAM turns label names into text prompts for segmentation and object detection"
            constraintText="Required"
          >
            <Select
              selectedOption={
                flatAutoLabelOptions.find((o) => o.value === autoLabelModel) ||
                null
              }
              onChange={({ detail }) =>
                setAutoLabelModel((detail.selectedOption.value as string) || '')
              }
              options={autoLabelOptions}
              placeholder={
                taskType
                  ? 'Select an auto-label model'
                  : 'Select a task type first'
              }
              disabled={!taskType || flatAutoLabelOptions.length === 0}
              empty="No compatible auto-label models for this task type"
              selectedAriaLabel="Selected"
              filteringType="auto"
              filteringAriaLabel="Search models"
              filteringPlaceholder="Search by model name or id"
              noMatch="No models match the search"
            />
            {bedrockModelsUnavailable && BEDROCK_MODALITIES.includes(modality) && (
              <Box color="text-status-inactive" padding={{ top: 'xxs' }}>
                Bedrock model options could not be loaded.
              </Box>
            )}
            {bedrockModelsUnavailable && LLM_MODALITIES.includes(modality) && (
              <Box padding={{ top: 'xxs' }}>
                <Box color="text-status-inactive" padding={{ bottom: 'xxs' }}>
                  The model catalog is unavailable. Enter a model identifier
                  to use prompt-guided auto-labeling.
                </Box>
                <Input
                  value={
                    isLlmAutoLabelModel
                      ? autoLabelModel.slice('llm:'.length)
                      : ''
                  }
                  onChange={({ detail }) =>
                    setAutoLabelModel(
                      detail.value ? `llm:${detail.value}` : ''
                    )
                  }
                  placeholder="e.g., us.amazon.nova-pro-v1:0"
                  ariaLabel="Prompt-guided model identifier"
                />
              </Box>
            )}
            {/* All-excluded affordance: the catalog loaded but every model
                is positively known text-only, so the picker's LLM family is
                empty — offer the same Free_Text_Fallback the
                Catalog_Unavailable path offers
                (llm-model-picker-search-and-image-filter Req 2.4). */}
            {bedrockModels.length > 0 &&
              imageCapableModels.length === 0 &&
              LLM_MODALITIES.includes(modality) && (
                <Box padding={{ top: 'xxs' }}>
                  <Box
                    color="text-status-inactive"
                    padding={{ bottom: 'xxs' }}
                  >
                    No model in the catalog accepts image input. Enter a
                    model identifier to use prompt-guided auto-labeling.
                  </Box>
                  <Input
                    value={
                      isLlmAutoLabelModel
                        ? autoLabelModel.slice('llm:'.length)
                        : ''
                    }
                    onChange={({ detail }) =>
                      setAutoLabelModel(
                        detail.value ? `llm:${detail.value}` : ''
                      )
                    }
                    placeholder="e.g., us.amazon.nova-pro-v1:0"
                    ariaLabel="Prompt-guided model identifier"
                  />
                </Box>
              )}
          </FormField>
        )}

        {/* Prompt_Override entries: Grounded-SAM family only — one
            optional single-line entry per effective Label_Set label, the
            label name as the placeholder (and the default prompt when the
            entry stays empty); no other selection renders the block
            (grounded-sam-autolabel Requirements 2.1, 2.2, 2.6). */}
        {autoLabelEnabled && autoLabelModel === 'grounded-sam' && (
          <SpaceBetween size="m">
            {effectiveLabelSet.length === 0 ? (
              <Alert type="info">
                Define the label set above to enter per-label text prompts.
              </Alert>
            ) : (
              effectiveLabelSet.map((label) => (
                <FormField
                  key={label}
                  label={`Text prompt for "${label}"`}
                  description="Optional. Sent to Grounding DINO instead of the label name."
                  constraintText="Optional, at most 256 characters"
                  errorText={
                    (groundedSamPromptOverrides[label] || '').length >
                    MAX_PROMPT_OVERRIDE_LENGTH
                      ? `The text prompt for label "${label}" exceeds ${MAX_PROMPT_OVERRIDE_LENGTH} characters`
                      : undefined
                  }
                >
                  <Input
                    value={groundedSamPromptOverrides[label] || ''}
                    placeholder={label}
                    onChange={({ detail }) =>
                      setGroundedSamPromptOverrides((current) => ({
                        ...current,
                        [label]: detail.value,
                      }))
                    }
                    ariaLabel={`Text prompt for ${label}`}
                  />
                </FormField>
              ))
            )}
          </SpaceBetween>
        )}

        {autoLabelEnabled && isLlmAutoLabelModel && (
          <FormField
            label="Detection prompt"
            description="Describe the objects or defects the model should locate in each image"
            constraintText={`Required, 1-${MAX_DETECTION_PROMPT_LENGTH.toLocaleString()} characters (${detectionPrompt.length.toLocaleString()} used)`}
            errorText={
              detectionPrompt.length > MAX_DETECTION_PROMPT_LENGTH
                ? `The detection prompt exceeds ${MAX_DETECTION_PROMPT_LENGTH.toLocaleString()} characters`
                : undefined
            }
          >
            <Textarea
              value={detectionPrompt}
              onChange={({ detail }) => setDetectionPrompt(detail.value)}
              placeholder="e.g., Find surface scratches and dents on the metal panel..."
              rows={4}
              ariaLabel="Detection prompt"
            />
          </FormField>
        )}

        {/* Few_Shot_Option: prompt-guided LLM family only, disabled by
            default (Req 1.2, 6.1, 10.5). */}
        {showFewShotControls && (
          <FormField
            label="Few-shot examples"
            description="Attach the good and bad example images above to every model request as labeled examples alongside the detection prompt"
            errorText={
              fewShotEnabled && exampleImageCount === 0
                ? 'At least one example image is required for the few-shot examples option'
                : undefined
            }
          >
            <SpaceBetween size="xs">
              <Toggle
                checked={fewShotEnabled}
                onChange={({ detail }) => setFewShotEnabled(detail.checked)}
              >
                Attach example images as few-shot examples
              </Toggle>
              {fewShotEnabled && exampleImageCount > 0 && (
                <Box color="text-status-inactive">
                  {/* Attach/omit counts for the selected model's image
                      limit, recomputed on every model or example change
                      (Req 7.5). */}
                  {fewShotAttachedCount} of {exampleImageCount} example
                  {exampleImageCount === 1 ? '' : 's'} will be attached
                  {fewShotOmittedCount > 0
                    ? `, ${fewShotOmittedCount} omitted`
                    : ', 0 omitted'}{' '}
                  (this model accepts {selectedModelImageLimit} image
                  {selectedModelImageLimit === 1 ? '' : 's'} per request,
                  one reserved for the dataset image).
                </Box>
              )}
            </SpaceBetween>
          </FormField>
        )}

        {/* Prompt_Tuning_Preview: offered inside the creation flow for the
            prompt-guided LLM family only, so `sam`, `bedrock:` and a cleared
            selection render nothing new (Req 1.1, 1.2, 10.5). The component
            is fed entirely from wizard state and never writes back to it, so
            the job stays submittable whether or not a Preview_Run has been
            started, and prompt/model/few-shot edits flow straight into the
            next run (Req 1.5, 5.1). */}
        {autoLabelEnabled && isLlmAutoLabelModel && (
          <PromptTuningPreview
            /* A restore bumps the nonce so the preview remounts with the
               restored Sample_Selection and resumeRun applied
               deterministically (labeling-setup-session-recovery). */
            key={previewRestoreNonce}
            usecaseId={selectedUseCase?.usecase_id || ''}
            datasetPrefix={datasetPrefix}
            model={autoLabelModel}
            detectionPrompt={detectionPrompt}
            taskType={modality as LabelingModality}
            labelSet={effectiveLabelSet}
            fewShotEnabled={fewShotEnabled}
            goodExampleCount={combinedGoodExampleCount}
            badExampleCount={combinedBadExampleCount}
            ensureExampleImagesUploaded={ensureExampleImagesUploaded}
            /* The Downscale_Setting and Token_Budget_Selection live in
               wizard state so the values driving the Preview_Runs are the
               ones job submission persists (llm-model-token-and-image-sizing
               Req 3.6, 5.7). The preview's controls write back through the
               change callbacks. */
            downscaleMaxEdge={downscaleMaxEdge}
            tokenBudget={tokenBudget}
            onDownscaleMaxEdgeChange={setDownscaleMaxEdge}
            onTokenBudgetChange={setTokenBudget}
            /* Sample_Selection and Preview_Run_Reference mirrors for the
               Setup_Draft; a new run replaces the persisted reference and
               retires the resumed one (labeling-setup-session-recovery
               Req 2.4, 5.6, 5.7). */
            initialSelectedKeys={previewSelectedKeys}
            onSelectedKeysChange={setPreviewSelectedKeys}
            resumeRun={restoredPreviewRunRef.current}
            onRunStarted={(ref) => {
              setPreviewRunRef(ref);
              restoredPreviewRunRef.current = null;
            }}
          />
        )}

        {isAdmin && (
          <SpaceBetween size="m">
            <FormField
              label="Skip verification (admin only)"
              description="A Bedrock model labels every image with your per-label prompts; no labeler tasks are created and you review all results at the end"
            >
              <Toggle
                checked={skipVerification}
                onChange={({ detail }) => {
                  setSkipVerification(detail.checked);
                  if (detail.checked) setSelectedTeam(null);
                }}
              >
                Enable skip verification
              </Toggle>
            </FormField>

            {skipVerification && (
              <>
                <FormField
                  label="Bedrock model"
                  description="The Bedrock model that auto-labels the dataset"
                  constraintText="Required"
                >
                  {bedrockModels.length > 0 ? (
                    <Select
                      selectedOption={
                        bedrockModels
                          .map((m) => ({ label: m.label, value: m.id }))
                          .find((o) => o.value === skipVerificationModelId) ||
                        (skipVerificationModelId
                          ? { label: skipVerificationModelId, value: skipVerificationModelId }
                          : null)
                      }
                      onChange={({ detail }) =>
                        setSkipVerificationModelId((detail.selectedOption.value as string) || '')
                      }
                      options={bedrockModels.map((m) => ({ label: m.label, value: m.id }))}
                      placeholder="Select a Bedrock model"
                      selectedAriaLabel="Selected"
                    />
                  ) : (
                    <Input
                      value={skipVerificationModelId}
                      onChange={({ detail }) => setSkipVerificationModelId(detail.value)}
                      placeholder="e.g., anthropic.claude-3-5-sonnet-20241022-v2:0"
                    />
                  )}
                </FormField>

                {effectiveLabelSet.length === 0 ? (
                  <Alert type="info">
                    Define the label set above to enter per-label prompts.
                  </Alert>
                ) : (
                  effectiveLabelSet.map((label) => (
                    <FormField
                      key={label}
                      label={`Prompt for "${label}"`}
                      description="Sent to the Bedrock model to guide auto-labeling for this label"
                      constraintText="Required"
                    >
                      <Textarea
                        value={perLabelPrompts[label] || ''}
                        onChange={({ detail }) =>
                          setPerLabelPrompts((current) => ({
                            ...current,
                            [label]: detail.value,
                          }))
                        }
                        rows={2}
                        placeholder={`Describe what qualifies an image region as "${label}"...`}
                      />
                    </FormField>
                  ))
                )}
              </>
            )}
          </SpaceBetween>
        )}
      </SpaceBetween>
    ),
  };

  const reviewStep = {
    title: 'Review and Create',
    description: 'Review your configuration',
    content: (
      <SpaceBetween size="l">
        <Box variant="h3">Job Configuration</Box>
        <Box>
          <Box variant="awsui-key-label">Labeling Backend</Box>
          <Box>
            {labelingBackend === 'DDA'
              ? 'DDA Data Labeling System'
              : labelingBackend === 'GroundTruth'
                ? 'SageMaker Ground Truth'
                : '-'}
          </Box>
        </Box>
        <Box>
          <Box variant="awsui-key-label">Job Name</Box>
          <Box>{jobName || '-'}</Box>
        </Box>
        <Box>
          <Box variant="awsui-key-label">Description</Box>
          <Box>{description || '-'}</Box>
        </Box>

        <Box variant="h3">Dataset</Box>
        <Box>
          <Box variant="awsui-key-label">Source Images</Box>
          <Box>{datasetS3Uri}</Box>
        </Box>
        <Box>
          <Box variant="awsui-key-label">Labeling Output</Box>
          <Box>
            {selectedUseCase?.s3_bucket 
              ? `s3://${selectedUseCase.s3_bucket}/labeled/` 
              : <Alert type="error">Output bucket not configured. Please update the UseCase settings to add an S3 bucket for labeling outputs.</Alert>
            }
          </Box>
        </Box>

        <Box variant="h3">Task Configuration</Box>
        <Box>
          <Box variant="awsui-key-label">Task Type</Box>
          <Box>{taskType?.label || '-'}</Box>
        </Box>
        {isDda ? (
          <Box>
            <Box variant="awsui-key-label">Label Set</Box>
            <Box>{effectiveLabelSet.length > 0 ? effectiveLabelSet.join(', ') : '-'}</Box>
          </Box>
        ) : (
          <>
            <Box>
              <Box variant="awsui-key-label">Label Categories</Box>
              <Box>{labelCategories || '-'}</Box>
            </Box>
            <Box>
              <Box variant="awsui-key-label">Automated Labeling</Box>
              <Box>{enableAutomatedLabeling ? 'Enabled' : 'Disabled'}</Box>
            </Box>
          </>
        )}

        {isDda ? (
          <>
            <Box variant="h3">Labeling Setup</Box>
            <Box>
              <Box variant="awsui-key-label">Labeling Team</Box>
              <Box>
                {skipVerification
                  ? 'Not required (skip verification)'
                  : selectedTeam?.label || '-'}
              </Box>
            </Box>
            <Box>
              <Box variant="awsui-key-label">Instructions</Box>
              <Box>
                {ddaInstructions
                  ? `${ddaInstructions.length.toLocaleString()} characters`
                  : '-'}
              </Box>
            </Box>
            <Box>
              <Box variant="awsui-key-label">Example Images</Box>
              <Box>
                {combinedGoodExampleCount} good, {combinedBadExampleCount} bad
              </Box>
            </Box>
            <Box>
              <Box variant="awsui-key-label">Auto-Labeling</Box>
              <Box>
                {autoLabelEnabled && autoLabelModel
                  ? autoLabelModel === 'sam'
                    ? 'Segment Anything (SAM)'
                    : autoLabelModel.startsWith('llm:')
                      ? `Prompt-guided: ${autoLabelModel.slice('llm:'.length)}`
                      : autoLabelModel.replace(/^bedrock:/, 'Bedrock: ')
                  : 'Disabled'}
              </Box>
            </Box>
            {autoLabelEnabled && isLlmAutoLabelModel && (
              <>
                <Box>
                  <Box variant="awsui-key-label">Detection Prompt</Box>
                  <Box>
                    <span style={{ whiteSpace: 'pre-wrap' }}>
                      {detectionPrompt || '-'}
                    </span>
                  </Box>
                </Box>
                <Box>
                  <Box variant="awsui-key-label">Few-Shot Examples</Box>
                  <Box>
                    {fewShotEnabled
                      ? `Enabled (${fewShotAttachedCount} attached, ${fewShotOmittedCount} omitted)`
                      : 'Disabled'}
                  </Box>
                </Box>
              </>
            )}
            <Box>
              <Box variant="awsui-key-label">Skip Verification</Box>
              <Box>
                {skipVerification
                  ? `Enabled (${skipVerificationModelId || 'no model selected'})`
                  : 'Disabled'}
              </Box>
            </Box>
          </>
        ) : (
          <>
            <Box variant="h3">Workforce</Box>
            <Box>
              <Box variant="awsui-key-label">Workforce Type</Box>
              <Box>{workforceType?.label || '-'}</Box>
            </Box>
            {workforceType?.value === 'private' && (
              <Box>
                <Box variant="awsui-key-label">Workteam</Box>
                <Box>{selectedWorkteam?.label || '-'}</Box>
              </Box>
            )}
          </>
        )}

        {(!labelingBackend || !jobName || !datasetS3Uri || !taskType ||
          (!isDda && !labelCategories)) && (
          <Alert type="warning">
            Please complete all required fields before creating the job.
          </Alert>
        )}
      </SpaceBetween>
    ),
    isOptional: false,
  };

  return (
    <Container
      header={
        <Header variant="h1" description="Create a new labeling job">
          Create Labeling Job
        </Header>
      }
    >
      <SpaceBetween size="l">
        {/* Restore_Offer: shown while a Setup_Draft exists for the
            resolved use case, with exactly the two actions Restore and
            Discard; not dismissible (labeling-setup-session-recovery
            Requirements 3.1, 3.7). */}
        {draftOffer !== null &&
          draftOffer.usecaseId === selectedUseCase?.usecase_id && (
            <Alert
              type="info"
              header="Restore your saved labeling job setup?"
              data-testid="draft-restore-offer"
              action={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    data-testid="draft-restore-button"
                    onClick={() => applyDraftRestore(draftOffer.draft)}
                  >
                    Restore draft
                  </Button>
                  <Button
                    data-testid="draft-discard-button"
                    onClick={handleDraftDiscard}
                  >
                    Discard
                  </Button>
                </SpaceBetween>
              }
            >
              A setup draft for this use case was saved{' '}
              {new Date(draftOffer.draft.savedAtMs).toLocaleString()}.
              Restore it to continue where you left off, or discard it.
            </Alert>
          )}
        {error && (
          <Alert
            type="error"
            header="Couldn't create the labeling job"
            dismissible
            onDismiss={() => setError('')}
          >
            {error}
          </Alert>
        )}
      <Wizard
        i18nStrings={{
          stepNumberLabel: (stepNumber) => `Step ${stepNumber}`,
          collapsedStepsLabel: (stepNumber, stepsCount) =>
            `Step ${stepNumber} of ${stepsCount}`,
          skipToButtonLabel: (step) => `Skip to ${step.title}`,
          navigationAriaLabel: 'Steps',
          cancelButton: 'Cancel',
          previousButton: 'Previous',
          nextButton: 'Next',
          submitButton: 'Create Job',
          optional: 'optional',
        }}
        onNavigate={({ detail }) => {
          // Validate current step before allowing navigation
          if (detail.requestedStepIndex > activeStepIndex) {
            // Moving forward - validate current step
            if (!validateStep(activeStepIndex)) {
              return; // Don't navigate if validation fails
            }
          }
          setActiveStepIndex(detail.requestedStepIndex);
          setError(''); // Clear error when navigating
        }}
        onCancel={() => navigate('/labeling')}
        onSubmit={handleSubmit}
        activeStepIndex={activeStepIndex}
        isLoadingNextStep={creating}
        steps={[
          backendStep,
          jobConfigStep,
          datasetStep,
          taskConfigStep,
          // The DDA branch replaces the Workforce step with the labeling
          // setup step (team, labels, instructions, examples, auto-label,
          // skip verification).
          isDda ? ddaSetupStep : workforceStep,
          reviewStep,
        ]}
      />

      {/* S3 Browser Modal for dataset selection (folder-selection mode) */}
      <S3Browser
        visible={showBrowseModal}
        onDismiss={() => setShowBrowseModal(false)}
        usecaseId={selectedUseCase?.usecase_id || ''}
        onSelectFolder={selectDatasetFromBrowser}
        title="Select Dataset Folder"
        selectButtonText="Select Folder"
      />
      </SpaceBetween>
    </Container>
  );
}
