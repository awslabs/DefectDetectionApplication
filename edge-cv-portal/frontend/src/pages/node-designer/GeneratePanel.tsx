/**
 * Prompt-based custom node generation panel (custom-node-designer,
 * Requirements 2.1, 2.3, 2.6, 2.7).
 *
 * Two phases:
 *
 * 1. Setup: the Custom_Node_Type declaration the scaffold implements
 *    (same form state and assembly helpers as the create wizard,
 *    declaration.ts) plus the first natural-language prompt (2.1).
 *
 * 2. Chat: the generation transcript with a follow-up prompt box
 *    (follow-ups modify the current source server-side, 2.4) and the
 *    generated Plugin_Scaffold source displayed for review and optional
 *    editing before acceptance (2.3). Generation failures
 *    (GENERATED_SCAFFOLD_INVALID) and Bedrock failures/timeouts
 *    (BEDROCK_*, GENERATION_TIMEOUT) display the error and leave the
 *    prompt in the input box for retry (2.6, 2.7). Accepting creates
 *    the Plugin_Record (kind 'generated') and navigates to its detail
 *    view; local edits are persisted to the record's source (2.5).
 *
 * Generation is asynchronous (start/poll, mirroring SimulatorView):
 * submitting a prompt starts the turn (202) and the panel polls
 * GET /plugins/generate/{session} every few seconds with an in-progress
 * indicator until the turn completes or fails.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Container,
  FormField,
  Header,
  Input,
  Multiselect,
  Select,
  SelectProps,
  SpaceBetween,
  StatusIndicator,
  Tabs,
  Textarea,
} from '@cloudscape-design/components';
import { useNavigate } from 'react-router-dom';
import { ApiError, apiService } from '../../services/api';
import { useUsecase } from '../../contexts/UsecaseContext';
import { UseCase } from '../../types';
import { nodeDesignerApi } from './api';
import {
  ARCHITECTURE_LABELS,
  CATEGORIES,
  DEVICE_ARCHITECTURES,
  PARAMETER_TYPES,
  PORT_TYPES,
  ScaffoldFiles,
} from './types';
import {
  WizardForm,
  architecturesStepErrors,
  buildDeclaration,
  detailsStepErrors,
  emptyParameter,
  emptyPort,
  parametersStepErrors,
  portsStepErrors,
} from './declaration';
import {
  ChatMessage,
  GENERATION_POLL_MS,
  GenerationErrorView,
  appendTurn,
  describeGenerationError,
  describeTurnError,
  isTerminalTurn,
} from './generate';
import type { GenerationTurnState } from './types';

const initialForm = (): WizardForm => ({
  name: '',
  description: '',
  category: 'preprocessing',
  inputs: [{ ...emptyPort(), name: 'in' }],
  outputs: [{ ...emptyPort(), name: 'out' }],
  parameters: [],
  architectures: ['x86_64'],
});

export default function GeneratePanel() {
  const navigate = useNavigate();
  const { selectedUsecaseId, setSelectedUsecaseId } = useUsecase();

  // ---------------------------------------------------------- use case
  const [useCases, setUseCases] = useState<UseCase[]>([]);
  const [selectedUseCase, setSelectedUseCase] = useState<SelectProps.Option | null>(null);

  useEffect(() => {
    const loadUseCases = async () => {
      try {
        const response = await apiService.listUseCases();
        const list = response.usecases || [];
        setUseCases(list);
        const saved = selectedUsecaseId
          ? list.find((uc) => uc.usecase_id === selectedUsecaseId)
          : undefined;
        const chosen = saved || list[0];
        if (chosen) {
          setSelectedUseCase({ label: chosen.name, value: chosen.usecase_id });
          setSelectedUsecaseId(chosen.usecase_id);
        }
      } catch (err) {
        console.error('Failed to load use cases:', err);
      }
    };
    loadUseCases();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ------------------------------------------------------- setup state
  const [form, setForm] = useState<WizardForm>(initialForm);
  const [attempted, setAttempted] = useState(false);

  // -------------------------------------------------------- chat state
  // `prompt` is the single prompt input box for both the first and the
  // follow-up prompts; it is cleared only on a successful turn so a
  // failed generation preserves it for retry (2.6, 2.7).
  const [prompt, setPrompt] = useState('');
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [files, setFiles] = useState<ScaffoldFiles>({});
  const [activeFile, setActiveFile] = useState<string | null>(null);
  const [edited, setEdited] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<GenerationErrorView | null>(null);
  // Async start/poll flow: the session whose in-flight turn is being
  // polled, plus the prompt that started it (appended to the transcript
  // only when the turn completes, so a failed turn preserves it in the
  // input box, 2.6, 2.7).
  const [pollSessionId, setPollSessionId] = useState<string | null>(null);
  const pendingPromptRef = useRef('');

  // ------------------------------------------------------ accept state
  const [accepting, setAccepting] = useState(false);
  const [acceptedPluginId, setAcceptedPluginId] = useState<string | null>(null);

  const patch = (changes: Partial<WizardForm>) =>
    setForm((current) => ({ ...current, ...changes }));

  const setupErrors = useMemo(
    () => [
      ...detailsStepErrors(form),
      ...portsStepErrors(form),
      ...parametersStepErrors(form),
      ...architecturesStepErrors(form),
    ],
    [form]
  );

  // ------------------------------------------------------- generation

  /** A polled turn settled: apply the outcome and stop polling. */
  const finishTurn = (turn: GenerationTurnState) => {
    setPollSessionId(null);
    setGenerating(false);
    if (turn.turn_status === 'completed') {
      const turnFiles = turn.files || {};
      setSessionId(turn.session_id);
      setMessages((current) =>
        appendTurn(current, pendingPromptRef.current, turn.assistant_text)
      );
      setFiles(turnFiles);
      setActiveFile((current) =>
        current && current in turnFiles
          ? current
          : Object.keys(turnFiles).sort()[0] || null
      );
      setEdited(false);
      // Success: the prompt became a transcript entry; clear the box.
      setPrompt('');
    } else {
      // Generation and Bedrock failures display the error and preserve
      // the prompt in the input box for retry (2.6, 2.7).
      setError(describeTurnError(turn.turn_error));
    }
  };

  const runTurn = async () => {
    const text = prompt.trim();
    if (!text || generating) {
      return;
    }
    if (!sessionId) {
      setAttempted(true);
      if (setupErrors.length > 0) {
        return;
      }
      if (!selectedUseCase?.value) {
        setError({
          header: 'Select a use case',
          message: 'Select a use case before generating the node.',
          defects: [],
        });
        return;
      }
    }
    setGenerating(true);
    setError(null);
    try {
      // Start the turn: the backend answers 202 immediately and runs
      // the Bedrock generation asynchronously; the poll effect below
      // follows the turn until it settles.
      const started = sessionId
        ? await nodeDesignerApi.continueGeneration(sessionId, text)
        : await nodeDesignerApi.startGeneration({
            usecase_id: selectedUseCase!.value!,
            prompt: text,
            declaration: buildDeclaration(form),
          });
      pendingPromptRef.current = text;
      if (isTerminalTurn(started.turn_status)) {
        finishTurn(started);
      } else {
        setPollSessionId(started.session_id);
      }
    } catch (err) {
      // Start-request failures (validation errors, network) display the
      // error and preserve the prompt for retry (2.6, 2.7).
      setError(describeGenerationError(err));
      setGenerating(false);
    }
  };

  // Poll the in-flight turn until it settles (mirrors the SimulatorView
  // run polling). The first poll fires immediately; transient poll
  // failures keep polling, an expired session ends the turn as failed.
  useEffect(() => {
    if (!pollSessionId) {
      return;
    }
    let cancelled = false;
    const poll = async () => {
      try {
        const turn = await nodeDesignerApi.getGenerationTurn(pollSessionId);
        if (!cancelled && isTerminalTurn(turn.turn_status)) {
          finishTurn(turn);
        }
      } catch (err) {
        if (!cancelled && err instanceof ApiError && err.status === 404) {
          finishTurn({
            session_id: pollSessionId,
            usecase_id: '',
            turn_status: 'failed',
            turn_error: { code: 'SESSION_NOT_FOUND', message: err.message },
          });
        }
        // otherwise transient poll failure: keep polling
      }
    };
    poll();
    const timer = setInterval(poll, GENERATION_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pollSessionId]);

  // -------------------------------------------------------- acceptance

  const accept = async () => {
    if (!sessionId || accepting) {
      return;
    }
    setAccepting(true);
    setError(null);
    try {
      const response = await nodeDesignerApi.acceptGeneration(sessionId, {
        name: form.name.trim(),
        description: form.description.trim() || undefined,
      });
      const plugin = response.plugin;
      setAcceptedPluginId(plugin.plugin_id);
      if (edited) {
        // Persist the user's local edits over the accepted snapshot
        // before leaving (review and optional editing, 2.3).
        try {
          await nodeDesignerApi.putVersionSource(plugin.plugin_id, plugin.version, files);
        } catch (err: any) {
          setError({
            header: 'Plugin record created, but the edited source was rejected',
            message:
              (err && err.message) ||
              'The edited source could not be saved; the record keeps the generated source.',
            defects:
              err && err.details && Array.isArray(err.details.defects)
                ? (err.details.defects as string[])
                : [],
          });
          return;
        }
      }
      navigate(`/node-designer/plugins/${plugin.plugin_id}`);
    } catch (err) {
      setError(describeGenerationError(err));
    } finally {
      setAccepting(false);
    }
  };

  // ----------------------------------------------------------- render

  const errorAlert = error && (
    <Alert type="error" header={error.header} dismissible onDismiss={() => setError(null)}>
      <SpaceBetween size="xs">
        <div>{error.message}</div>
        {error.defects.length > 0 && (
          <ul>
            {error.defects.map((defect, index) => (
              <li key={index}>{defect}</li>
            ))}
          </ul>
        )}
        {!acceptedPluginId && <div>Your prompt is preserved below — adjust it and retry.</div>}
        {acceptedPluginId && (
          <Button onClick={() => navigate(`/node-designer/plugins/${acceptedPluginId}`)}>
            Open the plugin record
          </Button>
        )}
      </SpaceBetween>
    </Alert>
  );

  const promptBox = (
    <SpaceBetween size="s">
      <FormField
        label={sessionId ? 'Follow-up prompt' : 'Describe the node behavior'}
        description={
          sessionId
            ? 'Follow-up prompts modify the current generated source rather than starting over.'
            : 'Natural-language description of the per-frame processing the node should perform.'
        }
        stretch
      >
        <Textarea
          value={prompt}
          onChange={({ detail }) => setPrompt(detail.value)}
          rows={4}
          placeholder="Blur every region listed in the regions parameter of each incoming frame"
          ariaLabel="Node behavior prompt"
        />
      </FormField>
      <Button
        variant="primary"
        loading={generating}
        disabled={!prompt.trim()}
        onClick={runTurn}
      >
        {sessionId ? 'Send follow-up' : 'Generate scaffold'}
      </Button>
      {generating && (
        <StatusIndicator type="in-progress">
          Generating the scaffold — this can take about a minute…
        </StatusIndicator>
      )}
    </SpaceBetween>
  );

  // ---------------------------------------------------- chat phase view

  if (sessionId) {
    const paths = Object.keys(files).sort();
    return (
      <SpaceBetween size="l">
        <Header
          variant="h1"
          description="Review the generated Plugin_Scaffold source, refine it with follow-up prompts, optionally edit it, then accept it into a plugin record."
        >
          Generate custom node
        </Header>

        {errorAlert}

        <Container header={<Header variant="h2">Conversation</Header>}>
          <SpaceBetween size="s">
            {messages.map((message, index) => (
              <Box key={index}>
                <Box variant="awsui-key-label">
                  {message.role === 'user' ? 'You' : 'Generator'}
                </Box>
                <Box>{message.text}</Box>
              </Box>
            ))}
            {promptBox}
          </SpaceBetween>
        </Container>

        <Container
          header={
            <Header
              variant="h2"
              description="Review and optionally edit the generated source before accepting it."
              actions={
                <Button
                  variant="primary"
                  loading={accepting}
                  disabled={paths.length === 0 || generating}
                  onClick={accept}
                >
                  Accept and create plugin record
                </Button>
              }
            >
              Generated source{edited ? ' (edited)' : ''}
            </Header>
          }
        >
          {paths.length === 0 ? (
            <Box color="text-status-inactive">No generated source yet.</Box>
          ) : (
            <Tabs
              activeTabId={activeFile || undefined}
              onChange={({ detail }) => setActiveFile(detail.activeTabId)}
              tabs={paths.map((path) => ({
                id: path,
                label: path,
                content: (
                  <Textarea
                    value={files[path]}
                    onChange={({ detail }) => {
                      setFiles((current) => ({ ...current, [path]: detail.value }));
                      setEdited(true);
                    }}
                    rows={24}
                    spellcheck={false}
                    ariaLabel={`Source of ${path}`}
                  />
                ),
              }))}
            />
          )}
        </Container>

        <Box>
          Accepted source enters the standard build, simulation, and lifecycle
          path. Builds will target:{' '}
          {form.architectures
            .map((arch) => ARCHITECTURE_LABELS[arch as keyof typeof ARCHITECTURE_LABELS] ?? arch)
            .join(', ')}
        </Box>
      </SpaceBetween>
    );
  }

  // --------------------------------------------------- setup phase view

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Describe the node you need in natural language; the configured Bedrock model generates the Plugin_Scaffold source for your review."
      >
        Generate custom node
      </Header>

      {errorAlert}

      {attempted && setupErrors.length > 0 && (
        <Alert type="error" header="Complete the node declaration first">
          {setupErrors.join(' ')}
        </Alert>
      )}

      <Container header={<Header variant="h2">Node declaration</Header>}>
        <SpaceBetween size="l">
          <FormField label="Use case">
            <Select
              placeholder="Select use case"
              selectedOption={selectedUseCase}
              options={useCases.map((uc) => ({ label: uc.name, value: uc.usecase_id }))}
              onChange={({ detail }) => {
                setSelectedUseCase(detail.selectedOption);
                if (detail.selectedOption.value) {
                  setSelectedUsecaseId(detail.selectedOption.value);
                }
              }}
            />
          </FormField>
          <FormField
            label="Name"
            description="Display name of the custom node type in the palette."
          >
            <Input
              value={form.name}
              onChange={({ detail }) => patch({ name: detail.value })}
              placeholder="Blur Regions"
            />
          </FormField>
          <FormField label={<span>Description <i>- optional</i></span>}>
            <Input
              value={form.description}
              onChange={({ detail }) => patch({ description: detail.value })}
              placeholder="Blurs configured regions in each frame"
            />
          </FormField>
          <FormField label="Palette category">
            <Select
              selectedOption={{ label: form.category, value: form.category }}
              options={CATEGORIES.map((c) => ({ label: c, value: c }))}
              onChange={({ detail }) =>
                patch({ category: detail.selectedOption.value || 'preprocessing' })
              }
            />
          </FormField>
          <FormField label="Target architectures">
            <Multiselect
              selectedOptions={form.architectures.map((arch) => ({
                label: ARCHITECTURE_LABELS[arch as keyof typeof ARCHITECTURE_LABELS] ?? arch,
                value: arch,
              }))}
              options={DEVICE_ARCHITECTURES.map((arch) => ({
                label: ARCHITECTURE_LABELS[arch],
                value: arch,
              }))}
              onChange={({ detail }) =>
                patch({
                  architectures: detail.selectedOptions
                    .map((option) => option.value)
                    .filter((value): value is string => Boolean(value)),
                })
              }
              placeholder="Select target architectures"
            />
          </FormField>
        </SpaceBetween>
      </Container>

      {(['inputs', 'outputs'] as const).map((side) => (
        <Container
          key={side}
          header={
            <Header
              variant="h3"
              actions={
                <Button onClick={() => patch({ [side]: [...form[side], emptyPort()] })}>
                  Add {side === 'inputs' ? 'input' : 'output'} port
                </Button>
              }
            >
              {side === 'inputs' ? 'Input ports' : 'Output ports'}
            </Header>
          }
        >
          <SpaceBetween size="s">
            {form[side].length === 0 && (
              <Box color="text-status-inactive">No {side} declared.</Box>
            )}
            {form[side].map((port, index) => (
              <SpaceBetween key={index} direction="horizontal" size="s">
                <FormField label={index === 0 ? 'Port name' : undefined}>
                  <Input
                    value={port.name}
                    placeholder={side === 'inputs' ? 'in' : 'out'}
                    onChange={({ detail }) => {
                      const ports = [...form[side]];
                      ports[index] = { ...ports[index], name: detail.value };
                      patch({ [side]: ports });
                    }}
                  />
                </FormField>
                <FormField label={index === 0 ? 'Port type' : undefined}>
                  <Select
                    selectedOption={{ label: port.portType, value: port.portType }}
                    options={PORT_TYPES.map((t) => ({ label: t, value: t }))}
                    onChange={({ detail }) => {
                      const ports = [...form[side]];
                      ports[index] = {
                        ...ports[index],
                        portType: detail.selectedOption.value || port.portType,
                      };
                      patch({ [side]: ports });
                    }}
                  />
                </FormField>
                <FormField label={index === 0 ? ' ' : undefined}>
                  <Button
                    iconName="remove"
                    ariaLabel={`Remove ${side} port ${index + 1}`}
                    onClick={() =>
                      patch({ [side]: form[side].filter((_, i) => i !== index) })
                    }
                  />
                </FormField>
              </SpaceBetween>
            ))}
          </SpaceBetween>
        </Container>
      ))}

      <Container
        header={
          <Header
            variant="h3"
            description="Parameters reach the Frame_Processing_Hook as named values; the generated code reads them instead of hard-coding."
            actions={
              <Button
                onClick={() => patch({ parameters: [...form.parameters, emptyParameter()] })}
              >
                Add parameter
              </Button>
            }
          >
            Parameters
          </Header>
        }
      >
        <SpaceBetween size="l">
          {form.parameters.length === 0 && (
            <Box color="text-status-inactive">No parameters declared.</Box>
          )}
          {form.parameters.map((parameter, index) => (
            <Container
              key={index}
              header={
                <Header
                  variant="h3"
                  actions={
                    <Button
                      iconName="remove"
                      ariaLabel={`Remove parameter ${index + 1}`}
                      onClick={() =>
                        patch({
                          parameters: form.parameters.filter((_, i) => i !== index),
                        })
                      }
                    />
                  }
                >
                  {parameter.name.trim() || `Parameter ${index + 1}`}
                </Header>
              }
            >
              <SpaceBetween size="s">
                <SpaceBetween direction="horizontal" size="s">
                  <FormField label="Name">
                    <Input
                      value={parameter.name}
                      placeholder="radius"
                      onChange={({ detail }) => {
                        const parameters = [...form.parameters];
                        parameters[index] = { ...parameters[index], name: detail.value };
                        patch({ parameters });
                      }}
                    />
                  </FormField>
                  <FormField label="Type">
                    <Select
                      selectedOption={{
                        label: parameter.paramType,
                        value: parameter.paramType,
                      }}
                      options={PARAMETER_TYPES.map((t) => ({ label: t, value: t }))}
                      onChange={({ detail }) => {
                        const parameters = [...form.parameters];
                        parameters[index] = {
                          ...parameters[index],
                          paramType: detail.selectedOption.value || parameter.paramType,
                        };
                        patch({ parameters });
                      }}
                    />
                  </FormField>
                  <FormField label="Required">
                    <Checkbox
                      checked={parameter.required}
                      onChange={({ detail }) => {
                        const parameters = [...form.parameters];
                        parameters[index] = {
                          ...parameters[index],
                          required: detail.checked,
                        };
                        patch({ parameters });
                      }}
                    >
                      required
                    </Checkbox>
                  </FormField>
                </SpaceBetween>
                <FormField
                  label="Description"
                  description="Shown as field help in the node configuration panel."
                >
                  <Input
                    value={parameter.description}
                    placeholder="Blur radius in pixels"
                    onChange={({ detail }) => {
                      const parameters = [...form.parameters];
                      parameters[index] = {
                        ...parameters[index],
                        description: detail.value,
                      };
                      patch({ parameters });
                    }}
                  />
                </FormField>
                <SpaceBetween direction="horizontal" size="s">
                  <FormField
                    label="Example value"
                    description="A working value satisfying the parameter type."
                  >
                    <Input
                      value={parameter.example}
                      placeholder={parameter.paramType === 'bool' ? 'true' : '5'}
                      onChange={({ detail }) => {
                        const parameters = [...form.parameters];
                        parameters[index] = {
                          ...parameters[index],
                          example: detail.value,
                        };
                        patch({ parameters });
                      }}
                    />
                  </FormField>
                  <FormField label={<span>Default <i>- optional</i></span>}>
                    <Input
                      value={parameter.defaultValue}
                      onChange={({ detail }) => {
                        const parameters = [...form.parameters];
                        parameters[index] = {
                          ...parameters[index],
                          defaultValue: detail.value,
                        };
                        patch({ parameters });
                      }}
                    />
                  </FormField>
                  {parameter.paramType === 'enum' && (
                    <FormField
                      label="Allowed values"
                      description="Comma-separated enum values."
                    >
                      <Input
                        value={parameter.enumValues}
                        placeholder="low, medium, high"
                        onChange={({ detail }) => {
                          const parameters = [...form.parameters];
                          parameters[index] = {
                            ...parameters[index],
                            enumValues: detail.value,
                          };
                          patch({ parameters });
                        }}
                      />
                    </FormField>
                  )}
                </SpaceBetween>
              </SpaceBetween>
            </Container>
          ))}
        </SpaceBetween>
      </Container>

      <Container header={<Header variant="h2">Prompt</Header>}>{promptBox}</Container>
    </SpaceBetween>
  );
}
