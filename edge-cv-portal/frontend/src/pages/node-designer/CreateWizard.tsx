/**
 * Custom node create wizard (custom-node-designer,
 * Requirements 1.1, 1.5, 1.6, 1.7).
 *
 * Two phases:
 *
 * 1. Declaration wizard collecting name, description, palette category,
 *    Port declarations, parameter declarations, and Target_Architectures
 *    (1.1). Submitting renders the Plugin_Scaffold server-side via
 *    POST /plugins; a scaffold generation failure displays the failing
 *    input identified by the backend and no Plugin_Record is created
 *    (1.7).
 *
 * 2. Scaffold review: file preview with zip download (1.5), an inline
 *    source editor, and submission of the (original or edited) source to
 *    the Plugin_Build_Service for the selected architectures (1.6).
 */
import { useEffect, useMemo, useState } from 'react';
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
  Tabs,
  Textarea,
  Wizard,
} from '@cloudscape-design/components';
import { useNavigate } from 'react-router-dom';
import { apiService, ApiError } from '../../services/api';
import { useUsecase } from '../../contexts/UsecaseContext';
import { UseCase } from '../../types';
import { nodeDesignerApi } from './api';
import {
  ARCHITECTURE_LABELS,
  CATEGORIES,
  DEVICE_ARCHITECTURES,
  PARAMETER_TYPES,
  PORT_TYPES,
  PluginVersionDetail,
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
import { downloadZip } from './zip';

const initialForm = (): WizardForm => ({
  name: '',
  description: '',
  category: 'preprocessing',
  inputs: [{ ...emptyPort(), name: 'in' }],
  outputs: [{ ...emptyPort(), name: 'out' }],
  parameters: [],
  architectures: ['x86_64'],
});

export default function CreateWizard() {
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

  // -------------------------------------------------------- wizard state
  const [form, setForm] = useState<WizardForm>(initialForm);
  const [activeStep, setActiveStep] = useState(0);
  const [attempted, setAttempted] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [createError, setCreateError] = useState<{
    message: string;
    field?: string;
  } | null>(null);

  // ------------------------------------------------------ scaffold state
  const [plugin, setPlugin] = useState<PluginVersionDetail | null>(null);
  const [files, setFiles] = useState<ScaffoldFiles>({});
  const [activeFile, setActiveFile] = useState<string | null>(null);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [buildSubmitting, setBuildSubmitting] = useState(false);

  const patch = (changes: Partial<WizardForm>) =>
    setForm((current) => ({ ...current, ...changes }));

  const stepErrors = useMemo(
    () => [
      detailsStepErrors(form),
      portsStepErrors(form),
      parametersStepErrors(form),
      architecturesStepErrors(form),
    ],
    [form]
  );

  // -------------------------------------------------------- create phase

  const submitDeclaration = async () => {
    setAttempted(true);
    const failingStep = stepErrors.findIndex((errors) => errors.length > 0);
    if (failingStep >= 0) {
      setActiveStep(failingStep);
      return;
    }
    if (!selectedUseCase?.value) {
      setCreateError({ message: 'Select a use case before creating the node.' });
      return;
    }
    setSubmitting(true);
    setCreateError(null);
    try {
      const response = await nodeDesignerApi.createScaffoldPlugin({
        usecase_id: selectedUseCase.value,
        name: form.name.trim(),
        description: form.description.trim(),
        declaration: buildDeclaration(form),
      });
      setPlugin(response.plugin);
      setFiles(response.files || {});
      setActiveFile(Object.keys(response.files || {}).sort()[0] || null);
    } catch (err: any) {
      // Scaffold generation failure: display the failing input; no
      // Plugin_Record was created (Requirement 1.7).
      const field =
        err instanceof ApiError && err.details && typeof err.details.field === 'string'
          ? (err.details.field as string)
          : undefined;
      setCreateError({ message: err.message || 'Scaffold generation failed', field });
    } finally {
      setSubmitting(false);
    }
  };

  // ------------------------------------------------------ scaffold phase

  const saveSource = async (): Promise<boolean> => {
    if (!plugin) return false;
    setSourceError(null);
    try {
      await nodeDesignerApi.putVersionSource(plugin.plugin_id, plugin.version, files);
      return true;
    } catch (err: any) {
      setSourceError(err.message || 'Failed to save the scaffold source');
      return false;
    }
  };

  const submitToBuild = async () => {
    if (!plugin) return;
    setBuildSubmitting(true);
    try {
      const saved = await saveSource();
      if (!saved) return;
      await nodeDesignerApi.startBuilds(
        plugin.plugin_id,
        plugin.version,
        form.architectures
      );
      navigate(`/node-designer/plugins/${plugin.plugin_id}`);
    } catch (err: any) {
      setSourceError(err.message || 'Failed to submit the source for building');
    } finally {
      setBuildSubmitting(false);
    }
  };

  // -------------------------------------------------------------- render

  if (plugin) {
    const paths = Object.keys(files).sort();
    return (
      <SpaceBetween size="l">
        <Header
          variant="h1"
          description="Review the generated Plugin_Scaffold, edit the Frame_Processing_Hook, download the project, and submit it to build."
        >
          Scaffold for {plugin.name}
        </Header>

        {sourceError && (
          <Alert type="error" dismissible onDismiss={() => setSourceError(null)}>
            {sourceError}
          </Alert>
        )}

        <Container
          header={
            <Header
              variant="h2"
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    iconName="download"
                    onClick={() => downloadZip(files, plugin.name.replace(/\s+/g, '-'))}
                  >
                    Download zip
                  </Button>
                  <Button onClick={saveSource}>Save source</Button>
                  <Button
                    variant="primary"
                    loading={buildSubmitting}
                    onClick={submitToBuild}
                  >
                    Submit to build
                  </Button>
                </SpaceBetween>
              }
            >
              Source files
            </Header>
          }
        >
          <Tabs
            activeTabId={activeFile || undefined}
            onChange={({ detail }) => setActiveFile(detail.activeTabId)}
            tabs={paths.map((path) => ({
              id: path,
              label: path,
              content: (
                <Textarea
                  value={files[path]}
                  onChange={({ detail }) =>
                    setFiles((current) => ({ ...current, [path]: detail.value }))
                  }
                  rows={24}
                  spellcheck={false}
                  ariaLabel={`Source of ${path}`}
                />
              ),
            }))}
          />
        </Container>

        <Box>
          Builds will target:{' '}
          {form.architectures
            .map((arch) => ARCHITECTURE_LABELS[arch as keyof typeof ARCHITECTURE_LABELS] ?? arch)
            .join(', ')}
        </Box>
      </SpaceBetween>
    );
  }

  return (
    <SpaceBetween size="l">
      {createError && (
        <Alert type="error" header="Scaffold generation failed" dismissible
          onDismiss={() => setCreateError(null)}
        >
          <SpaceBetween size="xs">
            <div>{createError.message}</div>
            {createError.field && (
              <div>
                Failing input: <code>{createError.field}</code>
              </div>
            )}
            <div>No plugin record was created.</div>
          </SpaceBetween>
        </Alert>
      )}

      <Wizard
        i18nStrings={{
          stepNumberLabel: (stepNumber) => `Step ${stepNumber}`,
          collapsedStepsLabel: (stepNumber, stepsCount) =>
            `Step ${stepNumber} of ${stepsCount}`,
          cancelButton: 'Cancel',
          previousButton: 'Previous',
          nextButton: 'Next',
          submitButton: 'Generate scaffold',
          optional: 'optional',
        }}
        activeStepIndex={activeStep}
        onNavigate={({ detail }) => {
          if (detail.requestedStepIndex > activeStep) {
            setAttempted(true);
            if (stepErrors[activeStep].length > 0) {
              return;
            }
          }
          setActiveStep(detail.requestedStepIndex);
        }}
        onCancel={() => navigate('/node-designer')}
        onSubmit={submitDeclaration}
        isLoadingNextStep={submitting}
        steps={[
          {
            title: 'Node details',
            description:
              'Name, description, and the Node_Palette category of the new custom node.',
            content: (
              <SpaceBetween size="l">
                {attempted && stepErrors[0].length > 0 && (
                  <Alert type="error">{stepErrors[0].join(' ')}</Alert>
                )}
                <FormField label="Use case">
                  <Select
                    placeholder="Select use case"
                    selectedOption={selectedUseCase}
                    options={useCases.map((uc) => ({
                      label: uc.name,
                      value: uc.usecase_id,
                    }))}
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
              </SpaceBetween>
            ),
          },
          {
            title: 'Ports',
            description:
              'Typed input and output ports connections attach to on the canvas.',
            content: (
              <SpaceBetween size="l">
                {attempted && stepErrors[1].length > 0 && (
                  <Alert type="error">{stepErrors[1].join(' ')}</Alert>
                )}
                {(['inputs', 'outputs'] as const).map((side) => (
                  <Container
                    key={side}
                    header={
                      <Header
                        variant="h3"
                        actions={
                          <Button
                            onClick={() => patch({ [side]: [...form[side], emptyPort()] })}
                          >
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
              </SpaceBetween>
            ),
          },
          {
            title: 'Parameters',
            description:
              'Parameters surface as GObject properties and reach the Frame_Processing_Hook as named values.',
            isOptional: true,
            content: (
              <SpaceBetween size="l">
                {attempted && stepErrors[2].length > 0 && (
                  <Alert type="error">{stepErrors[2].join(' ')}</Alert>
                )}
                <Box>
                  <Button
                    onClick={() =>
                      patch({ parameters: [...form.parameters, emptyParameter()] })
                    }
                  >
                    Add parameter
                  </Button>
                </Box>
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
            ),
          },
          {
            title: 'Target architectures',
            description:
              'One build configuration is generated per selected Target_Architecture, and builds are submitted for each.',
            content: (
              <SpaceBetween size="l">
                {attempted && stepErrors[3].length > 0 && (
                  <Alert type="error">{stepErrors[3].join(' ')}</Alert>
                )}
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
            ),
          },
        ]}
      />
    </SpaceBetween>
  );
}
