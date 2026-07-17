/**
 * Custom_Node_Type registration wizard (custom-node-designer,
 * task 12.5, Requirements 4.6, 8.1, 8.5).
 *
 * Prompted after the first successful build of a Plugin_Record version
 * (linked from the detail page's RegistrationPrompt). Collects the
 * Custom_Node_Type declaration: display name and palette category,
 * input/output Ports with types from PORT_TYPES, parameters with
 * descriptions and examples, the element/property mapping per built
 * Target_Architecture, the hardware-dependence flag, and the Use_Case
 * scoping (8.1). Submits to POST /custom-node-types; invalid Port
 * declarations (and every other declaration defect) are surfaced with
 * the offending field the backend identifies (8.5).
 */
import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  Checkbox,
  Container,
  FormField,
  Header,
  Input,
  Multiselect,
  Select,
  SpaceBetween,
  Spinner,
  Toggle,
  Wizard,
} from '@cloudscape-design/components';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { apiService } from '../../services/api';
import { UseCase } from '../../types';
import { nodeDesignerApi } from './api';
import {
  ARCHITECTURE_LABELS,
  CATEGORIES,
  NodeTypeDetail,
  PARAMETER_TYPES,
  PORT_TYPES,
  PluginVersionDetail,
} from './types';
import {
  defaultPortsForCategory,
  detailsStepErrors,
  emptyParameter,
  emptyPort,
  isDefaultPortArrangement,
  parametersStepErrors,
  portsStepErrors,
} from './declaration';
import type { ParameterForm, PortForm, WizardForm } from './declaration';
import ParameterScanPanel, {
  ParameterScanMergeResult,
} from './ParameterScanPanel';
import PortGuidancePanel from './PortGuidancePanel';
import PortScanPanel, { PortScanApplyResult } from './PortScanPanel';
import { removalBlockReason } from './portScan';
import {
  MappingForm,
  RegistrationForm,
  buildRegistrationDeclaration,
  defaultElementFactory,
  formFromDeclaration,
  initialMappings,
  mappingsStepErrors,
  registrationErrorView,
  scopeStepErrors,
  successfulBuildArchs,
} from './registration';

export default function RegistrationWizard() {
  const navigate = useNavigate();
  const { pluginId } = useParams<{ pluginId: string }>();
  const [searchParams] = useSearchParams();
  const requestedVersion = searchParams.get('version');

  const [plugin, setPlugin] = useState<PluginVersionDetail | null>(null);
  const [useCases, setUseCases] = useState<UseCase[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Existing registration backed by this plugin: the wizard switches to
  // update mode (PUT as a new version) instead of registering a duplicate.
  const [existing, setExisting] = useState<NodeTypeDetail | null>(null);

  const [form, setForm] = useState<RegistrationForm | null>(null);
  const [activeStep, setActiveStep] = useState(0);
  const [attempted, setAttempted] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<{
    message: string;
    field?: string;
  } | null>(null);

  // ------------------------------------------------------------ loading

  useEffect(() => {
    const load = async () => {
      if (!pluginId) return;
      setLoading(true);
      setLoadError(null);
      try {
        const detail = requestedVersion
          ? (await nodeDesignerApi.getVersion(pluginId, Number(requestedVersion))).plugin
          : (await nodeDesignerApi.getPlugin(pluginId)).plugin;
        setPlugin(detail);

        const factory = defaultElementFactory(detail.name);

        // A plugin already backing a Custom_Node_Type is updated (a new
        // retained version of the existing registration) instead of
        // registered again as a duplicate palette entry.
        let existingDetail: NodeTypeDetail | null = null;
        try {
          const { nodeTypes } = await nodeDesignerApi.listNodeTypes(detail.plugin_id);
          const active = nodeTypes.find((nodeType) => !nodeType.deprecated);
          if (active) {
            existingDetail = (
              await nodeDesignerApi.getNodeType(active.node_type_id)
            ).nodeType;
          }
        } catch {
          // Detection is best-effort; the backend still rejects duplicate
          // type ids on submit.
        }
        setExisting(existingDetail);

        if (existingDetail) {
          setForm(
            formFromDeclaration(
              existingDetail.declaration,
              successfulBuildArchs(detail.artifacts),
              existingDetail.usecase_ids?.length
                ? existingDetail.usecase_ids
                : [detail.usecase_id],
              factory
            )
          );
        } else {
          setForm({
            name: detail.name,
            description: detail.description || '',
            category: 'preprocessing',
            ...defaultPortsForCategory('preprocessing'),
            parameters: [],
            mappings: initialMappings(successfulBuildArchs(detail.artifacts), factory),
            hardwareDependent: false,
            usecaseIds: [detail.usecase_id],
          });
        }

        try {
          const response = await apiService.listUseCases();
          setUseCases(response.usecases || []);
        } catch {
          // Scoping falls back to the plugin's own Use_Case.
        }
      } catch (err: any) {
        setLoadError(err.message || 'Failed to load the plugin record');
      } finally {
        setLoading(false);
      }
    };
    load();
  }, [pluginId, requestedVersion]);

  const builtArchs = useMemo(
    () => successfulBuildArchs(plugin?.artifacts),
    [plugin]
  );

  const stepErrors = useMemo(() => {
    if (!form) return [[], [], [], [], []];
    const asWizardForm: WizardForm = { ...form, architectures: ['x86_64'] };
    return [
      detailsStepErrors(asWizardForm),
      portsStepErrors(asWizardForm),
      parametersStepErrors(asWizardForm),
      mappingsStepErrors(form),
      scopeStepErrors(form),
    ];
  }, [form]);

  const patch = (changes: Partial<RegistrationForm>) =>
    setForm((current) => (current ? { ...current, ...changes } : current));

  const patchMapping = (index: number, changes: Partial<MappingForm>) => {
    if (!form) return;
    const mappings = [...form.mappings];
    mappings[index] = { ...mappings[index], ...changes };
    patch({ mappings });
  };

  // ---------------------------------------------------- parameter rows
  //
  // Rows added by the property scan carry a "from scan" badge until the
  // user edits them (gst-parameter-prepopulation, Requirement 6.4). The
  // scanned names live outside the form so the merge flows through the
  // exact same patch({parameters}) path as manual edits and step gating
  // (parametersStepErrors) is untouched (3.3, 5.5).

  const [scannedNames, setScannedNames] = useState<Set<string>>(new Set());

  const dropScannedName = (name: string) =>
    setScannedNames((current) => {
      if (!current.has(name)) return current;
      const next = new Set(current);
      next.delete(name);
      return next;
    });

  /** Edit one parameter row; any edit clears its scan provenance (6.4). */
  const patchParameter = (index: number, changes: Partial<ParameterForm>) => {
    if (!form) return;
    const parameters = [...form.parameters];
    const editedName = parameters[index].name.trim();
    parameters[index] = { ...parameters[index], ...changes };
    patch({ parameters });
    dropScannedName(editedName);
  };

  const removeParameter = (index: number) => {
    if (!form) return;
    const removedName = form.parameters[index].name.trim();
    patch({ parameters: form.parameters.filter((_, i) => i !== index) });
    dropScannedName(removedName);
  };

  /** Scan merge result applied through the ordinary patch path (5.1, 5.2). */
  const applyScanMerge = (result: ParameterScanMergeResult) => {
    patch({ parameters: result.parameters });
    if (result.added.length > 0) {
      setScannedNames((current) => {
        const next = new Set(current);
        result.added.forEach((name) => next.add(name.trim()));
        return next;
      });
    }
  };

  // --------------------------------------------------------- port rows
  //
  // Ports applied by the pad scan whose Port_Type needs confirmation
  // carry a "confirm type" warning badge until the user edits the row
  // (port-guidance-and-pad-prepopulation, Requirement 6.5). Like the
  // parameter scan's scannedNames, the set lives outside the form so
  // the apply flows through the exact same patch({inputs, outputs})
  // path as manual edits — applied ports stay indistinguishable from
  // manual ones for editing, removal, validation, and step gating
  // (Requirement 6.8) and portsStepErrors is untouched.

  const [unconfirmedPortNames, setUnconfirmedPortNames] = useState<Set<string>>(
    new Set()
  );

  const dropUnconfirmedPortName = (name: string) =>
    setUnconfirmedPortNames((current) => {
      if (!current.has(name)) return current;
      const next = new Set(current);
      next.delete(name);
      return next;
    });

  /**
   * Edit one port row; any name or type edit — including re-selecting
   * the same type, the confirmation gesture — confirms the port (6.5).
   */
  const patchPort = (
    side: 'inputs' | 'outputs',
    index: number,
    changes: Partial<PortForm>
  ) => {
    if (!form) return;
    const ports = [...form[side]];
    const editedName = ports[index].name.trim();
    ports[index] = { ...ports[index], ...changes };
    patch({ [side]: ports });
    dropUnconfirmedPortName(editedName);
  };

  const removePort = (side: 'inputs' | 'outputs', index: number) => {
    if (!form) return;
    const removedName = form[side][index].name.trim();
    patch({ [side]: form[side].filter((_, i) => i !== index) });
    dropUnconfirmedPortName(removedName);
  };

  /** Port_Scan apply, through the ordinary patch path (6.1, 6.8). */
  const applyPortScan = (result: PortScanApplyResult) => {
    patch({ inputs: result.inputs, outputs: result.outputs });
    if (result.unconfirmed.length > 0) {
      setUnconfirmedPortNames((current) => {
        const next = new Set(current);
        result.unconfirmed.forEach((name) => next.add(name.trim()));
        return next;
      });
    }
  };

  // ------------------------------------------------------------- submit

  const submit = async () => {
    if (!form || !plugin) return;
    setAttempted(true);
    const failingStep = stepErrors.findIndex((errors) => errors.length > 0);
    if (failingStep >= 0) {
      setActiveStep(failingStep);
      return;
    }
    setSubmitting(true);
    setSubmitError(null);
    try {
      if (existing) {
        // Update mode: a new version of the existing registration. The
        // declaration keeps the registered type id (which is derived
        // from the original name) even if the display name changed.
        await nodeDesignerApi.updateNodeType(existing.node_type_id, {
          plugin_version: plugin.version,
          declaration: {
            ...buildRegistrationDeclaration(form),
            typeId: existing.node_type_id,
          },
          usecase_ids: form.usecaseIds,
        });
      } else {
        await nodeDesignerApi.registerNodeType({
          plugin_id: plugin.plugin_id,
          plugin_version: plugin.version,
          declaration: buildRegistrationDeclaration(form),
          usecase_ids: form.usecaseIds,
        });
      }
      navigate(`/node-designer/plugins/${plugin.plugin_id}`);
    } catch (err) {
      // Invalid declarations come back with details.field identifying
      // the offending input (Requirement 8.5).
      setSubmitError(registrationErrorView(err));
    } finally {
      setSubmitting(false);
    }
  };

  // ------------------------------------------------------------- render

  if (loading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
      </Box>
    );
  }

  if (loadError || !plugin || !form) {
    return (
      <SpaceBetween size="l">
        <Alert type="error">{loadError || 'Plugin record not found'}</Alert>
        <Button onClick={() => navigate('/node-designer')}>Back to Node Designer</Button>
      </SpaceBetween>
    );
  }

  if (builtArchs.length === 0) {
    return (
      <SpaceBetween size="l">
        <Header variant="h1">Register custom node type</Header>
        <Alert type="warning" header="No successful build yet">
          Registering a Custom_Node_Type requires at least one successfully
          built Target_Architecture. Build the plugin first, then return here.
        </Alert>
        <Button onClick={() => navigate(`/node-designer/plugins/${plugin.plugin_id}`)}>
          Back to plugin
        </Button>
      </SpaceBetween>
    );
  }

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description={
          existing
            ? `Update the registered palette node type backed by ${plugin.name} v${plugin.version}.`
            : `Declare the palette node type backed by ${plugin.name} v${plugin.version}.`
        }
      >
        {existing ? 'Update custom node type' : 'Register custom node type'}
      </Header>

      {existing && (
        <Alert type="info" header="Already registered">
          This plugin already backs the node type{' '}
          <code>{existing.node_type_id}</code> (v{existing.version}).
          Submitting saves your changes as a new version of that node type
          instead of registering a duplicate.
        </Alert>
      )}

      {submitError && (
        <Alert
          type="error"
          header="Registration rejected"
          dismissible
          onDismiss={() => setSubmitError(null)}
        >
          <SpaceBetween size="xs">
            <div>{submitError.message}</div>
            {submitError.field && (
              <div>
                Offending field: <code>{submitError.field}</code>
              </div>
            )}
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
          submitButton: existing ? 'Update node type' : 'Register node type',
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
        onCancel={() => navigate(`/node-designer/plugins/${plugin.plugin_id}`)}
        onSubmit={submit}
        isLoadingNextStep={submitting}
        steps={[
          {
            title: 'Node details',
            description:
              'Display name and Node_Palette category of the custom node type.',
            content: (
              <SpaceBetween size="l">
                {attempted && stepErrors[0].length > 0 && (
                  <Alert type="error">{stepErrors[0].join(' ')}</Alert>
                )}
                <FormField
                  label="Display name"
                  description="Shown in the palette like a built-in node type."
                >
                  <Input
                    value={form.name}
                    onChange={({ detail }) => patch({ name: detail.value })}
                  />
                </FormField>
                <FormField label={<span>Description <i>- optional</i></span>}>
                  <Input
                    value={form.description}
                    onChange={({ detail }) => patch({ description: detail.value })}
                  />
                </FormField>
                <FormField label="Palette category">
                  <Select
                    selectedOption={{ label: form.category, value: form.category }}
                    options={CATEGORIES.map((c) => ({ label: c, value: c }))}
                    onChange={({ detail }) => {
                      const category =
                        detail.selectedOption.value || form.category;
                      // Untouched default port rows follow the selected
                      // category's typical arrangement; user-edited rows
                      // are preserved exactly (workflow-designer-bugfixes
                      // Bug 2, Req 2.4, 2.5, 3.6).
                      if (isDefaultPortArrangement(form.inputs, form.outputs)) {
                        patch({ category, ...defaultPortsForCategory(category) });
                      } else {
                        patch({ category });
                      }
                    }}
                  />
                </FormField>
                <FormField
                  label="Hardware dependent"
                  description="Hardware-dependent node types are stubbed in cloud test runs."
                >
                  <Toggle
                    checked={form.hardwareDependent}
                    onChange={({ detail }) => patch({ hardwareDependent: detail.checked })}
                  >
                    Requires device hardware
                  </Toggle>
                </FormField>
              </SpaceBetween>
            ),
          },
          {
            title: 'Ports',
            description: 'Typed input and output ports (types from the Node_Type_Catalog).',
            content: (
              <SpaceBetween size="l">
                {attempted && stepErrors[1].length > 0 && (
                  <Alert type="error">{stepErrors[1].join(' ')}</Alert>
                )}
                <PortGuidancePanel
                  category={form.category}
                  inputs={form.inputs}
                  outputs={form.outputs}
                />
                <PortScanPanel
                  pluginId={plugin.plugin_id}
                  version={plugin.version}
                  preferredFactory={defaultElementFactory(plugin.name)}
                  inputs={form.inputs}
                  outputs={form.outputs}
                  onApply={applyPortScan}
                />
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
                      {form[side].map((port, index) => {
                        // Update-mode removal protection (6.9): a port
                        // the registered declaration depends on cannot
                        // be removed, whatever its provenance.
                        const blockReason = removalBlockReason(
                          side,
                          port.name,
                          existing?.declaration ?? null
                        );
                        const unconfirmed = unconfirmedPortNames.has(
                          port.name.trim()
                        );
                        return (
                          <SpaceBetween key={index} size="xxs">
                            <SpaceBetween direction="horizontal" size="s">
                              <FormField label={index === 0 ? 'Port name' : undefined}>
                                <Input
                                  value={port.name}
                                  placeholder={side === 'inputs' ? 'in' : 'out'}
                                  onChange={({ detail }) =>
                                    patchPort(side, index, { name: detail.value })
                                  }
                                />
                              </FormField>
                              <FormField label={index === 0 ? 'Port type' : undefined}>
                                <Select
                                  selectedOption={{ label: port.portType, value: port.portType }}
                                  options={PORT_TYPES.map((t) => ({ label: t, value: t }))}
                                  onChange={({ detail }) =>
                                    patchPort(side, index, {
                                      portType:
                                        detail.selectedOption.value || port.portType,
                                    })
                                  }
                                />
                              </FormField>
                              <FormField
                                label={index === 0 ? ' ' : undefined}
                                warningText={blockReason ?? undefined}
                              >
                                <Button
                                  iconName="remove"
                                  ariaLabel={`Remove ${side} port ${index + 1}`}
                                  disabled={blockReason != null}
                                  onClick={() => removePort(side, index)}
                                />
                              </FormField>
                              {unconfirmed && (
                                <Badge color="severity-medium">confirm type</Badge>
                              )}
                            </SpaceBetween>
                            {unconfirmed && (
                              <Box color="text-status-warning" fontSize="body-s">
                                This port came from the pad scan with a type
                                GStreamer caps cannot determine (see its caps
                                in the scan summary above): InferenceMeta and
                                EventSignal are DDA semantic concepts, so the
                                type defaults to VideoFrames. Confirm it by
                                re-selecting the type, or change it.
                              </Box>
                            )}
                          </SpaceBetween>
                        );
                      })}
                    </SpaceBetween>
                  </Container>
                ))}
              </SpaceBetween>
            ),
          },
          {
            title: 'Parameters',
            description:
              'Parameters with types, defaults, constraints, descriptions, and example values.',
            isOptional: true,
            content: (
              <SpaceBetween size="l">
                {attempted && stepErrors[2].length > 0 && (
                  <Alert type="error">{stepErrors[2].join(' ')}</Alert>
                )}
                <ParameterScanPanel
                  pluginId={plugin.plugin_id}
                  version={plugin.version}
                  preferredFactory={defaultElementFactory(plugin.name)}
                  parameters={form.parameters}
                  onMerge={applyScanMerge}
                />
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
                            onClick={() => removeParameter(index)}
                          />
                        }
                      >
                        {parameter.name.trim() || `Parameter ${index + 1}`}{' '}
                        {scannedNames.has(parameter.name.trim()) && (
                          <Badge color="blue">from scan</Badge>
                        )}
                      </Header>
                    }
                  >
                    <SpaceBetween size="s">
                      <SpaceBetween direction="horizontal" size="s">
                        <FormField label="Name">
                          <Input
                            value={parameter.name}
                            placeholder="radius"
                            onChange={({ detail }) =>
                              patchParameter(index, { name: detail.value })
                            }
                          />
                        </FormField>
                        <FormField label="Type">
                          <Select
                            selectedOption={{
                              label: parameter.paramType,
                              value: parameter.paramType,
                            }}
                            options={PARAMETER_TYPES.map((t) => ({ label: t, value: t }))}
                            onChange={({ detail }) =>
                              patchParameter(index, {
                                paramType: detail.selectedOption.value || parameter.paramType,
                              })
                            }
                          />
                        </FormField>
                        <FormField label="Required">
                          <Checkbox
                            checked={parameter.required}
                            onChange={({ detail }) =>
                              patchParameter(index, { required: detail.checked })
                            }
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
                          onChange={({ detail }) =>
                            patchParameter(index, { description: detail.value })
                          }
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
                            onChange={({ detail }) =>
                              patchParameter(index, { example: detail.value })
                            }
                          />
                        </FormField>
                        <FormField label={<span>Default <i>- optional</i></span>}>
                          <Input
                            value={parameter.defaultValue}
                            onChange={({ detail }) =>
                              patchParameter(index, { defaultValue: detail.value })
                            }
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
                              onChange={({ detail }) =>
                                patchParameter(index, { enumValues: detail.value })
                              }
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
            title: 'Element mapping',
            description:
              'Map the plugin element and its properties to the declared parameters for each successfully built Target_Architecture.',
            content: (
              <SpaceBetween size="l">
                {attempted && stepErrors[3].length > 0 && (
                  <Alert type="error">{stepErrors[3].join(' ')}</Alert>
                )}
                {form.mappings.map((mapping, index) => (
                  <Container
                    key={mapping.arch}
                    header={
                      <Header variant="h3">
                        {ARCHITECTURE_LABELS[
                          mapping.arch as keyof typeof ARCHITECTURE_LABELS
                        ] ?? mapping.arch}
                      </Header>
                    }
                  >
                    <SpaceBetween size="s">
                      <Checkbox
                        checked={mapping.include}
                        onChange={({ detail }) =>
                          patchMapping(index, { include: detail.checked })
                        }
                      >
                        Declare a mapping for this architecture
                      </Checkbox>
                      {mapping.include && (
                        <>
                          <FormField
                            label="Element factory"
                            description="GStreamer element factory name of the plugin's element."
                          >
                            <Input
                              value={mapping.factory}
                              onChange={({ detail }) =>
                                patchMapping(index, { factory: detail.value })
                              }
                            />
                          </FormField>
                          <FormField
                            label="Element properties"
                            description={
                              'Element property to value template, e.g. {radius} to plumb the radius parameter.'
                            }
                          >
                            <SpaceBetween size="xs">
                              {mapping.properties.map((row, rowIndex) => (
                                <SpaceBetween key={rowIndex} direction="horizontal" size="s">
                                  <Input
                                    value={row.property}
                                    placeholder="property"
                                    ariaLabel={`Property name ${rowIndex + 1} (${mapping.arch})`}
                                    onChange={({ detail }) => {
                                      const properties = [...mapping.properties];
                                      properties[rowIndex] = {
                                        ...properties[rowIndex],
                                        property: detail.value,
                                      };
                                      patchMapping(index, { properties });
                                    }}
                                  />
                                  <Input
                                    value={row.value}
                                    placeholder="{parameter}"
                                    ariaLabel={`Property value ${rowIndex + 1} (${mapping.arch})`}
                                    onChange={({ detail }) => {
                                      const properties = [...mapping.properties];
                                      properties[rowIndex] = {
                                        ...properties[rowIndex],
                                        value: detail.value,
                                      };
                                      patchMapping(index, { properties });
                                    }}
                                  />
                                  <Button
                                    iconName="remove"
                                    ariaLabel={`Remove property ${rowIndex + 1} (${mapping.arch})`}
                                    onClick={() =>
                                      patchMapping(index, {
                                        properties: mapping.properties.filter(
                                          (_, i) => i !== rowIndex
                                        ),
                                      })
                                    }
                                  />
                                </SpaceBetween>
                              ))}
                              <Box>
                                <Button
                                  onClick={() =>
                                    patchMapping(index, {
                                      properties: [
                                        ...mapping.properties,
                                        { property: '', value: '' },
                                      ],
                                    })
                                  }
                                >
                                  Add property
                                </Button>
                              </Box>
                            </SpaceBetween>
                          </FormField>
                        </>
                      )}
                    </SpaceBetween>
                  </Container>
                ))}
              </SpaceBetween>
            ),
          },
          {
            title: 'Use case scoping',
            description:
              'The Use_Cases whose palettes include the custom node type.',
            content: (
              <SpaceBetween size="l">
                {attempted && stepErrors[4].length > 0 && (
                  <Alert type="error">{stepErrors[4].join(' ')}</Alert>
                )}
                <FormField label="Use cases">
                  <Multiselect
                    selectedOptions={form.usecaseIds.map((id) => ({
                      label: useCases.find((uc) => uc.usecase_id === id)?.name || id,
                      value: id,
                    }))}
                    options={
                      useCases.length > 0
                        ? useCases.map((uc) => ({ label: uc.name, value: uc.usecase_id }))
                        : form.usecaseIds.map((id) => ({ label: id, value: id }))
                    }
                    onChange={({ detail }) =>
                      patch({
                        usecaseIds: detail.selectedOptions
                          .map((option) => option.value)
                          .filter((value): value is string => Boolean(value)),
                      })
                    }
                    placeholder="Select use cases"
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
