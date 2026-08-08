/**
 * Component tests for the Workflow_Builder Code_Assistant integration
 * (custom-node-code-assist task 7.3, Requirements 1.1, 1.2, 3.1, 3.5,
 * 3.6, 3.7, 3.8, 6.5).
 *
 * The assistant renders beside the `code` editor for both custom
 * Python node types and for no other node type (1.1, 1.2); it is
 * omitted entirely for Viewer/Operator roles (6.5). The 750 ms-debounced
 * Import_Analyzer runs one derivation pass after a code change and
 * writes the reconciled `requirements` parameter (3.1, 3.5) — and
 * writes nothing when the reconciled text is unchanged. Accepted
 * generated code flows through the same channel and triggers derivation
 * of the imported library's distribution (3.8). The requirements
 * control stays an editable Textarea (3.6) with read-only "derived" and
 * "verify package name" badge annotations (3.6, 3.7), and manual pins
 * survive a derivation pass verbatim (3.5).
 *
 * The CodeAssistPanel's own flows (review/accept/reject/errors) are
 * covered by CodeAssistPanel.test.tsx; the importAnalyzer's derivation
 * rules by its property tests.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import NodeConfigPanel from './NodeConfigPanel';
import { WORKFLOW_NODE_TYPE, type BuilderNode } from './builderGraph';
import {
  deriveRequirements,
  extractImports,
  reconcileRequirements,
} from './importAnalyzer';
import type { UserRole } from '../../types';
import type { JsonValue, NodeTypeDescriptor } from './types';

const { listModels, codeAssist, useUsecaseMock } = vi.hoisted(() => ({
  listModels: vi.fn(),
  codeAssist: vi.fn(),
  useUsecaseMock: vi.fn(),
}));

vi.mock('../../services/api', () => {
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
  return { ApiError, apiService: { listModels, codeAssist } };
});

vi.mock('../../contexts/UsecaseContext', () => ({
  useUsecase: useUsecaseMock,
}));

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------

const CUSTOM_PYTHON: NodeTypeDescriptor = {
  typeId: 'custom_python',
  category: 'post_processing',
  displayName: 'Custom Python',
  inputs: [{ name: 'in', portType: 'VideoFrames' }],
  outputs: [{ name: 'out', portType: 'VideoFrames' }],
  parameters: [
    { name: 'code', paramType: 'code', required: true, default: null, constraints: {} },
    { name: 'requirements', paramType: 'string', required: false, default: '', constraints: {} },
  ],
  mappings: [],
  hardwareDependent: false,
};

const CUSTOM_PYTHON_PREPROCESS: NodeTypeDescriptor = {
  typeId: 'custom_python_preprocess',
  category: 'preprocessing',
  displayName: 'Custom Python (Frames)',
  inputs: [{ name: 'in', portType: 'VideoFrames' }],
  outputs: [{ name: 'out', portType: 'VideoFrames' }],
  parameters: [
    { name: 'code', paramType: 'code', required: true, default: null, constraints: {} },
    { name: 'requirements', paramType: 'string', required: false, default: '', constraints: {} },
  ],
  mappings: [],
  hardwareDependent: false,
};

const CUSTOM_PYTHON_SOURCE: NodeTypeDescriptor = {
  typeId: 'custom_python_source',
  category: 'input',
  displayName: 'Custom Python (Source)',
  inputs: [{ name: 'activation', portType: 'EventSignal' }],
  outputs: [{ name: 'out', portType: 'VideoFrames' }],
  parameters: [
    { name: 'code', paramType: 'code', required: true, default: null, constraints: {} },
    { name: 'requirements', paramType: 'string', required: false, default: '', constraints: {} },
    {
      name: 'allowed_uri_prefixes',
      paramType: 'string',
      required: false,
      default: '',
      constraints: {},
    },
  ],
  mappings: [],
  hardwareDependent: true,
};

/** A non-custom-Python node type: no assistant, no Import_Analyzer. */
const CAMERA: NodeTypeDescriptor = {
  typeId: 'camera_source',
  category: 'input',
  displayName: 'Camera source',
  inputs: [],
  outputs: [{ name: 'out', portType: 'VideoFrames' }],
  parameters: [
    { name: 'code', paramType: 'code', required: false, default: null, constraints: {} },
  ],
  mappings: [],
  hardwareDependent: true,
};

function builderNode(
  descriptor: NodeTypeDescriptor,
  parameters: Record<string, JsonValue> = {}
): BuilderNode {
  return {
    id: `${descriptor.typeId}_1`,
    type: WORKFLOW_NODE_TYPE,
    position: { x: 0, y: 0 },
    data: { descriptor, parameters, validationMessages: [] },
  };
}

/** The assistant's prompt textarea (accessible name = FormField label). */
const assistantPrompt = () => screen.queryByRole('textbox', { name: 'Code assistant' });

/** The reconciled text a code sample derives over a current requirements text. */
function expectedReconciled(code: string, currentRequirements: string): string {
  const scan = extractImports(code);
  if (!scan.ok) {
    throw new Error('fixture code must be scannable');
  }
  return reconcileRequirements(currentRequirements, deriveRequirements(scan.imports));
}

const CV2_CODE =
  'import cv2\n\ndef process_frame(frame, metadata):\n    return cv2.GaussianBlur(frame, (5, 5), 0)\n';

beforeEach(() => {
  vi.clearAllMocks();
  listModels.mockResolvedValue({ models: [], count: 0, usecase_id: 'uc-1' });
  useUsecaseMock.mockReturnValue({
    selectedUsecaseId: 'uc-1',
    setSelectedUsecaseId: vi.fn(),
  });
});

// --------------------------------------------------------------------------
// Assistant presence per node type and role (1.1, 1.2, 6.5)
// --------------------------------------------------------------------------

describe('Code_Assistant presence in NodeConfigPanel', () => {
  it('renders the assistant beside the code editor of a custom_python node (1.1)', () => {
    const { container } = render(
      <NodeConfigPanel
        node={builderNode(CUSTOM_PYTHON, { code: '' })}
        onParametersChange={vi.fn()}
        role="DataScientist"
      />
    );
    // The code editor and the assistant share the same view.
    expect(container.querySelector('textarea[aria-label="code"]')).not.toBeNull();
    expect(assistantPrompt()).toBeInTheDocument();
  });

  it('renders the assistant beside the code editor of a custom_python_preprocess node (1.2)', () => {
    const { container } = render(
      <NodeConfigPanel
        node={builderNode(CUSTOM_PYTHON_PREPROCESS, { code: '' })}
        onParametersChange={vi.fn()}
        role="UseCaseAdmin"
      />
    );
    expect(container.querySelector('textarea[aria-label="code"]')).not.toBeNull();
    expect(assistantPrompt()).toBeInTheDocument();
  });

  it('renders the code editor and the assistant for a custom_python_source node (custom-python-source Requirements 9.6, 10.2)', () => {
    const { container } = render(
      <NodeConfigPanel
        node={builderNode(CUSTOM_PYTHON_SOURCE, { code: '' })}
        onParametersChange={vi.fn()}
        role="DataScientist"
      />
    );
    // Selecting the node renders a code editor for `code` and offers the
    // Code_Assistant panel on the same terms as the other Custom Python
    // node types.
    expect(container.querySelector('textarea[aria-label="code"]')).not.toBeNull();
    expect(assistantPrompt()).toBeInTheDocument();
  });

  it('renders no assistant for other node types, even beside a code parameter', () => {
    const { container } = render(
      <NodeConfigPanel
        node={builderNode(CAMERA, { code: '# not a custom python node' })}
        onParametersChange={vi.fn()}
        role="PortalAdmin"
      />
    );
    expect(container.querySelector('textarea[aria-label="code"]')).not.toBeNull();
    expect(assistantPrompt()).toBeNull();
    expect(screen.queryByText('Code assistant')).toBeNull();
  });

  it.each<UserRole>(['Viewer', 'Operator'])(
    'omits the assistant entry point for the %s role (6.5)',
    (role) => {
      const { container } = render(
        <NodeConfigPanel
          node={builderNode(CUSTOM_PYTHON, { code: '' })}
          onParametersChange={vi.fn()}
          role={role}
        />
      );
      // The editor itself is still there; only the assistant is gone.
      expect(container.querySelector('textarea[aria-label="code"]')).not.toBeNull();
      expect(assistantPrompt()).toBeNull();
      expect(screen.queryByText('Code assistant')).toBeNull();
    }
  );

  it('omits the assistant when no role is provided', () => {
    render(
      <NodeConfigPanel
        node={builderNode(CUSTOM_PYTHON, { code: '' })}
        onParametersChange={vi.fn()}
      />
    );
    expect(assistantPrompt()).toBeNull();
  });
});

// --------------------------------------------------------------------------
// Debounced Import_Analyzer (3.1, 3.5)
// --------------------------------------------------------------------------

describe('debounced Import_Analyzer', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('runs one analysis 750 ms after a code change and writes the requirements parameter (3.1, 3.5)', () => {
    const onParametersChange = vi.fn();
    const node = builderNode(CUSTOM_PYTHON_PREPROCESS, { code: '', requirements: '' });
    const { rerender } = render(
      <NodeConfigPanel node={node} onParametersChange={onParametersChange} role="DataScientist" />
    );

    // The code changes (e.g. the user typed an OpenCV import).
    const edited = builderNode(CUSTOM_PYTHON_PREPROCESS, { code: CV2_CODE, requirements: '' });
    rerender(
      <NodeConfigPanel
        node={edited}
        onParametersChange={onParametersChange}
        role="DataScientist"
      />
    );

    // Nothing fires before the debounce interval elapses…
    vi.advanceTimersByTime(749);
    expect(onParametersChange).not.toHaveBeenCalled();

    // …then exactly one derivation pass writes the reconciled list.
    vi.advanceTimersByTime(1);
    expect(onParametersChange).toHaveBeenCalledTimes(1);
    const expected = expectedReconciled(CV2_CODE, '');
    expect(expected).toContain('opencv-python-headless');
    expect(onParametersChange).toHaveBeenCalledWith('custom_python_preprocess_1', {
      code: CV2_CODE,
      requirements: expected,
    });

    // One run per change: no further writes without another code change.
    vi.advanceTimersByTime(5000);
    expect(onParametersChange).toHaveBeenCalledTimes(1);
  });

  it('derives requirements from a custom_python_source code change on the same terms (custom-python-source Requirement 9.6)', () => {
    const onParametersChange = vi.fn();
    const node = builderNode(CUSTOM_PYTHON_SOURCE, { code: '', requirements: '' });
    const { rerender } = render(
      <NodeConfigPanel node={node} onParametersChange={onParametersChange} role="DataScientist" />
    );

    const sourceCode =
      'import cv2\n\ndef produce_frame(context):\n    return cv2.imread("/aws_dda/reference.png")\n';
    rerender(
      <NodeConfigPanel
        node={builderNode(CUSTOM_PYTHON_SOURCE, { code: sourceCode, requirements: '' })}
        onParametersChange={onParametersChange}
        role="DataScientist"
      />
    );

    vi.advanceTimersByTime(750);
    const expected = expectedReconciled(sourceCode, '');
    expect(expected).toContain('opencv-python-headless');
    expect(onParametersChange).toHaveBeenCalledWith('custom_python_source_1', {
      code: sourceCode,
      requirements: expected,
    });
  });

  it('writes nothing when the reconciled text equals the current requirements', () => {
    const onParametersChange = vi.fn();
    // Requirements already at the derivation fixed point for this code.
    const settled = expectedReconciled(CV2_CODE, '');
    const node = builderNode(CUSTOM_PYTHON_PREPROCESS, {
      code: CV2_CODE,
      requirements: settled,
    });
    render(
      <NodeConfigPanel node={node} onParametersChange={onParametersChange} role="DataScientist" />
    );

    vi.advanceTimersByTime(2000);
    expect(onParametersChange).not.toHaveBeenCalled();
  });

  it('keeps manual pins and comments verbatim through a derivation pass (3.5)', () => {
    const onParametersChange = vi.fn();
    const manual = 'numpy==1.24.0\n# pinned for reproducibility';
    const code =
      'import cv2\nimport numpy\n\ndef process_frame(frame, metadata):\n    return numpy.abs(frame)\n';
    const node = builderNode(CUSTOM_PYTHON_PREPROCESS, { code, requirements: manual });
    render(
      <NodeConfigPanel node={node} onParametersChange={onParametersChange} role="DataScientist" />
    );

    vi.advanceTimersByTime(750);
    expect(onParametersChange).toHaveBeenCalledTimes(1);
    const written = onParametersChange.mock.calls[0][1].requirements as string;
    expect(written).toBe(expectedReconciled(code, manual));

    const lines = written.split('\n');
    // Manual lines survive verbatim, in order, at the top.
    expect(lines[0]).toBe('numpy==1.24.0');
    expect(lines[1]).toBe('# pinned for reproducibility');
    // cv2 derives its mapped distribution…
    expect(lines.some((line) => line.startsWith('opencv-python-headless'))).toBe(true);
    // …while the manually pinned numpy gains no derived duplicate.
    expect(lines.filter((line) => line.includes('numpy'))).toEqual(['numpy==1.24.0']);
  });
});

// --------------------------------------------------------------------------
// Accepted generated code triggers derivation (3.8)
// --------------------------------------------------------------------------

describe('accepted generated code', () => {
  const GENERATED_CODE =
    'import numpy\n\ndef process_frame(frame, metadata):\n    return numpy.flipud(frame)\n';

  it('flows through onParametersChange and its imports derive requirements (3.8)', async () => {
    codeAssist.mockResolvedValue({
      code: GENERATED_CODE,
      notes: 'Flips the frame vertically.',
      model_id: 'us.anthropic.test-model',
      contract: 'process_frame',
    });
    const onParametersChange = vi.fn();
    const node = builderNode(CUSTOM_PYTHON_PREPROCESS, { code: '', requirements: '' });
    const { rerender } = render(
      <NodeConfigPanel node={node} onParametersChange={onParametersChange} role="DataScientist" />
    );

    // Prompt, generate, review, accept.
    fireEvent.change(assistantPrompt()!, { target: { value: 'Flip the frame vertically' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate' }));
    await screen.findByLabelText('Generated code for review');
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));

    // Accepted code lands in the node's parameters through the same
    // channel manual edits use.
    expect(codeAssist).toHaveBeenCalledTimes(1);
    expect(codeAssist).toHaveBeenCalledWith(
      expect.objectContaining({
        usecase_id: 'uc-1',
        surface: 'workflow-builder',
        contract: 'process_frame',
        prompt: 'Flip the frame vertically',
      })
    );
    expect(onParametersChange).toHaveBeenCalledWith('custom_python_preprocess_1', {
      code: GENERATED_CODE,
      requirements: '',
    });

    // The owning page applies the change; the analyzer then derives the
    // generated import's distribution into the requirements parameter.
    onParametersChange.mockClear();
    const accepted = builderNode(CUSTOM_PYTHON_PREPROCESS, {
      code: GENERATED_CODE,
      requirements: '',
    });
    rerender(
      <NodeConfigPanel
        node={accepted}
        onParametersChange={onParametersChange}
        role="DataScientist"
      />
    );

    const expected = expectedReconciled(GENERATED_CODE, '');
    expect(expected).toContain('numpy');
    await waitFor(
      () =>
        expect(onParametersChange).toHaveBeenCalledWith('custom_python_preprocess_1', {
          code: GENERATED_CODE,
          requirements: expected,
        }),
      { timeout: 3000 }
    );
  });
});

// --------------------------------------------------------------------------
// Requirements Textarea and badge annotations (3.6, 3.7)
// --------------------------------------------------------------------------

describe('requirements annotations', () => {
  // Requirements text with one mapped derived entry, one unmapped
  // (needs-review) derived entry, and one manual pin.
  const REQUIREMENTS_TEXT = [
    'opencv-python-headless  # via code imports',
    'somefancylib  # via code imports (verify package name)',
    'numpy==1.24.0',
  ].join('\n');

  it('renders derived and needs-review badges under the editable Textarea (3.6, 3.7)', () => {
    const { container } = render(
      <NodeConfigPanel
        node={builderNode(CUSTOM_PYTHON, { code: '', requirements: REQUIREMENTS_TEXT })}
        onParametersChange={vi.fn()}
        role="DataScientist"
      />
    );

    // The populated list is shown in an editable Textarea (3.6).
    const textarea = container.querySelector('textarea[aria-label="requirements"]');
    expect(textarea).toHaveValue(REQUIREMENTS_TEXT);

    // Read-only annotation list: one row per derived entry.
    const list = screen.getByRole('list', { name: 'Derived requirements' });
    expect(list.textContent).toContain('opencv-python-headless');
    expect(list.textContent).toContain('somefancylib');
    // Manual entries carry no annotation row.
    expect(list.textContent).not.toContain('numpy==1.24.0');

    // "derived" badge per derived entry; the unmapped entry additionally
    // carries the "verify package name" warning badge (3.7).
    expect(screen.getAllByText('derived')).toHaveLength(2);
    expect(screen.getAllByText('verify package name')).toHaveLength(1);
  });

  it('accepts user edits to the requirements list (3.6)', () => {
    const onParametersChange = vi.fn();
    const { container } = render(
      <NodeConfigPanel
        node={builderNode(CUSTOM_PYTHON, { code: '', requirements: REQUIREMENTS_TEXT })}
        onParametersChange={onParametersChange}
        role="DataScientist"
      />
    );
    const textarea = container.querySelector('textarea[aria-label="requirements"]')!;
    fireEvent.change(textarea, { target: { value: 'scipy==1.11.0' } });
    expect(onParametersChange).toHaveBeenCalledWith(
      'custom_python_1',
      expect.objectContaining({ requirements: 'scipy==1.11.0' })
    );
  });

  it('renders no annotation list when the requirements hold no derived entries', () => {
    render(
      <NodeConfigPanel
        node={builderNode(CUSTOM_PYTHON, { code: '', requirements: 'numpy==1.24.0' })}
        onParametersChange={vi.fn()}
        role="DataScientist"
      />
    );
    expect(screen.queryByRole('list', { name: 'Derived requirements' })).toBeNull();
  });
});
