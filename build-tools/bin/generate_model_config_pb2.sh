#! /bin/bash
# Use script to generate model_config_pb2.py from model_config.proto, commit change after build, 
# NOTE: only to be used on cloud/dev desktops
set -e
PATH_TO_PROTO=$("[EdgeML-SDK-artifacts]pkg.lib")/../extracted/dependencies/server/build/tritonserver/install/protobuf/
protoc \
     -I=$PATH_TO_PROTO  --python_out=$(package-src-root)/src/dda_triton \
      $PATH_TO_PROTO/model_config.proto

echo "Generated Python code for protobuf model_config, commit the updated file."