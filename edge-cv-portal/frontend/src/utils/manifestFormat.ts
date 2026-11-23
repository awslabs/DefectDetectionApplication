/**
 * Manifest-format classification for the Create Training page.
 *
 * Extracted from CreateTraining.tsx so the heuristic is unit-testable. The
 * order matters: a bounding-box manifest has `<attr>-metadata` keys exactly
 * like a Ground Truth classification manifest, so it must be recognized
 * BEFORE the Ground Truth check — otherwise the page insists on a manifest
 * transform that manifest_transformer.py cannot perform for ObjectDetection
 * (docs/detection-training-gap.md §5.2). The same holds for an
 * Instance_Manifest (rfdetr-seg-support Req 3.5): it is recognised as
 * `'instance'` before the Ground Truth rule, so it is never sent to the
 * Manifest Transformer.
 */
export type ManifestFormat = 'detection' | 'instance' | 'ground-truth' | 'dda' | 'unknown';

const DDA_BBOX_ATTRIBUTE = 'bounding-box';
const GT_OBJECT_DETECTION_TYPE = 'object-detection';
/** The Instance_Manifest's `<attr>-metadata.type` (backend GT_INSTANCE_SEGMENTATION_TYPE). */
const GT_INSTANCE_SEGMENTATION_TYPE = 'groundtruth/instance-segmentation';

function isObjectDetectionMetadata(value: unknown): boolean {
  if (!value || typeof value !== 'object') return false;
  const type = (value as { type?: unknown }).type;
  return typeof type === 'string' && type.toLowerCase().includes(GT_OBJECT_DETECTION_TYPE);
}

function isInstanceSegmentationMetadata(value: unknown): boolean {
  if (!value || typeof value !== 'object') return false;
  const type = (value as { type?: unknown }).type;
  return typeof type === 'string' && type.toLowerCase() === GT_INSTANCE_SEGMENTATION_TYPE;
}

/**
 * Classify a single manifest sample entry.
 *
 * - `'detection'`: has the DDA literal `bounding-box` object, or any
 *   `<attr>-metadata` whose `type` is a Ground Truth object-detection type
 *   with a sibling `<attr>` object.
 * - `'instance'`: has an `<attr>-metadata` whose `type` is exactly
 *   `groundtruth/instance-segmentation` with a sibling `<attr>` object (an
 *   Instance_Manifest, rfdetr-seg-support Req 3.1).
 * - `'ground-truth'`: has a job-named `<attr>-metadata` key that is not one
 *   of the DDA classification/segmentation metadata keys (needs transform).
 * - `'dda'`: has `anomaly-label` (classification / segmentation DDA format).
 * - `'unknown'`: anything else (including a missing entry).
 */
export function classifyManifestFormat(sampleEntry: unknown): ManifestFormat {
  if (!sampleEntry || typeof sampleEntry !== 'object' || Array.isArray(sampleEntry)) {
    return 'unknown';
  }
  const entry = sampleEntry as Record<string, unknown>;

  const bbox = entry[DDA_BBOX_ATTRIBUTE];
  if (bbox && typeof bbox === 'object') return 'detection';

  for (const [key, value] of Object.entries(entry)) {
    if (!key.endsWith('-metadata') || !isObjectDetectionMetadata(value)) continue;
    const base = key.slice(0, -'-metadata'.length);
    const sibling = entry[base];
    if (sibling && typeof sibling === 'object') return 'detection';
  }

  // Before the generic Ground Truth rule below, which would otherwise send
  // an Instance_Manifest to the transformer (Req 3.5).
  for (const [key, value] of Object.entries(entry)) {
    if (!key.endsWith('-metadata') || !isInstanceSegmentationMetadata(value)) continue;
    const base = key.slice(0, -'-metadata'.length);
    const sibling = entry[base];
    if (sibling && typeof sibling === 'object') return 'instance';
  }

  const hasGroundTruthAttrs = Object.keys(entry).some(
    key =>
      key.endsWith('-metadata') &&
      key !== 'anomaly-label-metadata' &&
      key !== 'anomaly-mask-ref-metadata'
  );
  if (hasGroundTruthAttrs) return 'ground-truth';
  if (entry['anomaly-label'] !== undefined) return 'dda';
  return 'unknown';
}

/** The model_type value the backend accepts for YOLO / RF-DETR detection training. */
export const OBJECT_DETECTION_MODEL_TYPE = 'object_detection';

/** The model_type value the backend accepts for RF-DETR-Seg training (rfdetr-seg-support D3). */
export const INSTANCE_SEGMENTATION_MODEL_TYPE = 'instance_segmentation';

/**
 * The detection-family model types (backend `DETECTION_MODEL_TYPES`): the
 * ones that take the Detection Settings form, an ONNX runtime and the
 * detection detail panels. The one helper every gate shares (C16).
 */
export function isDetectionModelType(modelType: unknown): boolean {
  return modelType === OBJECT_DETECTION_MODEL_TYPE || modelType === INSTANCE_SEGMENTATION_MODEL_TYPE;
}
