FROM public.ecr.aws/docker/library/python:3.9-slim-bookworm
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y software-properties-common wget build-essential libffi-dev zlib1g-dev libssl3
RUN apt-get update
RUN wget https://www.python.org/ftp/python/3.9.23/Python-3.9.23.tgz
RUN tar -xzf Python-3.9.23.tgz
RUN cd Python-3.9.23 && \
    ./configure --enable-optimizations && \
    make -j$(nproc) && \
    make install

RUN apt-get install --only-upgrade ncurses-base 
RUN apt-get install --only-upgrade ncurses-bin
RUN yes|apt-get install --only-upgrade libncursesw6
COPY requirements.txt ./

COPY edge_ml1_p_camera_management ./edge_ml1_p_camera_management
#COPY deps ./deps
COPY edgeagent ./edgeagent
RUN ./edge_ml1_p_camera_management/prereqs_install.sh 
RUN ./edge_ml1_p_camera_management/install_aravis.sh
RUN python -m grpc_tools.protoc --proto_path=./edgeagent --python_out=. --grpc_python_out=. edgeagent/edge-agent.proto
RUN echo "deb http://deb.debian.org/debian/ bullseye main contrib non-free" | tee -a /etc/apt/sources.list.d/bullseye.list
RUN apt update
RUN apt-get install libssl1.1
RUN rm /etc/apt/sources.list.d/bullseye.list
RUN apt update
RUN apt install libexif12 libcurl4 libarchive13
RUN apt purge -y ffmpeg gtk-doc-tools
RUN apt clean

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
