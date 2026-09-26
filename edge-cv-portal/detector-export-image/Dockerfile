# Export_Image for imported detector checkpoints
# (spec: .kiro/specs/detector-checkpoint-import, Requirements 5 and 6).
#
# Runs as a SageMaker training job with EnableNetworkIsolation=True: the
# container gets no network and no AWS credentials, so EVERYTHING the export
# needs is installed here at build time and nothing is installed or downloaded
# at run time. The entry point is baked in (no sagemaker_submit_directory, no
# requirements.txt), and SageMaker's `train` argument is ignored.
#
# Build with ./build-and-push.sh, which stages a minimal context (this
# Dockerfile, the lock files and the three entry-point modules) instead of
# sending the repository root.
#
# Base: an ECR-hosted image pinned by digest (docker_base_image_audit policy),
# linux/amd64 manifest (SageMaker ml.m5 hosts are x86_64). Debian 12
# (bookworm, glibc 2.36) rather than 13 (trixie, glibc 2.41): the
# onnxruntime 1.16.3 wheel in the Fleet_Floor_Runtime venv marks its pybind .so
# as needing an executable stack, which glibc >= 2.41 refuses to grant in
# dlopen ("cannot enable executable stack ... Invalid argument"). Loading the
# unmodified wheel is the point of the floor check, so the base moves instead
# of the binary being patched.
ARG BASE_IMAGE=public.ecr.aws/docker/library/python:3.11-slim-bookworm@sha256:4b4c524dc3dce996864e030c7bd9c6b0e517597189fee48f48e05b499442444b
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # ultralytics: never pip-install at run time (the job has no network anyway,
    # but an install attempt would turn into a confusing failure), never phone home.
    YOLO_AUTOINSTALL=False \
    YOLO_OFFLINE=True \
    # ultralytics appends "Ultralytics" and needs the parent writable.
    YOLO_CONFIG_DIR=/tmp \
    MPLCONFIGDIR=/tmp/matplotlib \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    ORT_FLOOR_PYTHON=/opt/ort-floor/bin/python

# Shared libraries opencv-python needs (pulled in by ultralytics, which
# requires the non-headless wheel).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libxcb1 libx11-6 libxext6 libsm6 libxrender1 \
 && rm -rf /var/lib/apt/lists/*

# Fleet_Floor_Runtime: the oldest onnxruntime any default packaging target runs
# (JP5 GPU build + the CPU / x86 images). Separate venv: two onnxruntime
# versions cannot share one environment.
COPY ort-floor.lock /tmp/ort-floor.lock
RUN python -m venv /opt/ort-floor \
 && /opt/ort-floor/bin/pip install --no-deps -r /tmp/ort-floor.lock

# Exporter environment. torch CPU wheels come from the PyTorch index; every
# other package is pinned exactly in requirements.lock (generated from
# requirements.in by build-and-push.sh --lock and committed).
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-deps --index-url https://download.pytorch.org/whl/cpu \
        torch==2.5.1+cpu torchvision==0.20.1+cpu \
 && pip install --no-deps -r /tmp/requirements.lock \
 && pip check

# Entry point + the trainer modules it reuses (RF-DETR export/verify helpers,
# metadata writer). Copied last so code edits rebuild one small layer.
WORKDIR /opt/program
COPY export_checkpoint.py train_rfdetr.py _common.py /opt/program/
RUN python -c "import export_checkpoint" \
 && /opt/ort-floor/bin/python -c "import onnxruntime, sys; assert onnxruntime.__version__ == '1.16.3', onnxruntime.__version__"

ENTRYPOINT ["python", "/opt/program/export_checkpoint.py"]
