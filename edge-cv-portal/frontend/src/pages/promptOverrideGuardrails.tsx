/**
 * promptOverrideGuardrails — the frontend's single source of truth for the
 * grounded-sam Prompt_Guardrail and Prompt_Guidance
 * (grounded-sam-prompt-guardrails-and-prelabel-retry Requirements 1.1,
 * 1.2, 3.1-3.5).
 *
 * Imported by CreateLabelingJob.tsx (wizard setup validation and the
 * override FormFields) and LabelingDetail.tsx (the re-run pre-labels
 * dialog), so both pages reject and teach identically. The backend
 * re-validates with the same rules (dda_labeling.py).
 */
import { Box } from '@cloudscape-design/components';

/**
 * The Alignment_Breaking_Character reject set: exactly the ASCII period.
 *
 * The Grounded-SAM worker's caption-span walk derives its separator set
 * in grounded-sam-worker/handler.py `_marker_token_ids` from
 * `tokenizer.token_to_id('.')` alone (plus the [CLS]/[SEP]/[PAD]
 * specials; [UNK] deliberately excluded). An inner `.` in any prompt
 * therefore splits that prompt into two token spans and trips the
 * worker's spans-vs-prompts alignment guard on every image. `?` `!` `;`
 * `,` and the CJK full stop tokenize as ordinary tokens with their own
 * ids and do not split spans — only `.` is rejected.
 */
export const ALIGNMENT_BREAKING_PATTERN = /\./;

/**
 * True when a Prompt_Override survives trimming — the survival rule the
 * consumer's `_grounded_sam_prompts` applies before falling back to the
 * label name (dda_autolabel_worker.py).
 */
function survivesTrimming(override: string | undefined): override is string {
  return typeof override === 'string' && override.trim() !== '';
}

/**
 * The Effective_Prompt for one Label_Set label: the Prompt_Override when
 * it survives trimming (kept character-for-character, untrimmed),
 * otherwise the label name itself — mirroring the consumer's
 * `_grounded_sam_prompts` fallback, the exact value the Prompt_Map sends
 * to Grounding DINO.
 */
export function effectivePrompt(
  label: string,
  override: string | undefined
): string {
  return survivesTrimming(override) ? override : label;
}

/**
 * A Prompt_Guardrail offender: the label whose Effective_Prompt contains
 * a period, plus which source carries it — the surviving override, or
 * the label-name fallback when no override survives trimming.
 */
export interface PromptGuardrailViolation {
  label: string;
  source: 'override' | 'label';
}

/**
 * First Prompt_Guardrail offender in Label_Set order (the wizard's
 * first-offender error style), or null when every Effective_Prompt is
 * period-free (Requirements 1.1, 1.2).
 */
export function findPromptGuardrailViolation(
  labels: string[],
  overrides: Record<string, string>
): PromptGuardrailViolation | null {
  for (const label of labels) {
    const override = overrides[label];
    if (ALIGNMENT_BREAKING_PATTERN.test(effectivePrompt(label, override))) {
      return {
        label,
        source: survivesTrimming(override) ? 'override' : 'label',
      };
    }
  }
  return null;
}

/**
 * The step-blocking / field-level error for a Prompt_Guardrail violation,
 * naming the label and its offending source (Requirements 1.1, 1.2).
 */
export function promptGuardrailMessage(
  violation: PromptGuardrailViolation
): string {
  if (violation.source === 'override') {
    return `The text prompt for label "${violation.label}" contains a period. Periods separate labels in the detection caption — remove them or split the idea into a short noun phrase`;
  }
  return `Label "${violation.label}" contains a period and has no text prompt. Grounded-SAM uses the label name as its text prompt — enter a text prompt without periods for this label`;
}

/**
 * Constraint text for Prompt_Override entries: the pre-existing length
 * rule plus the no-periods rule (Requirement 3.1).
 */
export const PROMPT_GUIDANCE_CONSTRAINT =
  'Optional, at most 256 characters. No periods — they separate labels in the caption';

/**
 * The Prompt_Guidance info-slot content (Requirements 3.2, 3.3, 3.4),
 * rendered through the FormField `info` slot — the page's Label
 * Categories `info={<Box>…</Box>}` precedent.
 */
export function PromptGuidanceContent() {
  return (
    <Box>
      <Box variant="p">
        A text prompt is a short noun phrase naming the visual thing to
        find — for example "gap between broken cookie pieces" or "scratch
        on metal surface". The detector localizes what the text names,
        and the mask model turns the resulting boxes into masks.
      </Box>
      <Box variant="p">
        Instruction-style text (for example "draw a polygon around each
        gap" or "produce json") does not work: the model grounds noun
        phrases rather than following directions.
      </Box>
      <Box variant="p">
        Periods are not allowed because they separate labels in the
        detection caption. Commas are acceptable. Leave an entry empty to
        use the label name itself as the prompt.
      </Box>
    </Box>
  );
}
