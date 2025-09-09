ARG OS
FROM public.ecr.aws/ubuntu/ubuntu:${OS}
ARG OS
ARG PLATFORM
# Use non-interactive mode to suppress any prompt during installation
ENV DEBIAN_FRONTEND noninteractive

# Install basic dev libraries
RUN apt-get update 
RUN apt-get install build-essential software-properties-common lsb-release git curl libcurl4 libcurl4-openssl-dev \
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev libgstreamer-plugins-bad1.0-dev gstreamer1.0-plugins-base gstreamer1.0-plugins-good libsqlite3-dev gnupg pkg-config \
    gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-tools gstreamer1.0-x gstreamer1.0-alsa gstreamer1.0-gl \
    gstreamer1.0-gtk3 gstreamer1.0-qt5 libgstrtspserver-1.0-dev libgirepository1.0-dev libudev-dev libssl-dev --fix-missing -y
RUN apt-key adv --recv-keys --keyserver hkp://keyserver.ubuntu.com:80 16FAAD7AF99A65E2
# build openssl3 if on older system for the AWS IoT library
RUN if [ "$OS" = "18.04" ]; then \
    	apt-get install -y wget build-essential zlib1g-dev ca-certificates; \
    	wget https://github.com/openssl/openssl/releases/download/openssl-3.0.17/openssl-3.0.17.tar.gz; \
    	tar -xzf openssl-3.0.17.tar.gz;  \
    	cd openssl-3.0.17; \
    	./config --prefix=/usr/local/ssl --openssldir=/usr/local/ssl shared zlib; \
    	make; \
    	make install; \
        ldconfig; \ 
        ls -al /usr/local/ssl; \
        mkdir -p /usr/local/ssl/certs; \
        cp /etc/ssl/certs/ca-certificates.crt /usr/local/ssl/certs/; \
       
    fi
RUN echo "This is where the openssl should be installed OS=$OS PLATFORM=$PLATFORM"
RUN ls -al /usr/local/ssl/lib
ENV LD_LIBRARY_PATH=/usr/local/lib/:/usr/local/ssl/lib
RUN echo "LD_LIBRARY_PATH=$LD_LIBRARY_PATH"
RUN /usr/local/ssl/bin/openssl  version
RUN apt-key adv --recv-keys --keyserver hkp://keyserver.ubuntu.com:80 16FAAD7AF99A65E2
# Install gcc/g++ 11

RUN add-apt-repository ppa:ubuntu-toolchain-r/test && \
    apt-get update && \  
    apt install gcc-11 g++-11 -y && \
    rm /usr/bin/gcc && \
    rm /usr/bin/g++ && \
    ln -s /usr/bin/gcc-11 /usr/bin/gcc && \
    ln -s /usr/bin/g++-11 /usr/bin/g++

# Install latest version of clang
RUN apt-get install gpg wget -y && \
    wget -O - https://apt.llvm.org/llvm-snapshot.gpg.key | apt-key add - && \
    apt-key adv --recv-keys --keyserver hkp://keyserver.ubuntu.com:80 15CF4D18AF4F7421 && \
    apt update

ARG OS
RUN if [ "$OS" = "18.04" ]; then \
        add-apt-repository "deb http://apt.llvm.org/bionic/ llvm-toolchain-bionic main"; \
    else \
        add-apt-repository "deb https://apt.llvm.org/focal/ llvm-toolchain-focal-19 main"; \
    fi

RUN apt update && \
    apt-get install clang -y

# Install the latest versions of CMake
RUN if [ "$OS" = "18.04" ]; then \
        apt-get install -y software-properties-common lsb-release && \
        apt-get clean all && \
        wget -O - https://apt.kitware.com/keys/kitware-archive-latest.asc 2>/dev/null | gpg --dearmor - | tee /etc/apt/trusted.gpg.d/kitware.gpg >/dev/null && \
        apt-add-repository "deb https://apt.kitware.com/ubuntu/ $(lsb_release -cs) main" && \
        apt-key adv --recv-keys --keyserver hkp://keyserver.ubuntu.com:80  6AF7F09730B3F0A4 && \
        apt-get update && \
        apt-get install kitware-archive-keyring && \
        rm /etc/apt/trusted.gpg.d/kitware.gpg && \
        apt-get update && \
        apt-get install cmake=3.21.3-0kitware1ubuntu18.04.1 cmake-data=3.21.3-0kitware1ubuntu18.04.1 -y; \
    else \
        apt-get install -y software-properties-common lsb-release && \
        apt-get clean all && \
        wget -O - https://apt.kitware.com/keys/kitware-archive-latest.asc 2>/dev/null | gpg --dearmor - | tee /etc/apt/trusted.gpg.d/kitware.gpg >/dev/null && \
        apt-add-repository "deb https://apt.kitware.com/ubuntu/ $(lsb_release -cs) main" && \
        apt-key adv --recv-keys --keyserver hkp://keyserver.ubuntu.com:80  6AF7F09730B3F0A4 && \
        apt-get update && \
        apt-get install kitware-archive-keyring && \
        rm /etc/apt/trusted.gpg.d/kitware.gpg && \
        apt-get update && \
        apt-get install cmake=3.21.3-0kitware1ubuntu20.04.1 cmake-data=3.21.3-0kitware1ubuntu20.04.1 -y; \
    fi

# Install specific version of Python
#ARG PYTHON_VERSION
#RUN echo "Python Version = ${PYTHON_VERSION}"
#RUN apt-get install python${PYTHON_VERSION}-dev libpython${PYTHON_VERSION} python${PYTHON_VERSION} python${PYTHON_VERSION}-venv python3-pip -y && \
#    python${PYTHON_VERSION} -m pip install --user --upgrade pip && \
#    python${PYTHON_VERSION} -m pip install --upgrade build

RUN wget https://www.python.org/ftp/python/3.9.23/Python-3.9.23.tgz
RUN tar -xzf Python-3.9.23.tgz
RUN cd Python-3.9.23 && \
    ./configure --enable-optimizations --enable-shared --with-openssl=/usr/local/ssl && \
    make -j$(nproc) && \
    make install && \
    ldconfig



RUN find / -name "libpython3.9.so.1.0" -print

RUN python3.9 -m pip install --upgrade pip setuptools wheel build hatchling
RUN python3.9 -c "import ssl; print('SSL support:', ssl.OPENSSL_VERSION)" || echo "SSL module not available"
RUN python3.9 -c "import urllib.request; urllib.request.urlopen('https://pypi.org')" || echo "HTTPS connection failed"

# Conan version 2.1.0 has a bug causing build failure. Github issue: https://github.com/conan-io/conan/issues/15742
# Installing v.2.0.17 for now. Modify this after bug is resolved
RUN python3.9 -m pip install pyYAML && \
    python3.9 -m pip install conan && \
    conan profile detect
RUN python3.9 -m pip install --upgrade pydantic setuptools
# Coverage Tools
RUN apt-get install lcov -y

# Install AWS and panorama cli
RUN apt-get install unzip -y
RUN curl "https://awscli.amazonaws.com/awscli-exe-linux-${PLATFORM}.zip" -o awscliv2.zip \
    && unzip awscliv2.zip \
    && ./aws/install

# Install openssl
# Although Ubuntu 18.04 and 20.04 use openssl 1.1.x, when DDA consumes EdgeML SDK the automated container scan detects multiple CVEs
# We need to install openssl >= 3.1.1, may as well get the newest version as of writing (3.3.0)
# Using a mismatched version can lead to seg-fault from some AWS calls, so make sure to distribute and use the same version
RUN apt-get install checkinstall -y && \
    mkdir -p /dependencies/debs
RUN cd /dependencies \
    && git clone https://github.com/openssl/openssl.git \
    && cd openssl \
    && git checkout f84622c7e7f83a9bba30b25898182090801e752d \
    && git submodule update --init --recursive \
    && ./Configure --prefix=/usr/local/ssl --openssldir=/usr/local/ssl shared zlib no-docs no-tests \
    && make -j$(nproc)\
    && checkinstall -y \
    && mv /dependencies/openssl/openssl*.deb /dependencies/debs \
    && rm -rf /dependencies/openssl

# Get the AWS dependencies
# TODO: Should be made to Conan package
# Install Aws Cpp SDK
# Prefer shared libs
# Link to crypto from the system and as a shared lib
# Do not build aws-crt-cpp twice (once from aws-sdk-cpp and other from aws-iot-device-sdk-cpp-v2
# TODO: have to build aws-crt-cpp separately as BUILD_SHARED_LIBS is not propagated to it from its parent aws-sdk-cpp
RUN rm -rf /usr/local/include/openssl 
RUN apt-get install ninja-build -y
RUN cd /dependencies \
    && git clone https://github.com/awslabs/aws-crt-cpp.git \
    && cd aws-crt-cpp \
    && git checkout 6158ecefd0e0d53b2cf5e9d09f8c42f57e741e35 \
    && git submodule update --init --recursive
# Fix artifacts tests:
# s2n_init() failed: 402653268 (Failed to load or unload an openssl provider)
# Fatal error condition occurred in /dependencies/aws-crt-cpp/crt/aws-c-io/source/s2n/s2n_tls_channel_handler.c:197: 0 && "s2n_init() failed"
# https://stackoverflow.com/a/74611780
COPY patches/aws-crt-cpp-s2n.diff /dependencies/aws-crt-cpp/crt/s2n/aws-crt-cpp-s2n.diff
RUN cd /dependencies/aws-crt-cpp/crt/s2n \
    && git apply aws-crt-cpp-s2n.diff \
    && mkdir /dependencies/aws-crt-cpp/build \
    && cd /dependencies/aws-crt-cpp/build \
    && cmake -GNinja -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH=/usr/ -DCMAKE_INSTALL_PREFIX=/usr/ -DUSE_OPENSSL=ON -DBUILD_SHARED_LIBS=ON ../ \
    && ninja install
RUN ls -al /usr/local/ssl/lib


RUN cd /dependencies \
    && PLATFORM=$(uname -m) \
    && git clone https://github.com/aws/aws-sdk-cpp \
    && cd aws-sdk-cpp \
    && git checkout c63eb9e5f059ae2595cae8c79732c6938ca36b7d \
    && git submodule update --init --recursive \
    && mkdir build \
    && cd build \
    && cmake -GNinja -DBUILD_TESTING=OFF -DCMAKE_MODULE_PATH=/usr/lib/$PLATFORM-linux-gnu/cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH=/usr/ -DCMAKE_INSTALL_PREFIX=/usr/ -DBUILD_DEPS=OFF -DBUILD_ONLY="s3;sts;transfer;iot;iot-data;secretsmanager" ../ \
    && ninja install

# We build this separately as its part of aws-iot-sdk crt, and not in aws-cpp-sdk crt
RUN cd /dependencies \
     && git clone https://github.com/awslabs/aws-c-iot.git \
     && cd aws-c-iot \
     && git checkout 6e59b4660fecbc20c74493aee7da8a7b486b2966 \
     && git submodule update --init --recursive \
     && mkdir build \
     && cd build \
     && cmake -GNinja -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DCMAKE_PREFIX_PATH=/usr/ -DCMAKE_INSTALL_PREFIX=/usr/ -DBUILD_SHARED_LIBS=ON -DCMAKE_BUILD_TYPE="Release" -DBUILD_TESTING=OFF ../ \
     && ninja install

# Install Dependencies for aravis sdk
RUN apt-get install libxml2-dev libglib2.0-dev libusb-1.0-0-dev gobject-introspection \
            libgtk-3-dev libjpeg-dev gtk-doc-tools  xsltproc \
            libgstreamer-plugins-good1.0-dev \
            gettext -y
ENV PKG_CONFIG_PATH = /usr/lib/aarch64-linux-gnu/pkgconfig./usr/lib/aarch64-linux-gnu/pkgconfig
 
# Install meson
RUN python3.9 -m pip install meson

# Install aravis sdk release version 0.8
RUN cd /dependencies \
    && git clone https://github.com/AravisProject/aravis.git \
    && cd aravis \
    && git checkout 96cea9835ce1535547d8680c8b0be33e97f6cb30 \
    && git submodule update --init --recursive \
    && meson setup build -Dtests=false \
    && cd build \
    && ninja \
    && ninja install

# Build shared libs, and don't build aws-crt-app deps as its already build by aws-sdk-cpp
# build aws-c-iot separately
# TODO: AwsFindPackage from aws-crt-cpp is installed in /usr/lib/$ARCH-linux-gnu/ needed to add CMAKE_MODULE_PATH override
# TODO: turned off testing BUILD_TESTING to workaround  eventstream_rpc test build failure
RUN cd /dependencies \
    && PLATFORM=$(uname -m) \
    && git clone https://github.com/aws/aws-iot-device-sdk-cpp-v2.git \
    && cd aws-iot-device-sdk-cpp-v2 \
    && git checkout f7cbf0982d2f23902406a0ba696a1a900b732a7e \
    && git submodule update --init --recursive \
    && mkdir build \
    && cd build \
    && cmake -GNinja -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DCMAKE_MODULE_PATH=/usr/lib/$PLATFORM-linux-gnu/cmake -DCMAKE_INSTALL_PREFIX=/usr/ -DBUILD_SHARED_LIBS=ON -DBUILD_DEPS=OFF -DCMAKE_BUILD_TYPE="Release" -DBUILD_TESTING=OFF ../ \
    && ninja install

# Debug helpers
RUN apt-get install gdb -y
RUN apt-get install vim -y

# Python Dependencies
RUN echo "check which python we are using"
RUN which python3
RUN which python3.9
RUN python3 --version
RUN ls -al /usr/bin/python3.6
#RUN apt-get remove --autoremove python3.6 -y
RUN apt-get install libcairo2 libcairo2-dev -y
RUN apt-get remove --autoremove python3-gi -y \
    && python3.9 -m pip install Cython boto3 awscrt multiprocess conan coverage pylint \
    && python3.9 -m pip install -U scalene \
    && update-alternatives --install /usr/local/bin/python3 python3 /usr/local/bin/python3.9 1 \
    && update-alternatives --set python3 /usr/local/bin/python3.9

RUN python3.9 -m pip install pygobject==3.44.1
ENV PYTHON_EXECUTABLE=/usr/local/bin/python3.9
ENV Python3_EXECUTABLE=/usr/local/bin/python3.9
ENV Python3_INCLUDE_DIR=/usr/local/include/python3.9
ENV Python3_LIBRARY=/usr/local/lib/libpython3.9.so

# Install SWIG
RUN mkdir /swig \
    && cd /swig \
    && wget http://prdownloads.sourceforge.net/swig/swig-4.0.2.tar.gz \
    && mkdir swig \
    && tar -xzvf swig-4.0.2.tar.gz -C ./swig \
    && rm swig-4.0.2.tar.gz \
    && cd ./swig/swig-4.0.2 \
    && ./configure \
    && make \
    && make install

RUN apt-get install rsync -y

# Install powershell
ARG PWSH_ARCH
RUN curl -L -o /tmp/powershell.tar.gz https://github.com/PowerShell/PowerShell/releases/download/v7.2.3/powershell-7.2.3-linux-$PWSH_ARCH.tar.gz \
    && mkdir -p /opt/microsoft/powershell/7 \
    && tar zxf /tmp/powershell.tar.gz -C /opt/microsoft/powershell/7 \
    && chmod +x /opt/microsoft/powershell/7/pwsh \
    && ln -s /opt/microsoft/powershell/7/pwsh /usr/bin/pwsh

RUN apt-get install nasm -y

# ffmpeg
RUN cd /dependencies \
    && git clone https://github.com/FFmpeg/FFmpeg.git

RUN cd /dependencies/FFmpeg \
    && git checkout ffb5153

    RUN export CFLAGS='-fstack-protector-strong -fpie -pie -Wl,-z,relro,-z,now -D_FORTIFY_SOURCE=2' \
    && export CXXFLAGS='${CFLAGS}' \
    && cd /dependencies/FFmpeg \
    && ./configure --disable-everything --disable-gpl --disable-version3 --disable-nonfree --disable-static --disable-network --enable-shared --enable-muxer=mp4 --prefix=/dependencies/FFmpeg/install \
    && make \
    && make install

# Install ffmpeg to use some of it's tools without needing to compile everything
RUN apt-get install ffmpeg -y

# Install Triton Server and it's dependencies
RUN apt-get update && \
    apt-get install rapidjson-dev libre2-dev -y
RUN apt-get install python-dev -y

# Use --no-check-certificate to resolve following error
# ERROR: cannot verify boostorg.jfrog.io's certificate, issued by 'CN=GeoTrust TLS RSA CA G1,OU=www.digicert.com,O=DigiCert Inc,C=US':
# Self-signed certificate encountered.
RUN cd /dependencies && \
    wget --no-check-certificate https://github.com/boostorg/boost/releases/download/boost-1.81.0/boost-1.81.0.tar.gz && \
    tar -xzvf boost-1.81.0.tar.gz && \
    rm boost-1.81.0.tar.gz && \
    cd boost-1.81.0 && \
    ./bootstrap.sh && \
    ./b2 install


#RUN cd /dependencies && \
 #   wget --no-check-certificate https://github.com/boostorg/boost/releases/download/boost-1.81.0/boost-1.81.0.tar.gz && \
    #wget https://cytranet-dal.dl.sourceforge.net/project/boost/boost/1.81.0/boost_1_81_0.tar.gz --no-check-certificate && \
  #  tar -xzvf boost_1_81_0.tar.gz && \
   # rm boost-1_81_0.tar.gz && \
    #cd boost-1_81_0 && \
    #./bootstrap.sh && \
    #./b2 install
RUN apt-get install libnuma-dev -y \
    && rm /usr/bin/python \
    && ln -s /usr/bin/python3.9 /usr/bin/python

RUN apt-get install libarchive-dev -y

# Triton checkout tag
# Current tag 2.45.0 released on April 30,2024.
# Fixed tag for reproducible build and patching.
# Patching for building with static stdc++.
RUN cd /dependencies && \
    git clone https://github.com/triton-inference-server/server.git && \
    cd server && \
    git checkout tags/v2.45.0
# copy server diff and core diff
COPY patches/edgeml-triton-server.diff /dependencies/server/edgeml-triton-server.diff
COPY patches/edgeml-triton-core.diff /dependencies/server/edgeml-triton-core.diff
#b64-dev required for triton metrics
RUN ls -al /usr/bin/python3.6
RUN dpkg -l |grep python3.6
#RUN apt remove python3.6 -y
RUN apt-get install libb64-dev -y
# Apply server patch, internally the server patch applies core patch, build triton.
RUN echo PATH=$PATH
RUN export
# Remove Python 3.6 system packages that pybind11 is finding
RUN apt-get remove --purge python python3.6 python3.6-minimal libpython3.6 libpython3.6-minimal libpython3.6-stdlib python3-minimal python3-distutils python3-lib2to3 -y && \
    apt-get autoremove -y && \
    ln -sf /usr/local/bin/python3.9 /usr/bin/python3
RUN mkdir -p /usr/include/python3.6m && \ 
    ln -sf /usr/local/include/python3.9/* /usr/include/python3.6m/
#RUN echo "=== Python Debug Info ===" && \
#    python3 --version && \
#    which python3 && \
#    ls -la /usr/bin/python* && \
#    ls -la /usr/include/python* && \
#    find /usr -name "python*.pc" 2>/dev/null && \
#    dpkg -l | grep python3 && \
#    echo "=== Environment Variables ===" && \
#    env | grep -i python && \
#    echo "=== End Debug ==="
RUN export PKG_CONFIG_PATH="/usr/local/ssl/lib/pkgconfig:$PKG_CONFIG_PATH" && \
    export PYBIND11_FINDPYTHON=ON && \
    cd /dependencies/server && \ 
    git apply edgeml-triton-server.diff && \
    python3.9 build.py --enable-logging  --enable-stats --enable-metrics --enable-cpu-metrics --no-container-build \
    --extra-core-cmake-arg=TRITON_ENABLE_ENSEMBLE=ON \
    --extra-core-cmake-arg=PYBIND11_FINDPYTHON=ON \
    --extra-core-cmake-arg=-DPYBIND11_FINDPYTHON=ON \
    --extra-core-cmake-arg=CMAKE_POLICY_VERSION_MINIMUM=3.5 \
    --extra-core-cmake-arg=PYTHON_EXECUTABLE=/usr/local/bin/python3.9 \
    --extra-core-cmake-arg=-DPYTHON_EXECUTABLE=/usr/local/bin/python3.9 \
    --extra-core-cmake-arg=PYTHON_INCLUDE_DIR=/usr/local/include/python3.9 \
    --extra-core-cmake-arg=PYTHON_LIBRARY=/usr/local/lib/libpython3.9.so \
    --extra-core-cmake-arg=PYTHON_LIBRARIES=/usr/local/lib/libpython3.9.so \ 
    --extra-core-cmake-arg=-DPYTHON_INCLUDE_DIR=/usr/local/include/python3.9 \
    --extra-core-cmake-arg=PYBIND11_PYTHON_VERSION=3.9 \
    --backend python --build-dir=`pwd`/build
RUN mv /dependencies/server/build/python/install/backends/python /dependencies/server/build/tritonserver/install/backends

RUN cd /dependencies/server/build/opt/ \
    && tar cvzf triton_installation_files.tar.gz tritonserver/ \
    && mkdir -p /dependencies/tars/ \
    && cp triton_installation_files.tar.gz /dependencies/tars/

# Download c-periphery for GPIO protocol client.
RUN cd /dependencies \
        && git clone https://github.com/vsergeev/c-periphery \
        && cd c-periphery \
        && git checkout 91f9678d2a35a3af3c633f20165bdde8bea32209 \
        && mkdir build \
        && cd build \
        && cmake ../ \
        && make \
        && make install
  
# Download exif dependencies.
RUN apt-get -y install libexif-dev


COPY src ./src

COPY create_aws_triton_deps_pkg.sh /dependencies/debs
RUN cd /dependencies/debs \
    && chmod +x create_aws_triton_deps_pkg.sh \
    && ./create_aws_triton_deps_pkg.sh

# Install GStreamer packages needed for build
RUN apt-get update && apt-get install -y \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    libglib2.0-dev \
    libxml2-dev

# Download other packages
RUN cd /dependencies/debs \
    && apt-get download libgstreamer1.0-dev \
    && apt-get download libgstreamer1.0 \
    && apt-get download libgstreamer-plugins-base1.0 \
    && apt-get download libgstreamer-plugins-base1.0-dev \
    && apt-get download liborc-0.4-0 \
    && apt-get download libstdc++6
#TODO add if check here, 1.15 for ubuntu 18.04/20.04 and 1.16 for 22.04+
#RUN wget --no-check-certificate https://github.com/civetweb/civetweb/archive/refs/tags/v1.15.tar.gz \
#    && tar -xzvf v1.15.tar.gz \
#    && cd civetweb-1.15 \
#    && make build \
#    && make build install \
#    && ldconfig 

RUN cd src/ \
    && conan build . --build=missing \
    && cd build/Release \
    && export LD_LIBRARY_PATH=$(pwd)/lib \
    && make install \
    && cd ./install/ \
    && chmod a+x create_debian.sh \
    && ./create_debian.sh \
    && pwsh ./create_whl.ps1 -InstallDirectory ./lib -Version 1.0 \
    && mkdir -p /debs/ \
    && cd ../../../../ \
    && cp src/build/Release/install/Panorama*.deb /debs/PanoramaSDK.deb \
    && cp src/build/Release/install/lib/python_package/dist/*.whl /debs/panorama.whl \
    && cp /dependencies/debs/aws-c-iot.deb /debs/aws-c-iot.deb \ 
    && cp /dependencies/debs/aws-crt-cpp.deb  /debs/aws-crt-cpp.deb \
    && cp /dependencies/debs/libgstreamer1.0-0_*.deb /debs/libgstreamer1.0.deb \
    && cp /dependencies/debs/libgstreamer1.0-dev*.deb /debs/libgstreamer1.0-dev.deb \
    && cp /dependencies/debs/aws-iot-device-sdk-cpp-v2.deb  /debs/aws-iot-device-sdk-cpp-v2.deb \
    && cp /dependencies/debs/liborc-0.4-0*.deb /debs/liborc-0.4-0.deb \
    && cp /dependencies/debs/aws-sdk-cpp.deb /debs/aws-sdk-cpp.deb \
    && cp /dependencies/debs/libstdc++6*.deb /debs/libstdc++6.deb \
    && cp /dependencies/debs/libgstreamer-plugins-base1.0-0*.deb /debs/libgstreamer-plugins-base1.0-dev.deb \
    && cp /dependencies/debs/openssl_*.deb /debs/openssl.deb \
    && cp /dependencies/debs/libgstreamer-plugins-base1.0-dev*.deb  /debs/libgstreamer-plugins-base1.0-dev.deb \
    && cp /dependencies/debs/triton-core.deb /debs/triton-core.deb \
    && cp /dependencies/debs/triton-python-backend.deb /debs/triton-python-backend.deb \
    && mkdir -p /tars \
    && cd /tars \
    && cp /dependencies/tars/triton_installation_files.tar.gz .

# cleanup - this only applies to JP4.x
RUN if test -f /usr/bin/python3.6-bak; then \
        mv /usr/bin/python3.6-bak /usr/bin/python3.6; \
    else \
        echo "no python3.6 backup found to restore"; \
    fi


RUN apt-get clean

### End of EdgeML SDK
