FROM nvcr.io/nvidia/l4t-jetpack:r35.4.1 AS builder
ARG OS
ARG PLATFORM
ARG PWSH_ARCH
ARG PYTHON_VERSION
ENV DEBIAN_FRONTEND noninteractive

# ── 0. Detect QEMU — sets IS_QEMU=1 if running under emulation ────────────
# Native aarch64 builds skip all retry loops, -O0, -j1, and libevent patches.
RUN if head -1 /proc/self/maps 2>/dev/null | grep -q qemu; then \
        echo "1" > /tmp/.is_qemu; \
    elif [ -f /usr/bin/qemu-aarch64-static ] || [ -f /usr/bin/qemu-aarch64 ]; then \
        echo "1" > /tmp/.is_qemu; \
    else \
        echo "0" > /tmp/.is_qemu; \
    fi && \
    echo "QEMU detected: $(cat /tmp/.is_qemu)"

# Helper: sets shell vars used by every build step
# Usage: source /tmp/build-env.sh at the start of each RUN
RUN IS_QEMU=$(cat /tmp/.is_qemu) && \
    if [ "$IS_QEMU" = "1" ]; then \
        printf 'JOBS=1\nRETRIES=30\nOPENSSL_CFLAGS="-O0"\nOPENSSL_ASM="no-asm"\nEXTRA_CFLAGS="-O0 -D_GNU_SOURCE"\nCONAN_JOBS_ARGS="-c tools.build:jobs=1"\nMAKE_CMD="make -j1"\nNINJA_CMD="ninja -j1"\nB2_CMD="./b2 -j1"\nTRITON_PARALLEL=1\n' > /tmp/build-env.sh; \
    else \
        NCPU=$(nproc) && \
        printf "JOBS=$NCPU\nRETRIES=1\nOPENSSL_CFLAGS=\"\"\nOPENSSL_ASM=\"\"\nEXTRA_CFLAGS=\"\"\nCONAN_JOBS_ARGS=\"\"\nMAKE_CMD=\"make -j$NCPU\"\nNINJA_CMD=\"ninja -j$NCPU\"\nB2_CMD=\"./b2 -j$NCPU\"\nTRITON_PARALLEL=$NCPU\n" > /tmp/build-env.sh; \
    fi && \
    echo "=== Build environment ===" && cat /tmp/build-env.sh


# ── 1. System packages ─────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential software-properties-common lsb-release git curl wget gpg unzip rsync \
    clang \
    libcurl4 libcurl4-openssl-dev libsqlite3-dev gnupg pkg-config libudev-dev libssl-dev \
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev libgstreamer-plugins-bad1.0-dev \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-tools gstreamer1.0-x \
    gstreamer1.0-alsa gstreamer1.0-gl gstreamer1.0-gtk3 gstreamer1.0-qt5 \
    libgstrtspserver-1.0-dev libgstreamer-plugins-good1.0-dev \
    libgirepository1.0-dev gobject-introspection \
    libxml2-dev libglib2.0-dev libusb-1.0-0-dev libgtk-3-dev libjpeg-dev \
    gtk-doc-tools xsltproc gettext \
    libcairo2 libcairo2-dev libexif-dev libarchive-dev libnuma-dev libb64-dev \
    rapidjson-dev libre2-dev python-dev \
    ninja-build checkinstall ffmpeg swig \
    && rm -rf /var/lib/apt/lists/*

ENV LD_LIBRARY_PATH=/usr/local/lib/

# ── 2. CMake 3.21.3 from Kitware ──────────────────────────────────────────
RUN wget -O /etc/apt/trusted.gpg.d/kitware.asc https://apt.kitware.com/keys/kitware-archive-latest.asc && \
    echo "deb https://apt.kitware.com/ubuntu/ focal main" > /etc/apt/sources.list.d/kitware.list && \
    apt-get update && \
    apt-get install -y cmake-data=3.21.3-0kitware1ubuntu20.04.1 cmake=3.21.3-0kitware1ubuntu20.04.1 && \
    rm -f /etc/apt/sources.list.d/kitware.list /etc/apt/trusted.gpg.d/kitware.asc && \
    rm -rf /var/lib/apt/lists/*

# ── 3. Python 3.9 + pip tooling ───────────────────────────────────────────
RUN rm -f /etc/apt/sources.list.d/*kitware* && \
    sed -i '/kitware/d' /etc/apt/sources.list && \
    apt-get update && \
    apt-get install -y python${PYTHON_VERSION}-dev libpython${PYTHON_VERSION} \
    python${PYTHON_VERSION} python${PYTHON_VERSION}-venv python3-pip && \
    rm -rf /var/lib/apt/lists/* && \
    update-alternatives --install /usr/bin/python3 python3 /usr/bin/python${PYTHON_VERSION} 1 && \
    update-alternatives --set python3 /usr/bin/python${PYTHON_VERSION} && \
    rm -f /usr/bin/python && ln -s /usr/bin/python3.9 /usr/bin/python && \
    python${PYTHON_VERSION} -m pip install --user --upgrade pip && \
    python${PYTHON_VERSION} -m pip install --upgrade build

ENV PYTHON_EXECUTABLE=/usr/bin/python3.9
ENV Python3_EXECUTABLE=/usr/bin/python3.9
ENV Python3_INCLUDE_DIR=/usr/include/python3.9
ENV Python3_LIBRARY=/usr/lib/libpython3.9.so

# ── 4. Python packages ────────────────────────────────────────────────────
RUN pip3 install --upgrade pip setuptools==64.0.3 wheel build hatchling packaging==24.2 && \
    pip3 install --ignore-installed PyYAML && \
    pip3 install conan && conan profile detect && \
    pip3 install --upgrade pydantic setuptools==64.0.3 meson && \
    pip3 install Cython boto3 awscrt multiprocess coverage pylint && \
    pip3 install pygobject==3.44.1

# ── 5. AWS CLI ─────────────────────────────────────────────────────────────
RUN curl "https://awscli.amazonaws.com/awscli-exe-linux-${PLATFORM}.zip" -o awscliv2.zip && \
    unzip -q awscliv2.zip && ./aws/install && rm -rf awscliv2.zip aws/

# ── 6. PowerShell ─────────────────────────────────────────────────────────
RUN curl -L -o /tmp/powershell.tar.gz https://github.com/PowerShell/PowerShell/releases/download/v7.2.3/powershell-7.2.3-linux-$PWSH_ARCH.tar.gz && \
    mkdir -p /opt/microsoft/powershell/7 && \
    tar zxf /tmp/powershell.tar.gz -C /opt/microsoft/powershell/7 && \
    chmod +x /opt/microsoft/powershell/7/pwsh && \
    ln -s /opt/microsoft/powershell/7/pwsh /usr/bin/pwsh && \
    rm /tmp/powershell.tar.gz

# ── 7. OpenSSL 3.x (cached or built from source) ──────────────────────────
RUN mkdir -p /dependencies/debs
COPY cached-debs/ /dependencies/cached-debs/
RUN . /tmp/build-env.sh && \
    if ls /dependencies/cached-debs/openssl*.deb 1>/dev/null 2>&1; then \
        echo "=== Using cached OpenSSL deb ===" && \
        cp /dependencies/cached-debs/openssl*.deb /dependencies/debs/ && \
        dpkg -i /dependencies/debs/openssl*.deb; \
    else \
        echo "=== Building OpenSSL from source (QEMU=$(cat /tmp/.is_qemu)) ===" && \
        cd /dependencies && \
        git clone --depth 1 -b openssl-3.3.0 https://github.com/openssl/openssl.git && \
        cd openssl && \
        CFLAGS="$OPENSSL_CFLAGS" ./Configure --prefix=/usr/local/ssl --openssldir=/usr/local/ssl \
            shared zlib no-docs no-tests $OPENSSL_ASM && \
        for i in $(seq 1 $RETRIES); do \
            echo "=== OpenSSL build attempt $i ===" && \
            $MAKE_CMD && break; \
            echo "=== Attempt $i failed, retrying... ===" && sleep 1; \
        done && \
        test -f libssl.so || (echo "OpenSSL build failed" && exit 1) && \
        checkinstall -y && \
        mv /dependencies/openssl/openssl*.deb /dependencies/debs && \
        rm -rf /dependencies/openssl; \
    fi
RUN rm -rf /usr/local/include/openssl

# ── 8. aws-crt-cpp ────────────────────────────────────────────────────────
COPY patches/aws-crt-cpp-s2n.diff /tmp/aws-crt-cpp-s2n.diff
RUN . /tmp/build-env.sh && \
    cd /dependencies && \
    git clone https://github.com/awslabs/aws-crt-cpp.git && \
    cd aws-crt-cpp && \
    git checkout 6158ecefd0e0d53b2cf5e9d09f8c42f57e741e35 && \
    git submodule update --init --recursive && \
    cp /tmp/aws-crt-cpp-s2n.diff crt/s2n/aws-crt-cpp-s2n.diff && \
    cd crt/s2n && git apply aws-crt-cpp-s2n.diff && \
    mkdir /dependencies/aws-crt-cpp/build && \
    cd /dependencies/aws-crt-cpp/build && \
    cmake -GNinja -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH=/usr/ \
        -DCMAKE_INSTALL_PREFIX=/usr/ -DUSE_OPENSSL=ON -DBUILD_SHARED_LIBS=ON ../ && \
    for i in $(seq 1 $RETRIES); do $NINJA_CMD install && break; echo "=== ninja attempt $i failed ===" && sleep 1; done

# ── 9. aws-sdk-cpp ────────────────────────────────────────────────────────
RUN . /tmp/build-env.sh && \
    cd /dependencies && \
    PLATFORM=$(uname -m) && \
    git clone https://github.com/aws/aws-sdk-cpp && \
    cd aws-sdk-cpp && \
    git checkout c63eb9e5f059ae2595cae8c79732c6938ca36b7d && \
    git submodule update --init --recursive && \
    mkdir build && cd build && \
    cmake -GNinja -DBUILD_TESTING=OFF \
        -DCMAKE_MODULE_PATH=/usr/lib/$PLATFORM-linux-gnu/cmake \
        -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH=/usr/ -DCMAKE_INSTALL_PREFIX=/usr/ \
        -DBUILD_DEPS=OFF -DBUILD_ONLY="s3;sts;transfer;iot;iot-data;sagemaker-edge;secretsmanager" ../ && \
    for i in $(seq 1 $RETRIES); do $NINJA_CMD install && break; echo "=== ninja attempt $i failed ===" && sleep 1; done

# ── 10. aws-c-iot ─────────────────────────────────────────────────────────
RUN . /tmp/build-env.sh && \
    cd /dependencies && \
    git clone https://github.com/awslabs/aws-c-iot.git && \
    cd aws-c-iot && \
    git checkout 6e59b4660fecbc20c74493aee7da8a7b486b2966 && \
    git submodule update --init --recursive && \
    mkdir build && cd build && \
    cmake -GNinja -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DCMAKE_PREFIX_PATH=/usr/ \
        -DCMAKE_INSTALL_PREFIX=/usr/ -DBUILD_SHARED_LIBS=ON \
        -DCMAKE_BUILD_TYPE="Release" -DBUILD_TESTING=OFF \
        -DCMAKE_HAVE_THREADS_LIBRARY=1 -DCMAKE_USE_PTHREADS_INIT=1 -DTHREADS_PREFER_PTHREAD_FLAG=ON ../ && \
    for i in $(seq 1 $RETRIES); do $NINJA_CMD install && break; echo "=== ninja attempt $i failed ===" && sleep 1; done

# ── 11. Aravis SDK ────────────────────────────────────────────────────────
RUN . /tmp/build-env.sh && \
    cd /dependencies && \
    git clone https://github.com/AravisProject/aravis.git && \
    cd aravis && \
    git checkout 96cea9835ce1535547d8680c8b0be33e97f6cb30 && \
    git submodule update --init --recursive && \
    meson setup build -Dtests=false -Dintrospection=disabled && \
    cd build && \
    for i in $(seq 1 $RETRIES); do $NINJA_CMD && break; echo "=== ninja attempt $i failed ===" && sleep 1; done && \
    ninja install

# ── 12. aws-iot-device-sdk-cpp-v2 ─────────────────────────────────────────
RUN . /tmp/build-env.sh && \
    cd /dependencies && \
    PLATFORM=$(uname -m) && \
    git clone https://github.com/aws/aws-iot-device-sdk-cpp-v2.git && \
    cd aws-iot-device-sdk-cpp-v2 && \
    git checkout f7cbf0982d2f23902406a0ba696a1a900b732a7e && \
    git submodule update --init --recursive && \
    mkdir build && cd build && \
    cmake -GNinja -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
        -DCMAKE_MODULE_PATH=/usr/lib/$PLATFORM-linux-gnu/cmake \
        -DCMAKE_INSTALL_PREFIX=/usr/ -DBUILD_SHARED_LIBS=ON \
        -DBUILD_DEPS=OFF -DCMAKE_BUILD_TYPE="Release" -DBUILD_TESTING=OFF ../ && \
    for i in $(seq 1 $RETRIES); do $NINJA_CMD install && break; echo "=== ninja attempt $i failed ===" && sleep 1; done

# ── 13. Boost 1.81 ────────────────────────────────────────────────────────
RUN . /tmp/build-env.sh && \
    cd /dependencies && \
    wget --no-check-certificate https://github.com/boostorg/boost/releases/download/boost-1.81.0/boost-1.81.0.tar.gz && \
    tar -xzf boost-1.81.0.tar.gz && rm boost-1.81.0.tar.gz && \
    cd boost-1.81.0 && ./bootstrap.sh && \
    for i in $(seq 1 $RETRIES); do $B2_CMD install && break; echo "=== b2 attempt $i failed ===" && sleep 1; done && \
    cd / && rm -rf /dependencies/boost-1.81.0

# ── 14. Triton Inference Server ────────────────────────────────────────────
COPY patches/edgeml-triton-server.diff /tmp/edgeml-triton-server.diff
COPY patches/edgeml-triton-core.diff /tmp/edgeml-triton-core.diff
RUN . /tmp/build-env.sh && \
    IS_QEMU=$(cat /tmp/.is_qemu) && \
    cd /dependencies && \
    git clone https://github.com/triton-inference-server/server.git && \
    cd server && git checkout tags/v2.45.0 && \
    cp /tmp/edgeml-triton-server.diff . && \
    cp /tmp/edgeml-triton-core.diff . && \
    export PYBIND11_FINDPYTHON=ON && \
    export MAKEFLAGS="-j$JOBS" && \
    if [ "$IS_QEMU" = "1" ]; then export CFLAGS="$EXTRA_CFLAGS"; export CXXFLAGS="$EXTRA_CFLAGS"; fi && \
    git apply edgeml-triton-server.diff && \
    for i in $(seq 1 $RETRIES); do \
        echo "=== Triton build attempt $i ===" && \
        if [ "$IS_QEMU" = "1" ]; then \
            LE_CMAKE=$(find build/ -path "*/libevent/src/libevent/CMakeLists.txt" 2>/dev/null | head -1) && \
            if [ -n "$LE_CMAKE" ] && ! grep -q "QEMU_FIX" "$LE_CMAKE" 2>/dev/null; then \
                echo "Patching libevent CMakeLists.txt for QEMU" && \
                sed -i '1i # QEMU_FIX: force POSIX features that cmake tests miss under QEMU\nset(EVENT__HAVE_FCNTL_H 1)\nset(EVENT__HAVE_NETDB_H 1)\nset(EVENT__HAVE_UNISTD_H 1)\nset(EVENT__HAVE_ARPA_INET_H 1)\nset(EVENT__HAVE_NETINET_IN_H 1)\nset(EVENT__HAVE_SYS_SOCKET_H 1)\nset(EVENT__HAVE_POLL_H 1)\nset(EVENT__HAVE_PTHREADS 1)\nset(EVENT__HAVE_EPOLL 1)\nset(EVENT__HAVE_PIPE2 1)\nset(EVENT__HAVE_GETADDRINFO 1)\nset(EVENT__HAVE_GETSERVBYNAME 1)\nset(EVENT__HAVE_GETPROTOBYNUMBER 1)\nset(EVENT__HAVE_GETNAMEINFO 1)' "$LE_CMAKE" && \
                LE_BUILD=$(find build/ -path "*/libevent/src/libevent-build" -type d 2>/dev/null | head -1) && \
                if [ -n "$LE_BUILD" ]; then rm -rf "$LE_BUILD"; fi; \
            fi; \
        fi && \
        python3.9 build.py --enable-logging --enable-stats --enable-metrics --enable-cpu-metrics --no-container-build \
            --build-parallel $TRITON_PARALLEL \
            --extra-core-cmake-arg=TRITON_ENABLE_ENSEMBLE=ON \
            --extra-core-cmake-arg=PYBIND11_FINDPYTHON=ON \
            --extra-core-cmake-arg=-DPYBIND11_FINDPYTHON=ON \
            --extra-core-cmake-arg=CMAKE_POLICY_VERSION_MINIMUM=3.5 \
            --extra-core-cmake-arg=TRITON_CORE_ENABLE_TESTS=OFF \
            --extra-core-cmake-arg=PYTHON_EXECUTABLE=/usr/bin/python3.9 \
            --extra-core-cmake-arg=-DPYTHON_EXECUTABLE=/usr/bin/python3.9 \
            --extra-core-cmake-arg=PYTHON_INCLUDE_DIR=/usr/include/python3.9 \
            --extra-core-cmake-arg=PYTHON_LIBRARY=/usr/lib/libpython3.9.so \
            --extra-core-cmake-arg=PYTHON_LIBRARIES=/usr/lib/libpython3.9.so \
            --extra-core-cmake-arg=-DPYTHON_INCLUDE_DIR=/usr/include/python3.9 \
            --extra-core-cmake-arg=PYBIND11_PYTHON_VERSION=3.9 \
            --backend python --build-dir=$(pwd)/build && break; \
        echo "=== Triton attempt $i failed, retrying... ===" && sleep 2; \
    done && \
    mv build/python/install/backends/python build/tritonserver/install/backends && \
    cd build/opt/ && \
    tar cvzf triton_installation_files.tar.gz tritonserver/ && \
    mkdir -p /dependencies/tars/ && \
    cp triton_installation_files.tar.gz /dependencies/tars/

# ── 15. c-periphery (GPIO) ────────────────────────────────────────────────
RUN . /tmp/build-env.sh && \
    cd /dependencies && \
    git clone https://github.com/vsergeev/c-periphery && \
    cd c-periphery && \
    git checkout 91f9678d2a35a3af3c633f20165bdde8bea32209 && \
    mkdir build && cd build && cmake ../ && \
    for i in $(seq 1 $RETRIES); do $MAKE_CMD && break; echo "=== make attempt $i failed ===" && sleep 1; done && \
    make install

# ── 16. EdgeML SDK build + package all debs ────────────────────────────────
COPY src ./src
COPY create_aws_triton_deps_pkg.sh /dependencies/debs/
RUN cd /dependencies/debs && chmod +x create_aws_triton_deps_pkg.sh && ./create_aws_triton_deps_pkg.sh

# Download system packages needed by backend
RUN cd /dependencies/debs && \
    apt-get update && \
    apt-get download libgstreamer1.0-dev && \
    apt-get download libgstreamer1.0 && \
    apt-get download libgstreamer-plugins-base1.0 && \
    apt-get download libgstreamer-plugins-base1.0-dev && \
    apt-get download liborc-0.4-0 && \
    apt-get download libstdc++6

# Build EdgeML SDK (conan + cmake)
# civetweb SSL disabled — the SDK uses mg_init_library(0) so SSL is not needed.
RUN . /tmp/build-env.sh && \
    cd src/ && \
    for i in $(seq 1 $RETRIES); do \
        echo "=== conan build attempt $i ===" && \
        MAKEFLAGS="-j$JOBS" CONAN_CPU_COUNT=$JOBS \
        conan build . --build=missing \
          -o civetweb/*:with_ssl=False \
          $CONAN_JOBS_ARGS && break; \
        echo "=== conan attempt $i failed, retrying... ===" && sleep 2; \
    done && \
    cd build/Release && \
    export LD_LIBRARY_PATH=$(pwd)/lib && \
    for i in $(seq 1 $RETRIES); do $MAKE_CMD install && break; echo "=== make attempt $i failed ===" && sleep 1; done && \
    cd ./install/ && \
    chmod a+x create_debian.sh && \
    ./create_debian.sh && \
    pwsh ./create_whl.ps1 -InstallDirectory ./lib -Version 1.0

# ── 17. Collect all output artifacts ───────────────────────────────────────
RUN mkdir -p /debs/ /tars/ && \
    cp src/build/Release/install/Panorama*.deb /debs/PanoramaSDK.deb && \
    cp src/build/Release/install/lib/python_package/dist/*.whl /debs/panorama.whl && \
    cp /dependencies/debs/aws-c-iot.deb /debs/aws-c-iot.deb && \
    cp /dependencies/debs/aws-crt-cpp.deb /debs/aws-crt-cpp.deb && \
    cp /dependencies/debs/libgstreamer1.0-0_*.deb /debs/libgstreamer1.0.deb && \
    cp /dependencies/debs/libgstreamer1.0-dev*.deb /debs/libgstreamer1.0-dev.deb && \
    cp /dependencies/debs/aws-iot-device-sdk-cpp-v2.deb /debs/aws-iot-device-sdk-cpp-v2.deb && \
    cp /dependencies/debs/liborc-0.4-0*.deb /debs/liborc-0.4-0.deb && \
    cp /dependencies/debs/aws-sdk-cpp.deb /debs/aws-sdk-cpp.deb && \
    cp /dependencies/debs/libstdc++6*.deb /debs/libstdc++6.deb && \
    cp /dependencies/debs/libgstreamer-plugins-base1.0-0*.deb /debs/libgstreamer-plugins-base1.0.deb && \
    cp /dependencies/debs/openssl_*.deb /debs/openssl.deb && \
    cp /dependencies/debs/libgstreamer-plugins-base1.0-dev*.deb /debs/libgstreamer-plugins-base1.0-dev.deb && \
    cp /dependencies/debs/triton-core.deb /debs/triton-core.deb && \
    cp /dependencies/debs/triton-python-backend.deb /debs/triton-python-backend.deb && \
    cp /dependencies/tars/triton_installation_files.tar.gz /tars/

RUN apt-get clean

### End of EdgeML SDK

# Extractor stage: only contains the debs and tars for --output type=local extraction
FROM scratch AS extractor
COPY --from=builder /debs/ /debs/
COPY --from=builder /tars/ /tars/
