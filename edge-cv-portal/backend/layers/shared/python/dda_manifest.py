"""
DDA Manifest Utility
Pure functions for serializing DDA labeling annotations into the exact
DDA augmented manifest (JSON Lines) format consumed by the portal's
existing training and compilation jobs, and for parsing such manifests
back into the canonical annotation model (round-trip property).

Canonical modality-tagged annotation model (design: "Annotation model"):

    # Binary_Classification
    {"modality": "Classification", "label": "normal" | "anomaly"}

    # Object_Detection (pixel coords, validated in-bounds)
    {"modality": "ObjectDetection",
     "image_size": {"width": W, "height": H},
     "boxes": [{"class": "<label-set name>",
                "left": int, "top": int, "width": int, "height": int}]}

    # Semantic_Segmentation (per-class RLE bitmaps at source resolution)
    {"modality": "Segmentation",
     "image_size": {"width": W, "height": H},
     "regions": [{"class": "<label-set name>", "rle": "<COCO-style counts>"}],
     "classification": "normal" | "anomaly"}   # anomaly iff any non-empty region

An AnnotationRecord wraps one canonical annotation with its serialization
context:

    {"source_ref": "s3://bucket/img.png",
     "annotation": <canonical annotation above>,
     "human_annotated": bool,          # True for labeler submissions (req 7.7/8.4),
                                       # False for skip-verification results (req 9.11)
     "confidence": float,              # optional; defaults 1.0 human / 0.99 machine
     "creation_date": "<ISO-8601>",    # the annotation's persisted timestamp
     "mask_s3_uri": "s3://.../mask.png"}   # Segmentation only (key must contain no colons)

A JobContext supplies the job-wide fields:

    {"job_name": str,
     "modality": "Classification" | "Segmentation" | "ObjectDetection",
     "label_set": [str]}               # ordered; ['normal','anomaly'] for Classification

RLE format: COCO-style uncompressed counts — space-separated run lengths
over the mask flattened in column-major (Fortran) order, alternating
background/foreground runs and always starting with the background count
(which may be 0).

Requirements: 10.2, 10.3, 10.4, 10.5, 10.7, 9.11
"""
from __future__ import annotations

import io
import json
from typing import Callable, Dict, List, Optional, Sequence

# Modality identifiers (existing portal task-type identifiers)
MODALITY_CLASSIFICATION = 'Classification'
MODALITY_SEGMENTATION = 'Segmentation'
MODALITY_OBJECT_DETECTION = 'ObjectDetection'
SUPPORTED_MODALITIES = (
    MODALITY_CLASSIFICATION,
    MODALITY_SEGMENTATION,
    MODALITY_OBJECT_DETECTION,
)

# Fixed Binary_Classification label set (req 4.3)
NORMAL_LABEL = 'normal'
ANOMALY_LABEL = 'anomaly'

# Ground Truth type identifiers (what training/compile jobs consume)
CLASSIFICATION_TYPE = 'groundtruth/image-classification'
SEGMENTATION_TYPE = 'groundtruth/semantic-segmentation'
OBJECT_DETECTION_TYPE = 'groundtruth/object-detection'

# Fixed mask palette (req 10.4): background plus one distinct color per
# Label_Set class, deterministic from Label_Set order. Class i gets
# MASK_CLASS_PALETTE[i]; background is white at color-map index 0.
BACKGROUND_CLASS_NAME = 'BACKGROUND'
BACKGROUND_HEX_COLOR = '#FFFFFF'
MASK_CLASS_PALETTE = [
    '#23A436',  # green
    '#1E90FF',  # dodger blue
    '#FF8C00',  # dark orange
    '#B22222',  # firebrick
    '#9400D3',  # dark violet
    '#00CED1',  # dark turquoise
    '#FFD700',  # gold
    '#FF1493',  # deep pink
    '#8B4513',  # saddle brown
    '#2F4F4F',  # dark slate gray
]

# Default machine confidence when the model reports none
DEFAULT_MACHINE_CONFIDENCE = 0.99


# ---------------------------------------------------------------------------
# Color map
# ---------------------------------------------------------------------------

def build_color_map(label_set: List[str]) -> Dict[str, Dict[str, str]]:
    """
    Build the job-wide internal color map for segmentation masks.

    Deterministic from Label_Set order: background is '#FFFFFF' at index
    '0'; class i (zero-based in the Label_Set) gets the fixed palette
    color i at color-map index str(i + 1). The identical map is used for
    every image in a job (req 10.4).

    Args:
        label_set: Ordered, distinct, non-empty class names (1-10 classes)

    Returns:
        {'0': {'class-name': 'BACKGROUND', 'hex-color': '#FFFFFF'},
         '1': {'class-name': label_set[0], 'hex-color': '#23A436'}, ...}

    Raises:
        ValueError: empty label set, more classes than palette colors,
            duplicate or empty class names
    """
    if not label_set:
        raise ValueError('label_set must contain at least one class name')
    if len(label_set) > len(MASK_CLASS_PALETTE):
        raise ValueError(
            f'label_set has {len(label_set)} classes; at most '
            f'{len(MASK_CLASS_PALETTE)} are supported by the fixed palette'
        )
    if len(set(label_set)) != len(label_set):
        raise ValueError('label_set class names must be distinct')
    if any(not name for name in label_set):
        raise ValueError('label_set class names must be non-empty')

    color_map: Dict[str, Dict[str, str]] = {
        '0': {'class-name': BACKGROUND_CLASS_NAME, 'hex-color': BACKGROUND_HEX_COLOR}
    }
    for i, class_name in enumerate(label_set):
        color_map[str(i + 1)] = {
            'class-name': class_name,
            'hex-color': MASK_CLASS_PALETTE[i],
        }
    return color_map


def _hex_to_rgb(hex_color: str) -> tuple:
    """Parse '#RRGGBB' into an (r, g, b) tuple."""
    value = hex_color.lstrip('#')
    if len(value) != 6:
        raise ValueError(f'invalid hex color: {hex_color!r}')
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _color_map_entries(color_map: Dict) -> List[tuple]:
    """
    Normalize a color map into [(index, class_name, hex_color)] sorted by
    index. Accepts str or int keys (JSON round trips produce str keys).
    """
    entries = []
    for key, value in color_map.items():
        index = int(key)
        if not isinstance(value, dict) or 'class-name' not in value or 'hex-color' not in value:
            raise ValueError(f'invalid color map entry for index {key!r}')
        entries.append((index, value['class-name'], value['hex-color']))
    entries.sort(key=lambda e: e[0])
    return entries


# ---------------------------------------------------------------------------
# RLE encoding/decoding (COCO-style counts, column-major)
# ---------------------------------------------------------------------------

def rle_encode(mask: Sequence[int], width: int, height: int) -> str:
    """
    Encode a binary mask into COCO-style RLE counts.

    Args:
        mask: Row-major flat sequence of 0/1 values, length width*height
        width: Mask width in pixels
        height: Mask height in pixels

    Returns:
        Space-separated run lengths over the column-major flattening,
        starting with the background (0) run count.
    """
    if width < 1 or height < 1:
        raise ValueError('width and height must be positive')
    if len(mask) != width * height:
        raise ValueError(f'mask length {len(mask)} != width*height {width * height}')

    counts: List[int] = []
    current = 0
    run = 0
    for p in range(width * height):
        # Column-major position p -> row-major offset
        x, y = divmod(p, height)
        value = 1 if mask[y * width + x] else 0
        if value == current:
            run += 1
        else:
            counts.append(run)
            current = 1 - current
            run = 1
    counts.append(run)
    return ' '.join(str(c) for c in counts)


def rle_decode(rle: str, width: int, height: int) -> bytearray:
    """
    Decode COCO-style RLE counts into a row-major binary mask.

    Args:
        rle: Space-separated run lengths (column-major, background first)
        width: Mask width in pixels
        height: Mask height in pixels

    Returns:
        Row-major bytearray of 0/1 values, length width*height

    Raises:
        ValueError: counts are malformed or do not sum to width*height
    """
    if width < 1 or height < 1:
        raise ValueError('width and height must be positive')
    try:
        counts = [int(token) for token in rle.split()]
    except (AttributeError, ValueError):
        raise ValueError(f'invalid RLE counts: {rle!r}')
    if any(c < 0 for c in counts):
        raise ValueError('RLE counts must be non-negative')
    if sum(counts) != width * height:
        raise ValueError(
            f'RLE counts sum {sum(counts)} != width*height {width * height}'
        )

    mask = bytearray(width * height)
    position = 0
    value = 0
    for count in counts:
        if value:
            for p in range(position, position + count):
                x, y = divmod(p, height)
                mask[y * width + x] = 1
        position += count
        value = 1 - value
    return mask


def _rle_has_foreground(rle: str) -> bool:
    """True when the RLE contains at least one foreground pixel."""
    counts = rle.split()
    return any(int(c) > 0 for c in counts[1::2])


# ---------------------------------------------------------------------------
# Mask rendering/decoding (Pillow)
# ---------------------------------------------------------------------------

def render_mask_png(regions: List[Dict], width: int, height: int,
                    color_map: Dict) -> bytes:
    """
    Render RLE class regions into a palette PNG mask (req 10.4).

    The PNG has the source image's pixel dimensions, one distinct color
    per Label_Set class from the job-wide color map, and the background
    color everywhere else. Overlapping regions are painted in list order
    (later regions win).

    Args:
        regions: [{'class': '<label-set name>', 'rle': '<counts>'}]
        width: Source image width in pixels
        height: Source image height in pixels
        color_map: Output of build_color_map (or its JSON round trip)

    Returns:
        PNG bytes

    Raises:
        ValueError: unclassified region (class None/empty), class not in
            the color map, malformed RLE, or non-positive dimensions
    """
    from PIL import Image  # lazy: only mask paths need Pillow

    if width < 1 or height < 1:
        raise ValueError('width and height must be positive')

    entries = _color_map_entries(color_map)
    name_to_index = {name: index for index, name, _ in entries
                     if name != BACKGROUND_CLASS_NAME}

    index_buffer = bytearray(width * height)  # 0 = background
    for region in regions:
        class_name = region.get('class')
        if not class_name:
            raise ValueError('region has no class; unclassified regions cannot be rendered')
        if class_name not in name_to_index:
            raise ValueError(f'region class {class_name!r} is not in the color map')
        class_index = name_to_index[class_name]
        region_mask = rle_decode(region['rle'], width, height)
        for offset, value in enumerate(region_mask):
            if value:
                index_buffer[offset] = class_index

    palette = [0] * (256 * 3)
    for index, _, hex_color in entries:
        r, g, b = _hex_to_rgb(hex_color)
        palette[index * 3:index * 3 + 3] = [r, g, b]

    image = Image.new('P', (width, height), 0)
    image.putpalette(palette)
    image.putdata(bytes(index_buffer))

    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


def decode_mask_png(png_bytes: bytes, color_map: Dict) -> tuple:
    """
    Decode a rendered PNG mask back into per-class RLE regions.

    Inverse of render_mask_png through the same color map: pixels are
    matched to class colors; background pixels are ignored. Returns one
    region per class that has at least one pixel, ordered by color-map
    index.

    Args:
        png_bytes: PNG mask bytes
        color_map: The job's internal color map

    Returns:
        (regions, width, height) where regions is
        [{'class': name, 'rle': '<counts>'}]

    Raises:
        ValueError: a pixel color is not in the color map
    """
    from PIL import Image  # lazy: only mask paths need Pillow

    image = Image.open(io.BytesIO(png_bytes)).convert('RGB')
    width, height = image.size

    entries = _color_map_entries(color_map)
    background_rgb = None
    rgb_to_name = {}
    class_order = []
    for _, name, hex_color in entries:
        rgb = _hex_to_rgb(hex_color)
        if name == BACKGROUND_CLASS_NAME:
            background_rgb = rgb
        else:
            rgb_to_name[rgb] = name
            class_order.append(name)

    masks: Dict[str, bytearray] = {}
    for offset, pixel in enumerate(image.getdata()):  # row-major
        if pixel == background_rgb:
            continue
        class_name = rgb_to_name.get(pixel)
        if class_name is None:
            raise ValueError(f'mask pixel color {pixel} is not in the color map')
        if class_name not in masks:
            masks[class_name] = bytearray(width * height)
        masks[class_name][offset] = 1

    regions = [
        {'class': name, 'rle': rle_encode(masks[name], width, height)}
        for name in class_order if name in masks
    ]
    return regions, width, height


# ---------------------------------------------------------------------------
# Serialization (canonical annotations -> DDA_Manifest JSON Lines)
# ---------------------------------------------------------------------------

def _resolve_confidence(record: Dict) -> float:
    """
    Confidence for a record's metadata: 1.0 for human annotations,
    the model-reported value clamped to [0, 1] (or 0.99 default) for
    machine annotations (req 10.3).
    """
    if record.get('human_annotated', True):
        confidence = record.get('confidence', 1.0)
    else:
        confidence = record.get('confidence', DEFAULT_MACHINE_CONFIDENCE)
    return min(1.0, max(0.0, float(confidence)))


def _human_annotated_value(record: Dict) -> str:
    """'yes' for labeler submissions, 'no' for machine annotations (req 9.11)."""
    return 'yes' if record.get('human_annotated', True) else 'no'


def _validate_no_colons_in_key(mask_s3_uri: str) -> None:
    """
    Reject mask object keys containing colons (the known GT timestamp bug
    that manifest_validator.py exists to fix; req 10.4).
    """
    if not mask_s3_uri or not mask_s3_uri.startswith('s3://'):
        raise ValueError(f'invalid mask S3 URI: {mask_s3_uri!r}')
    bucket_and_key = mask_s3_uri[len('s3://'):]
    if '/' not in bucket_and_key:
        raise ValueError(f'mask S3 URI has no key: {mask_s3_uri!r}')
    key = bucket_and_key.split('/', 1)[1]
    if ':' in key:
        raise ValueError(f'mask object key must not contain colons: {key!r}')


def _classification_fields(class_name: str, record: Dict, job_name: str) -> Dict:
    """Shared `anomaly-label` + `anomaly-label-metadata` emission (req 10.3)."""
    if class_name not in (NORMAL_LABEL, ANOMALY_LABEL):
        raise ValueError(
            f'classification label must be {NORMAL_LABEL!r} or {ANOMALY_LABEL!r}, '
            f'got {class_name!r}'
        )
    return {
        'anomaly-label': 0 if class_name == NORMAL_LABEL else 1,
        'anomaly-label-metadata': {
            'class-name': class_name,
            'confidence': _resolve_confidence(record),
            'type': CLASSIFICATION_TYPE,
            'job-name': job_name,
            'human-annotated': _human_annotated_value(record),
            'creation-date': record.get('creation_date', ''),
        },
    }


def _serialize_classification(record: Dict, job: Dict) -> Dict:
    annotation = record['annotation']
    entry = {'source-ref': record['source_ref']}
    entry.update(_classification_fields(annotation['label'], record, job['job_name']))
    return entry


def _serialize_segmentation(record: Dict, job: Dict, color_map: Dict) -> Dict:
    annotation = record['annotation']
    regions = annotation.get('regions', [])

    # Derived classification: anomaly iff any non-empty region
    classification = annotation.get('classification')
    if classification is None:
        classification = (
            ANOMALY_LABEL
            if any(_rle_has_foreground(r['rle']) for r in regions)
            else NORMAL_LABEL
        )

    mask_s3_uri = record.get('mask_s3_uri')
    _validate_no_colons_in_key(mask_s3_uri)

    entry = {'source-ref': record['source_ref']}
    entry.update(_classification_fields(classification, record, job['job_name']))
    entry['anomaly-mask-ref'] = mask_s3_uri
    entry['anomaly-mask-ref-metadata'] = {
        'internal-color-map': color_map,
        'type': SEGMENTATION_TYPE,
        'job-name': job['job_name'],
        'human-annotated': _human_annotated_value(record),
        'creation-date': record.get('creation_date', ''),
    }
    return entry


def _serialize_object_detection(record: Dict, job: Dict) -> Dict:
    annotation = record['annotation']
    label_set = job['label_set']
    class_to_id = {name: i for i, name in enumerate(label_set)}
    width = annotation['image_size']['width']
    height = annotation['image_size']['height']
    confidence = _resolve_confidence(record)

    gt_annotations = []
    for box in annotation.get('boxes', []):
        class_name = box.get('class')
        if class_name not in class_to_id:
            raise ValueError(f'box class {class_name!r} is not in the Label_Set')
        left, top = int(box['left']), int(box['top'])
        box_w, box_h = int(box['width']), int(box['height'])
        if left < 0 or top < 0 or box_w < 0 or box_h < 0 \
                or left + box_w > width or top + box_h > height:
            raise ValueError(
                f'box {box!r} lies outside the image bounds {width}x{height}'
            )
        gt_annotations.append({
            'class_id': class_to_id[class_name],
            'left': left,
            'top': top,
            'width': box_w,
            'height': box_h,
        })

    return {
        'source-ref': record['source_ref'],
        'bounding-box': {
            'image_size': [{'width': width, 'height': height, 'depth': 3}],
            'annotations': gt_annotations,
        },
        'bounding-box-metadata': {
            'objects': [{'confidence': confidence} for _ in gt_annotations],
            'class-map': {str(i): name for i, name in enumerate(label_set)},
            'type': OBJECT_DETECTION_TYPE,
            'human-annotated': _human_annotated_value(record),
            'creation-date': record.get('creation_date', ''),
            'job-name': job['job_name'],
        },
    }


def serialize_manifest(annotations: List[Dict], job: Dict) -> List[str]:
    """
    Serialize canonical annotation records into DDA_Manifest JSON Lines.

    Emits exactly one JSON object per record (req 10.2), in the exact
    field shape existing training and compilation jobs consume:

    - Classification (req 10.3): `source-ref`, `anomaly-label` (0=normal,
      1=anomaly), `anomaly-label-metadata` {class-name, confidence in
      [0,1], type, job-name, human-annotated, creation-date}
    - Segmentation (req 10.4): classification fields plus
      `anomaly-mask-ref` (mask S3 URI, key without colons) and
      `anomaly-mask-ref-metadata` carrying the job-wide
      `internal-color-map`
    - Object_Detection (req 10.5): GT `bounding-box` structure with
      zero-based `class_id` and in-bounds pixel coordinates, plus its
      `bounding-box-metadata` class-map

    Args:
        annotations: AnnotationRecord dicts (see module docstring)
        job: JobContext with job_name, modality, label_set, and
            optionally a prebuilt color_map for Segmentation

    Returns:
        JSON Lines strings, one per record, in input order

    Raises:
        ValueError: unsupported modality, record/job modality mismatch,
            invalid labels, out-of-bounds boxes, classes outside the
            Label_Set, or a mask key containing colons
    """
    modality = job.get('modality')
    if modality not in SUPPORTED_MODALITIES:
        raise ValueError(f'unsupported modality: {modality!r}')

    color_map = None
    if modality == MODALITY_SEGMENTATION:
        color_map = job.get('color_map') or build_color_map(job['label_set'])

    lines: List[str] = []
    for record in annotations:
        annotation = record.get('annotation') or {}
        record_modality = annotation.get('modality')
        if record_modality != modality:
            raise ValueError(
                f'annotation modality {record_modality!r} does not match '
                f'job modality {modality!r} for {record.get("source_ref")!r}'
            )
        if modality == MODALITY_CLASSIFICATION:
            entry = _serialize_classification(record, job)
        elif modality == MODALITY_SEGMENTATION:
            entry = _serialize_segmentation(record, job, color_map)
        else:
            entry = _serialize_object_detection(record, job)
        lines.append(json.dumps(entry))
    return lines


# ---------------------------------------------------------------------------
# Parsing (DDA_Manifest JSON Lines -> canonical annotations)
# ---------------------------------------------------------------------------

def _parse_classification(entry: Dict) -> Dict:
    metadata = entry.get('anomaly-label-metadata', {})
    label = ANOMALY_LABEL if int(entry['anomaly-label']) == 1 else NORMAL_LABEL
    return {
        'source_ref': entry['source-ref'],
        'annotation': {'modality': MODALITY_CLASSIFICATION, 'label': label},
        'human_annotated': metadata.get('human-annotated', 'yes') == 'yes',
        'confidence': metadata.get('confidence'),
        'creation_date': metadata.get('creation-date', ''),
    }


def _parse_segmentation(entry: Dict,
                        mask_loader: Optional[Callable[[str], bytes]]) -> Dict:
    metadata = entry.get('anomaly-label-metadata', {})
    mask_metadata = entry.get('anomaly-mask-ref-metadata', {})
    mask_s3_uri = entry.get('anomaly-mask-ref')
    color_map = mask_metadata.get('internal-color-map', {})
    label = ANOMALY_LABEL if int(entry['anomaly-label']) == 1 else NORMAL_LABEL

    annotation: Dict = {
        'modality': MODALITY_SEGMENTATION,
        'classification': label,
    }
    if mask_loader is not None and mask_s3_uri:
        regions, width, height = decode_mask_png(mask_loader(mask_s3_uri), color_map)
        annotation['image_size'] = {'width': width, 'height': height}
        annotation['regions'] = regions
    else:
        annotation['regions'] = None  # not decodable without the mask bytes

    return {
        'source_ref': entry['source-ref'],
        'annotation': annotation,
        'human_annotated': metadata.get('human-annotated', 'yes') == 'yes',
        'confidence': metadata.get('confidence'),
        'creation_date': metadata.get('creation-date', ''),
        'mask_s3_uri': mask_s3_uri,
        'color_map': color_map,
    }


def _parse_object_detection(entry: Dict) -> Dict:
    bounding_box = entry['bounding-box']
    metadata = entry.get('bounding-box-metadata', {})
    class_map = metadata.get('class-map', {})
    image_size = bounding_box['image_size'][0]

    boxes = []
    for gt_box in bounding_box.get('annotations', []):
        class_id = str(gt_box['class_id'])
        if class_id not in class_map:
            raise ValueError(f'class_id {class_id} is not in the class-map')
        boxes.append({
            'class': class_map[class_id],
            'left': int(gt_box['left']),
            'top': int(gt_box['top']),
            'width': int(gt_box['width']),
            'height': int(gt_box['height']),
        })

    objects = metadata.get('objects', [])
    confidence = objects[0].get('confidence') if objects else None

    return {
        'source_ref': entry['source-ref'],
        'annotation': {
            'modality': MODALITY_OBJECT_DETECTION,
            'image_size': {
                'width': int(image_size['width']),
                'height': int(image_size['height']),
            },
            'boxes': boxes,
        },
        'human_annotated': metadata.get('human-annotated', 'yes') == 'yes',
        'confidence': confidence,
        'creation_date': metadata.get('creation-date', ''),
    }


def parse_manifest(lines: List[str], modality: str,
                   mask_loader: Optional[Callable[[str], bytes]] = None) -> List[Dict]:
    """
    Parse DDA_Manifest JSON Lines back into canonical annotation records —
    the inverse of serialize_manifest (round-trip property, req 10.7).

    Args:
        lines: JSON Lines strings (blank lines ignored)
        modality: 'Classification' | 'Segmentation' | 'ObjectDetection'
        mask_loader: Segmentation only — callable mapping a mask S3 URI
            to its PNG bytes so regions can be decoded through the
            entry's internal-color-map. When omitted, segmentation
            records keep their mask_s3_uri and color_map but carry
            regions=None.

    Returns:
        AnnotationRecord dicts (see module docstring) in manifest order

    Raises:
        ValueError: unsupported modality, malformed JSON, or entries
            missing the modality's required fields
    """
    if modality not in SUPPORTED_MODALITIES:
        raise ValueError(f'unsupported modality: {modality!r}')

    records: List[Dict] = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f'line {i + 1}: invalid JSON - {e}')
        try:
            if modality == MODALITY_CLASSIFICATION:
                records.append(_parse_classification(entry))
            elif modality == MODALITY_SEGMENTATION:
                records.append(_parse_segmentation(entry, mask_loader))
            else:
                records.append(_parse_object_detection(entry))
        except (KeyError, TypeError) as e:
            raise ValueError(f'line {i + 1}: missing or invalid field - {e}')
    return records
