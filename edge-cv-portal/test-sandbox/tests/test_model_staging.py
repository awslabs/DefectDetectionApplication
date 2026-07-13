"""Triton model staging inside the sandbox (harness/model_staging.py).

The STAGED_MODELS manifest lists model artifact zips the portal copied
under the run's prefix. The harness unpacks each into the Triton model
repository — converting the DDA greengrass model component layout
(manifest.json + runtime artifact) into the python-backend repository
the device-side model_convertor produces, or copying a prebuilt
repository with CPU normalization — and rewrites the compiled
document's ``sim_inference_<nodeId>`` identity stubs into real
emltriton elements whose ``model=<modelName>`` matches the staged
repository entry name. A missing/corrupt artifact fails with a
per-node error and exit code 1 (Requirement 12.10 semantics).
"""

import io
import json
import os
import zipfile

import pytest

from harness import model_staging
from harness.model_staging import (
    ModelStagingError,
    download_and_stage,
    force_cpu_instance_groups,
    parse_staged_models,
    realize_inference_elements,
    resolve_base_input_shape,
    stage_model_zip,
)

ONNX_MANIFEST = {
    "runtime": "onnx",
    "runtime_artifact": "model.onnx",
    "model_graph": {
        "model_graph_type": "single_stage_model_graph",
        "pixel_level_classes": {"names": ["background", "defect"],
                                "normal_ids": [0]},
    },
    "input_shape": [1, 3, 312, 312],
    "preprocessing": {"resize": [312, 312], "channel_order": "RGB"},
    "dataset": {"image_width": 312, "image_height": 312},
}


def write_zip(path, files):
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            if isinstance(content, (dict, list)):
                content = json.dumps(content)
            archive.writestr(name, content)
    return path


@pytest.fixture
def templates(tmp_path):
    """A fake DDA_TRITON_RESOURCES directory: the four conversion
    templates plus an app package the templates import."""
    directory = tmp_path / "dda_triton_resources"
    directory.mkdir()
    (directory / "lfv_model_template.py").write_text("# base model.py\n")
    (directory / "inference_runtimes.py").write_text("# runtimes\n")
    (directory / "marshal_for_capture_template.py").write_text("# marshal\n")
    (directory / "ensemble_model").write_text("# placeholder\n")
    package = directory / "lyra_anomalies_mask_utils"
    package.mkdir()
    (package / "__init__.py").write_text("")
    return str(directory)


@pytest.fixture
def repo(tmp_path):
    directory = tmp_path / "triton_model_repo"
    directory.mkdir()
    return str(directory)


# ===========================================================================
# Manifest parsing
# ===========================================================================

class TestParseStagedModels:

    def test_valid_manifest_parsed(self):
        raw = json.dumps([
            {"nodeId": "inf", "modelName": "m", "s3Key": "runs/1/models/m.zip"},
        ])
        assert parse_staged_models(raw) == [
            {"nodeId": "inf", "modelName": "m", "s3Key": "runs/1/models/m.zip"},
        ]

    def test_absent_or_empty_yields_nothing(self):
        assert parse_staged_models(None) == []
        assert parse_staged_models("") == []
        assert parse_staged_models("[]") == []

    def test_malformed_json_and_shapes_yield_nothing(self):
        assert parse_staged_models("{not json") == []
        assert parse_staged_models('{"nodeId": "inf"}') == []

    def test_incomplete_entries_are_dropped(self):
        raw = json.dumps([
            {"nodeId": "a", "modelName": "m", "s3Key": "k"},
            {"nodeId": "b", "modelName": "m"},          # no key
            {"modelName": "m", "s3Key": "k"},           # no node
            "nonsense",
        ])
        assert parse_staged_models(raw) == [
            {"nodeId": "a", "modelName": "m", "s3Key": "k"},
        ]


# ===========================================================================
# KIND_CPU normalization
# ===========================================================================

class TestForceCpuInstanceGroups:

    def test_gpu_kinds_rewritten_to_cpu(self):
        config = (
            'name: "m"\n'
            "instance_group [\n"
            "  {\n"
            "    count: 2\n"
            "    kind: KIND_GPU\n"
            "    gpus: [0, 1]\n"
            "  }\n"
            "]\n"
        )
        rewritten = force_cpu_instance_groups(config)
        assert "KIND_GPU" not in rewritten
        assert "kind: KIND_CPU" in rewritten
        assert "gpus:" not in rewritten

    def test_auto_kind_rewritten(self):
        assert "KIND_CPU" in force_cpu_instance_groups("kind: KIND_AUTO\n")

    def test_cpu_config_unchanged(self):
        config = 'name: "m"\nbackend: "python"\n'
        assert force_cpu_instance_groups(config) == config


# ===========================================================================
# DDA component conversion (the inspected zip layout: manifest.json +
# runtime artifact, converted like the device-side model_convertor)
# ===========================================================================

class TestDdaComponentConversion:

    def stage(self, tmp_path, repo, templates, manifest=None,
              model_name="my-model"):
        zip_path = write_zip(str(tmp_path / "artifact.zip"), {
            "manifest.json": manifest or ONNX_MANIFEST,
            "model.onnx": b"onnx-bytes".decode("latin-1"),
        })
        stage_model_zip(zip_path, model_name, repo, templates=templates,
                        node_id="inf", workdir=str(tmp_path / "work"))
        return repo

    def test_three_entry_repository_layout(self, tmp_path, repo, templates):
        self.stage(tmp_path, repo, templates)
        assert sorted(os.listdir(repo)) == [
            "base_my-model", "marshal_my-model", "my-model"]

        base = os.path.join(repo, "base_my-model")
        assert os.path.isfile(os.path.join(base, "config.pbtxt"))
        version = os.path.join(base, "1")
        assert os.path.isfile(os.path.join(version, "model.py"))
        assert os.path.isfile(os.path.join(version, "inference_runtimes.py"))
        assert os.path.isfile(os.path.join(version, "model.onnx"))
        assert os.path.isfile(os.path.join(version, "manifest.json"))
        # App packages the templates import travel with the model.
        assert os.path.isdir(os.path.join(version,
                                          "lyra_anomalies_mask_utils"))

        marshal = os.path.join(repo, "marshal_my-model")
        assert os.path.isfile(os.path.join(marshal, "config.pbtxt"))
        assert os.path.isfile(os.path.join(marshal, "1", "model.py"))

        ensemble = os.path.join(repo, "my-model")
        assert os.path.isfile(os.path.join(ensemble, "config.pbtxt"))
        assert os.path.isfile(os.path.join(ensemble, "1", "ensemble_model"))

    def test_ensemble_entry_matches_the_compiler_model_name(
            self, tmp_path, repo, templates):
        """The emltriton element requests model=<modelName>; the ensemble
        config must carry exactly that name and chain base -> marshal."""
        self.stage(tmp_path, repo, templates)
        config = open(os.path.join(repo, "my-model", "config.pbtxt")).read()
        assert 'name: "my-model"' in config
        assert 'platform: "ensemble"' in config
        assert 'model_name: "base_my-model"' in config
        assert 'model_name: "marshal_my-model"' in config
        # The tensor set emltriton consumes.
        for tensor in ("output_anomalous", "output_confidence",
                       "output_overlay", "output_mask", "output_capture"):
            assert tensor in config

    def test_onnx_models_get_dynamic_input_shape(self, tmp_path, repo,
                                                 templates):
        """ONNX-runtime models must use a dynamic input regardless of
        pixel_level_classes (the python model resizes internally) — the
        model_convertor rule replicated for the sandbox."""
        self.stage(tmp_path, repo, templates)
        config = open(os.path.join(repo, "base_my-model",
                                   "config.pbtxt")).read()
        assert "dims: [-1, -1, -1]" in config
        assert 'backend: "python"' in config
        # No instance_group: the python backend defaults to CPU.
        assert "instance_group" not in config

    def test_dlr_localization_models_get_fixed_input_shape(self):
        manifest = {
            "runtime": "dlr",
            "model_graph": {"pixel_level_classes": {"names": ["bg", "a"]}},
            "dataset": {"image_width": 640, "image_height": 480},
        }
        assert resolve_base_input_shape(manifest) == [480, 640, 3]
        assert resolve_base_input_shape({"runtime": "onnx"}) == [-1, -1, -1]
        assert resolve_base_input_shape({}) == [-1, -1, -1]

    def test_staged_manifest_is_pinned_to_cpu(self, tmp_path, repo,
                                              templates):
        """The staged manifest.json gets device=cpu so the ONNX runner
        never probes for CUDA on Fargate (the KIND_CPU analog for the
        python runtime)."""
        self.stage(tmp_path, repo, templates)
        staged = json.load(open(os.path.join(
            repo, "base_my-model", "1", "manifest.json")))
        assert staged["device"] == "cpu"
        assert staged["runtime"] == "onnx"

    def test_missing_templates_fail_with_clear_error(self, tmp_path, repo):
        empty_templates = tmp_path / "empty-resources"
        empty_templates.mkdir()
        zip_path = write_zip(str(tmp_path / "artifact.zip"), {
            "manifest.json": ONNX_MANIFEST,
            "model.onnx": "bytes",
        })
        with pytest.raises(ModelStagingError) as exc:
            stage_model_zip(zip_path, "m", repo,
                            templates=str(empty_templates), node_id="inf",
                            workdir=str(tmp_path / "work"))
        assert exc.value.node_id == "inf"
        assert "DDA Triton conversion resources" in str(exc.value)

    def test_wrapped_zip_layout_is_found(self, tmp_path, repo, templates):
        """Some component zips wrap contents one directory down."""
        zip_path = write_zip(str(tmp_path / "artifact.zip"), {
            "wrapper/manifest.json": ONNX_MANIFEST,
            "wrapper/model.onnx": "bytes",
        })
        stage_model_zip(zip_path, "m", repo, templates=templates,
                        node_id="inf", workdir=str(tmp_path / "work"))
        assert os.path.isfile(os.path.join(repo, "base_m", "1", "model.onnx"))


# ===========================================================================
# Prebuilt Triton repository zips
# ===========================================================================

GPU_CONFIG = (
    'name: "other-name"\n'
    'backend: "onnxruntime"\n'
    "instance_group [\n"
    "  {\n"
    "    kind: KIND_GPU\n"
    "    gpus: [0]\n"
    "  }\n"
    "]\n"
)


class TestPrebuiltRepositoryZips:

    def test_single_entry_renamed_to_model_name_and_cpu_forced(
            self, tmp_path, repo, templates):
        zip_path = write_zip(str(tmp_path / "repo.zip"), {
            "other-name/config.pbtxt": GPU_CONFIG,
            "other-name/1/model.onnx": "bytes",
        })
        stage_model_zip(zip_path, "wanted-name", repo, templates=templates,
                        node_id="inf", workdir=str(tmp_path / "work"))

        assert os.listdir(repo) == ["wanted-name"]
        config = open(os.path.join(repo, "wanted-name",
                                   "config.pbtxt")).read()
        assert 'name: "wanted-name"' in config
        assert "KIND_GPU" not in config
        assert "kind: KIND_CPU" in config
        assert "gpus:" not in config
        assert os.path.isfile(os.path.join(repo, "wanted-name", "1",
                                           "model.onnx"))

    def test_multi_entry_repo_with_matching_name_copied_as_is(
            self, tmp_path, repo, templates):
        zip_path = write_zip(str(tmp_path / "repo.zip"), {
            "m/config.pbtxt": 'name: "m"\nplatform: "ensemble"\n',
            "base_m/config.pbtxt": 'name: "base_m"\nkind: KIND_GPU\n',
            "base_m/1/model.onnx": "bytes",
        })
        stage_model_zip(zip_path, "m", repo, templates=templates,
                        node_id="inf", workdir=str(tmp_path / "work"))
        assert sorted(os.listdir(repo)) == ["base_m", "m"]
        base_config = open(os.path.join(repo, "base_m",
                                        "config.pbtxt")).read()
        assert "KIND_CPU" in base_config
        # Entry names are untouched when the requested name exists.
        assert 'name: "base_m"' in base_config

    def test_multi_entry_repo_without_matching_name_fails(
            self, tmp_path, repo, templates):
        zip_path = write_zip(str(tmp_path / "repo.zip"), {
            "a/config.pbtxt": 'name: "a"\n',
            "b/config.pbtxt": 'name: "b"\n',
        })
        with pytest.raises(ModelStagingError) as exc:
            stage_model_zip(zip_path, "m", repo, templates=templates,
                            node_id="inf", workdir=str(tmp_path / "work"))
        assert "no entry named m" in str(exc.value)


# ===========================================================================
# Corrupt / unrecognized artifacts and download failures
# ===========================================================================

class FakeS3:
    """download_file stub backed by an in-memory {key: bytes} map."""

    def __init__(self, objects):
        self.objects = objects
        self.downloads = []

    def download_file(self, bucket, key, path):
        self.downloads.append((bucket, key))
        if key not in self.objects:
            raise RuntimeError("NoSuchKey: " + key)
        with open(path, "wb") as handle:
            handle.write(self.objects[key])


def zip_bytes(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            if isinstance(content, (dict, list)):
                content = json.dumps(content)
            archive.writestr(name, content)
    return buffer.getvalue()


class TestDownloadAndStage:

    def test_missing_artifact_raises_with_node_identified(self, tmp_path,
                                                          repo, templates):
        s3 = FakeS3({})
        with pytest.raises(ModelStagingError) as exc:
            download_and_stage(
                s3, "bucket",
                [{"nodeId": "inf", "modelName": "m", "s3Key": "runs/m.zip"}],
                repo, str(tmp_path / "work"), templates=templates)
        assert exc.value.node_id == "inf"
        assert exc.value.model_name == "m"
        assert "could not be downloaded" in str(exc.value)

    def test_corrupt_zip_raises_with_node_identified(self, tmp_path, repo,
                                                     templates):
        s3 = FakeS3({"runs/m.zip": b"this is not a zip archive"})
        with pytest.raises(ModelStagingError) as exc:
            download_and_stage(
                s3, "bucket",
                [{"nodeId": "inf", "modelName": "m", "s3Key": "runs/m.zip"}],
                repo, str(tmp_path / "work"), templates=templates)
        assert exc.value.node_id == "inf"
        assert "not a readable zip" in str(exc.value)

    def test_unrecognized_layout_raises(self, tmp_path, repo, templates):
        s3 = FakeS3({"runs/m.zip": zip_bytes({"readme.txt": "hi"})})
        with pytest.raises(ModelStagingError) as exc:
            download_and_stage(
                s3, "bucket",
                [{"nodeId": "inf", "modelName": "m", "s3Key": "runs/m.zip"}],
                repo, str(tmp_path / "work"), templates=templates)
        assert "unrecognized artifact layout" in str(exc.value)

    def test_shared_model_downloaded_once_and_mapped_per_node(
            self, tmp_path, repo, templates):
        s3 = FakeS3({"runs/m.zip": zip_bytes({
            "manifest.json": ONNX_MANIFEST,
            "model.onnx": "bytes",
        })})
        staged = download_and_stage(
            s3, "bucket",
            [{"nodeId": "infA", "modelName": "m", "s3Key": "runs/m.zip"},
             {"nodeId": "infB", "modelName": "m", "s3Key": "runs/m.zip"}],
            repo, str(tmp_path / "work"), templates=templates)
        assert staged == {"infA": "m", "infB": "m"}
        assert s3.downloads == [("bucket", "runs/m.zip")]
        assert os.path.isdir(os.path.join(repo, "m"))


# ===========================================================================
# Compiled-document reconciliation (sim stub -> emltriton)
# ===========================================================================

def sim_document():
    """A simulation-compiled document: dataset-fed source, one staged
    inference node and one that keeps its stub."""
    return {
        "segments": [
            {"name": "s0", "from": None, "linkTo": None, "elements": [
                {"nodeId": "src", "factory": "multifilesrc",
                 "args": {"location": "{dataset_location}"}},
                {"nodeId": "infA", "factory": "capsfilter",
                 "args": {"caps": "video/x-raw,format=RGB"}},
                {"nodeId": "infA", "factory": "identity",
                 "args": {"name": "sim_inference_infA"}},
                {"nodeId": "infB", "factory": "identity",
                 "args": {"name": "sim_inference_infB"}},
            ]},
        ],
        "executorBindings": [],
    }


class TestRealizeInferenceElements:

    def test_staged_stub_rewritten_to_emltriton(self):
        document = sim_document()
        realized = realize_inference_elements(
            document, {"infA": "my-model"},
            repo_dir="/aws_dda/dda_triton/triton_model_repo",
            server="/opt/tritonserver")
        assert realized == ["infA"]

        elements = document["segments"][0]["elements"]
        rewritten = elements[2]
        assert rewritten["factory"] == "emltriton"
        assert rewritten["nodeId"] == "infA"
        # The exact argument set device builds carry (workflow_core
        # MODEL_INFERENCE mapping), with model= matching the staged
        # repository entry name.
        assert rewritten["args"] == {
            "model-repo": "/aws_dda/dda_triton/triton_model_repo",
            "server-path": "/opt/tritonserver",
            "model": "my-model",
        }

    def test_unstaged_nodes_keep_their_stub(self):
        document = sim_document()
        realize_inference_elements(document, {"infA": "my-model"},
                                   repo_dir="/repo", server="/srv")
        elements = document["segments"][0]["elements"]
        assert elements[3]["factory"] == "identity"
        assert elements[3]["args"] == {"name": "sim_inference_infB"}

    def test_preceding_capsfilter_and_other_elements_untouched(self):
        document = sim_document()
        realize_inference_elements(document, {"infA": "m"},
                                   repo_dir="/repo", server="/srv")
        elements = document["segments"][0]["elements"]
        assert elements[0]["factory"] == "multifilesrc"
        assert elements[1] == {"nodeId": "infA", "factory": "capsfilter",
                               "args": {"caps": "video/x-raw,format=RGB"}}

    def test_realized_nodes_leave_sim_inference_detection(self):
        """After the rewrite the harness no longer treats the node as
        simulation-stubbed (no simulated outcome injection) and finds it
        via the emltriton factory for real output attribution."""
        from harness import renderer
        document = sim_document()
        realize_inference_elements(document, {"infA": "m"},
                                   repo_dir="/repo", server="/srv")
        assert renderer.sim_inference_node_ids(document) == ["infB"]
        assert renderer.nodes_with_factory(document, "emltriton") == ["infA"]

    def test_default_paths_come_from_module_defaults(self, monkeypatch):
        monkeypatch.delenv("TRITON_MODEL_REPO", raising=False)
        monkeypatch.delenv("TRITON_SERVER_PATH", raising=False)
        document = sim_document()
        realize_inference_elements(document, {"infA": "m"})
        args = document["segments"][0]["elements"][2]["args"]
        assert args["model-repo"] == model_staging.DEFAULT_MODEL_REPO
        assert args["server-path"] == model_staging.DEFAULT_SERVER_PATH


# ===========================================================================
# Harness-level integration: staging failure -> per-node error + exit 1
# ===========================================================================

class TestExecuteStagingFailure:

    def test_missing_artifact_records_per_node_error_and_exits_1(
            self, tmp_path, monkeypatch):
        """execute() with a STAGED_MODELS manifest whose artifact is
        missing records the error on the owning model_inference node,
        skips the rest, and returns 1 — before any pipeline or dataset
        work (12.10)."""
        from harness import harness as harness_module
        from harness.results import ResultsStore, STATUS_SKIPPED

        monkeypatch.setenv("STAGED_MODELS", json.dumps([
            {"nodeId": "infA", "modelName": "m", "s3Key": "runs/m.zip"},
        ]))
        monkeypatch.setenv("TRITON_MODEL_REPO",
                           str(tmp_path / "triton_model_repo"))

        flushes = []
        store = ResultsStore(["src", "infA", "infB"], flushes.append)
        document = sim_document()

        exit_code = harness_module.execute(
            FakeS3({}), "bucket", "runs/results.json", "runs/dataset/",
            document, store)

        assert exit_code == 1
        by_id = {r["nodeId"]: r for r in flushes[-1]["nodes"]}
        assert by_id["infA"]["status"] == "failed"
        assert by_id["infA"]["error"]["code"] == "MODEL_STAGING_FAILED"
        assert "could not be downloaded" in by_id["infA"]["error"]["message"]
        assert by_id["src"]["status"] == STATUS_SKIPPED
        assert by_id["infB"]["status"] == STATUS_SKIPPED
