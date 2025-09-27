ARG OS
FROM public.ecr.aws/ubuntu/ubuntu:${OS}
#FROM public.ecr.aws/docker/library/python:3.9-slim-bookworm
ENV DEBIAN_FRONTEND=noninteractive
#RUN apt-key adv --recv-keys --keyserver hkp://keyserver.ubuntu.com:80 0E98404D386FA1D9
#RUN apt-key adv --recv-keys --keyserver hkp://keyserver.ubuntu.com:80 605C66F00D6C9793
#RUN apt-key adv --recv-keys --keyserver hkp://keyserver.ubuntu.com:80 6ED0E7B82643E131
RUN apt-get update && apt-get install -y software-properties-common wget build-essential libffi-dev zlib1g-dev libssl-dev libsqlite3-dev gdb
RUN apt-get update
RUN wget https://www.python.org/ftp/python/3.9.23/Python-3.9.23.tgz
RUN tar -xzf Python-3.9.23.tgz
RUN cd Python-3.9.23 && \
    ./configure --enable-optimizations --enable-shared && \
    make -j$(nproc) && \
    make install
RUN find / -name "libpython3.9.so.1.0" -print
RUN update-alternatives --install /usr/local/bin/python3 python3 /usr/local/bin/python3.9 1 \
    && update-alternatives --set python3 /usr/local/bin/python3.9

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
#RUN yes|apt-get install --only-upgrade libncursesw6
COPY requirements.txt ./

COPY edge_ml1_p_camera_management ./edge_ml1_p_camera_management
#COPY deps ./deps
COPY edgeagent ./edgeagent
RUN ./edge_ml1_p_camera_management/prereqs_install.sh 
RUN ./edge_ml1_p_camera_management/install_aravis.sh
RUN python3.9 -m grpc_tools.protoc --proto_path=./edgeagent --python_out=. --grpc_python_out=. edgeagent/edge-agent.proto
#RUN echo "deb http://deb.debian.org/debian/ bullseye main contrib non-free" | tee -a /etc/apt/sources.list.d/bullseye.list
RUN apt update -y
RUN apt-get install libssl1.1 -y
#RUN rm /etc/apt/sources.list.d/bullseye.list
RUN apt update -y
RUN apt install libexif12 libcurl4 libarchive13 gstreamer1.0-tools gstreamer1.0-libav ffmpeg -y
RUN if [ "$OS" = "18.04" ] ; then \
    apt install libavcodec-extra57i -y; \
    fi

RUN find / -name "libpython3.9.so.1.0" -print

#RUN apt purge -y ffmpeg gtk-doc-tools
RUN apt clean -y

COPY app.py ./
COPY logging.conf ./
COPY dao ./dao
COPY utils ./utils
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
CMD ["python3.9", "app.py"]

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
RUN python3.9 dlr_disable_phone_home.py

#TODO:change to user to scope the permission. Currently needed to persist the TinyDB data
USER root
