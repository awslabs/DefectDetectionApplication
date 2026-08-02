"""Containerized Plugin_Simulator integration tests (custom-node-designer
task 8.5, Requirements 7.2, 7.6, 7.7).

Runs the real single-plugin simulate harness (``HARNESS_MODE=simulate``,
``python3 -m harness``) inside the sandbox container image against
moto-served S3, reusing the session fixtures from ``conftest.py``
(Docker/moto/Pillow prerequisites; each test skips cleanly when one is
missing):

- A containerized single-plugin run: the plugin ``.so`` is staged from
  the run's S3 prefix into the task's plugin scan directory, the
  single-plugin pipeline executes with declared parameter values, and
  the results document covers every input frame with input/output frame
  images uploaded under the run's prefix (Requirement 7.2).
- Timeout behavior with a shortened limit: ``PIPELINE_TIMEOUT_SEC=3``
  terminates a slow run as failed-with-timeout while the incrementally
  flushed partial results (frames produced before termination) are
  retained (Requirement 7.7 semantics at the harness level).
- Failure containment: a plugin element that errors mid-run exits the
  task with code 1 (contained to the container), and the flushed
  results document reports the failure with the plugin's error output
  included and the frames produced before the failure retained
  (Requirement 7.6).

Documented limitation (same shape as test_sandbox_e2e.py): no custom
plugin can be compiled in CI, so ``<element>`` is a stock GStreamer
element and the staged ``.so`` is a real loadable GStreamer plugin
extracted from the image itself (the videofilter plugin). The staging
path — S3 download into the scan directory, ``GST_PLUGIN_PATH``
extension, registry load — is executed for real; only the plugin's
provenance differs from a portal-built artifact.
"""

import json
import subprocess
import uuid

import pytest

pytestmark = pytest.mark.integration

#: Path (arch-globbed) of a real loadable GStreamer plugin inside the
#: image, staged as the run's Plugin_Artifact. It provides the stock
#: elements the tests simulate (videoflip / identity).
_IMAGE_PLUGIN_GLOB = "/usr/lib/*/gstreamer-1.0/libgstvideofilter.so"


@pytest.fixture(scope="session")
def plugin_so_bytes(sandbox_image):
    """A real loadable x86-image plugin ``.so`` extracted from the
    sandbox image (see module docstring limitation)."""
    completed = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh", sandbox_image,
         "-c", "cat {0}".format(_IMAGE_PLUGIN_GLOB)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
    if completed.returncode != 0 or not completed.stdout:
        pytest.skip("could not extract a plugin .so from the sandbox image")
    return completed.stdout


class SimulateRun:
    """One prepared simulation run against the containerized harness:
    run-prefix S3 keys mirroring the plugin_simulator.py layout plus a
    ``HARNESS_MODE=simulate`` container runner."""

    def __init__(self, s3_client, moto_url, image, bucket, run_id):
        self.s3 = s3_client
        self.moto_url = moto_url
        self.image = image
        self.bucket = bucket
        self.run_id = run_id
        prefix = "plugin-simulations/uc-integration/{0}/".format(run_id)
        self.run_prefix = prefix
        self.dataset_prefix = prefix + "inputs"
        self.results_key = prefix + "results.json"
        self.plugin_key = prefix + "plugin/libgstvideofilter.so"

    def upload_plugin(self, so_bytes):
        self.s3.put_object(Bucket=self.bucket, Key=self.plugin_key,
                           Body=so_bytes)

    def upload_dataset_jpegs(self, count=3, size=(32, 24)):
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow is not installed on the host")
        import tempfile
        colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
        for index in range(count):
            with tempfile.NamedTemporaryFile(suffix=".jpg") as handle:
                Image.new("RGB", size, colors[index % len(colors)]).save(
                    handle.name, "JPEG", quality=90)
                self.s3.upload_file(
                    handle.name, self.bucket,
                    "{0}/sample_{1}.jpg".format(self.dataset_prefix, index))

    def run_harness(self, element_factory, parameters=None, extra_env=None,
                    timeout=300):
        """``docker run`` the simulate harness; returns (exit_code, logs)."""
        env = {
            "HARNESS_MODE": "simulate",
            "SIMULATION_RUN_ID": self.run_id,
            "ARTIFACTS_BUCKET": self.bucket,
            "DATASET_S3_PREFIX": self.dataset_prefix,
            "RESULTS_S3_KEY": self.results_key,
            "PLUGIN_S3_KEY": self.plugin_key,
            "ELEMENT_FACTORY": element_factory,
            "ELEMENT_PARAMETERS": json.dumps(parameters or {}),
            "AWS_ENDPOINT_URL": self.moto_url,
            "AWS_ACCESS_KEY_ID": "testing",
            "AWS_SECRET_ACCESS_KEY": "testing",
            "AWS_DEFAULT_REGION": "us-east-1",
        }
        env.update(extra_env or {})
        args = ["docker", "run", "--rm", "--network=host"]
        for name, value in env.items():
            args.extend(["-e", "{0}={1}".format(name, value)])
        args.append(self.image)
        completed = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout)
        return completed.returncode, completed.stdout

    def fetch_results(self):
        response = self.s3.get_object(Bucket=self.bucket,
                                      Key=self.results_key)
        return json.loads(response["Body"].read().decode("utf-8"))

    def frame_keys(self):
        """Every staged frame image object under the run's frames/ prefix."""
        listed = self.s3.list_objects_v2(Bucket=self.bucket,
                                         Prefix=self.run_prefix + "frames/")
        return sorted(obj["Key"] for obj in listed.get("Contents", []))


@pytest.fixture
def simulate_run(s3, moto_endpoint, sandbox_image, plugin_so_bytes):
    run_id = "sim-{0}".format(uuid.uuid4().hex[:12])
    bucket = "simulate-it-{0}".format(run_id)
    s3.create_bucket(Bucket=bucket)
    run = SimulateRun(s3, moto_endpoint, sandbox_image, bucket, run_id)
    run.upload_plugin(plugin_so_bytes)
    return run


# ---------------------------------------------------------------------------
# Requirement 7.2: containerized single-plugin run in the sandbox image
# ---------------------------------------------------------------------------

def test_containerized_single_plugin_run(simulate_run):
    """The simulate harness stages the plugin .so from the run's S3
    prefix into its scan directory and executes the single-plugin
    pipeline with declared parameter values; the completed results
    document covers every input frame with input and output frame
    images uploaded under the run's prefix (7.2, 7.3 execution
    semantics)."""
    simulate_run.upload_dataset_jpegs(count=3)

    exit_code, logs = simulate_run.run_harness(
        "videoflip", parameters={"method": "rotate-180"})
    assert exit_code == 0, "harness exited {0}; logs:\n{1}".format(
        exit_code, logs)

    results = simulate_run.fetch_results()
    assert results["status"] == "completed"
    assert results["error"] is None
    assert results["element"] == "videoflip"
    assert results["parameters"] == {"method": "rotate-180"}

    # Every input frame is covered by a result record carrying its
    # input/output frame references and per-frame metadata.
    assert results["frameCount"] == 3
    assert [f["frameIndex"] for f in results["frames"]] == [0, 1, 2]
    for record in results["frames"]:
        assert record["inputRef"].startswith(simulate_run.run_prefix
                                             + "frames/input_")
        assert record["outputRef"].startswith(simulate_run.run_prefix
                                              + "frames/output_")
        assert record["metadata"]["bytes"] > 0
        assert "image/jpeg" in (record["metadata"]["caps"] or "")

    # The referenced frame images exist under the run's prefix (and only
    # there — the task role has no other S3 path; see the CDK policy
    # assertions in node-designer-simulator.test.ts).
    frame_keys = simulate_run.frame_keys()
    assert len(frame_keys) == 6  # 3 inputs + 3 outputs
    for record in results["frames"]:
        assert record["inputRef"] in frame_keys
        assert record["outputRef"] in frame_keys

    # The plugin .so was staged into the task's scan directory before
    # GStreamer initialized (the harness logs the staging step).
    assert "Staged plugin" in logs


# ---------------------------------------------------------------------------
# Requirement 7.7 semantics: shortened timeout retains partial results
# ---------------------------------------------------------------------------

def test_timeout_with_shortened_limit_retains_partial_results(simulate_run):
    """With PIPELINE_TIMEOUT_SEC=3 a run that cannot finish in time
    (identity sleeping 0.4 s per frame over 20 frames needs ~8 s) is
    terminated: the harness exits 1, the flushed results document is
    failed with the timeout indication, and every frame produced before
    termination is retained (7.7 at the harness level; the state
    machine's 5-minute task stop and run marking are asserted by
    node-designer-simulator.test.ts and test_plugin_simulator.py)."""
    simulate_run.upload_dataset_jpegs(count=20)

    exit_code, logs = simulate_run.run_harness(
        "identity", parameters={"sleep-time": 400000},
        extra_env={"PIPELINE_TIMEOUT_SEC": "3"}, timeout=120)
    assert exit_code == 1, "harness exited {0}; logs:\n{1}".format(
        exit_code, logs)

    results = simulate_run.fetch_results()
    assert results["status"] == "failed"
    assert results["error"]["code"] == "SIMULATION_TIMEOUT"
    assert "timed out after 3s" in results["error"]["message"]

    # Partial results: some frames were produced and retained, but the
    # run never covered the full dataset.
    assert results["frameCount"] == 20
    produced = results["frames"]
    assert 0 < len(produced) < 20, \
        "expected partial frame coverage, got {0}/20".format(len(produced))
    for record in produced:
        assert record["outputRef"] is not None
        assert record["outputRef"] in simulate_run.frame_keys()


# ---------------------------------------------------------------------------
# Requirement 7.6: failure containment with plugin error output reported
# ---------------------------------------------------------------------------

def test_failure_containment_reports_plugin_error_output(simulate_run):
    """A plugin element failing mid-run (identity error-after=3 posts a
    bus error from the element after two frames) is contained to the
    task: the container exits 1, the flushed results document reports
    the failure with the element identified and the captured error
    output included, and the frames produced before the failure are
    retained (7.6)."""
    simulate_run.upload_dataset_jpegs(count=5)

    exit_code, logs = simulate_run.run_harness(
        "identity", parameters={"error-after": 3}, timeout=120)
    assert exit_code == 1, "harness exited {0}; logs:\n{1}".format(
        exit_code, logs)

    results = simulate_run.fetch_results()
    assert results["status"] == "failed"

    # The failure is reported with the plugin element identified and the
    # captured error-output channel included in the document (7.6).
    error = results["error"]
    assert error["code"] == "PIPELINE_EXECUTION_ERROR"
    assert "identity" in error["message"]
    assert isinstance(error["errorOutput"], str)

    # Frames produced before the plugin failure are retained.
    produced = results["frames"]
    assert 0 < len(produced) < 5
    for record in produced:
        assert record["outputRef"] in simulate_run.frame_keys()

    # Containment: the failure ended this task alone — the harness still
    # flushed the failure document and exited normally with code 1 (no
    # crash without a report), which is what the state machine's catch
    # converts into a failed run.
    assert "Simulation pipeline failed" in logs
