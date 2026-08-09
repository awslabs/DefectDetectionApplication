ARG OS
FROM public.ecr.aws/ubuntu/ubuntu:${OS}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y software-properties-common wget build-essential cmake libffi-dev zlib1g-dev libssl-dev libsqlite3-dev gdb
RUN apt-get update
RUN wget https://www.python.org/ftp/python/3.11.9/Python-3.11.9.tgz
RUN tar -xzf Python-3.11.9.tgz
RUN cd Python-3.11.9 && \
    ./configure --enable-optimizations --enable-shared && \
    make -j$(nproc) && \
    make install
RUN find / -name "libpython3.11.so.1.0" -print
RUN update-alternatives --install /usr/local/bin/python3 python3 /usr/local/bin/python3.11 1 \
    && update-alternatives --set python3 /usr/local/bin/python3.11

RUN add-apt-repository ppa:ubuntu-toolchain-r/test -y && \
    apt-get update && \
    apt install gcc-11 g++-11 -y && \
    rm /usr/bin/gcc && \
    rm /usr/bin/g++ && \
    ln -s /usr/bin/gcc-11 /usr/bin/gcc && \
    ln -s /usr/bin/g++-11 /usr/bin/g++

RUN apt-get install --only-upgrade ncurses-base 
RUN apt-get install --only-upgrade ncurses-bin
RUN apt-get update && apt-get install -y libb64-0d && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
COPY dda_triton/model_conversion_requirements.txt ./model_conversion_requirements.txt

COPY edge_ml1_p_camera_management ./edge_ml1_p_camera_management
COPY edgeagent ./edgeagent
# Pre-build the PINNED awscrt==0.14.7 (transitive dep of awsiotsdk==1.11.9) so its
# vendored aws-lc is linked correctly. awscrt 0.14.7 always builds and statically
# links its own bundled aws-lc (BoringSSL) — it has no flag to opt out — but on
# this image the system OpenSSL static archive (/usr/lib/<triplet>/libcrypto.a,
# from libssl-dev) sits EARLIER on the linker search path than awscrt's vendored
# libcrypto.a, so the final `_awscrt*.so` link resolves `-l:libcrypto.a` against
# system OpenSSL, which lacks the AWS-LC AEAD API. Result at import:
# `undefined symbol: EVP_aead_aes_128_gcm_tls13`. Temporarily move the system
# static archive aside (its .so + headers stay in place for every other build)
# while awscrt compiles so the linker falls through to the vendored aws-lc lib,
# then restore it. This pre-install satisfies awsiotsdk's pin so prereqs_install.sh
# below does not rebuild awscrt. The awscrt/awsiotsdk versions are unchanged.
RUN ARCH_TRIPLET="$(gcc -dumpmachine)" && LC="/usr/lib/${ARCH_TRIPLET}/libcrypto.a" && \
    if [ -f "$LC" ]; then mv "$LC" "${LC}.bak"; fi && \
    python3 -m pip install --no-cache-dir --no-binary :all: awscrt==0.14.7 && \
    if [ -f "${LC}.bak" ]; then mv "${LC}.bak" "$LC"; fi && \
    python3 -c "import awscrt.mqtt; print('awscrt vendored aws-lc link OK')"
RUN ./edge_ml1_p_camera_management/prereqs_install.sh 
# Bake the pinned model conversion dependencies into the image at build time so they
# are already present and importable at container startup, removing the need for a
# network-dependent runtime pip install on offline/air-gapped devices.
RUN python3.11 -m pip install --no-cache-dir -r ./model_conversion_requirements.txt
# ── ONNX Runtime (CPU) for the pluggable ONNX inference engine ─────────────
# The OnnxRunner in dda_triton/resources_for_copy/inference_runtimes.py imports
# onnxruntime lazily when a model's manifest declares runtime="onnx" (YOLO /
# RF-DETR object-detection + segmentation models). Without it those models fail
# to initialize in the Triton python stub with "ModuleNotFoundError: No module
# named 'onnxruntime'" and sit in LOADING forever (DLR/Neo models are
# unaffected — engines import lazily per-runner). JP5/JP6 install this too; JP4
# was the only target missing it. JetPack 4 (CUDA 10.2, L4T r32) has no
# supported GPU onnxruntime build path, so this is CPU-only: the onnxruntime
# 1.16.3 cp311 aarch64 manylinux_2_17 wheel installs on this glibc 2.27
# (Ubuntu 18.04) base, and OnnxRunner falls back to the CPU provider.
ARG ONNXRUNTIME_SPEC=onnxruntime==1.16.3
RUN python3.11 -m pip install --no-cache-dir ${ONNXRUNTIME_SPEC}
RUN ./edge_ml1_p_camera_management/install_aravis.sh
RUN python3.11 -m grpc_tools.protoc --proto_path=./edgeagent --python_out=. --grpc_python_out=. edgeagent/edge-agent.proto
RUN apt update -y
# libssl1.1 (OpenSSL 1.1 runtime) exists only through focal; on
# jammy the base already ships libssl3 and the edgemlsdk debs carry their
# own OpenSSL 3.x (openssl.deb). Gate on the base's own /etc/os-release:
# the OS build-arg is out of scope in RUN (declared only before FROM).
RUN . /etc/os-release && if [ "$VERSION_ID" = "18.04" ] || [ "$VERSION_ID" = "20.04" ] ; then \
    apt-get install libssl1.1 -y; \
    fi
RUN apt update -y
RUN apt install libexif12 libcurl4 libarchive13 gstreamer1.0-tools gstreamer1.0-libav ffmpeg -y
RUN if [ "$OS" = "18.04" ] ; then \
    apt install libavcodec-extra57i -y; \
    fi

RUN find / -name "libpython3.11.so.1.0" -print

RUN apt clean -y

COPY app.py ./
# Docker healthcheck helper (edge-deploy-reliability, Defect B): compose runs
# `python3 /healthcheck.py` as the backend healthcheck. Lands at /healthcheck.py
# (no WORKDIR is set, so ./ is /).
COPY healthcheck.py ./
COPY logging.conf ./
COPY dao ./dao
COPY utils ./utils
# Verify Nvidia CSI config is correct
RUN echo "Verifying Nvidia CSI configuration..." && \
    grep -A 5 "Nvidia CSI" ./utils/config/default_camera_configurations.json && \
    echo "Config verification complete"
COPY metrics ./metrics
COPY model ./model
COPY mqtt ./mqtt
COPY resources ./resources
COPY gstreamer ./gstreamer
COPY triggers ./triggers
COPY exceptions ./exceptions
COPY snapshot ./snapshot
COPY defect_detection_config ./defect_detection_config
COPY dda_logging ./dda_logging
COPY endpoints ./endpoints
COPY data_models ./data_models
COPY camera_discovery ./camera_discovery
COPY camera_sync ./camera_sync
COPY workflow_engine ./workflow_engine
# vllm-triton-inference (Requirements 4.1, 8.7): the companion vLLM runtime
# package. Every new src/backend module needs a COPY line here — missing ones
# have caused startup regressions (endpoints/text_generation.py imports
# vllm_runtime.manager at module load). The other modules this feature adds
# ride existing COPYs: endpoints/text_generation.py (COPY endpoints),
# workflow_engine/llm_inference.py + vendored catalog (COPY workflow_engine),
# dda_triton/vllm_model_prep.py (COPY dda_triton),
# utils/feature_configs_utils.py (COPY utils), app.py (COPY app.py).
COPY vllm_runtime ./vllm_runtime
COPY local_auth ./local_auth
COPY user_accounts_sync ./user_accounts_sync
COPY alembic ./alembic
COPY alembic.ini ./
COPY dda_triton ./dda_triton
COPY lyra_anomalies_mask_utils ./lyra_anomalies_mask_utils
COPY lyra_science_processing_utils ./lyra_science_processing_utils
CMD ["python3.11", "app.py"]

# Temporary to fix CVE-2024-24806 and CVE-2024-0553 until slim-buster base image is fixed
RUN apt-get update \
    # Install build tools (if needed for compiling)
    && apt-get install -y --no-install-recommends build-essential \
    # Install tools for managing libraries
    libgnutls28-dev libuv1 \
    # Clean up to reduce image size
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

## TODO: Remove copy edgemlsdk files here. Change to direct build in Docker
# To test and build - Copy artifacts file to /src/backend/edgemlsdk
COPY edgemlsdk ./edgemlsdk
RUN ./edge_ml1_p_camera_management/install_edgemlsdk.sh
COPY dlr_disable_phone_home.py ./
RUN python3.11 dlr_disable_phone_home.py

#TODO:change to user to scope the permission. Currently needed to persist the TinyDB data
USER root
