"""Synthetic checkpoint envelopes for ``checkpoint_probe`` tests.

See ``builders`` for the individual builders; each writes one file and returns
the bytes written. Spec: rfdetr-training-and-transfer-learning task 7.1.
"""
from .builders import (  # noqa: F401
    build_elf,
    build_empty,
    build_garbage,
    build_gzip_tarball,
    build_legacy_torch_tar,
    build_onnx,
    build_plain_pickle_dict,
    build_rfdetr_ptl,
    build_rfdetr_published,
    build_rfdetr_v1101,
    build_state_dict,
    build_torchscript,
    build_truncated_pickle,
    build_truncated_zip,
    build_ultralytics_ckpt,
    build_zip_without_data_pkl,
    write_bytes,
)
