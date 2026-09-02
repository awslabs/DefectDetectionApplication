"""Defect E diagnosis: funnel/two-bridge stall on JP7 1.0.16.

Runs the EXACT v5 pipeline (execution ed3b60aa) with synthetic bayer
frame data against the REAL v5 handler artifacts. At t+8s a side thread
dumps every element's state, funnel pad status/caps, and try-pull-preroll
on both bridge appsinks, then dumps a .dot graph of the stalled pipeline.
Then attempts a manual pump through the preprocess bridge to see if the
pipeline unwedges.
"""
import os
import sys
import threading
import time

WORK = "/tmp/defectE-work"
os.makedirs(WORK, exist_ok=True)
os.environ.setdefault("GST_DEBUG_DUMP_DOT_DIR", WORK)
sys.path.insert(0, "/")

from workflow_engine.python_bridge import (  # noqa: E402
    BridgeSpec, build_bridges, run_bridged_pipeline, sink_name, src_name,
)

ART = "/aws_dda/workflows/bdfabc2a-d246-466f-a4ca-53bb40c9e119/5"
W, H = 4608, 3288
PRE, N4 = "custom_python_preprocess_1", "n4"

# Verbatim topology from execution ed3b60aa (v5), work_dir -> WORK.
LAUNCH = (
    "appsrc name=appsrc caps=video/x-bayer,format=bggr "
    "! bayer2rgb ! videoconvert ! tee name=t0 "
    "t0. ! queue ! appsink name=%s emit-signals=true sync=false "
    "max-buffers=1 caps=video/x-raw,format=RGB "
    "appsrc name=%s is-live=true format=time block=true ! tee name=t1 "
    "t1. ! queue ! f0. "
    "t1. ! queue ! videoconvert ! capsfilter caps=video/x-raw,format=I420 "
    "! jpegenc ! multifilesink location=%s/bedrock_frame_pre.jpg "
    "t0. ! queue ! f0. "
    "t0. ! queue ! videoconvert ! capsfilter caps=video/x-raw,format=I420 "
    "! jpegenc ! multifilesink location=%s/bedrock_frame_n2.jpg "
    "funnel name=f0 ! appsink name=%s emit-signals=true sync=false "
    "max-buffers=1 caps=video/x-raw,format=RGB "
    "appsrc name=%s is-live=true format=time block=true ! fakesink sync=false"
) % (sink_name(PRE), src_name(PRE), WORK, WORK, sink_name(N4), src_name(N4))

bridges = build_bridges([
    BridgeSpec(node_id=PRE, handler_path="python/%s/handler.py" % PRE,
               sink_name=sink_name(PRE), src_name=src_name(PRE)),
    BridgeSpec(node_id=N4, handler_path="python/%s/handler.py" % N4,
               sink_name=sink_name(N4), src_name=src_name(N4)),
], ART)

pipeline_box = {}


def _hook_parse():
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    orig = Gst.parse_launch

    def capture(launch):
        p = orig(launch)
        pipeline_box["pipeline"] = p
        pipeline_box["Gst"] = Gst
        return p

    Gst.parse_launch = capture


def interrogate():
    time.sleep(8)
    p = pipeline_box.get("pipeline")
    Gst = pipeline_box.get("Gst")
    if p is None:
        print("DIAG: pipeline never captured", flush=True)
        return
    print("\n=== DIAG t+8s: element states ===", flush=True)
    it = p.iterate_elements()
    while True:
        ok, el = it.next()
        if ok != Gst.IteratorResult.OK:
            break
        st = el.get_state(0)
        print("  %-44s %-10s pending=%-10s ret=%s"
              % (el.get_name(), st.state.value_nick, st.pending.value_nick,
                 st[0].value_nick), flush=True)
    ok, cur, pend = p.get_state(0)
    print("  PIPELINE: %s pending=%s ret=%s"
          % (cur.value_nick, pend.value_nick, ok.value_nick), flush=True)

    f0 = p.get_by_name("f0")
    if f0 is not None:
        print("=== funnel f0 pads ===", flush=True)
        for pad in f0.sinkpads + [f0.srcpad]:
            caps = pad.get_current_caps()
            peer = pad.get_peer()
            print("  %-12s linked=%s peer=%s caps=%s" % (
                pad.get_name(), pad.is_linked(),
                peer.get_parent_element().get_name() if peer else None,
                caps.to_string()[:90] if caps else None), flush=True)

    for name in ("py_in_" + PRE, "py_in_" + N4):
        sink = p.get_by_name(name)
        if sink is None:
            continue
        sample = sink.emit("try-pull-preroll", 0)
        print("  %s try-pull-preroll -> %s" % (
            name, "SAMPLE PRESENT (un-pulled)" if sample is not None
            else "None"), flush=True)
        pipeline_box.setdefault("samples", {})[name] = sample

    Gst.debug_bin_to_dot_file(p, Gst.DebugGraphDetails.ALL, "defectE_stalled")
    print("  dot graph -> %s/defectE_stalled.dot" % WORK, flush=True)

    # Manual pump through the REAL preprocess bridge if a preroll is stuck.
    sample = pipeline_box.get("samples", {}).get("py_in_" + PRE)
    if sample is not None:
        buf = sample.get_buffer()
        okm, mi = buf.map(Gst.MapFlags.READ)
        data = bytes(mi.data)
        buf.unmap(mi)
        caps = sample.get_caps()
        s0 = caps.get_structure(0)
        fmt = s0.get_string("format")
        w = s0.get_int("width").value
        h = s0.get_int("height").value
        print("  manual pump: %d bytes fmt=%s %dx%d" % (
            len(data), fmt, w, h), flush=True)
        src = p.get_by_name("py_out_" + PRE)
        src.set_property("caps", caps)
        try:
            out, _meta = bridges[0].process_frame(
                data, metadata={}, width=w, height=h, frame_format=fmt)
            ob = Gst.Buffer.new_wrapped(out)
            ob.pts = buf.pts
            ob.dts = buf.dts
            print("  manual pump OK -> pushing %d bytes" % len(out),
                  flush=True)
            src.emit("push-buffer", ob)
        except Exception as e:
            print("  manual pump FAILED: %s: %s"
                  % (type(e).__name__, str(e)[:200]), flush=True)


_hook_parse()
threading.Thread(target=interrogate, daemon=True).start()

t0 = time.time()
try:
    run_bridged_pipeline(LAUNCH, bridges, frame_data={
        "data": bytes(W * H), "width": W, "height": H,
        "pixel_format": "bayer:bggr"})
    print("RESULT: completed in %.1fs" % (time.time() - t0), flush=True)
except Exception as e:
    print("RESULT after %.1fs: %s: %s"
          % (time.time() - t0, type(e).__name__, str(e)[:160]), flush=True)
finally:
    for b in bridges:
        try:
            b.stop()
        except Exception:
            pass

jpgs = sorted(f for f in os.listdir(WORK) if f.endswith(".jpg"))
print("jpegs: %s" % jpgs, flush=True)
