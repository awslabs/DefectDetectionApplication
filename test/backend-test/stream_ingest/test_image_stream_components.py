#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""The in-image stream camera components gate (rtsp-rtmp-stream-cameras
Requirements 17.2, 17.3).

``build-custom.sh`` runs this inside the flask-app image it just built, with
``DDA_STREAM_COMPONENT_GATE=1`` and ``--noconftest`` (the suite conftest
mocks ``gi``), and any failure fails the build. It checks what every
LocalServer target needs for RTSP and RTMP with H.264 and H.265, from the
lists the capability probe itself uses (``stream_ingest.capabilities``):

- the GStreamer elements of both ingest heads and of the shared decode
  tail, and the H.264 and H.265 software decoders;
- PyAV, with FFmpeg 6.1 or later (libavformat 60.16, the first with
  Enhanced RTMP), the ``flv`` demuxer and the ``rtmp`` protocol;
- an H.264 and an H.265 sample, which the capability probe must be able to
  generate (it verifies each decoder with one, and reports no decoder
  without it), each decoding in software;
- RTMP end to end, offline: H.264 and H.265 (Enhanced FLV) muxed into FLV,
  as an RTMP server sends them, go through the RTMP head's demuxer and
  decode.

Hardware decoders are not required here: they need a Jetson's device nodes
at run time, where the capability probe reports them.

Anywhere else (the host venv, or a run with the suite conftest), a missing
component skips its check instead of failing it.
"""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from stream_ingest import capabilities  # noqa: E402

#: Set by build-custom.sh's in-image test phase.
GATE = os.environ.get("DDA_STREAM_COMPONENT_GATE") == "1"
CODECS = ("h264", "h265")
LABELS = {"h264": "H.264", "h265": "H.265"}
REQUIRED_ELEMENTS = sorted(set(
    capabilities.HEAD_ELEMENTS["rtsp"]
    + capabilities.HEAD_ELEMENTS["rtmp"]
    + capabilities.TAIL_ELEMENTS
    + tuple(capabilities.SOFTWARE_DECODERS.values())))


def missing(what: str) -> None:
    """Fail the image gate on ``what``; skip anywhere else."""
    message = f"the image lacks {what} (rtsp-rtmp-stream-cameras Requirement 17.3)"
    if GATE:
        pytest.fail(message, pytrace=False)
    pytest.skip(message)


@pytest.fixture(scope="module")
def gst():
    try:
        Gst = capabilities._gstreamer()
        real = isinstance(Gst.version_string(), str)
    except Exception as error:  # noqa: BLE001 - reported as missing
        missing(f"GStreamer ({type(error).__name__}: {error})")
    if not real:
        missing("GStreamer (it is mocked here; the gate runs with --noconftest)")
    return Gst


@pytest.fixture(scope="module")
def pyav():
    try:
        import av
    except Exception as error:  # noqa: BLE001 - reported as missing
        missing(f"PyAV ({type(error).__name__}: {error})")
    return av


@pytest.fixture(scope="module")
def samples(pyav):
    """The capability probe's own samples, one per codec."""
    return {codec: capabilities.sample(codec) for codec in CODECS}


#: Where the backend's own code lives in the image (``app.py`` at the root).
APP_ROOT = os.environ.get("DDA_IMAGE_APP_ROOT", "/")


def backend_packages():
    """The top-level directories of the repository's ``src/backend`` that
    hold Python code: every one must be in the image."""
    return sorted(name for name in os.listdir(_BACKEND)
                  if not name.startswith((".", "__")) and os.path.isdir(os.path.join(_BACKEND, name))
                  and any(file.endswith(".py") for _, _, files in os.walk(os.path.join(_BACKEND, name))
                          for file in files))


def test_every_backend_package_is_in_the_image():
    """Found on hardware (task 25.2): the JP7 ``1.0.50`` image had no
    ``/stream_ingest``, because the Dockerfiles copy the backend package by
    package and had no line for it. This gate imports its modules from the
    mounted repository, so it passed, and the backend crash-looped on the
    device with ``ModuleNotFoundError``. The image's own root must hold every
    package."""
    if not os.path.isfile(os.path.join(APP_ROOT, "app.py")):
        missing(f"the backend application at {APP_ROOT} (no app.py)")
    absent = [name for name in backend_packages() if not os.path.isdir(os.path.join(APP_ROOT, name))]
    if absent:
        missing(f"the backend package(s) {absent} under {APP_ROOT} (a Dockerfile COPY line is missing)")


def test_the_image_imports_the_stream_ingest_service_from_its_own_root():
    """``stream_ingest`` imports from the image's root with the repository
    off the path, as the backend itself imports it."""
    import subprocess

    if not os.path.isfile(os.path.join(APP_ROOT, "app.py")):
        missing(f"the backend application at {APP_ROOT} (no app.py)")
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, "-c", "import stream_ingest, stream_ingest.credentials, stream_ingest.pipeline"],
        cwd=APP_ROOT, env=environment, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        missing(f"an importable stream_ingest under {APP_ROOT}: {result.stderr.strip()[-600:]}")


@pytest.mark.parametrize("element", REQUIRED_ELEMENTS)
def test_gstreamer_element_is_present(gst, element):
    if gst.ElementFactory.find(element) is None:
        missing(f"the GStreamer element {element}")


def test_pyav_bundles_ffmpeg_6_1_or_later(pyav):
    version = tuple((getattr(pyav, "library_versions", None) or {}).get("libavformat") or ())[:2]
    if not version or version < capabilities.MIN_LIBAVFORMAT:
        missing(f"FFmpeg 6.1 or later in PyAV (libavformat {version or 'unknown'}; "
                f"Enhanced RTMP needs 60.16)")


def test_pyav_has_the_flv_demuxer(pyav):
    if "flv" not in (getattr(pyav, "formats_available", None) or ()):
        missing("the FFmpeg flv demuxer")


def test_ffmpeg_has_the_rtmp_protocol(pyav):
    protocols = set(capabilities._ffmpeg_protocols())
    if "rtmp" not in protocols:
        missing(f"the FFmpeg rtmp protocol ({len(protocols)} input protocols found)")


@pytest.mark.parametrize("codec", CODECS)
def test_software_decoding(gst, samples, codec):
    data = samples[codec]
    if not data:
        missing(f"an {LABELS[codec]} encoder for the capability probe's sample")
    decoder = capabilities.SOFTWARE_DECODERS[codec]
    if not capabilities.decodes(gst, codec, decoder, (), data):
        missing(f"{LABELS[codec]} software decoding ({decoder} decoded no frame)")


def _flv_sample(av, codec: str, path: str) -> None:
    """``codec`` frames muxed into FLV, as an RTMP server sends them; H.265
    is carried as Enhanced FLV."""
    import numpy

    width, height = capabilities.SAMPLE_SIZE
    encoder = "libx264" if codec == "h264" else "libx265"
    options = {"x265-params": "log-level=error"} if codec == "h265" else {}
    with av.open(path, mode="w", format="flv") as container:
        stream = container.add_stream(encoder, rate=10, options=options)
        stream.width, stream.height = width, height
        stream.pix_fmt = "yuv420p"
        for index in range(capabilities.SAMPLE_FRAMES):
            image = numpy.zeros((height, width, 3), dtype=numpy.uint8)
            image[:, :, index % 3] = 40 + index * 20
            frame = av.VideoFrame.from_ndarray(image, format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)


@pytest.mark.parametrize("codec", CODECS)
def test_rtmp_head_demuxes_and_decodes(gst, pyav, tmp_path, codec):
    from stream_ingest import health
    from stream_ingest.health import StreamError
    from stream_ingest.rtmp_demux import RtmpDemux

    label = LABELS[codec] + (" over Enhanced RTMP" if codec == "h265" else " over RTMP")
    path = str(tmp_path / f"sample-{codec}.flv")
    try:
        _flv_sample(pyav, codec, path)
    except Exception as error:  # noqa: BLE001 - reported as missing
        missing(f"{label}: FFmpeg could not mux the FLV sample ({type(error).__name__}: {error})")

    chunks, errors = [], []
    demux = RtmpDemux(path)
    try:
        opened = demux.open()
        demux.run(lambda data, pts, dts: chunks.append(data) or True, lambda: False,
                  lambda category, message: errors.append((category, message)))
    except StreamError as error:
        missing(f"{label}: the RTMP head could not demux it ({error.category}: {error.message})")
    finally:
        demux.close()

    assert opened == codec
    # The file ends like a stream that stops; nothing else may go wrong.
    assert errors == [(health.NETWORK_ERROR, "the RTMP stream ended")]
    if not chunks:
        missing(f"{label}: the RTMP head demuxed no video")
    decoder = capabilities.SOFTWARE_DECODERS[codec]
    if not capabilities.decodes(gst, codec, decoder, (), b"".join(chunks)):
        missing(f"{label}: {decoder} decoded no frame of the demuxed stream")
