"""Sandbox integration test fixtures (task 11.8).

Containerized end-to-end tests of the test harness (Requirements 12.5,
12.9, 12.13). Prerequisites — each test skips cleanly with a reason when
one is missing:

- A working Docker daemon (the harness's real GStreamer execution path
  only exists inside the sandbox container image).
- ``moto`` with its server extra (``flask``) to stand in for S3; the
  container reaches it through ``AWS_ENDPOINT_URL`` over host networking.
- ``Pillow`` on the host to generate the tiny JPEG Test_Dataset.

The image under test is a slim variant of ``test-sandbox/Dockerfile``:
identical Ubuntu 22.04 + GStreamer + harness layers, without the
multi-gigabyte CPU Triton stage (``COPY --from=triton``) and without the
proprietary DDA plugin ``.so`` set, neither of which is available in CI.
Set ``SANDBOX_IT_IMAGE`` to a fully built ``dda-workflow-test-sandbox``
image to run against the real image instead. Consequence (documented
limitation): emltriton/CPU-Triton inference is not end-to-end testable
here, so the sample workflow exercises the identical harness execution
semantics (dataset staging, ``{dataset_location}`` resolution,
``Gst.parse_launch`` execution, incremental results flushing) with stock
GStreamer elements only.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import pytest

TEST_SANDBOX_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EDGE_CV_PORTAL_DIR = os.path.dirname(TEST_SANDBOX_DIR)
WORKFLOW_CORE_PYTHON_DIR = os.path.join(
    EDGE_CV_PORTAL_DIR, "backend", "layers", "workflow_core", "python")
TEST_RUNNER_STACK_TS = os.path.join(
    EDGE_CV_PORTAL_DIR, "infrastructure", "lib", "test-runner-stack.ts")

if TEST_SANDBOX_DIR not in sys.path:
    sys.path.insert(0, TEST_SANDBOX_DIR)
# Appended rather than prepended: python/ also carries the layer's
# vendored Lambda-runtime dependencies (CPython 3.11 manylinux wheels,
# e.g. jsonschema's rpds), which must not shadow the host interpreter's
# own packages when the tests run locally.
if WORKFLOW_CORE_PYTHON_DIR not in sys.path:
    sys.path.append(WORKFLOW_CORE_PYTHON_DIR)

#: Tag the slim image is built under (stable so docker layer caching works).
SLIM_IMAGE_TAG = "dda-workflow-test-sandbox:it-slim"

#: Slim variant of test-sandbox/Dockerfile: the Triton COPY stage is the
#: only omission (see module docstring); every other layer matches.
SLIM_DOCKERFILE = """\
FROM public.ecr.aws/ubuntu/ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \\
        python3 \\
        python3-pip \\
        python3-gi \\
        python3-gst-1.0 \\
        gstreamer1.0-tools \\
        gstreamer1.0-plugins-base \\
        gstreamer1.0-plugins-good \\
        gstreamer1.0-plugins-bad \\
        gstreamer1.0-libav \\
        gir1.2-gst-plugins-base-1.0 \\
        libexif12 \\
        libcurl4 \\
        libarchive13 \\
        libb64-0d \\
        ca-certificates \\
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /aws_dda/dda_triton/triton_model_repo
COPY test-sandbox/plugins/ /opt/dda/gst-plugins/
ENV GST_PLUGIN_PATH=/opt/dda/gst-plugins
COPY test-sandbox/requirements.txt /opt/harness/requirements.txt
RUN python3 -m pip install --no-cache-dir -r /opt/harness/requirements.txt
COPY backend/layers/workflow_core/python/workflow_core /opt/harness/workflow_core
COPY test-sandbox/harness /opt/harness/harness
ENV PYTHONPATH=/opt/harness
WORKDIR /opt/harness
CMD ["python3", "-m", "harness"]
"""


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: containerized sandbox end-to-end tests (task 11.8); "
        "skip cleanly when Docker/moto/Pillow prerequisites are missing",
    )


# ---------------------------------------------------------------------------
# Prerequisite probes
# ---------------------------------------------------------------------------

def _docker_ready():
    if shutil.which("docker") is None:
        return False, "docker CLI not found on PATH"
    probe = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    if probe.returncode != 0:
        return False, "docker daemon is not reachable (docker info failed)"
    return True, None


def _moto_server_ready():
    try:
        import moto.server  # noqa: F401  (needs the flask server extra)
    except ImportError as error:
        return False, "moto server unavailable: {0}".format(error)
    return True, None


def _pillow_ready():
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False, "Pillow is not installed on the host"
    return True, None


# ---------------------------------------------------------------------------
# Session fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def docker():
    ok, reason = _docker_ready()
    if not ok:
        pytest.skip(reason)


@pytest.fixture(scope="session")
def sandbox_image(docker):
    """The sandbox container image tag to test against.

    ``SANDBOX_IT_IMAGE`` selects a prebuilt image (e.g. the full
    Triton-bearing ``dda-workflow-test-sandbox``); otherwise the slim
    variant is built from the repository sources.
    """
    override = os.environ.get("SANDBOX_IT_IMAGE")
    if override:
        return override

    # The context lives next to the sandbox sources (not /tmp): snap-
    # confined Docker installations cannot read /tmp or hidden home
    # paths. The directory is removed after the build.
    context_dir = tempfile.mkdtemp(prefix="sandbox-it-context-",
                                   dir=TEST_SANDBOX_DIR)
    try:
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
        shutil.copytree(
            os.path.join(TEST_SANDBOX_DIR, "harness"),
            os.path.join(context_dir, "test-sandbox", "harness"),
            ignore=ignore)
        shutil.copytree(
            os.path.join(TEST_SANDBOX_DIR, "plugins"),
            os.path.join(context_dir, "test-sandbox", "plugins"))
        shutil.copyfile(
            os.path.join(TEST_SANDBOX_DIR, "requirements.txt"),
            os.path.join(context_dir, "test-sandbox", "requirements.txt"))
        shutil.copytree(
            os.path.join(WORKFLOW_CORE_PYTHON_DIR, "workflow_core"),
            os.path.join(context_dir, "backend", "layers", "workflow_core",
                         "python", "workflow_core"),
            ignore=ignore)
        dockerfile = os.path.join(context_dir, "Dockerfile")
        with open(dockerfile, "w") as handle:
            handle.write(SLIM_DOCKERFILE)

        build = subprocess.run(
            ["docker", "build", "-t", SLIM_IMAGE_TAG, "-f", dockerfile,
             context_dir],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=1800)
        if build.returncode != 0:
            pytest.skip("sandbox image build failed (likely no network "
                        "access to base image/apt/pip):\n"
                        + build.stdout[-2000:])
        return SLIM_IMAGE_TAG
    finally:
        shutil.rmtree(context_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def moto_endpoint():
    """A moto S3 server on a free localhost port, reachable from
    ``--network=host`` containers via ``AWS_ENDPOINT_URL``."""
    ok, reason = _moto_server_ready()
    if not ok:
        pytest.skip(reason)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    process = subprocess.Popen(
        [sys.executable, "-m", "moto.server", "-H", "127.0.0.1",
         "-p", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    endpoint = "http://127.0.0.1:{0}".format(port)
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                if process.poll() is not None:
                    pytest.skip("moto server exited immediately")
                time.sleep(0.2)
        else:
            pytest.skip("moto server did not start listening within 30s")
        yield endpoint
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture(scope="session")
def s3(moto_endpoint):
    import boto3
    return boto3.client(
        "s3", endpoint_url=moto_endpoint, region_name="us-east-1",
        aws_access_key_id="testing", aws_secret_access_key="testing")


# ---------------------------------------------------------------------------
# Per-test run scaffolding
# ---------------------------------------------------------------------------

class SandboxRun:
    """One prepared test run: bucket keys plus a container runner."""

    def __init__(self, s3_client, moto_url, image, bucket, run_id):
        self.s3 = s3_client
        self.moto_url = moto_url
        self.image = image
        self.bucket = bucket
        self.run_id = run_id
        self.dataset_prefix = "testdata/{0}/dataset".format(run_id)
        self.results_key = "testruns/{0}/results.json".format(run_id)
        self.compiled_key = "testruns/{0}/compiled.json".format(run_id)

    def upload_document(self, document):
        self.s3.put_object(Bucket=self.bucket, Key=self.compiled_key,
                           Body=json.dumps(document).encode("utf-8"))

    def upload_dataset_jpegs(self, count=3, size=(32, 24)):
        """Generate ``count`` tiny JPEGs with Pillow and upload them as
        the Test_Dataset (small Test_Dataset per Requirement 12.5)."""
        ok, reason = _pillow_ready()
        if not ok:
            pytest.skip(reason)
        from PIL import Image
        colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
        for index in range(count):
            with tempfile.NamedTemporaryFile(suffix=".jpg") as handle:
                Image.new("RGB", size, colors[index % len(colors)]).save(
                    handle.name, "JPEG", quality=90)
                self.s3.upload_file(
                    handle.name, self.bucket,
                    "{0}/sample_{1}.jpg".format(self.dataset_prefix, index))

    def container_env(self, extra=None):
        env = {
            "TEST_RUN_ID": self.run_id,
            "WORKFLOW_ID": "wf-integration",
            "USECASE_ID": "uc-integration",
            "ARTIFACTS_BUCKET": self.bucket,
            "DATASET_S3_PREFIX": self.dataset_prefix,
            "RESULTS_S3_KEY": self.results_key,
            "COMPILED_DOCUMENT_S3_KEY": self.compiled_key,
            # moto S3 through boto3's endpoint override; dummy credentials.
            "AWS_ENDPOINT_URL": self.moto_url,
            "AWS_ACCESS_KEY_ID": "testing",
            "AWS_SECRET_ACCESS_KEY": "testing",
            "AWS_DEFAULT_REGION": "us-east-1",
        }
        env.update(extra or {})
        return env

    def run_harness(self, extra_env=None, command=None, timeout=300):
        """``docker run`` the harness; returns (exit_code, logs)."""
        args = ["docker", "run", "--rm", "--network=host"]
        for name, value in self.container_env(extra_env).items():
            args.extend(["-e", "{0}={1}".format(name, value)])
        args.append(self.image)
        if command:
            args.extend(command)
        completed = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout)
        return completed.returncode, completed.stdout

    def fetch_results(self):
        response = self.s3.get_object(Bucket=self.bucket, Key=self.results_key)
        return json.loads(response["Body"].read().decode("utf-8"))


@pytest.fixture
def sandbox_run(s3, moto_endpoint, sandbox_image):
    run_id = "it-{0}".format(uuid.uuid4().hex[:12])
    bucket = "sandbox-it-{0}".format(run_id)
    s3.create_bucket(Bucket=bucket)
    return SandboxRun(s3, moto_endpoint, sandbox_image, bucket, run_id)
