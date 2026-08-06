/**
 * Prompt-based generation chat panel for the Workflow_Builder
 * (Requirements 10.1, 10.3, 10.4, 10.7).
 *
 * Rendered inside the builder's right-hand side drawer (the "Generate"
 * tab), the panel accepts natural-language workflow requests and sends
 * them to POST /workflows/generate (10.1). The panel:
 *
 *   - Maintains the chat `session_id` across turns and sends the current
 *     canvas definition with every prompt, so follow-up prompts modify
 *     the workflow rather than regenerating it (10.5, handled backend-side).
 *   - Renders the generated workflow onto the canvas only after both the
 *     backend validation ran (the generate endpoint always validates and
 *     returns the findings, 10.3) and the client-side parse
 *     (`fromWorkflowDefinition` against the node catalog) succeeded. The
 *     findings are displayed with the result for user review before any
 *     save or deployment (10.3).
 *   - On any failure — API error (parse failure backend-side, Bedrock
 *     invocation failure, timeout) or client-side parse failure — shows
 *     the error, leaves the canvas unchanged, and preserves the typed
 *     prompt in the input for retry (10.4, 10.7).
 *
 * Generation_Gate handling (portal-build-fleet-and-workflow-gates
 * Requirements 8.6, 8.8): a 422 `GENERATION_REJECTED` /
 * `GENERATION_VALIDATION_INCOMPLETE` rejection renders an error Alert
 * listing each structural error with the affected nodes/connections
 * (display name, falling back to id) and a plain-language explanation,
 * with the prompt kept in the input for retry (cleared only on 200); a
 * repaired acceptance (`gate.repaired`) renders an info Alert listing
 * the corrected original structural errors.
 *
 * Task 11.6's test panel is the sibling drawer tab; this component is
 * self-contained and exposes no canvas state beyond the
 * `onApplyGenerated` callback. It stays mounted while the drawer is
 * collapsed, so the chat session and a typed prompt survive toggling.
 */

import { useCallback, useState } from 'react';
import type { Edge } from '@xyflow/react';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Spinner from '@cloudscape-design/components/spinner';
import Textarea from '@cloudscape-design/components/textarea';
import { ApiError, apiService } from '../../services/api';
import type { UserRole } from '../../types';
import { fromWorkflowDefinition, type BuilderNode } from './builderGraph';
import { canEditWorkflows } from './WorkflowToolbar';
import type {
  GenerationStructuralError,
  NodeTypeDescriptor,
  ValidationFinding,
  WorkflowDefinition,
  WorkflowGenerationResult,
} from './types';

// --------------------------------------------------------------------------
// Props and message shapes
// --------------------------------------------------------------------------

/** One turn displayed in the chat history. */
export interface ChatMessage {
  role: 'user' | 'assistant';
  text: string;
}

export interface GenerateChatPanelProps {
  /** The acting user's role; generation edits the canvas (11.1). */
  role: UserRole | undefined;
  /** The selected Use_Case scoping generation and its chat session. */
  usecaseId: string | null;
  /** Node catalog used for the client-side parse of the generated definition. */
  catalog: NodeTypeDescriptor[];
  /** The current canvas as a Workflow_Definition document (sent for follow-ups). */
  getDefinition: () => WorkflowDefinition;
  /**
   * Called only after the generated definition passed the client-side
   * parse; the parent replaces the canvas contents (10.3).
   */
  onApplyGenerated: (generated: { nodes: BuilderNode[]; edges: Edge[] }) => void;
}

/**
 * Error codes of a Generation_Gate rejection
 * (portal-build-fleet-and-workflow-gates Req 8.5, 8.8, 8.11): structural
 * rejection and the fail-closed validator-incomplete path. Both leave the
 * canvas unchanged and preserve the prompt for retry.
 */
const GATE_REJECTION_CODES = new Set([
  'GENERATION_REJECTED',
  'GENERATION_VALIDATION_INCOMPLETE',
]);

/** One gate rejection held for display (Req 8.8). */
interface GateRejection {
  message: string;
  structuralErrors: GenerationStructuralError[];
}

/**
 * The affected nodes/connections of one structural error, identified by
 * display name with the id as fallback (Req 8.8); null when the error is
 * graph-level (no affected elements).
 */
function affectedLabel(error: GenerationStructuralError): string | null {
  if (error.affected.length === 0) {
    return null;
  }
  return error.affected
    .map((element) =>
      element.displayName && element.displayName.trim() !== ''
        ? element.displayName
        : element.id
    )
    .join(', ');
}

/** Summary line for the backend validation findings of a generation. */
function validationSummary(result: WorkflowGenerationResult): string {
  if (result.validation_passed) {
    return `Validation passed (${result.warning_count} warning${
      result.warning_count === 1 ? '' : 's'
    })`;
  }
  return `Validation found ${result.error_count} error${
    result.error_count === 1 ? '' : 's'
  } and ${result.warning_count} warning${result.warning_count === 1 ? '' : 's'}`;
}

// --------------------------------------------------------------------------
// Panel
// --------------------------------------------------------------------------

export default function GenerateChatPanel({
  role,
  usecaseId,
  catalog,
  getDefinition,
  onApplyGenerated,
}: GenerateChatPanelProps) {
  const canEdit = canEditWorkflows(role);

  // Chat session: the session id returned by the first successful turn
  // is sent with every subsequent prompt so the backend applies
  // modifications to the session's current definition (10.5).
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);

  // The prompt is cleared only on success; on failure it stays in the
  // input for retry (10.4, 10.7).
  const [prompt, setPrompt] = useState('');

  // Optional per-request model temperature override (0..1). Blank means
  // "use the configured value" - a slider cannot represent that unset
  // state, so a compact number input is used. Sent with the request only
  // when set and valid; lower values reduce invented (hallucinated) nodes.
  const [temperature, setTemperature] = useState('');
  const parsedTemperature = temperature.trim() === '' ? null : Number(temperature);
  const temperatureInvalid =
    parsedTemperature !== null &&
    (!Number.isFinite(parsedTemperature) || parsedTemperature < 0 || parsedTemperature > 1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [findings, setFindings] = useState<ValidationFinding[] | null>(null);
  const [resultSummary, setResultSummary] = useState<string | null>(null);
  // Generation_Gate rejection (422 GENERATION_REJECTED /
  // GENERATION_VALIDATION_INCOMPLETE): the structural errors are listed
  // in an error Alert and the prompt stays in the input for retry (8.8).
  const [rejection, setRejection] = useState<GateRejection | null>(null);
  // Repaired acceptance (gate.repaired): the corrected original
  // Structural_Errors are listed in an info Alert (8.6).
  const [correctedErrors, setCorrectedErrors] = useState<ValidationFinding[] | null>(null);

  const handleSend = useCallback(async () => {
    const trimmed = prompt.trim();
    if (trimmed === '' || !usecaseId || busy || !canEdit || temperatureInvalid) {
      return;
    }
    setBusy(true);
    setError(null);
    setFindings(null);
    setResultSummary(null);
    setRejection(null);
    setCorrectedErrors(null);
    try {
      const definition = getDefinition();
      const result = await apiService.generateWorkflow({
        usecase_id: usecaseId,
        prompt: trimmed,
        ...(sessionId !== null ? { session_id: sessionId } : {}),
        // Send the canvas snapshot when it has content, so follow-ups
        // (and first prompts over an existing canvas) modify rather
        // than regenerate.
        ...(definition.nodes.length > 0 ? { current_definition: definition } : {}),
        // Per-request temperature override; blank uses the configured value.
        ...(parsedTemperature !== null ? { temperature: parsedTemperature } : {}),
      });

      // Client-side parse against the catalog: a definition referencing
      // unknown node types cannot be rendered; the canvas stays
      // unchanged and the prompt is preserved (10.4).
      const generated = fromWorkflowDefinition(result.definition, catalog);

      // Both backend validation and the client-side parse succeeded:
      // render onto the canvas for user review (10.3).
      onApplyGenerated(generated);
      setSessionId(result.session_id);
      setMessages((existing) => [
        ...existing,
        { role: 'user', text: trimmed },
        {
          role: 'assistant',
          text:
            result.assistant_text?.trim() ||
            'Generated a workflow and rendered it on the canvas.',
        },
      ]);
      setFindings(result.findings);
      setResultSummary(validationSummary(result));
      // Repaired acceptance: surface the automatic-correction notice
      // with the corrected original Structural_Errors (8.6).
      if (result.gate?.repaired) {
        setCorrectedErrors(result.gate.corrected_errors);
      }
      setPrompt('');
    } catch (err) {
      // Generation_Gate rejection (8.5, 8.7, 8.11): list each structural
      // error with its affected elements and plain-language explanation;
      // the canvas stays unchanged and the prompt stays in the input for
      // retry (8.8; the prompt is cleared only on a 200 above).
      if (err instanceof ApiError && err.code !== undefined && GATE_REJECTION_CODES.has(err.code)) {
        const structuralErrors = err.details?.structural_errors;
        setRejection({
          message: err.message,
          structuralErrors: Array.isArray(structuralErrors)
            ? (structuralErrors as GenerationStructuralError[])
            : [],
        });
        return;
      }
      // API error (backend parse failure, Bedrock failure, timeout) or
      // client-side parse failure: display the error, leave the canvas
      // unchanged, and preserve the prompt for retry (10.4, 10.7).
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [
    prompt,
    usecaseId,
    busy,
    canEdit,
    temperatureInvalid,
    parsedTemperature,
    sessionId,
    getDefinition,
    catalog,
    onApplyGenerated,
  ]);

  const disabledReason = !canEdit
    ? 'Your role does not permit workflow changes'
    : !usecaseId
      ? 'Select a use case first'
      : undefined;

  return (
    <section aria-label="Generate workflow panel">
      <SpaceBetween size="s">
        {/* Compact drawer-tab title: small bold text, not a heading. */}
        <div>
          <Box fontSize="body-m" fontWeight="bold">
            Generate workflow
          </Box>
          <Box fontSize="body-s" color="text-body-secondary">
            Describe the pipeline in natural language and it is generated onto the canvas for
            review.
          </Box>
        </div>
        {messages.length > 0 && (
          <ul
            aria-label="Chat history"
            style={{ listStyle: 'none', margin: 0, padding: 0, maxHeight: 240, overflowY: 'auto' }}
          >
            {messages.map((message, index) => (
              <li key={index}>
                <Box padding={{ vertical: 'xxs' }}>
                  <Box variant="strong">{message.role === 'user' ? 'You' : 'Assistant'}</Box>
                  <Box color="text-body-secondary">{message.text}</Box>
                </Box>
              </li>
            ))}
          </ul>
        )}

        {error !== null && (
          <Alert
            type="error"
            header="Generation failed"
            dismissible
            onDismiss={() => setError(null)}
          >
            {error} Your prompt is preserved below - you can retry.
          </Alert>
        )}

        {rejection !== null && (
          <Alert
            type="error"
            header="Generation rejected"
            dismissible
            onDismiss={() => setRejection(null)}
          >
            <SpaceBetween size="xs">
              <span>{rejection.message} Your prompt is preserved below - you can retry.</span>
              {rejection.structuralErrors.length > 0 && (
                <ul aria-label="Structural errors" style={{ margin: 0, paddingLeft: 18 }}>
                  {rejection.structuralErrors.map((structuralError, index) => {
                    const affected = affectedLabel(structuralError);
                    return (
                      <li key={`${structuralError.code}-${index}`}>
                        {affected !== null && <Box variant="strong">{affected}: </Box>}
                        {structuralError.explanation}
                      </li>
                    );
                  })}
                </ul>
              )}
            </SpaceBetween>
          </Alert>
        )}

        {correctedErrors !== null && (
          <Alert
            type="info"
            header="Automatic correction applied"
            dismissible
            onDismiss={() => setCorrectedErrors(null)}
          >
            <SpaceBetween size="xs">
              <span>
                The generated workflow had structural errors that were corrected automatically
                before it was rendered on the canvas:
              </span>
              <ul aria-label="Corrected structural errors" style={{ margin: 0, paddingLeft: 18 }}>
                {correctedErrors.map((corrected, index) => (
                  <li key={`${corrected.code}-${index}`}>
                    {corrected.code}: {corrected.message}
                    {corrected.nodeId !== null && ` (node: ${corrected.nodeId})`}
                    {corrected.connectionId !== null && ` (connection: ${corrected.connectionId})`}
                  </li>
                ))}
              </ul>
            </SpaceBetween>
          </Alert>
        )}

        {findings !== null && resultSummary !== null && (
          <Alert
            type={findings.some((finding) => finding.severity === 'error') ? 'warning' : 'success'}
            header={resultSummary}
            dismissible
            onDismiss={() => {
              setFindings(null);
              setResultSummary(null);
            }}
          >
            {findings.length === 0 ? (
              'The generated workflow was rendered on the canvas. Review it before saving or deploying.'
            ) : (
              <ul aria-label="Generation validation findings" style={{ margin: 0, paddingLeft: 18 }}>
                {findings.map((finding, index) => (
                  <li key={`${finding.code}-${index}`}>
                    [{finding.severity}] {finding.code}: {finding.message}
                    {finding.nodeId !== null && ` (node: ${finding.nodeId})`}
                    {finding.connectionId !== null && ` (connection: ${finding.connectionId})`}
                  </li>
                ))}
              </ul>
            )}
          </Alert>
        )}

        <FormField label="Prompt" stretch>
          <Textarea
            value={prompt}
            onChange={({ detail }) => setPrompt(detail.value)}
            placeholder="e.g. Read from the camera, run the defect model, and raise a digital output on detections"
            ariaLabel="Workflow generation prompt"
            rows={3}
            disabled={busy}
          />
        </FormField>
        <FormField
          label={
            <span>
              Temperature <i>- optional</i>
            </span>
          }
          description="Lower values (e.g. 0-0.2) make generation more precise and reduce invented nodes; higher values are more creative."
          errorText={temperatureInvalid ? 'Enter a number between 0 and 1.' : undefined}
        >
          {/* A compact number input rather than a slider: blank must mean
              "use the configured value", which a slider cannot express. */}
          <div style={{ maxWidth: 120 }}>
            <Input
              type="number"
              step={0.05}
              inputMode="decimal"
              value={temperature}
              onChange={({ detail }) => setTemperature(detail.value)}
              placeholder="Configured"
              ariaLabel="Generation temperature"
              disabled={busy}
            />
          </div>
        </FormField>
        <SpaceBetween direction="horizontal" size="xs" alignItems="center">
          <Button
            variant="primary"
            onClick={handleSend}
            disabled={busy || !canEdit || !usecaseId || prompt.trim() === '' || temperatureInvalid}
            disabledReason={disabledReason}
          >
            Send
          </Button>
          {busy && (
            <SpaceBetween direction="horizontal" size="xs" alignItems="center">
              <Spinner />
              <Box color="text-body-secondary">Generating...</Box>
            </SpaceBetween>
          )}
        </SpaceBetween>
      </SpaceBetween>
    </section>
  );
}
