/**
 * Port scan panel (port-guidance-and-pad-prepopulation, task 8.1,
 * Requirements 6.1–6.4, 6.6, 6.7, 6.10, 7.1–7.3, 7.5, 7.6).
 *
 * Rendered by the Registration wizard's Ports step above the manual
 * port controls, mirroring ParameterScanPanel. Strictly additive: every
 * degraded scan state is an informational or error Alert rendered
 * beside — never instead of — the untouched manual port flow (7.6).
 *
 * - Fetches the stored Introspection_Report on mount
 *   (nodeDesignerApi.getGstProperties); the element is picked with the
 *   same pickElement + preferredFactory selection as the Parameter_Scan
 *   so both scans always agree on the factory (6.6).
 * - Auto-applies at most once per mount, and only when the response is
 *   available, the picked element has padsReason == null with at least
 *   one Port_Suggestion, and isUntouchedDefaults holds at apply time
 *   against the wizard's latest lists read through refs (6.1, 6.7).
 * - A "Scan plugin pads" button re-scans on demand (6.3); it is
 *   disabled while loading so no second scan runs concurrently (6.7)
 *   and doubles as the retry control after a failure (7.3).
 * - After an apply the panel shows the outcome summary: applied names
 *   per side, already-declared names (6.2), each Unconfirmed_Suggestion
 *   with its caps string and confirmation guidance, and each
 *   Unmapped_Pad with its name, direction, presence, and caveat (6.4).
 *   A scan deriving zero suggestions reports that outcome — including
 *   any Unmapped_Pads — and leaves the lists unchanged (6.10, 7.5).
 * - Degraded states (no_x86_64_build, pads_not_captured,
 *   introspection_failed / request failure, pads_read_failed,
 *   no_pad_templates) render as alerts (7.1–7.3, 7.5).
 *
 * All scan state is local; the panel communicates upward only through
 * the single onApply callback.
 */
import { useEffect, useRef, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Container,
  FormField,
  Header,
  Select,
  SpaceBetween,
} from '@cloudscape-design/components';
import { nodeDesignerApi } from './api';
import type { PortForm } from './declaration';
import {
  GstPropertiesResponse,
  ScanElement,
  pickElement,
} from './scan';
import {
  PortSuggestion,
  UnmappedPad,
  applySuggestions,
  isUntouchedDefaults,
} from './portScan';

/** The apply result reported upward through onApply (6.1, 6.2, 6.5). */
export interface PortScanApplyResult {
  inputs: PortForm[];
  outputs: PortForm[];
  /** Names newly added/applied (6.1, 6.11). */
  applied: string[];
  /** Names kept as declared, without modification (6.2). */
  alreadyDeclared: string[];
  /** Applied names needing Port_Type confirmation (6.5). */
  unconfirmed: string[];
}

export interface PortScanPanelProps {
  pluginId: string;
  version: number;
  /** The wizard's declared element factory, pre-picking the element (6.6). */
  preferredFactory?: string;
  /** The wizard's latest port lists (apply input, read through refs). */
  inputs: PortForm[];
  outputs: PortForm[];
  /** Apply output: the new lists plus applied/alreadyDeclared/unconfirmed. */
  onApply: (result: PortScanApplyResult) => void;
}

/** Outcome of the last apply, for the summary rendering (6.2, 6.4). */
interface ScanOutcome {
  factory: string;
  appliedInputs: string[];
  appliedOutputs: string[];
  alreadyDeclared: string[];
  /** Applied Unconfirmed_Suggestions, with caps for display (6.4). */
  unconfirmedSuggestions: PortSuggestion[];
  unmappedPads: UnmappedPad[];
}

const UNCONFIRMED_GUIDANCE =
  'Confirm the port type: InferenceMeta and EventSignal are DDA semantic ' +
  'concepts GStreamer caps cannot express, so this suggestion defaults to ' +
  'VideoFrames until you confirm or change it.';

/** Partition the applied names by their suggestion's direction (6.4). */
function partitionApplied(
  suggestions: PortSuggestion[],
  applied: string[]
): { inputs: string[]; outputs: string[] } {
  const remaining = [...applied];
  const inputs: string[] = [];
  const outputs: string[] = [];
  for (const suggestion of suggestions) {
    let index = remaining.indexOf(suggestion.name);
    if (index === -1) {
      index = remaining.indexOf(suggestion.name.trim());
    }
    if (index === -1) {
      continue;
    }
    const [name] = remaining.splice(index, 1);
    (suggestion.direction === 'input' ? inputs : outputs).push(name);
  }
  return { inputs, outputs };
}

export default function PortScanPanel({
  pluginId,
  version,
  preferredFactory,
  inputs,
  outputs,
  onApply,
}: PortScanPanelProps) {
  const [response, setResponse] = useState<GstPropertiesResponse | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [selectedFactory, setSelectedFactory] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<ScanOutcome | null>(null);

  // The apply always reads the wizard's latest lists, not the lists at
  // fetch start (the fetch is asynchronous and the user can keep
  // editing meanwhile — the manual flow is never blocked, 6.7).
  const inputsRef = useRef(inputs);
  inputsRef.current = inputs;
  const outputsRef = useRef(outputs);
  outputsRef.current = outputs;
  // The automatic apply runs at most once per mount (6.1).
  const autoAppliedRef = useRef(false);

  const applyFromElement = (element: ScanElement) => {
    const suggestions = element.portSuggestions ?? [];
    // Untouched_Defaults is decided at apply time against the latest
    // lists (6.1): replaced when untouched, additively merged when
    // edited (6.2, 6.11); empty suggestions leave the lists unchanged
    // (6.10) and just report the outcome.
    const untouched = isUntouchedDefaults(inputsRef.current, outputsRef.current);
    const result = applySuggestions(
      inputsRef.current,
      outputsRef.current,
      suggestions,
      untouched
    );
    if (suggestions.length > 0) {
      onApply(result);
    }
    const { inputs: appliedInputs, outputs: appliedOutputs } = partitionApplied(
      suggestions,
      result.applied
    );
    const appliedSet = new Set(result.applied);
    setOutcome({
      factory: element.factory,
      appliedInputs,
      appliedOutputs,
      alreadyDeclared: result.alreadyDeclared,
      unconfirmedSuggestions: suggestions.filter(
        (suggestion) =>
          !suggestion.confident &&
          (appliedSet.has(suggestion.name) ||
            appliedSet.has(suggestion.name.trim()))
      ),
      unmappedPads: element.unmappedPads ?? [],
    });
  };

  const fetchReport = async (): Promise<GstPropertiesResponse | null> => {
    setLoading(true);
    setFetchError(null);
    try {
      const result = await nodeDesignerApi.getGstProperties(pluginId, version);
      setResponse(result);
      return result;
    } catch (err: any) {
      // A failed scan request degrades to an error notice; the manual
      // flow stays untouched and the button doubles as retry (7.3).
      setFetchError(err?.message || 'The port scan request failed.');
      setResponse(null);
      return null;
    } finally {
      setLoading(false);
    }
  };

  // Fetch on mount; auto-apply at most once, only when available, the
  // picked element derives at least one suggestion with no padsReason,
  // and the lists are still the Untouched_Defaults at apply time (6.1).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const result = await fetchReport();
      if (cancelled || !result?.available) {
        return;
      }
      const picked = pickElement(result.elements || [], preferredFactory);
      setSelectedFactory(picked ? picked.factory : null);
      if (autoAppliedRef.current || !picked) {
        return;
      }
      const suggestions = picked.portSuggestions ?? [];
      if (
        picked.padsReason == null &&
        suggestions.length > 0 &&
        isUntouchedDefaults(inputsRef.current, outputsRef.current)
      ) {
        autoAppliedRef.current = true;
        applyFromElement(picked);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Manual scan (6.3): re-fetch the report (a rebuild may have
  // refreshed it, and this also retries after a failure, 7.3), keep
  // the user's factory selection when it still exists, then apply.
  const manualScan = async () => {
    const result = await fetchReport();
    if (!result?.available) {
      return;
    }
    const elements = result.elements || [];
    const kept = selectedFactory
      ? elements.find((element) => element.factory === selectedFactory)
      : undefined;
    const picked = kept ?? pickElement(elements, preferredFactory);
    setSelectedFactory(picked ? picked.factory : null);
    if (picked && picked.padsReason == null) {
      applyFromElement(picked);
    }
  };

  // ----------------------------------------------------------- render

  const elements = response?.available ? response.elements || [] : [];
  const multiElement = elements.length > 1;
  // The currently picked element drives the pad-availability alerts
  // reactively — degraded states show without any button press.
  const pickedElement = selectedFactory
    ? elements.find((element) => element.factory === selectedFactory) ?? null
    : pickElement(elements, preferredFactory);

  // Unavailability notice per machine-readable reason (7.1, 7.3).
  let unavailableAlert = null;
  if (response && !response.available) {
    if (response.reason === 'introspection_failed') {
      unavailableAlert = (
        <Alert
          type="error"
          header="Port scan unavailable"
          data-testid="port-scan-unavailable-alert"
        >
          Introspection failed for this build
          {response.message ? `: ${response.message}` : '.'} Declare the ports
          manually, or retry the scan — the rest of the wizard is unaffected.
        </Alert>
      );
    } else if (response.reason === 'no_x86_64_build') {
      unavailableAlert = (
        <Alert
          type="info"
          header="Port scan unavailable"
          data-testid="port-scan-unavailable-alert"
        >
          Port pre-population requires a successful x86_64 build. Build the
          plugin first, or declare the ports manually.
        </Alert>
      );
    } else {
      // not_captured (or an unrecognized reason): informational.
      unavailableAlert = (
        <Alert
          type="info"
          header="Port scan unavailable"
          data-testid="port-scan-unavailable-alert"
        >
          This build has no captured introspection report
          {response.message ? ` (${response.message})` : ''}. Rebuild the
          plugin to capture its pads, or declare the ports manually.
        </Alert>
      );
    }
  }

  // Pad-availability notice for the picked element (7.2, 7.5, 3.2
  // surfacing): the report is available but no pad data was derived.
  let padsAlert = null;
  if (pickedElement) {
    if (pickedElement.padsReason === 'pads_not_captured') {
      padsAlert = (
        <Alert
          type="info"
          header="Pad data unavailable"
          data-testid="port-scan-pads-alert"
        >
          Pad data is unavailable for this build (it predates pad capture).
          Rebuild the plugin to capture its pads, or declare the ports
          manually.
        </Alert>
      );
    } else if (pickedElement.padsReason === 'pads_read_failed') {
      padsAlert = (
        <Alert
          type="error"
          header="Pad data unavailable"
          data-testid="port-scan-pads-alert"
        >
          Reading the element&apos;s pad templates failed
          {pickedElement.padsMessage ? `: ${pickedElement.padsMessage}` : '.'}{' '}
          Declare the ports manually — the rest of the wizard is unaffected.
        </Alert>
      );
    } else if (pickedElement.padsReason === 'no_pad_templates') {
      padsAlert = (
        <Alert
          type="info"
          header="No pad templates"
          data-testid="port-scan-pads-alert"
        >
          The element <code>{pickedElement.factory}</code> declares no static
          pad templates, so there are no port suggestions. Declare the ports
          manually.
        </Alert>
      );
    } else if ((pickedElement.portSuggestions ?? []).length === 0) {
      // padsReason == null with zero suggestions: no always-present
      // pads (7.5); any Unmapped_Pads still surface with caveats (6.10).
      padsAlert = (
        <Alert
          type="info"
          header="No always-present pads"
          data-testid="port-scan-pads-alert"
        >
          <SpaceBetween size="xxs">
            <Box>
              The element <code>{pickedElement.factory}</code> declares no
              always-present pads, so no port suggestions were derived. The
              port lists are unchanged; declare the ports manually.
            </Box>
            {(pickedElement.unmappedPads ?? []).map((pad) => (
              <Box key={`${pad.direction}-${pad.name}`}>
                <code>{pad.name}</code> ({pad.direction}, {pad.presence}):{' '}
                {pad.caveat}
              </Box>
            ))}
          </SpaceBetween>
        </Alert>
      );
    }
  }

  return (
    <Container
      data-testid="port-scan-panel"
      header={
        <Header
          variant="h3"
          description="Pre-populate the port lists from the built plugin's pad templates. Scanning never changes ports you already declared."
          actions={
            <Button
              data-testid="port-scan-button"
              loading={loading}
              disabled={loading}
              onClick={manualScan}
            >
              Scan plugin pads
            </Button>
          }
        >
          Plugin pad scan
        </Header>
      }
    >
      <SpaceBetween size="s">
        {fetchError && (
          <Alert
            type="error"
            header="Port scan failed"
            data-testid="port-scan-error-alert"
          >
            {fetchError} Declare the ports manually, or retry the scan — the
            rest of the wizard is unaffected.
          </Alert>
        )}

        {unavailableAlert}

        {response?.available && elements.length === 0 && (
          <Alert type="info" data-testid="port-scan-empty-alert">
            The built plugin registered no elements to scan. Declare the ports
            manually.
          </Alert>
        )}

        {multiElement && (
          <FormField
            label="Element factory"
            description="The plugin registers multiple elements; choose which element's pads to scan."
          >
            <Select
              data-testid="port-scan-factory-select"
              placeholder="Select an element factory"
              selectedOption={
                selectedFactory
                  ? { label: selectedFactory, value: selectedFactory }
                  : null
              }
              options={elements.map((element) => ({
                label: element.factory,
                value: element.factory,
              }))}
              onChange={({ detail }) =>
                setSelectedFactory(detail.selectedOption.value || null)
              }
            />
          </FormField>
        )}

        {padsAlert}

        {outcome && (
          <Box data-testid="port-scan-outcome">
            <SpaceBetween size="xxs">
              <Box>
                Applied{' '}
                {outcome.appliedInputs.length + outcome.appliedOutputs.length}{' '}
                {outcome.appliedInputs.length + outcome.appliedOutputs.length ===
                1
                  ? 'port'
                  : 'ports'}{' '}
                from <code>{outcome.factory}</code>
                {outcome.appliedInputs.length > 0 ||
                outcome.appliedOutputs.length > 0
                  ? ':'
                  : '.'}
              </Box>
              {outcome.appliedInputs.length > 0 && (
                <Box>Inputs: {outcome.appliedInputs.join(', ')}</Box>
              )}
              {outcome.appliedOutputs.length > 0 && (
                <Box>Outputs: {outcome.appliedOutputs.join(', ')}</Box>
              )}
              {outcome.alreadyDeclared.length > 0 && (
                <Box color="text-status-inactive">
                  {outcome.alreadyDeclared.length} already declared (kept as
                  declared): {outcome.alreadyDeclared.join(', ')}
                </Box>
              )}
              {outcome.unconfirmedSuggestions.map((suggestion) => (
                <Box
                  key={`${suggestion.direction}-${suggestion.name}`}
                  color="text-status-warning"
                >
                  <code>{suggestion.name}</code> needs port type confirmation —
                  caps: <code>{suggestion.caps}</code>
                  {suggestion.capsTruncated ? ' (truncated)' : ''}.{' '}
                  {UNCONFIRMED_GUIDANCE}
                </Box>
              ))}
              {outcome.unmappedPads.map((pad) => (
                <Box
                  key={`${pad.direction}-${pad.name}`}
                  color="text-status-inactive"
                >
                  Not added: <code>{pad.name}</code> ({pad.direction},{' '}
                  {pad.presence}) — {pad.caveat}
                </Box>
              ))}
            </SpaceBetween>
          </Box>
        )}

        {response?.available && response.capturedAt && (
          <Box color="text-status-inactive" fontSize="body-s">
            Captured {response.capturedAt}
            {response.gstVersion ? ` with GStreamer ${response.gstVersion}` : ''}.
          </Box>
        )}
      </SpaceBetween>
    </Container>
  );
}
