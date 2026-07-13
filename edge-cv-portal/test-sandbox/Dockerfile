# Workflow test sandbox image (Workflow Manager, task 11.3).
#
# x86_64 image with GStreamer, the DDA plugin set, CPU Triton, and the
# vendored workflow_core package plus the test harness (design section 10,
# Requirements 12.5, 12.6, 12.7, 12.10). Runs as the Fargate task defined
# in edge-cv-portal/infrastructure/lib/test-runner-stack.ts and is pushed
# to the `dda-workflow-test-sandbox` ECR repository.
#
# Build from the edge-cv-portal/ directory (see test-sandbox/README.md):
#   docker build --platform linux/amd64 -f test-sandbox/Dockerfile -t dda-workflow-test-sandbox .
#
# CPU Triton comes from the NVIDIA Triton server image (works CPU-only;
# no GPU is present on Fargate). Override TRITON_IMAGE to pin a different
# release. The emltriton element locates the server via its server-path
# argument, which the compiler resolves to /opt/tritonserver — the same
# path LocalServer uses (workflow_core DEFAULT_CONTEXT_VALUES).
ARG TRITON_IMAGE=nvcr.io/nvidia/tritonserver:24.08-py3
FROM ${TRITON_IMAGE} AS triton

FROM public.ecr.aws/ubuntu/ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive

# GStreamer runtime + Python bindings (the harness executes the rendered
# launch string through Gst.parse_launch, like GstPipelineManager), plus
# the shared libraries the DDA plugin set links against (libexif/libcurl/
# libarchive, mirroring src/backend/Dockerfile).
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-gi \
        python3-gst-1.0 \
        gstreamer1.0-tools \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-libav \
        gir1.2-gst-plugins-base-1.0 \
        libexif12 \
        libcurl4 \
        libarchive13 \
        libb64-0d \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# CPU Triton (12.5): the sandbox serves model inference for emltriton
# exactly where LocalServer expects it.
COPY --from=triton /opt/tritonserver /opt/tritonserver
# The emltriton element links libcudart and libdcgm directly (even in
# CPU-only operation); both ship with the NVIDIA Triton image.
COPY --from=triton /usr/local/cuda/lib64/libcudart.so* /usr/lib/
COPY --from=triton /usr/lib/x86_64-linux-gnu/libdcgm.so* /usr/lib/

# Triton model repository path the compiler resolves into emltriton's
# model-repo argument (workflow_core DEFAULT_CONTEXT_VALUES). Test-run
# models are staged here at runtime by harness/model_staging.py from the
# STAGED_MODELS manifest.
RUN mkdir -p /aws_dda/dda_triton/triton_model_repo

# DDA Triton conversion resources (harness/model_staging.py): the
# python-backend model templates (lfv_model_template.py,
# marshal_for_capture_template.py, inference_runtimes.py, ensemble_model)
# plus the lyra_* app packages they import, staged from
# src/backend/dda_triton/resources_for_copy and src/backend before
# building — see README.md. Like the plugin set, the image stays
# buildable with the directory empty; model staging then fails at
# runtime with a clear per-node error.
COPY test-sandbox/dda_triton_resources/ /opt/dda/dda_triton_resources/

# Runtime dependencies of the staged Triton python-backend models: the
# model artifact zips carry raw ONNX models (model.onnx + manifest.json,
# converted into a python-backend repo at staging time), executed by
# onnxruntime on CPU; the marshal template needs numpy + OpenCV. The
# Triton python backend runs with the system python3 (Ubuntu 22.04 /
# 3.10 — the same interpreter the 24.08 Triton image was built against).
RUN python3 -m pip install --no-cache-dir \
        numpy==1.26.4 \
        onnxruntime==1.18.1 \
        opencv-python-headless==4.10.0.84

# The DDA GStreamer plugin set (emltriton, emexifextract, emlcapture,
# emoutputevent, ...). Like src/backend/Dockerfile (which copies staged
# edgemlsdk artifacts), the x86_64 plugin .so files are staged into
# test-sandbox/plugins/ before building — see README.md. The image stays
# buildable with the directory empty; workflows needing DDA elements then
# fail at parse time with a per-node error rather than at build time.
COPY test-sandbox/plugins/ /opt/dda/gst-plugins/
# The DDA plugins link against the edgemlsdk core libraries
# (libpanorama.*.so, staged into plugins/lib/ alongside the plugin set);
# install them on the system linker path and keep the plugin directory
# itself plugin-only. Triton client libraries resolve from the copied
# /opt/tritonserver tree.
RUN if [ -d /opt/dda/gst-plugins/lib ]; then \
        mv /opt/dda/gst-plugins/lib/* /usr/lib/ && \
        rmdir /opt/dda/gst-plugins/lib && \
        ldconfig; \
    fi
ENV GST_PLUGIN_PATH=/opt/dda/gst-plugins
ENV LD_LIBRARY_PATH=/opt/tritonserver/lib

# Harness dependencies.
COPY test-sandbox/requirements.txt /opt/harness/requirements.txt
RUN python3 -m pip install --no-cache-dir -r /opt/harness/requirements.txt

# Vendored workflow_core (shared node catalog / compiled-document
# semantics — the same package the portal Lambdas and LocalServer use).
COPY backend/layers/workflow_core/python/workflow_core /opt/harness/workflow_core

# The test harness itself.
COPY test-sandbox/harness /opt/harness/harness

ENV PYTHONPATH=/opt/harness
WORKDIR /opt/harness

CMD ["python3", "-m", "harness"]
