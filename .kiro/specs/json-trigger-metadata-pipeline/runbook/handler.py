"""Dual-path Custom Python source handler: extracts the run's frame
from the trigger payload — base64-embedded image data (wins) or an
image URI loaded via dda_frames — and echoes the trigger context plus
the image source into the producer metadata."""


def _produced(frame, image_source, context):
    """frame array -> the produce_frame mapping return, carrying the
    produce-request context echo and the image source used."""
    metadata = {"context": dict(context or {}), "image_source": image_source}
    if frame.ndim == 2:
        return {"array": frame, "format": "GRAY8", "metadata": metadata}
    # OpenCV decodes color images as BGR; the mapping return carries
    # bytes with no channel conversion, so convert to RGB here.
    return {"array": frame[:, :, ::-1], "format": "RGB",
            "metadata": metadata}


def _decode_b64_image(image_b64):
    """image_b64 field -> uint8 image array (2-D grayscale or BGR);
    every failure raises ValueError naming image_b64."""
    import base64

    import cv2
    import numpy

    try:
        raw = base64.b64decode(image_b64, validate=True)
    except (TypeError, ValueError) as e:
        raise ValueError(
            "image_b64 is not decodable as base64: {0}".format(e)
        )
    if not raw:
        raise ValueError("image_b64 decoded to zero bytes")
    buffer = numpy.frombuffer(raw, dtype=numpy.uint8)
    frame = cv2.imdecode(buffer, cv2.IMREAD_UNCHANGED)
    if frame is not None and frame.ndim == 2 \
            and frame.dtype == numpy.uint8:
        return frame  # grayscale sources decode to a 2-D array
    frame = cv2.imdecode(buffer, cv2.IMREAD_COLOR)  # 8-bit BGR
    if frame is None:
        raise ValueError(
            "image_b64 decoded bytes could not be decoded as an image"
        )
    return frame


def produce_frame(context):
    import dda_frames

    payload = context.get("payload_json") or {}
    if not isinstance(payload, dict):
        payload = {}
    image_b64 = payload.get("image_b64")
    image_uri = payload.get("image_uri")
    if image_b64:
        # base64 wins over the URI; the URI is never fetched.
        return _produced(_decode_b64_image(image_b64), "base64", context)
    if image_uri:
        # load_image errors already name the URI (and the prefix
        # restriction when the gate rejects it).
        frame = dda_frames.load_image(image_uri)
        return _produced(frame, image_uri, context)
    raise ValueError(
        "no image source in trigger payload: expected 'image_b64' "
        "or 'image_uri'"
    )
