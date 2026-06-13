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
RUN ./edge_ml1_p_camera_management/install_aravis.sh
RUN python3.11 -m grpc_tools.protoc --proto_path=./edgeagent --python_out=. --grpc_python_out=. edgeagent/edge-agent.proto
RUN apt update -y
RUN apt-get install libssl1.1 -y
RUN apt update -y
RUN apt install libexif12 libcurl4 libarchive13 gstreamer1.0-tools gstreamer1.0-libav ffmpeg -y
RUN if [ "$OS" = "18.04" ] ; then \
    apt install libavcodec-extra57i -y; \
    fi

RUN find / -name "libpython3.11.so.1.0" -print

RUN apt clean -y

COPY app.py ./
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
