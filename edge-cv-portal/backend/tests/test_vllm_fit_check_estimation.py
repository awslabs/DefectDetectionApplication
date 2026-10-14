"""Unit tests for `vllm_fit_check.estimate_weights`
(vllm-sizing-and-packaging-errors, task 1.4).

Covers the Weight_Estimate derivations with injected fetchers (no network,
no boto3):
- Hugging Face safetensors index (per-file sizes summed)   — Requirement 3.2
- parameter-count × dtype byte-width fallback              — Requirement 3.2
- quantization_config bits-per-weight sizing               — Requirement 3.2
- S3 artifact ContentLength                                — Requirement 3.3
- fetch/parse failure → None (fit check skipped, never blocks)
                                                           — Requirement 3.4
"""
from vllm_fit_check import DTYPE_BYTES, estimate_weights

GIB = 1024 ** 3


def hf_record(model_id="org/model-7b", engine_configuration=None):
    record = {"model_source": {"huggingface_model_id": model_id}}
    if engine_configuration is not None:
        record["engine_configuration"] = engine_configuration
    return record


def s3_record(uri="s3://weights-bucket/models/qwen.tar.gz"):
    return {"model_source": {"s3_model_artifact": uri}}


def fetch_returning(metadata, seen_urls=None):
    def hf_fetch(url):
        if seen_urls is not None:
            seen_urls.append(url)
        return metadata
    return hf_fetch


# ---------------------------------------------------------------------------
# Hugging Face: safetensors index (Requirement 3.2)
# ---------------------------------------------------------------------------

class TestSafetensorsIndex:
    def test_sums_safetensors_file_sizes(self):
        metadata = {
            "siblings": [
                {"rfilename": "model-00001-of-00002.safetensors",
                 "size": 8 * GIB},
                {"rfilename": "model-00002-of-00002.safetensors",
                 "size": 6 * GIB + 256},
                {"rfilename": "config.json", "size": 1234},
                {"rfilename": "tokenizer.json", "size": 5678},
            ],
        }
        estimate = estimate_weights(hf_record(),
                                    hf_fetch=fetch_returning(metadata))
        assert estimate is not None
        assert estimate.total_bytes == 14 * GIB + 256
        assert estimate.method == "safetensors_files"

    def test_requests_blobs_listing_for_the_model_id(self):
        seen = []
        metadata = {"siblings": [
            {"rfilename": "model.safetensors", "size": GIB}]}
        estimate_weights(hf_record(model_id="Qwen/Qwen2.5-7B-Instruct"),
                         hf_fetch=fetch_returning(metadata, seen))
        assert len(seen) == 1
        assert "Qwen/Qwen2.5-7B-Instruct" in seen[0]
        assert "blobs=true" in seen[0]

    def test_ignores_zero_and_missing_sizes(self):
        metadata = {"siblings": [
            {"rfilename": "a.safetensors", "size": 0},
            {"rfilename": "b.safetensors"},
            {"rfilename": "c.safetensors", "size": 3 * GIB},
        ]}
        estimate = estimate_weights(hf_record(),
                                    hf_fetch=fetch_returning(metadata))
        assert estimate is not None
        assert estimate.total_bytes == 3 * GIB


# ---------------------------------------------------------------------------
# Hugging Face: parameter-count fallback (Requirement 3.2)
# ---------------------------------------------------------------------------

class TestParamCountFallback:
    def test_param_count_times_dtype_bytes(self):
        metadata = {
            "siblings": [{"rfilename": "pytorch_model.bin",
                          "size": 14 * GIB}],  # not .safetensors
            "safetensors": {"total": 7_000_000_000},
        }
        estimate = estimate_weights(
            hf_record(engine_configuration={"dtype": "bfloat16"}),
            hf_fetch=fetch_returning(metadata))
        assert estimate is not None
        assert estimate.total_bytes == 7_000_000_000 * DTYPE_BYTES["bfloat16"]
        assert estimate.method == "param_count"

    def test_float32_uses_four_bytes_per_parameter(self):
        metadata = {"safetensors": {"total": 1_000_000}}
        estimate = estimate_weights(
            hf_record(engine_configuration={"dtype": "float32"}),
            hf_fetch=fetch_returning(metadata))
        assert estimate is not None
        assert estimate.total_bytes == 4_000_000

    def test_missing_engine_configuration_defaults_to_auto(self):
        metadata = {"safetensors": {"total": 1_000_000}}
        estimate = estimate_weights(hf_record(),
                                    hf_fetch=fetch_returning(metadata))
        assert estimate is not None
        assert estimate.total_bytes == 1_000_000 * DTYPE_BYTES["auto"]


# ---------------------------------------------------------------------------
# Hugging Face: quantization_config sizing (Requirement 3.2)
# ---------------------------------------------------------------------------

class TestQuantizationConfig:
    def test_bits_key_overrides_dtype_width(self):
        metadata = {
            "safetensors": {"total": 8_000_000_000},
            "config": {"quantization_config": {"bits": 4}},
        }
        estimate = estimate_weights(
            hf_record(engine_configuration={"dtype": "bfloat16"}),
            hf_fetch=fetch_returning(metadata))
        assert estimate is not None
        assert estimate.total_bytes == 8_000_000_000 * 4 // 8
        assert estimate.method == "param_count"

    def test_load_in_8bit_flag(self):
        metadata = {
            "safetensors": {"total": 2_000_000_000},
            "config": {"quantization_config": {"load_in_8bit": True}},
        }
        estimate = estimate_weights(hf_record(),
                                    hf_fetch=fetch_returning(metadata))
        assert estimate is not None
        assert estimate.total_bytes == 2_000_000_000  # 8 bits = 1 byte/param


# ---------------------------------------------------------------------------
# S3: artifact ContentLength (Requirement 3.3)
# ---------------------------------------------------------------------------

class TestS3Artifact:
    def test_content_length_used_as_estimate(self):
        calls = []

        def s3_head(**kwargs):
            calls.append(kwargs)
            return {"ContentLength": 14 * GIB}

        estimate = estimate_weights(s3_record(), s3_head=s3_head)
        assert estimate is not None
        assert estimate.total_bytes == 14 * GIB
        assert estimate.method == "s3_artifact"
        assert calls == [{"Bucket": "weights-bucket",
                          "Key": "models/qwen.tar.gz"}]


# ---------------------------------------------------------------------------
# Failure → None: fit check skipped, never blocks (Requirement 3.4)
# ---------------------------------------------------------------------------

class TestFailureReturnsNone:
    def test_hf_fetch_raising_returns_none(self):
        def hf_fetch(url):
            raise OSError("timed out")
        assert estimate_weights(hf_record(), hf_fetch=hf_fetch) is None

    def test_unparseable_metadata_returns_none(self):
        assert estimate_weights(
            hf_record(), hf_fetch=fetch_returning("not json object")) is None

    def test_metadata_without_sizes_or_param_count_returns_none(self):
        metadata = {"siblings": [{"rfilename": "README.md", "size": 10}]}
        assert estimate_weights(
            hf_record(), hf_fetch=fetch_returning(metadata)) is None

    def test_s3_head_raising_returns_none(self):
        def s3_head(**kwargs):
            raise RuntimeError("AccessDenied")
        assert estimate_weights(s3_record(), s3_head=s3_head) is None

    def test_s3_record_without_s3_head_returns_none(self):
        assert estimate_weights(s3_record()) is None

    def test_s3_missing_content_length_returns_none(self):
        assert estimate_weights(
            s3_record(), s3_head=lambda **kw: {}) is None

    def test_record_without_source_returns_none(self):
        assert estimate_weights({"model_source": {}}) is None
        assert estimate_weights({}) is None
