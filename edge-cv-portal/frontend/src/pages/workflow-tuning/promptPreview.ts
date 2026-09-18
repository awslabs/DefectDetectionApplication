/**
 * Candidate preview projection, warnings and the starter Candidate
 * (quality-prompt-tuning, task 8.3 — Requirements 5.3, 5.4, 5.5, 5.6).
 *
 * `GET .../candidates/{cid}/preview` is the AUTHORITATIVE preview: it runs
 * the real Invocation_Builder (`build_bedrock_invocation` /
 * `build_llm_invocation`) on the PERSISTED Candidate, so it is what the
 * editor shows whenever the draft is saved.
 *
 * Requirement 5.3 asks for the exact text *while* a Candidate is being
 * edited, i.e. before the draft is saved and therefore before the route can
 * see it. The helpers here project the same text client-side from the
 * builder's own two rules — Anomaly_Mode appends the Verdict_Instruction to
 * the user prompt separated by a blank line, and a whitespace-only system
 * prompt is sent as no system text at all — so an unsaved draft shows the
 * same string the saved Candidate will preview. The projection is only ever
 * used for the unsaved draft; as soon as the Candidate is saved the editor
 * replaces it with the route's answer, which is the single source of truth.
 *
 * The warning rules mirror `workflow_tuning.preview_warnings` for the same
 * reason (they must fire on the draft, not one save later); the route's
 * warnings replace them once the draft is saved.
 */
import type { CandidatePreviewWarning } from './types';

/**
 * The Verdict_Instruction the Invocation_Builder appends in Anomaly_Mode
 * (`workflow_core.anomaly_invocation.BEDROCK_JSON_INSTRUCTION`, shared by
 * both node types).
 */
export const VERDICT_INSTRUCTION =
  'Respond with JSON: {"is_anomalous": true|false, "confidence": 0..1}.';

/** Separator between the operator's prompt and the Verdict_Instruction. */
export const INSTRUCTION_SEPARATOR = '\n\n';

/** Token budget below which a truncated answer fails the Verdict_Parser. */
export const MIN_SAFE_MAX_TOKENS = 64;

/** Token budget the builder falls back to on any falsy `max_tokens`. */
export const DEFAULT_MAX_TOKENS = 256;

/**
 * The user message the Invocation_Builder will send for `prompt`: the
 * prompt, a blank line, the Verdict_Instruction.
 */
export function projectUserMessage(prompt: string): string {
  return `${prompt ?? ''}${INSTRUCTION_SEPARATOR}${VERDICT_INSTRUCTION}`;
}

/**
 * The system text the builder will send: absent, empty or whitespace-only
 * ⇒ no system text at all; anything else verbatim (never stripped).
 */
export function projectSystemText(
  systemPrompt: string | null | undefined
): string | null {
  if (systemPrompt === null || systemPrompt === undefined) return null;
  return systemPrompt.trim() ? systemPrompt : null;
}

/** The token budget the builder will use for a configured `max_tokens`. */
export function projectMaxTokens(maxTokens: number | null | undefined): number {
  return maxTokens ? Number(maxTokens) : DEFAULT_MAX_TOKENS;
}

/**
 * Requirements 5.4 and 5.5's non-blocking warnings for a draft Prompt_Set,
 * with the codes and wording of `workflow_tuning.preview_warnings`.
 */
export function draftWarnings(
  prompt: string,
  systemPrompt: string | null,
  maxTokens: number | null
): CandidatePreviewWarning[] {
  const warnings: CandidatePreviewWarning[] = [];
  const budget =
    maxTokens === null || maxTokens === undefined || Number.isNaN(Number(maxTokens))
      ? null
      : Number(maxTokens);
  // Requirement 5.5: below 64 tokens the answer truncates and the parser
  // rejects it.
  if (budget !== null && budget < MIN_SAFE_MAX_TOKENS) {
    warnings.push({
      code: 'max_tokens_truncation',
      message:
        `max_tokens is ${budget}: answers truncated below `
        + `${MIN_SAFE_MAX_TOKENS} tokens fail the verdict parser, which `
        + 'needs a complete JSON object.',
    });
  }
  // Requirement 5.4: a demanded answer format that omits `is_anomalous`.
  const fields: Array<[string, string | null]> = [
    ['prompt', prompt],
    ['systemPrompt', systemPrompt],
  ];
  for (const [field, text] of fields) {
    if (!text) continue;
    const lowered = text.toLowerCase();
    const demandsJson =
      lowered.includes('json') || (text.includes('{') && text.includes('}'));
    if (demandsJson && !lowered.includes('is_anomalous')) {
      warnings.push({
        code: 'answer_schema_missing_is_anomalous',
        field,
        message:
          `The ${field} specifies an answer format that does not include `
          + '"is_anomalous". The verdict parser requires a JSON object '
          + 'carrying is_anomalous (and optionally confidence).',
      });
    }
  }
  return warnings;
}

/**
 * Requirement 5.6's starter Candidate for two-image inspection: describe
 * the reference, describe the input, compare them, separate the differences
 * that are defects from those that are not, and answer in one JSON object
 * carrying `is_anomalous` and `confidence`. Editable, and inserted only when
 * the user asks for it.
 */
export const STARTER_CANDIDATE_NAME = 'Starter (describe · compare · decide)';

export const STARTER_CANDIDATE_PROMPT = [
  'You are inspecting a manufactured part. You are given two images: the '
    + 'first is the input image of the part that was produced, the second is '
    + 'the reference image of how the part should look.',
  '',
  'Work through these steps:',
  '1. Describe the reference image: what the part should look like, its '
    + 'shape, colour, markings and expected features.',
  '2. Describe the input image the same way, without comparing yet.',
  '3. Compare the two descriptions and list every difference you see.',
  '4. For each difference, say whether it is a defect of the part (damage, a '
    + 'missing or extra feature, a wrong colour or shape) or an acceptable '
    + 'variation that is not a defect (lighting, pose, camera angle, '
    + 'background, rendering style, scale, or a difference in how the two '
    + 'images were produced).',
  '5. Decide: the part is anomalous only if at least one difference is a '
    + 'defect of the part itself.',
  '',
  'Answer with your reasoning followed by exactly one JSON object carrying '
    + '"is_anomalous" (true or false) and "confidence" (0..1).',
].join('\n');

/** The starter Candidate as the editor inserts it. */
export const STARTER_CANDIDATE = {
  name: STARTER_CANDIDATE_NAME,
  prompt: STARTER_CANDIDATE_PROMPT,
  systemPrompt: null as string | null,
  maxTokens: 512 as number | null,
};
