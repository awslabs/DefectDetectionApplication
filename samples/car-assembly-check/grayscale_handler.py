# ITU-R 601-2 luma (0.299 R + 0.587 G + 0.114 B), the weights Pillow's mode "L"
# used for the training set. Paste into a custom_python_preprocess node's "code";
# requirements empty, wall_clock_limit_s default. The bridge pins frames to RGB,
# so only the RGB branch runs on a device; the others keep the handler honest.
def process_frame(frame, metadata):
    fmt = (metadata.get("frame") or {}).get("format")
    if fmt == "GRAY8":
        return None  # already single-channel: pass through
    if fmt == "RGB":
        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)
    if fmt == "BGR":
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    raise ValueError("grayscale handler: unsupported frame format %r" % (fmt,))
