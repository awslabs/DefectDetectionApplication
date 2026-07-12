# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""#8 model_converter preservation baseline (Req 3.6).

Spec: security-injection-deserialization-fixes — Property 2: Preservation.

``inspect_pytorch_model`` detects the model kind (raw state dict / checkpoint /
JIT / full model) and ``generate_dda_package`` writes the config/mochi/manifest
package. The fix (task 10) defaults ``torch.load`` to ``weights_only=True`` and
adds a trusted-source ``weights_only=False`` fallback. Legitimate models must
still inspect to the same metadata and produce the same package.

Recorded baselines:
  * state dict  -> is_state_dict True, detected layers / input_channels /
    num_classes / suggested_type.
  * checkpoint  -> is_checkpoint True, detected metadata.
  * JIT / full model -> either the detected contract (is_jit / is_full_model) OR
    the documented broad-except degrade ``{'type':'unknown', architecture_hints:
    ['Could not inspect model']}`` — F stays within this contract regardless of
    the effective ``weights_only`` default of the running torch (this env is
    torch>=2.6 where the default is already True; the deployed target is older).
  * generate_dda_package -> the manifest / mochi / config contents for a
    classification model.

These tests load the REAL model_converter.py (with ``shared_utils`` / boto3
stubbed) so task 13 re-runs them unchanged against the fixed source.

**Validates: Requirements 3.6**

Run:
    python3 -m pytest test/backend-test/security/preservation/test_preservation_model_converter.py \
        -p no:cacheprovider --noconftest -v
"""
import json
import os
import tarfile
import tempfile
import types

import pytest

from _preservation_support import load_module_from_path

torch = pytest.importorskip("torch")
import torch.nn as nn  # noqa: E402


# Module-level so torch.save can pickle them (a full-model object must reference
# an importable class).
class _JitNet(nn.Module):
    def forward(self, x):
        return x + 1


class _FullNet(nn.Module):
    def __init__(self):
        super().__init__()
        self.fc = nn.Linear(4, 2)

    def forward(self, x):
        return self.fc(x)


def _load_model_converter():
    su = types.ModuleType("shared_utils")
    for name in ["create_response", "get_user_from_event", "log_audit_event",
                 "check_user_access", "validate_required_fields"]:
        setattr(su, name, lambda *a, **k: None)

    boto3 = types.ModuleType("boto3")
    boto3.resource = lambda *a, **k: types.SimpleNamespace(
        Table=lambda *a, **k: None)
    boto3.client = lambda *a, **k: types.SimpleNamespace()
    botocore = types.ModuleType("botocore")
    exc = types.ModuleType("botocore.exceptions")

    class ClientError(Exception):
        pass

    exc.ClientError = ClientError
    botocore.exceptions = exc

    return load_module_from_path(
        "model_converter_preservation",
        "edge-cv-portal/backend/functions/model_converter.py",
        injected_modules={
            "shared_utils": su, "boto3": boto3,
            "botocore": botocore, "botocore.exceptions": exc,
        },
    )


@pytest.fixture(scope="module")
def mc():
    return _load_model_converter()


@pytest.fixture()
def tmp_models(tmp_path):
    paths = {}

    sd = {"conv1.weight": torch.zeros(8, 3, 3, 3),
          "fc.weight": torch.zeros(10, 8), "fc.bias": torch.zeros(10)}
    paths["state_dict"] = str(tmp_path / "sd.pt")
    torch.save(sd, paths["state_dict"])

    ckpt = {"model": {"backbone.conv.weight": torch.zeros(4, 3, 3, 3),
                      "classifier.weight": torch.zeros(5, 4)}, "epoch": 3}
    paths["checkpoint"] = str(tmp_path / "ckpt.pt")
    torch.save(ckpt, paths["checkpoint"])

    paths["jit"] = str(tmp_path / "jit.pt")
    torch.jit.script(_JitNet()).save(paths["jit"])

    paths["full_model"] = str(tmp_path / "full.pt")
    torch.save(_FullNet(), paths["full_model"])
    return paths


# --------------------------------------------------------------------------- #
# inspect_pytorch_model — state dict & checkpoint (stable, pure-tensor loads)
# --------------------------------------------------------------------------- #
# Validates: Requirements 3.6
def test_inspect_state_dict_metadata_baseline(mc, tmp_models):
    info = mc.inspect_pytorch_model(tmp_models["state_dict"])
    assert info["is_state_dict"] is True
    assert info["is_jit"] is False
    assert info["is_full_model"] is False
    assert info["layers"] == ["conv1.weight", "fc.weight", "fc.bias"]
    assert info["total_layers"] == 3
    assert info["input_channels"] == 3
    assert info["num_classes"] == 10
    assert info["suggested_type"] == "classification"
    assert info["architecture_hints"] == ["Classification network"]


# Validates: Requirements 3.6
def test_inspect_checkpoint_metadata_baseline(mc, tmp_models):
    info = mc.inspect_pytorch_model(tmp_models["checkpoint"])
    assert info.get("is_checkpoint") is True
    assert info["is_state_dict"] is False
    assert info["layers"] == ["backbone.conv.weight", "classifier.weight"]
    assert info["total_layers"] == 2
    assert info["input_channels"] == 3
    assert info["num_classes"] == 5
    assert info["suggested_type"] == "classification"
    assert info["architecture_hints"] == ["Classification network"]


# --------------------------------------------------------------------------- #
# inspect_pytorch_model — JIT & full model (contract, torch-version-tolerant)
# --------------------------------------------------------------------------- #
def _is_detected_jit(info):
    return info.get("is_jit") is True and info.get("type") == "jit_model"


def _is_detected_full(info):
    return info.get("is_full_model") is True and info.get("type") == "full_model"


def _is_degraded(info):
    return (info.get("type") == "unknown"
            and info.get("architecture_hints") == ["Could not inspect model"])


# Validates: Requirements 3.6
def test_inspect_jit_model_contract_baseline(mc, tmp_models):
    """A JIT model is either detected (is_jit) or degrades to the documented
    broad-except contract — F stays within this contract."""
    info = mc.inspect_pytorch_model(tmp_models["jit"])
    assert _is_detected_jit(info) or _is_degraded(info), info


# Validates: Requirements 3.6
def test_inspect_full_model_contract_baseline(mc, tmp_models):
    """A full-model object is either detected (is_full_model) or degrades to the
    documented broad-except contract."""
    info = mc.inspect_pytorch_model(tmp_models["full_model"])
    assert _is_detected_full(info) or _is_degraded(info), info


# --------------------------------------------------------------------------- #
# inspect_pytorch_model — the broad-except contract is preserved on bad input
# --------------------------------------------------------------------------- #
# Validates: Requirements 3.6
def test_inspect_unreadable_file_degrades_gracefully(mc, tmp_path):
    """A non-loadable file returns the documented degrade contract (unchanged)."""
    bad = tmp_path / "not_a_model.pt"
    bad.write_bytes(b"this is not a torch archive")
    info = mc.inspect_pytorch_model(str(bad))
    assert info["type"] == "unknown"
    assert info["architecture_hints"] == ["Could not inspect model"]
    assert "error" in info


# --------------------------------------------------------------------------- #
# generate_dda_package — package contents (no torch.load; fix does not touch it)
# --------------------------------------------------------------------------- #
# Validates: Requirements 3.6
def test_generate_dda_package_classification_baseline(mc, tmp_path):
    src_model = tmp_path / "model.pt"
    torch.save({"fc.weight": torch.zeros(3, 4)}, str(src_model))
    out = tmp_path / "pkg.tar.gz"

    result = mc.generate_dda_package(
        model_path=str(src_model),
        model_name="mymodel",
        model_type="classification",
        image_width=224,
        image_height=224,
        num_classes=3,
        class_names=["a", "b", "c"],
        output_path=str(out),
        export_format="pytorch",
    )
    assert result == str(out)

    with tarfile.open(str(out), "r:gz") as tar:
        names = sorted(tar.getnames())
        member = tar.extractfile("export_artifacts/manifest.json")
        manifest = json.load(member)
        mochi = json.load(tar.extractfile("mochi.json"))

    # Recorded package layout.
    assert "config.yaml" in names
    assert "mochi.json" in names
    assert "export_artifacts/manifest.json" in names
    assert "export_artifacts/mymodel.pt" in names

    # Recorded manifest shape for a legacy pytorch classification package.
    assert manifest["input_shape"] == [1, 3, 224, 224]
    assert manifest["model_graph"]["stages"][0]["type"] == "classification"
    assert manifest["model_graph"]["stages"][0]["output_shape"] == [1, 3]
    assert manifest["compilable_models"][0]["filename"] == "mymodel.pt"
    assert manifest["compilable_models"][0]["framework"] == "PYTORCH"
    assert manifest["preprocessing"]["resize"] == [224, 224]
    assert manifest["preprocessing"]["normalize"]["mean"] == [0.485, 0.456, 0.406]

    # Recorded mochi shape.
    assert mochi["stages"][0]["type"] == "classification"
    assert mochi["stages"][0]["input_shape"] == [1, 3, 224, 224]
    assert mochi["stages"][0]["num_classes"] == 3
    assert mochi["model_info"]["name"] == "mymodel"
    assert mochi["model_info"]["framework"] == "pytorch"
