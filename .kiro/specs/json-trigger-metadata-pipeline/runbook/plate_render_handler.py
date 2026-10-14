# Blue-plate reference renderer (custom_python_preprocess handler).
#
# Deployed as plate_render_1/2/3 in workflow
# 25794912-eb5a-4876-9aef-038e463d61ba v35 (component 35.0.0), verified on
# adlink-dlap-701 (JP7) against LocalServer 1.0.38. Committed here because the
# workflow record is otherwise the only copy of this code; REF_INDEX is the one
# line that differs between the three nodes (0, 1, 2).
#
# Camera-side orientation is handled by a rotate(rotate-180) node ahead of the
# detector, NOT here -- see ROTATE_180 below.

# Renders this inspection's design onto a synthetic blue plate that matches the
# real plates as the camera sees them, so Bedrock compares like with like.
# The incoming camera frame is deliberately discarded: this node exists only to
# supply its bedrock node's `reference` port, and it hangs off the camera
# because the runtime allows exactly one frame-feed source per workflow.
#
# Measured from run 2c1c0631 on adlink-dlap-701: detection crops 1116x773,
# 1112x756, 1101x738; plate blue #309be4 / #2b85de / #2491d7; design PNG
# 860x540 (card aspect).
#
# GEOMETRY -- why this letterboxes: the image Bedrock gets on the camera side is
# a DETECTION CROP (~1112x760), where the plate fills nearly the whole frame.
# The handler, though, must return the incoming frame's exact shape, because the
# bridge writes rows back at the input's stride and the appsrc caps are already
# negotiated. Rendering the plate directly into that frame left it as a small
# card adrift in a large portrait image -- a different subject scale AND aspect
# from the crop, which is exactly what makes the two images hard to compare.
# So the scene is composed at the crop's own geometry, then scaled to fill as
# much of the output frame as it can WITHOUT distorting it, and the remainder is
# filled with neutral letterbox bars. The plate then occupies the same fraction
# of the reference as it does of the crop, at the same aspect ratio.
#
# COLOUR SPACE: the frame the bridge hands us is RGB (the appsink caps pin
# video/x-raw,format=RGB) while cv2.imdecode returns BGR. Every constant below
# is in RGB order. Treating the frame as BGR renders the plate ORANGE, the
# exact red/blue swap of #2a90de.
REF_INDEX = 0

CROP_W, CROP_H = 1112, 760        # mean detection crop Bedrock sees per plate
PLATE_RGB = (42, 144, 222)        # mean measured plate blue, #2a90de
BACKDROP_RGB = (245, 245, 245)    # the light surface the plates rest on
LETTERBOX_RGB = (16, 16, 16)      # neutral bars outside the crop-shaped scene
PLATE_ASPECT = 1.585              # credit-card ratio; design PNG is 1.593
PLATE_FILL = 0.94                 # plate width as a fraction of the scene
DESIGN_FILL_W = 0.62              # design width as a fraction of the plate
CORNER_RADIUS_FRAC = 0.06         # corner rounding, fraction of plate height
INVERT_INK = True                 # the plates are printed white on blue, and
                                  # the design files are black artwork, so the
                                  # ink is painted white
ROTATE_180 = False                # The camera views the plates upside down, but
                                  # that is corrected on the CAMERA side by a
                                  # rotate node ahead of the detector, so the
                                  # crops arrive upright and the reference is
                                  # rendered upright too. Rotating here as well
                                  # would put the two back out of step.


def process_frame(frame, metadata):
    import dda_frames

    trigger = (metadata or {}).get("trigger") or {}
    payload = trigger.get("payload_json") or {}
    refs = payload.get("refs") or []
    if REF_INDEX >= len(refs):
        raise ValueError(
            "plate render: trigger payload has %d ref(s), need index %d "
            "(trigger keys: %s, payload keys: %s)"
            % (len(refs), REF_INDEX, sorted(trigger), sorted(payload)))
    url = (refs[REF_INDEX] or {}).get("image")
    if not url:
        raise ValueError("plate render: refs[%d].image is empty" % REF_INDEX)

    # Keep alpha: load_image() returns BGR only, and these designs are
    # transparent or white-backed PNGs that must be keyed onto the plate.
    raw = dda_frames.load_bytes(url)
    design = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_UNCHANGED)
    if design is None:
        raise ValueError("plate render: refs[%d].image did not decode as an "
                         "image (%d bytes)" % (REF_INDEX, len(raw)))

    if design.ndim == 2:
        design = cv2.cvtColor(design, cv2.COLOR_GRAY2BGR)
    if design.shape[2] == 4:
        colour = design[:, :, :3]
        mask = design[:, :, 3].astype(np.float32) / 255.0
    else:
        # Coverage from luminance, not a hard white threshold: an anti-aliased
        # glyph edge is PARTIAL ink coverage, and treating it as solid ink is
        # what leaves a grey fringe around the artwork once the ink colour is
        # replaced.
        colour = design[:, :, :3]
        grey = cv2.cvtColor(colour, cv2.COLOR_BGR2GRAY).astype(np.float32)
        mask = np.clip((255.0 - grey) / 255.0, 0.0, 1.0)
        mask[mask < 0.02] = 0.0          # kill encoder noise in the white field

    if INVERT_INK:
        # The design files are black artwork standing in for white printing, so
        # the ink is painted pure white and the artwork's own greys become
        # coverage (above) rather than muddy inverted pixels.
        colour = np.full_like(colour, 255)
    else:
        # imdecode gave us BGR; the frame we must return is RGB.
        colour = cv2.cvtColor(colour, cv2.COLOR_BGR2RGB)

    # Trim the PNG's wide margin down to the artwork itself.
    ys, xs = np.where(mask > 0.05)
    if len(xs):
        colour = colour[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        mask = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]

    # ---- compose the scene at the detection crop's own geometry ----------
    scene = np.empty((CROP_H, CROP_W, 3), np.uint8)
    scene[:, :, 0], scene[:, :, 1], scene[:, :, 2] = BACKDROP_RGB

    plate_w = int(CROP_W * PLATE_FILL)
    plate_h = int(round(plate_w / PLATE_ASPECT))
    if plate_h > int(CROP_H * PLATE_FILL):
        plate_h = int(CROP_H * PLATE_FILL)
        plate_w = int(round(plate_h * PLATE_ASPECT))
    px, py = (CROP_W - plate_w) // 2, (CROP_H - plate_h) // 2

    plate = np.zeros((plate_h, plate_w), np.uint8)
    r = max(1, int(plate_h * CORNER_RADIUS_FRAC))
    cv2.rectangle(plate, (r, 0), (plate_w - r, plate_h), 255, -1)
    cv2.rectangle(plate, (0, r), (plate_w, plate_h - r), 255, -1)
    for cx, cy in ((r, r), (plate_w - r, r), (r, plate_h - r),
                   (plate_w - r, plate_h - r)):
        cv2.circle(plate, (cx, cy), r, 255, -1)
    scene[py:py + plate_h, px:px + plate_w][plate > 0] = PLATE_RGB

    dh, dw = colour.shape[:2]
    target_w = max(1, int(plate_w * DESIGN_FILL_W))
    target_h = max(1, int(round(target_w * dh / float(dw))))
    if target_h > int(plate_h * 0.82):
        target_h = int(plate_h * 0.82)
        target_w = max(1, int(round(target_h * dw / float(dh))))
    interp = cv2.INTER_AREA if target_w < dw else cv2.INTER_CUBIC
    colour = cv2.resize(colour, (target_w, target_h), interpolation=interp)
    mask = cv2.resize(mask, (target_w, target_h), interpolation=interp)

    ox = px + (plate_w - target_w) // 2
    oy = py + (plate_h - target_h) // 2
    dst = scene[oy:oy + target_h, ox:ox + target_w].astype(np.float32)
    a = np.clip(mask, 0.0, 1.0)[:, :, None]
    scene[oy:oy + target_h, ox:ox + target_w] = (
        colour.astype(np.float32) * a + dst * (1.0 - a)).astype(np.uint8)

    # Match the camera's viewpoint. Done on the composed scene rather than the
    # artwork alone so the plate and its ink turn together, and before
    # letterboxing so the bars stay where they belong.
    if ROTATE_180:
        scene = cv2.rotate(scene, cv2.ROTATE_180)

    # ---- letterbox the crop-shaped scene into the frame we must return ---
    out_h, out_w = frame.shape[:2]
    channels = frame.shape[2] if frame.ndim > 2 else 1
    scale = min(out_w / float(CROP_W), out_h / float(CROP_H))
    fit_w = max(1, min(out_w, int(round(CROP_W * scale))))
    fit_h = max(1, min(out_h, int(round(CROP_H * scale))))
    if (fit_w, fit_h) != (CROP_W, CROP_H):
        scene = cv2.resize(
            scene, (fit_w, fit_h),
            interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC)

    canvas = np.empty((out_h, out_w, channels), np.uint8)
    if channels >= 3:
        canvas[:, :, 0], canvas[:, :, 1], canvas[:, :, 2] = LETTERBOX_RGB
        if channels > 3:
            canvas[:, :, 3:] = 255
    else:
        canvas[:, :, 0] = LETTERBOX_RGB[0]
    bx, by = (out_w - fit_w) // 2, (out_h - fit_h) // 2
    if channels >= 3:
        canvas[by:by + fit_h, bx:bx + fit_w, :3] = scene
    else:
        canvas[by:by + fit_h, bx:bx + fit_w, 0] = cv2.cvtColor(
            scene, cv2.COLOR_RGB2GRAY)

    if frame.ndim == 2:
        return canvas[:, :, 0]
    return canvas
