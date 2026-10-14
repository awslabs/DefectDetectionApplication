/**
 * Pure helpers behind the Model Detail "Fine-tuning" section for imported
 * records (rfdetr-training-and-transfer-learning Req 7.2, 7.6; task 7.4c).
 *
 * An import is fine-tunable when Smart Import kept its training checkpoint
 * (`metadata.fine_tunable` set: ultralytics `.pt` -> YOLO, RF-DETR `.pth`
 * -> RF-DETR). Everything else (`metadata.fine_tunable` null or absent) is
 * explained per kind, using the texts fixed in
 * `docs/transfer-learning-spike.md` §4.5; the kind is derived from the
 * record's validator metadata (`framework` / `pt_file` / `model_file`) and
 * `runtime`, the only signals a non-fine-tunable record carries.
 */
import type { TrainingJob } from '../types';

export type ImportedMetadata = NonNullable<TrainingJob['metadata']>;
export type FineTunable = NonNullable<ImportedMetadata['fine_tunable']>;

/** The record fields the helpers read (a `TrainingJob` or any subset). */
export interface ImportedRecordLike {
  source?: string | null;
  runtime?: string | null;
  metadata?: ImportedMetadata | null;
}

export type NotFineTunableKind = 'onnx' | 'torchscript' | 'state_dict';

export const FINE_TUNABLE_ARCH_LABELS: Record<string, string> = {
  yolo: 'YOLO',
  rf_detr: 'RF-DETR',
};

/** Spike §4.5 texts, verbatim (Req 7.2). */
export const NOT_FINE_TUNABLE_EXPLANATIONS: Record<NotFineTunableKind, string> = {
  onnx:
    'ONNX graphs cannot be fine-tuned. To continue training this model in the portal, ' +
    'Smart-Import its training checkpoint (ultralytics .pt or RF-DETR .pth); that import ' +
    'will appear under Base model.',
  torchscript: 'TorchScript models are frozen graphs and cannot be fine-tuned.',
  state_dict:
    'This file is a bare state_dict without a model definition and cannot be fine-tuned.',
};

/** `metadata.fine_tunable` when it is a usable descriptor, else null. */
export function fineTunableOf(record: ImportedRecordLike | null | undefined): FineTunable | null {
  const ft = record?.metadata?.fine_tunable;
  if (!ft || typeof ft !== 'object') return null;
  return typeof ft.arch === 'string' && ft.arch ? ft : null;
}

/** "Fine-tunable (YOLO)" / "Fine-tunable (RF-DETR)"; unknown arches shown as-is. */
export function fineTunableBadgeLabel(ft: FineTunable): string {
  return `Fine-tunable (${FINE_TUNABLE_ARCH_LABELS[ft.arch] ?? ft.arch})`;
}

/**
 * The class list when the checkpoint carries names, else the head width as a
 * count ("90 classes") — published RF-DETR COCO files store no names
 * (spike §2.5) — else null when neither is known.
 */
export function fineTunableClassSummary(ft: FineTunable): string | null {
  const names = Array.isArray(ft.class_names)
    ? ft.class_names.map(n => String(n)).filter(Boolean)
    : [];
  if (names.length > 0) return names.join(', ');
  const n = typeof ft.num_classes === 'number' && Number.isFinite(ft.num_classes) ? ft.num_classes : null;
  if (n === null) return null;
  return `${n} ${n === 1 ? 'class' : 'classes'}`;
}

/**
 * Which not-fine-tunable kind a record is, from the best signal it carries:
 * an `.onnx` artifact / ONNX framework / onnx runtime -> `onnx`; a `.pth`
 * (the bare-weights convention, e.g. LFV `mochi.pth`) -> `state_dict`; any
 * other PyTorch `.pt` on the legacy path -> `torchscript` (the DLR/Neo path
 * packages a traced graph, e.g. LFV `mochi.pt`).
 */
export function notFineTunableKind(record: ImportedRecordLike | null | undefined): NotFineTunableKind {
  const meta = record?.metadata ?? {};
  const file = String(meta.model_file ?? meta.pt_file ?? '').toLowerCase();
  const framework = String(meta.framework ?? '').toUpperCase();
  const runtime = String(record?.runtime ?? '').toLowerCase();
  if (file.endsWith('.onnx') || framework === 'ONNX' || runtime === 'onnx') return 'onnx';
  if (file.endsWith('.pth')) return 'state_dict';
  return 'torchscript';
}

/** The spike §4.5 explanation for a record with no fine-tunable checkpoint. */
export function notFineTunableExplanation(record: ImportedRecordLike | null | undefined): string {
  return NOT_FINE_TUNABLE_EXPLANATIONS[notFineTunableKind(record)];
}
