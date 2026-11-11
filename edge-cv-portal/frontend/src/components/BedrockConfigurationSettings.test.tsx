/**
 * Component tests for the Bedrock_Configuration settings section
 * (workflow-manager Requirement 10.6): the form is visible and editable
 * only for PortalAdmin, loads the stored configuration, validates the
 * timeout bound (<= 240 seconds, Requirement 10.7) client-side, and
 * offers the model identifier as a dropdown of invokable models (with a
 * free-text fallback when the model list is unavailable). Max tokens shows
 * the selected model's default until the admin enters another value; when
 * the form lacks a model's default, a model change saved with max tokens
 * unchanged leaves max_tokens out, and the form shows the configuration
 * the save returns.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import BedrockConfigurationSettings from './BedrockConfigurationSettings';

const { getBedrockConfiguration, getBedrockModels, updateBedrockConfiguration, useAuthMock } = vi.hoisted(() => ({
  getBedrockConfiguration: vi.fn(),
  getBedrockModels: vi.fn(),
  updateBedrockConfiguration: vi.fn(),
  useAuthMock: vi.fn(),
}));

vi.mock('../services/api', () => ({
  apiService: { getBedrockConfiguration, getBedrockModels, updateBedrockConfiguration },
}));

vi.mock('../contexts/AuthContext', () => ({
  useAuth: useAuthMock,
}));

const STORED_CONFIG = {
  model_id: 'anthropic.claude-3-5-sonnet-20240620-v1:0',
  region: 'us-east-1',
  max_tokens: 4096,
  temperature: 0.2,
  top_p: 0.9,
  timeout_seconds: 60,
};

const MODEL_OPTIONS = [
  { id: 'us.anthropic.claude-sonnet-4-5-20250929-v1:0', label: 'US Anthropic Claude Sonnet 4.5' },
  { id: 'amazon.titan-text-express-v1', label: 'Titan Text Express' },
];

beforeEach(() => {
  vi.clearAllMocks();
  getBedrockConfiguration.mockResolvedValue({
    bedrock_configuration: STORED_CONFIG,
    defaults: {},
    max_timeout_seconds: 240,
  });
  getBedrockModels.mockResolvedValue({
    models: MODEL_OPTIONS,
    region: 'us-east-1',
  });
  updateBedrockConfiguration.mockResolvedValue({
    message: 'ok',
    bedrock_configuration: STORED_CONFIG,
  });
});

function setAuthRole(role: string | null) {
  useAuthMock.mockReturnValue({ user: role ? { role } : null });
}

describe('BedrockConfigurationSettings', () => {
  it('shows the configuration form with stored values for PortalAdmin (Requirement 10.6)', async () => {
    setAuthRole('PortalAdmin');
    const { container } = render(<BedrockConfigurationSettings />);

    // The model dropdown shows the stored model id even though it is not
    // in the fetched model list (surfaced as a selectable option).
    await waitFor(() => {
      const select = createWrapper(container).findSelect();
      expect(select).not.toBeNull();
      expect(select!.findTrigger().getElement()).toHaveTextContent(STORED_CONFIG.model_id);
    });
    expect(screen.getByLabelText('Region')).toHaveValue(STORED_CONFIG.region);
    expect(screen.getByLabelText('Max tokens')).toHaveValue(4096);
    expect(screen.getByLabelText('Temperature')).toHaveValue(0.2);
    expect(screen.getByLabelText('Top P')).toHaveValue(0.9);
    expect(screen.getByLabelText('Timeout seconds')).toHaveValue(60);
    expect(screen.getByRole('button', { name: 'Save Configuration' })).toBeInTheDocument();
  });

  it.each(['Viewer', 'Operator', 'DataScientist', 'UseCaseAdmin'])(
    'shows an access notice instead of the form for %s (Requirement 10.6)',
    (role) => {
      setAuthRole(role);
      render(<BedrockConfigurationSettings />);

      expect(screen.getByText('Portal Admin access required')).toBeInTheDocument();
      expect(screen.queryByLabelText('Model identifier')).toBeNull();
      expect(getBedrockConfiguration).not.toHaveBeenCalled();
      expect(getBedrockModels).not.toHaveBeenCalled();
    },
  );

  it('offers the fetched models in the dropdown and saves the selected one', async () => {
    setAuthRole('PortalAdmin');
    const { container } = render(<BedrockConfigurationSettings />);

    await waitFor(() => {
      const wrapper = createWrapper(container).findSelect();
      expect(wrapper).not.toBeNull();
      expect(wrapper!.findTrigger().getElement()).toHaveTextContent(STORED_CONFIG.model_id);
    });

    const select = createWrapper(container).findSelect()!;
    await waitFor(() => {
      select.openDropdown();
      // Stored (custom) model id + the two fetched options.
      expect(select.findDropdown().findOptions()).toHaveLength(3);
    });
    expect(select.findDropdown().getElement().textContent).toContain('US Anthropic Claude Sonnet 4.5');
    expect(select.findDropdown().getElement().textContent).toContain('Titan Text Express');
    expect(select.findDropdown().getElement().textContent).toContain(STORED_CONFIG.model_id);

    select.selectOptionByValue('us.anthropic.claude-sonnet-4-5-20250929-v1:0');
    fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

    await waitFor(() => {
      expect(updateBedrockConfiguration).toHaveBeenCalledWith(
        expect.objectContaining({
          model_id: 'us.anthropic.claude-sonnet-4-5-20250929-v1:0',
        }),
      );
    });
  });

  it('falls back to a free-text model id input when the model list fails to load', async () => {
    setAuthRole('PortalAdmin');
    getBedrockModels.mockRejectedValue(new Error('boom'));
    const { container } = render(<BedrockConfigurationSettings />);

    await waitFor(() => {
      expect(screen.getByLabelText('Model identifier')).toHaveValue(STORED_CONFIG.model_id);
    });
    expect(createWrapper(container).findSelect()).toBeNull();

    fireEvent.change(screen.getByLabelText('Model identifier'), {
      target: { value: 'my.custom-model-v1:0' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

    await waitFor(() => {
      expect(updateBedrockConfiguration).toHaveBeenCalledWith(
        expect.objectContaining({ model_id: 'my.custom-model-v1:0' }),
      );
    });
  });

  it('falls back to a free-text model id input when the backend lacks list permissions', async () => {
    setAuthRole('PortalAdmin');
    getBedrockModels.mockResolvedValue({
      models: [],
      region: 'us-east-1',
      permissions: 'Missing bedrock list permissions',
    });
    const { container } = render(<BedrockConfigurationSettings />);

    await waitFor(() => {
      expect(screen.getByLabelText('Model identifier')).toHaveValue(STORED_CONFIG.model_id);
    });
    expect(createWrapper(container).findSelect()).toBeNull();
  });

  it('rejects a timeout above 240 seconds without calling the API (Requirement 10.7)', async () => {
    setAuthRole('PortalAdmin');
    render(<BedrockConfigurationSettings />);
    await waitFor(() => {
      expect(screen.getByLabelText('Timeout seconds')).toHaveValue(60);
    });

    fireEvent.change(screen.getByLabelText('Timeout seconds'), { target: { value: '241' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

    expect(
      await screen.findByText('Timeout must be an integer between 1 and 240 seconds'),
    ).toBeInTheDocument();
    expect(updateBedrockConfiguration).not.toHaveBeenCalled();
  });

  it('saves the edited configuration through the API', async () => {
    setAuthRole('PortalAdmin');
    render(<BedrockConfigurationSettings />);
    await waitFor(() => {
      expect(screen.getByLabelText('Timeout seconds')).toHaveValue(60);
    });

    fireEvent.change(screen.getByLabelText('Timeout seconds'), { target: { value: '45' } });
    fireEvent.change(screen.getByLabelText('Temperature'), { target: { value: '0.5' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

    await waitFor(() => {
      expect(updateBedrockConfiguration).toHaveBeenCalledWith({
        model_id: STORED_CONFIG.model_id,
        region: STORED_CONFIG.region,
        max_tokens: 4096,
        temperature: 0.5,
        top_p: 0.9,
        timeout_seconds: 45,
      });
    });
    expect(await screen.findByText('Bedrock configuration saved')).toBeInTheDocument();
  });

  it('loads a stored null temperature and top_p as blank fields (Bugfix Requirement 2.3)', async () => {
    setAuthRole('PortalAdmin');
    getBedrockConfiguration.mockResolvedValue({
      bedrock_configuration: { ...STORED_CONFIG, temperature: null, top_p: null },
      defaults: {},
      max_timeout_seconds: 240,
    });
    render(<BedrockConfigurationSettings />);

    await waitFor(() => {
      expect(screen.getByLabelText('Timeout seconds')).toHaveValue(60);
    });
    // Unset sampling parameters render as blank inputs, not "null".
    expect(screen.getByLabelText('Temperature')).toHaveValue(null);
    expect(screen.getByLabelText('Top P')).toHaveValue(null);
  });

  it('accepts blank temperature and top_p and saves them as explicit null (Bugfix Requirement 2.3)', async () => {
    setAuthRole('PortalAdmin');
    render(<BedrockConfigurationSettings />);
    await waitFor(() => {
      expect(screen.getByLabelText('Temperature')).toHaveValue(0.2);
    });

    fireEvent.change(screen.getByLabelText('Temperature'), { target: { value: '' } });
    fireEvent.change(screen.getByLabelText('Top P'), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

    await waitFor(() => {
      expect(updateBedrockConfiguration).toHaveBeenCalledWith({
        model_id: STORED_CONFIG.model_id,
        region: STORED_CONFIG.region,
        max_tokens: 4096,
        temperature: null,
        top_p: null,
        timeout_seconds: 60,
      });
    });
    expect(screen.queryByText('Temperature must be between 0 and 1')).toBeNull();
    expect(screen.queryByText('Top P must be between 0 and 1')).toBeNull();
    expect(await screen.findByText('Bedrock configuration saved')).toBeInTheDocument();
  });

  describe('default max tokens per model', () => {
    const OPUS = 'global.anthropic.claude-opus-5-5';
    const NOVA = 'us.amazon.nova-pro-v1:0';
    // What GET returns with nothing stored.
    const NOTHING_STORED = {
      model_id: OPUS,
      region: 'us-east-1',
      max_tokens: 128000,
      temperature: null,
      top_p: null,
      timeout_seconds: 240,
    };

    beforeEach(() => {
      setAuthRole('PortalAdmin');
      getBedrockConfiguration.mockResolvedValue({
        bedrock_configuration: NOTHING_STORED,
        defaults: NOTHING_STORED,
        max_timeout_seconds: 240,
      });
      getBedrockModels.mockResolvedValue({
        models: [
          { id: OPUS, label: 'Global Anthropic Claude Opus 5.5', default_max_tokens: 128000 },
          { id: NOVA, label: 'US Amazon Nova Pro', default_max_tokens: 4096 },
        ],
        region: 'us-east-1',
      });
    });

    async function renderLoaded() {
      const { container } = render(<BedrockConfigurationSettings />);
      await waitFor(() => {
        const select = createWrapper(container).findSelect();
        expect(select).not.toBeNull();
        expect(select!.findTrigger().getElement()).toHaveTextContent('Global Anthropic Claude Opus 5.5');
      });
      expect(screen.getByLabelText('Max tokens')).toHaveValue(128000);
      return createWrapper(container).findSelect()!;
    }

    type SelectWrapper = NonNullable<ReturnType<ReturnType<typeof createWrapper>['findSelect']>>;

    function choose(select: SelectWrapper, value: string) {
      select.openDropdown();
      select.selectOptionByValue(value);
    }

    it("replaces the previous model's default with the selected model's and saves it", async () => {
      const select = await renderLoaded();

      choose(select, NOVA);
      expect(screen.getByLabelText('Max tokens')).toHaveValue(4096);
      choose(select, OPUS);
      expect(screen.getByLabelText('Max tokens')).toHaveValue(128000);
      choose(select, NOVA);
      fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

      await waitFor(() => {
        expect(updateBedrockConfiguration).toHaveBeenCalledWith({
          model_id: NOVA,
          region: 'us-east-1',
          max_tokens: 4096,
          temperature: null,
          top_p: null,
          timeout_seconds: 240,
        });
      });
    });

    it('keeps a max tokens value the admin entered when the model changes', async () => {
      const select = await renderLoaded();

      fireEvent.change(screen.getByLabelText('Max tokens'), { target: { value: '20000' } });
      choose(select, NOVA);

      expect(screen.getByLabelText('Max tokens')).toHaveValue(20000);
    });

    it('saves the shown default unchanged when only other fields change', async () => {
      await renderLoaded();

      fireEvent.change(screen.getByLabelText('Timeout seconds'), { target: { value: '120' } });
      fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

      await waitFor(() => {
        expect(updateBedrockConfiguration).toHaveBeenCalledWith(
          expect.objectContaining({ model_id: OPUS, max_tokens: 128000, timeout_seconds: 120 }),
        );
      });
    });

    // The form lacks a default for a typed model id, and for a loaded model
    // the list does not include. A model change saved with max tokens
    // unchanged then leaves max_tokens out, so the backend applies the new
    // model's default (here 4096) instead of Claude Opus 5.5's 128000.
    async function saveAndExpectMaxTokensLeftOut(modelId: string) {
      updateBedrockConfiguration.mockResolvedValue({
        message: 'ok',
        bedrock_configuration: { ...NOTHING_STORED, model_id: modelId, max_tokens: 4096 },
      });
      expect(screen.getByLabelText('Max tokens')).toHaveValue(128000);
      fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

      expect(await screen.findByText('Bedrock configuration saved')).toBeInTheDocument();
      expect(updateBedrockConfiguration).toHaveBeenCalledTimes(1);
      const body = updateBedrockConfiguration.mock.calls[0][0];
      expect(body).not.toHaveProperty('max_tokens');
      expect(body).toEqual({
        model_id: modelId,
        region: 'us-east-1',
        temperature: null,
        top_p: null,
        timeout_seconds: 240,
      });
      expect(screen.getByLabelText('Max tokens')).toHaveValue(4096);
    }

    it.each([
      ['fails to load', () => getBedrockModels.mockRejectedValue(new Error('boom'))],
      [
        'is empty',
        () =>
          getBedrockModels.mockResolvedValue({
            models: [],
            region: 'us-east-1',
            permissions: 'Missing bedrock list permissions',
          }),
      ],
    ])('leaves max tokens out for a typed model when the model list %s', async (_case, mockModelList) => {
      mockModelList();
      const { container } = render(<BedrockConfigurationSettings />);
      await waitFor(() => {
        expect(screen.getByLabelText('Model identifier')).toHaveValue(OPUS);
      });
      expect(createWrapper(container).findSelect()).toBeNull();

      fireEvent.change(screen.getByLabelText('Model identifier'), { target: { value: NOVA } });

      await saveAndExpectMaxTokensLeftOut(NOVA);
    });

    it('leaves max tokens out when the list lacks the loaded model', async () => {
      const NOVA_FOUNDATION = 'amazon.nova-pro-v1:0';
      getBedrockModels.mockResolvedValue({
        models: [{ id: NOVA_FOUNDATION, label: 'Nova Pro', default_max_tokens: 4096 }],
        region: 'us-east-1',
      });
      const { container } = render(<BedrockConfigurationSettings />);
      await waitFor(() => {
        const select = createWrapper(container).findSelect();
        expect(select).not.toBeNull();
        // The loaded model is shown as an extra option, labelled with its id.
        expect(select!.findTrigger().getElement()).toHaveTextContent(OPUS);
      });

      choose(createWrapper(container).findSelect()!, NOVA_FOUNDATION);

      await saveAndExpectMaxTokensLeftOut(NOVA_FOUNDATION);
    });

    it('sends a max tokens value the admin entered for a typed model', async () => {
      getBedrockModels.mockRejectedValue(new Error('boom'));
      render(<BedrockConfigurationSettings />);
      await waitFor(() => {
        expect(screen.getByLabelText('Model identifier')).toHaveValue(OPUS);
      });

      fireEvent.change(screen.getByLabelText('Model identifier'), { target: { value: NOVA } });
      fireEvent.change(screen.getByLabelText('Max tokens'), { target: { value: '10000' } });
      fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

      await waitFor(() => {
        expect(updateBedrockConfiguration).toHaveBeenCalledWith(
          expect.objectContaining({ model_id: NOVA, max_tokens: 10000 }),
        );
      });
    });
  });

  it('keeps rejecting a non-blank out-of-range temperature and top_p without calling the API', async () => {
    setAuthRole('PortalAdmin');
    render(<BedrockConfigurationSettings />);
    await waitFor(() => {
      expect(screen.getByLabelText('Temperature')).toHaveValue(0.2);
    });

    fireEvent.change(screen.getByLabelText('Temperature'), { target: { value: '1.5' } });
    fireEvent.change(screen.getByLabelText('Top P'), { target: { value: '-0.1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

    expect(await screen.findByText('Temperature must be between 0 and 1')).toBeInTheDocument();
    expect(await screen.findByText('Top P must be between 0 and 1')).toBeInTheDocument();
    expect(updateBedrockConfiguration).not.toHaveBeenCalled();
  });
});
