/**
 * Parameter scan panel (gst-parameter-prepopulation, task 7.1,
 * Requirements 5.1–5.5, 6.3, 6.4, 7.1–7.4, 2.5).
 *
 * Rendered by both wizards' Parameters step above the existing
 * Add-parameter controls. Strictly additive: the panel never disables
 * the manual parameter flow or step navigation (5.5) — every degraded
 * scan state is an informational or error Alert beside an untouched
 * manual UI (7.1–7.4).
 *
 * - Without plugin context (Create_Wizard): a static notice that
 *   scanning requires a built plugin (5.6, 7.1).
 * - With plugin context (Registration_Wizard): fetches the stored
 *   Introspection_Report suggestions on mount
 *   (nodeDesignerApi.getGstProperties); when the report is available
 *   and the parameter list is empty, merges once automatically (5.1);
 *   a "Scan plugin properties" button re-scans on demand (5.2);
 *   multi-element reports get a factory Select pre-picked via
 *   pickElement with the wizard's preferred factory (5.4).
 * - After a merge the panel shows the outcome summary — added count and
 *   factory (5.3), names kept as declared (alreadyDeclared, 6.3), and
 *   the skipped properties with reasons (2.5) — and reports the added
 *   names upward through onMerge so the wizard can render the
 *   "from scan" badge on scanned rows (6.4).
 *
 * All scan state is local; the panel communicates only via onMerge.
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
import type { ParameterForm } from './declaration';
import {
  GstPropertiesResponse,
  ScanElement,
  mergeSuggestions,
  pickElement,
} from './scan';

export interface ParameterScanMergeResult {
  parameters: ParameterForm[];
  added: string[];
  alreadyDeclared: string[];
}

export interface ParameterScanPanelProps {
  /** Plugin context; absent in the create wizard (no build yet, 5.6). */
  pluginId?: string;
  version?: number;
  /** The wizard's element factory, pre-picking the element (5.4). */
  preferredFactory?: string;
  /** The wizard's current parameter rows (merge input). */
  parameters: ParameterForm[];
  /** Merge output: the new row list plus added/alreadyDeclared names (6.4). */
  onMerge: (result: ParameterScanMergeResult) => void;
}

/** Outcome of the last merge, for the summary rendering (5.3, 6.3, 2.5). */
interface ScanOutcome {
  factory: string;
  added: string[];
  alreadyDeclared: string[];
  skipped: { name: string; reason: string }[];
}

const NO_PLUGIN_NOTICE =
  'Property scanning pre-populates this list from the built plugin. ' +
  'This plugin has not been built yet; declare parameters manually, or ' +
  'rescan from the registration wizard after the first successful build.';

export default function ParameterScanPanel({
  pluginId,
  version,
  preferredFactory,
  parameters,
  onMerge,
}: ParameterScanPanelProps) {
  const hasPluginContext = Boolean(pluginId) && version !== undefined;

  const [response, setResponse] = useState<GstPropertiesResponse | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [selectedFactory, setSelectedFactory] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<ScanOutcome | null>(null);

  // The merge always reads the wizard's latest rows, not the rows at
  // fetch start (the fetch is asynchronous and the user can keep
  // editing meanwhile — the manual flow is never blocked, 5.5).
  const parametersRef = useRef(parameters);
  parametersRef.current = parameters;
  // The automatic merge runs at most once per mount (5.1).
  const autoMergedRef = useRef(false);

  const applyMerge = (element: ScanElement) => {
    const result = mergeSuggestions(parametersRef.current, element.suggestions);
    onMerge(result);
    setOutcome({
      factory: element.factory,
      added: result.added,
      alreadyDeclared: result.alreadyDeclared,
      skipped: element.skipped,
    });
  };

  const fetchReport = async (): Promise<GstPropertiesResponse | null> => {
    setLoading(true);
    setFetchError(null);
    try {
      const result = await nodeDesignerApi.getGstProperties(pluginId!, version!);
      setResponse(result);
      return result;
    } catch (err: any) {
      // A failed scan request degrades to an error notice; the manual
      // flow stays untouched (7.3).
      setFetchError(err?.message || 'The property scan request failed.');
      setResponse(null);
      return null;
    } finally {
      setLoading(false);
    }
  };

  // Fetch on mount; auto-merge once when the report is available and
  // the parameter list is empty (5.1).
  useEffect(() => {
    if (!hasPluginContext) {
      return;
    }
    let cancelled = false;
    (async () => {
      const result = await fetchReport();
      if (cancelled || !result?.available) {
        return;
      }
      const picked = pickElement(result.elements || [], preferredFactory);
      setSelectedFactory(picked ? picked.factory : null);
      if (autoMergedRef.current) {
        return;
      }
      autoMergedRef.current = true;
      if (picked && parametersRef.current.length === 0) {
        applyMerge(picked);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Manual re-scan (5.2): re-fetch the report (a rebuild may have
  // refreshed it, and this also retries after a fetch failure), keep
  // the user's factory selection when it still exists, then merge.
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
    if (picked) {
      applyMerge(picked);
    }
  };

  // ----------------------------------------------------------- render

  if (!hasPluginContext) {
    // Create wizard: no Plugin_Artifact exists yet (5.6, 7.1).
    return (
      <Alert
        type="info"
        header="Scan plugin properties"
        data-testid="scan-no-plugin-notice"
      >
        {NO_PLUGIN_NOTICE}
      </Alert>
    );
  }

  const elements = response?.available ? response.elements || [] : [];
  const multiElement = elements.length > 1;

  // Unavailability notice per machine-readable reason (7.1, 7.2, 7.4).
  let unavailableAlert = null;
  if (response && !response.available) {
    if (response.reason === 'introspection_failed') {
      unavailableAlert = (
        <Alert
          type="error"
          header="Property scan unavailable"
          data-testid="scan-unavailable-alert"
        >
          Property introspection failed for this build
          {response.message ? `: ${response.message}` : '.'} Declare the
          parameters manually — the rest of the wizard is unaffected.
        </Alert>
      );
    } else if (response.reason === 'no_x86_64_build') {
      unavailableAlert = (
        <Alert
          type="info"
          header="Property scan unavailable"
          data-testid="scan-unavailable-alert"
        >
          Property scanning requires a successful x86_64 build. Build the
          plugin first, or declare the parameters manually.
        </Alert>
      );
    } else {
      // not_captured (or an unrecognized reason): informational (7.4).
      unavailableAlert = (
        <Alert
          type="info"
          header="Property scan unavailable"
          data-testid="scan-unavailable-alert"
        >
          This build predates property capture
          {response.message ? ` (${response.message})` : ''}. Rebuild the
          plugin to capture its properties, or declare the parameters
          manually.
        </Alert>
      );
    }
  }

  return (
    <Container
      data-testid="parameter-scan-panel"
      header={
        <Header
          variant="h3"
          description="Pre-populate the parameter list from the built plugin's element properties. Scanning never changes parameters you already declared."
          actions={
            <Button
              data-testid="scan-button"
              loading={loading}
              onClick={manualScan}
            >
              Scan plugin properties
            </Button>
          }
        >
          Plugin property scan
        </Header>
      }
    >
      <SpaceBetween size="s">
        {fetchError && (
          <Alert
            type="error"
            header="Property scan failed"
            data-testid="scan-error-alert"
          >
            {fetchError} Declare the parameters manually, or retry the scan —
            the rest of the wizard is unaffected.
          </Alert>
        )}

        {unavailableAlert}

        {response?.available && elements.length === 0 && (
          <Alert type="info" data-testid="scan-empty-alert">
            The built plugin registered no elements to scan. Declare the
            parameters manually.
          </Alert>
        )}

        {multiElement && (
          <FormField
            label="Element factory"
            description="The plugin registers multiple elements; choose which element's properties to scan."
          >
            <Select
              data-testid="scan-factory-select"
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

        {outcome && (
          <Box data-testid="scan-outcome">
            <SpaceBetween size="xxs">
              <Box>
                Added {outcome.added.length}{' '}
                {outcome.added.length === 1 ? 'parameter' : 'parameters'} from{' '}
                <code>{outcome.factory}</code>
                {outcome.added.length > 0 ? `: ${outcome.added.join(', ')}` : '.'}
              </Box>
              {outcome.alreadyDeclared.length > 0 && (
                <Box color="text-status-inactive">
                  {outcome.alreadyDeclared.length} already declared (kept as
                  declared): {outcome.alreadyDeclared.join(', ')}
                </Box>
              )}
              {outcome.skipped.length > 0 && (
                <Box color="text-status-inactive">
                  {outcome.skipped.length} skipped:{' '}
                  {outcome.skipped
                    .map((entry) => `${entry.name} (${entry.reason})`)
                    .join(', ')}
                </Box>
              )}
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
