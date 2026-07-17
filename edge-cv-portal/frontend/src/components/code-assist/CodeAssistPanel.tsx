/**
 * Shared Code_Assistant panel (custom-node-code-assist, task 6.3).
 *
 * Rendered beside a code editor on every Code_Editing_Surface
 * (Requirements 1.1-1.3). The user describes the desired custom code or
 * filter in a prompt (1-4,000 characters, Requirement 1.4); the panel
 * POSTs `/code-assist` and shows the returned code for review before
 * any change to the editor (Requirement 2.4). Accepting hands the code
 * to the owning surface through `onAccept` — the ONLY path by which the
 * panel touches the editor (Requirement 2.5); rejecting discards the
 * code and keeps the prompt (Requirement 2.9). The panel never saves or
 * persists anything itself (Requirement 2.7) and never touches the
 * `requirements` parameter or the enclosing workflow/declaration, so a
 * failed invocation structurally leaves them unchanged (Requirement 5.4).
 *
 * All state transitions live in the pure reducer (codeAssistState.ts);
 * this component owns only the side effects: the API call on submit and
 * the onAccept callback on accept. The editor is a sibling component the
 * panel holds no reference to, so it stays enabled during an invocation
 * (Requirement 1.5).
 */
import { useReducer } from 'react';
import {
  Alert,
  Box,
  Button,
  FormField,
  SpaceBetween,
  StatusIndicator,
  Textarea,
} from '@cloudscape-design/components';
import {
  CodeAssistContract,
  CodeAssistRequest,
  apiService,
} from '../../services/api';
import {
  INITIAL_CODE_ASSIST_STATE,
  PROMPT_MAX_LENGTH,
  codeAssistReducer,
  isSubmittablePrompt,
} from './codeAssistState';
import { describeCodeAssistError } from './errors';

export interface CodeAssistPanelProps {
  /** Use_Case the edited workflow or node declaration belongs to. */
  usecaseId: string | null;
  /** Code_Editing_Surface kind, for backend authorization. */
  surface: 'workflow-builder' | 'node-designer';
  /** Node_Contract of the node type being edited. */
  contract: CodeAssistContract;
  /** Extra prompt context (frame_hook parameters, node type). */
  context?: CodeAssistRequest['context'];
  /**
   * Live editor value. Sent as `current_code` iff it contains a
   * non-whitespace character (Requirements 2.6, 2.10).
   */
  editorCode: string;
  /**
   * Accepted-code callback — the only path that touches the editor
   * (Requirements 2.5, 5.4). The panel itself persists nothing (2.7).
   */
  onAccept: (code: string) => void;
}

const CODE_BLOCK_STYLE: React.CSSProperties = {
  margin: 0,
  padding: '12px',
  maxHeight: '400px',
  overflow: 'auto',
  fontFamily: 'Monaco, Menlo, "Ubuntu Mono", monospace',
  fontSize: '12px',
  lineHeight: 1.5,
  whiteSpace: 'pre',
  border: '1px solid #d1d5db',
  borderRadius: '8px',
};

export default function CodeAssistPanel({
  usecaseId,
  surface,
  contract,
  context,
  editorCode,
  onAccept,
}: CodeAssistPanelProps) {
  const [state, dispatch] = useReducer(codeAssistReducer, INITIAL_CODE_ASSIST_STATE);

  const submitting = state.phase === 'submitting';
  const submittable = isSubmittablePrompt(state.prompt);

  // Mirror of the reducer's submit guard, plus the use-case precondition
  // the request body needs. The reason feeds the Generate button's
  // disabledReason tooltip (Requirements 1.4, 1.6, 2.8).
  const disabledReason = submitting
    ? 'A generation request is already in progress.'
    : !usecaseId
      ? 'Select a use case first.'
      : state.prompt.trim().length === 0
        ? 'Enter a description of the code or filter you need.'
        : state.prompt.length > PROMPT_MAX_LENGTH
          ? `Shorten the prompt to at most ${PROMPT_MAX_LENGTH.toLocaleString()} characters.`
          : null;

  const submit = async () => {
    // Same guard as the reducer: never fire a request the state machine
    // would ignore, and never double up an in-flight invocation
    // (Requirements 1.6, 2.8).
    if (state.phase !== 'idle' || !submittable || !usecaseId) {
      return;
    }
    const request: CodeAssistRequest = {
      usecase_id: usecaseId,
      surface,
      contract,
      prompt: state.prompt,
    };
    // `current_code` present iff the editor holds a non-whitespace
    // character; a blank editor is a fresh generation, not a
    // modification of empty code (Requirements 2.6, 2.10).
    if (editorCode.trim().length > 0) {
      request.current_code = editorCode;
    }
    if (context) {
      request.context = context;
    }
    dispatch({ type: 'submit' });
    try {
      const response = await apiService.codeAssist(request);
      dispatch({ type: 'succeeded', code: response.code, notes: response.notes });
    } catch (err) {
      // Back to idle with the prompt exactly as submitted, error shown
      // inline (Requirements 5.1, 5.2, 5.3, 5.5).
      dispatch({ type: 'failed', error: describeCodeAssistError(err) });
    }
  };

  const accept = () => {
    if (state.phase !== 'reviewing') {
      return;
    }
    const { code } = state;
    dispatch({ type: 'accept' });
    onAccept(code);
  };

  return (
    <SpaceBetween size="s">
      {state.phase === 'idle' && state.error && (
        <Alert type="error" header={state.error.header}>
          <SpaceBetween size="xxs">
            <div>{state.error.message}</div>
            <div>Your prompt is preserved below — adjust it and retry.</div>
          </SpaceBetween>
        </Alert>
      )}

      <FormField
        label="Code assistant"
        description="Describe the custom code or filter you need; the generated code is shown for review before it touches the editor."
        constraintText={`${state.prompt.length.toLocaleString()} / ${PROMPT_MAX_LENGTH.toLocaleString()} characters`}
        stretch
      >
        <Textarea
          value={state.prompt}
          onChange={({ detail }) => dispatch({ type: 'edit-prompt', value: detail.value })}
          disabled={state.phase !== 'idle'}
          rows={3}
          spellcheck={false}
          placeholder="Blur all faces in the frame using a Gaussian blur"
          ariaLabel="Code assistant prompt"
        />
      </FormField>

      <Button
        variant="primary"
        disabled={submitting || !submittable || !usecaseId}
        disabledReason={disabledReason ?? undefined}
        onClick={submit}
      >
        Generate
      </Button>

      {submitting && (
        <StatusIndicator type="in-progress">
          Generating code — you can keep editing the code while this runs…
        </StatusIndicator>
      )}

      {state.phase === 'reviewing' && (
        <SpaceBetween size="s">
          {state.notes && <Box>{state.notes}</Box>}
          <pre style={CODE_BLOCK_STYLE} aria-label="Generated code for review">
            {state.code}
          </pre>
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="primary" onClick={accept}>
              Accept
            </Button>
            <Button onClick={() => dispatch({ type: 'reject' })}>Reject</Button>
          </SpaceBetween>
        </SpaceBetween>
      )}
    </SpaceBetween>
  );
}
