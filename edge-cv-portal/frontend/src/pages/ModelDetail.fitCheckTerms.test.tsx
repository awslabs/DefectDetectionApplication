/**
 * Component tests for the Fit_Check term fields on the Model Detail page
 * (jp6-vllm-kv-cache-oom-regression, task 3.4 / File 7).
 *
 * `vllm_fit_check.FitFinding` gained an ADDITIVE term breakdown
 * (`weights_bytes`, `activation_bytes`, `kv_floor_bytes`, `co_tenancy_bytes`,
 * `fraction_cap`, `images_per_prompt`, `failed_conditions`, `warnings`). The
 * frontend declares all of them OPTIONAL on `VllmFitCheckFinding` and
 * deliberately renders NO term breakdown: the finding's `message` already
 * states every term with its number, so the Engine Configuration fit-check
 * alert is unchanged. These tests pin that contract:
 *
 * - absence tolerance (the compatibility guarantee): a finding carrying only
 *   the original five fields renders exactly as today;
 * - presence tolerance: a finding carrying every new field renders the SAME
 *   alert content — the extra fields are ignored by the panel and the numbers
 *   the operator audits come from `message` (Requirements 2.1, 2.3);
 * - the soft `warnings` terms on a PASSING finding do not add the finding to
 *   the panel (the existing `!fits` filter is untouched).
 *
 * The type-level half of the contract is checked by the two fixtures below
 * being annotated `VllmFitCheckFinding`: the legacy shape must still satisfy
 * the interface (that is what "optional" buys) and the extended shape must
 * type-check field for field. `npm run build` is the assertion.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import ModelDetail from './ModelDetail';
// Type-only import: erased at runtime, so the module mock below still applies.
import type { VllmFitCheckFinding } from '../services/api';

// ------------------------------------------------------------------ mocks

const {
  getModel,
  getTrainingJob,
  getVllmEngineSpec,
  updateVllmEngineConfiguration,
} = vi.hoisted(() => ({
  getModel: vi.fn(),
  getTrainingJob: vi.fn(),
  getVllmEngineSpec: vi.fn(),
  updateVllmEngineConfiguration: vi.fn(),
}));

vi.mock('../services/api', () => {
  class ApiError extends Error {
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
  return {
    ApiError,
    apiService: {
      getModel,
      getTrainingJob,
      getVllmEngineSpec,
      updateVllmEngineConfiguration,
    },
  };
});

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: {
      user_id: 'user-1',
      email: 'user@example.com',
      username: 'tester',
      role: 'DataScientist',
      is_super_user: false,
    },
  }),
}));

vi.mock('react-router-dom', () => ({
  useParams: () => ({ modelId: 'model-123' }),
  useNavigate: () => vi.fn(),
}));

vi.mock('../components/CompilationTab', () => ({
  default: () => <div data-testid="compilation-tab" />,
}));
vi.mock('../components/vllm-publish/VllmPackagePublishSection', () => ({
  default: () => <div data-testid="vllm-publish-stub" />,
}));

// -------------------------------------------------------------- fixtures

const ENGINE_CONFIGURATION = {
  dtype: 'bfloat16',
  gpu_memory_utilization: 0.4,
  max_model_len: 4096,
  tensor_parallel_size: 1,
  enforce_eager: true,
};

const vllmRecord = {
  model_id: 'model-123',
  usecase_id: 'usecase-1',
  name: 'My LLM',
  version: '1.0',
  stage: 'candidate',
  source: 'vllm',
  training_job_id: 'training-456',
  model_type: 'vllm',
  metrics: {},
  component_arns: {},
  deployed_devices: [],
  created_by: 'tester',
  created_at: 1700000000000,
  updated_at: 1700000000000,
  engine_configuration: ENGINE_CONFIGURATION,
};

const ENGINE_SPEC = {
  description: 'Documented vLLM engine settings',
  settings: {
    dtype: {
      default: 'auto',
      type: 'string',
      accepted_values: ['auto', 'float16', 'bfloat16', 'float32'],
      description: 'Model weight data type',
    },
    gpu_memory_utilization: {
      default: 0.5,
      type: 'number',
      range: '(0, 1]',
      description: 'Fraction of GPU memory vLLM may use',
    },
    max_model_len: {
      default: 4096,
      type: 'integer',
      description: 'Maximum sequence length',
    },
    tensor_parallel_size: {
      default: 1,
      type: 'integer',
      description: 'Number of GPUs for tensor parallelism',
    },
    enforce_eager: {
      default: true,
      type: 'boolean',
      description: 'Disable CUDA graphs',
    },
  },
  source: {
    description: '',
    huggingface_model_id: { type: 'string', format: '', example: '' },
    s3_model_artifact: { type: 'string', format: '', example: '' },
  },
};

/**
 * The incident verdict's message, shaped like `vllm_fit_check`'s shared terms
 * sentence: it carries EVERY term with its number, which is why no separate
 * breakdown is rendered.
 */
const FIT_MESSAGE =
  'arm64_jp6: estimated weights 6.50 GiB + activation allowance 4.88 GiB ' +
  '(an ESTIMATE: max(2.00 GiB, 0.75 x weights) for 1 image(s) per prompt) + ' +
  'KV cache floor 1.00 GiB = 12.38 GiB required, against a 12.00 GiB budget ' +
  "(gpu_memory_utilization=0.4 of the arm64_jp6 profile's 30.00 GiB TOTAL " +
  'device memory as the engine sees it, of which co-resident consumers hold ' +
  'about 6.00 GiB; co-tenancy cap on the fraction 0.80).';

/** A finding as serialized BEFORE the additive fields existed. */
const LEGACY_FINDING: VllmFitCheckFinding = {
  arch: 'arm64_jp6',
  fits: false,
  budget_bytes: 12884901888,
  required_bytes: 13292998164,
  message: FIT_MESSAGE,
};

/** The same finding with every additive term populated. */
const EXTENDED_FINDING: VllmFitCheckFinding = {
  ...LEGACY_FINDING,
  weights_bytes: 6979321856,
  activation_bytes: 5234491392,
  kv_floor_bytes: 1073741824,
  co_tenancy_bytes: 6442450944,
  fraction_cap: 0.8,
  images_per_prompt: 1,
  failed_conditions: ['budget'],
  warnings: [],
};

/** A PASSING finding carrying only soft warnings (never listed in the panel). */
const PASSING_FINDING_WITH_WARNINGS: VllmFitCheckFinding = {
  arch: 'arm64_jp7',
  fits: true,
  budget_bytes: 64424509440,
  required_bytes: 31138512896,
  message: 'arm64_jp7: fits with a thin margin.',
  weights_bytes: 17179869184,
  activation_bytes: 12884901888,
  kv_floor_bytes: 1073741824,
  co_tenancy_bytes: 8589934592,
  fraction_cap: 0.9333,
  images_per_prompt: 1,
  failed_conditions: [],
  warnings: ['thin_margin', 'near_cap'],
};

// --------------------------------------------------------------- helpers

/** The Cloudscape Alert holding the fit-check warnings. */
function fitCheckAlert(): HTMLElement {
  const alert = createWrapper(document.body)
    .findAllAlerts()
    .find((a) =>
      a.findHeader()?.getElement().textContent?.includes('Fit check warnings')
    );
  expect(alert, 'the fit-check warning alert should render').toBeDefined();
  return alert!.getElement();
}

/**
 * Run the engine-configuration update flow with the given findings and
 * return the rendered fit-check alert's text plus an unmount handle.
 */
async function renderFitCheck(findings: VllmFitCheckFinding[]) {
  getModel.mockResolvedValue({ model: vllmRecord });
  // Clear only the call history (implementations survive mockClear) so a
  // second render inside one test waits for ITS own submit.
  updateVllmEngineConfiguration.mockClear();
  updateVllmEngineConfiguration.mockResolvedValue({
    training_id: 'training-456',
    engine_configuration: ENGINE_CONFIGURATION,
    notice: 'Engine configuration updated.',
    fit_check: {
      status: 'warnings',
      estimate: {
        total_bytes: 6979321856,
        method: 'safetensors_files',
        detail: 'sum of safetensors sizes',
      },
      findings,
    },
  });

  const { unmount } = render(<ModelDetail />);
  await screen.findByText('Engine Configuration');
  fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
  await screen.findByText('gpu memory utilization');
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(updateVllmEngineConfiguration).toHaveBeenCalled());
  await screen.findByText('Fit check warnings');

  return { text: fitCheckAlert().textContent ?? '', unmount };
}

beforeEach(() => {
  getVllmEngineSpec.mockResolvedValue(ENGINE_SPEC);
});

afterEach(() => {
  vi.clearAllMocks();
});

// ------------------------------------------------------------------ tests

describe('ModelDetail — Fit_Check term fields (Requirements 2.1, 2.3, 3.1)', () => {
  it('renders a finding WITHOUT the additive term fields exactly as today', async () => {
    await renderFitCheck([LEGACY_FINDING]);

    // The panel is the message, unchanged: header plus one line per failing
    // finding, carrying every term's number from the backend's sentence.
    expect(screen.getByText('Fit check warnings')).toBeInTheDocument();
    expect(screen.getByText(FIT_MESSAGE)).toBeInTheDocument();
    for (const term of [
      'weights 6.50 GiB',
      'activation allowance 4.88 GiB',
      'an ESTIMATE',
      'KV cache floor 1.00 GiB',
      '12.38 GiB required',
      '12.00 GiB budget',
      'co-tenancy cap on the fraction 0.80',
    ]) {
      expect(fitCheckAlert().textContent, `message states '${term}'`).toContain(
        term
      );
    }
  });

  it('renders a finding WITH every additive term field to identical content', async () => {
    const legacy = await renderFitCheck([LEGACY_FINDING]);
    legacy.unmount();

    const extended = await renderFitCheck([EXTENDED_FINDING]);

    // The additive fields change nothing in the panel: the operator audits
    // the verdict from `message`, so presence and absence render alike.
    expect(extended.text).toBe(legacy.text);
    expect(screen.getByText(FIT_MESSAGE)).toBeInTheDocument();

    // No raw byte counts or internal condition/warning tokens leak into the UI.
    for (const raw of [
      '6979321856',
      '5234491392',
      '1073741824',
      '6442450944',
      'failed_conditions',
      'thin_margin',
    ]) {
      expect(extended.text, `'${raw}' is not rendered`).not.toContain(raw);
    }
  });

  it('lists only the failing finding when a passing one carries soft warnings', async () => {
    const { text } = await renderFitCheck([
      EXTENDED_FINDING,
      PASSING_FINDING_WITH_WARNINGS,
    ]);

    expect(text).toContain(FIT_MESSAGE);
    expect(text).not.toContain(PASSING_FINDING_WITH_WARNINGS.message);
    expect(screen.queryByText(PASSING_FINDING_WITH_WARNINGS.message)).toBeNull();
  });
});
