/**
 * Component tests for the Engine Configuration section and inline edit
 * flow on the Model Detail page (vllm-sizing-and-packaging-errors,
 * task 5.3):
 *
 * - display completeness: every stored Engine_Configuration setting
 *   renders with its stored value for a vLLM record, and the section is
 *   absent for non-vLLM records (Requirement 1.3);
 * - edit submit success: the edited values are submitted through
 *   `apiService.updateVllmEngineConfiguration`, and the re-package/
 *   publish notice plus any fit-check warnings render (Requirement 2.5);
 * - validation findings from a 400 rejection render per field next to
 *   the matching inputs (Requirement 2.5).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import ModelDetail from './ModelDetail';

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

// Stub the tabs/sections that carry their own API calls and polling so
// this test exercises only the Engine Configuration section.
vi.mock('../components/CompilationTab', () => ({
  default: () => <div data-testid="compilation-tab" />,
}));
vi.mock('../components/vllm-publish/VllmPackagePublishSection', () => ({
  default: () => <div data-testid="vllm-publish-stub" />,
}));

// -------------------------------------------------------------- fixtures

/** Stored Engine_Configuration on the vLLM record (Requirement 1.3). */
const ENGINE_CONFIGURATION = {
  dtype: 'bfloat16',
  gpu_memory_utilization: 0.35,
  max_model_len: 8192,
  tensor_parallel_size: 2,
  enforce_eager: false,
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

const visionRecord = {
  ...vllmRecord,
  source: 'trained',
  model_type: 'object-detection',
  engine_configuration: undefined,
};

/** Engine-spec response driving the edit form fields (2.5). */
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

// --------------------------------------------------------------- helpers

/** The Cloudscape Container holding the Engine Configuration section. */
function engineSection(): HTMLElement {
  const container = createWrapper(document.body)
    .findAllContainers()
    .find((c) =>
      c.findHeader()?.getElement().textContent?.includes('Engine Configuration')
    );
  expect(container, 'Engine Configuration section should render').toBeDefined();
  return container!.getElement();
}

/** A form field in the edit form, located by its rendered label. */
function engineFormField(label: string) {
  const field = createWrapper(document.body)
    .findAllFormFields()
    .find((ff) => ff.findLabel()?.getElement().textContent?.trim() === label);
  expect(field, `form field '${label}' should render`).toBeDefined();
  return field!;
}

function engineFieldInput(label: string): HTMLInputElement {
  const input = engineFormField(label).getElement().querySelector('input');
  expect(input, `form field '${label}' should carry an input`).not.toBeNull();
  return input as HTMLInputElement;
}

async function renderVllmDetail() {
  getModel.mockResolvedValue({ model: vllmRecord });
  render(<ModelDetail />);
  await screen.findByText('Engine Configuration');
}

/** Open the inline edit form and wait for the spec-driven fields. */
async function openEditForm() {
  fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
  await screen.findByText('gpu memory utilization');
}

beforeEach(() => {
  getVllmEngineSpec.mockResolvedValue(ENGINE_SPEC);
});

afterEach(() => {
  vi.clearAllMocks();
});

// ------------------------------------------------------------------ tests

describe('ModelDetail — Engine Configuration display (Requirement 1.3)', () => {
  it('renders every stored engine setting with its stored value for a vLLM model', async () => {
    await renderVllmDetail();

    const section = within(engineSection());
    for (const [key, value] of Object.entries(ENGINE_CONFIGURATION)) {
      expect(section.getByText(key), `label '${key}'`).toBeInTheDocument();
      expect(
        section.getByText(String(value)),
        `value of '${key}'`
      ).toBeInTheDocument();
    }
  });

  it('renders no Engine Configuration section for a non-vLLM model', async () => {
    getModel.mockResolvedValue({ model: visionRecord });
    getTrainingJob.mockResolvedValue({
      training_id: 'training-456',
      status: 'COMPLETED',
    });
    render(<ModelDetail />);
    await screen.findByText('Model Information');

    expect(screen.queryByText('Engine Configuration')).toBeNull();
  });
});

describe('ModelDetail — engine configuration edit flow (Requirements 2.5, 3.5)', () => {
  it('submits the edited values and shows the re-package/publish notice and fit-check warnings', async () => {
    const NOTICE =
      'Engine configuration updated. The change takes effect after the model is packaged and published again.';
    const FIT_MESSAGE =
      'Estimated weights 14.3 GiB + minimum KV cache exceed the arm64_jp6 budget ' +
      '(0.85 × 30 GiB): raise gpu_memory_utilization, reduce max_model_len, or choose a smaller model.';
    updateVllmEngineConfiguration.mockResolvedValue({
      training_id: 'training-456',
      engine_configuration: { ...ENGINE_CONFIGURATION, gpu_memory_utilization: 0.85 },
      notice: NOTICE,
      fit_check: {
        status: 'warnings',
        estimate: {
          total_bytes: 15300000000,
          method: 'safetensors_index',
          detail: 'sum of safetensors sizes',
        },
        findings: [
          {
            arch: 'arm64_jp6',
            fits: false,
            budget_bytes: 27381323366,
            required_bytes: 16373741824,
            message: FIT_MESSAGE,
          },
        ],
      },
    });

    await renderVllmDetail();
    await openEditForm();

    // The form is pre-filled with the stored values (spec defaults overlaid).
    expect(engineFieldInput('gpu memory utilization').value).toBe('0.35');
    expect(engineFieldInput('max model len').value).toBe('8192');

    fireEvent.change(engineFieldInput('gpu memory utilization'), {
      target: { value: '0.85' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    // The edited configuration is submitted with JSON-typed values against
    // the record's training id.
    await waitFor(() =>
      expect(updateVllmEngineConfiguration).toHaveBeenCalledWith('training-456', {
        dtype: 'bfloat16',
        gpu_memory_utilization: 0.85,
        max_model_len: 8192,
        tensor_parallel_size: 2,
        enforce_eager: false,
      })
    );

    // The notice and the fit-check warning render (2.4-surface, 3.5).
    expect(await screen.findByText(NOTICE)).toBeInTheDocument();
    expect(screen.getByText('Fit check warnings')).toBeInTheDocument();
    expect(screen.getByText(FIT_MESSAGE)).toBeInTheDocument();

    // The display reflects the complete updated configuration.
    expect(within(engineSection()).getByText('0.85')).toBeInTheDocument();
  });

  it('renders 400 validation findings per field next to the matching inputs', async () => {
    const { ApiError } = await import('../services/api');
    const GPU_REASON = 'gpu_memory_utilization must be greater than 0 and at most 1';
    const LEN_REASON = 'max_model_len must be a positive integer';
    updateVllmEngineConfiguration.mockRejectedValue(
      new ApiError('Engine configuration validation failed', 400, 'VALIDATION_FAILED', {
        findings: [
          {
            field: 'engine_configuration.gpu_memory_utilization',
            value: 5,
            reason: GPU_REASON,
          },
          {
            field: 'engine_configuration.max_model_len',
            value: 0,
            reason: LEN_REASON,
          },
        ],
      })
    );

    await renderVllmDetail();
    await openEditForm();

    fireEvent.change(engineFieldInput('gpu memory utilization'), {
      target: { value: '5' },
    });
    fireEvent.change(engineFieldInput('max model len'), {
      target: { value: '0' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    // Each finding renders as the error of its matching form field.
    await waitFor(() =>
      expect(
        engineFormField('gpu memory utilization').findError()?.getElement().textContent
      ).toBe(GPU_REASON)
    );
    expect(
      engineFormField('max model len').findError()?.getElement().textContent
    ).toBe(LEN_REASON);

    // The untouched fields carry no error, and the form-level summary points
    // at the highlighted fields.
    expect(engineFormField('dtype').findError()).toBeNull();
    expect(
      screen.getByText('The update failed validation. Fix the highlighted fields and retry.')
    ).toBeInTheDocument();

    // The stored display value is unchanged after cancelling the edit.
    // (Scoped to the section: hidden page modals also render Cancel buttons.)
    fireEvent.click(within(engineSection()).getByRole('button', { name: 'Cancel' }));
    expect(within(engineSection()).getByText('0.35')).toBeInTheDocument();
  });
});
