# dda_sam_worker — container-image Lambda for CPU ONNX SAM inference.
#
# Bakes a CPU ONNX-exported SAM variant (MobileSAM by default) into the
# image at build time and serves class-agnostic region proposals as
# COCO-style RLE masks (see handler.py). Deployed by the CDK compute
# stack as a lambda.DockerImageFunction (10 GB memory / 300 s timeout —
# task 14.1).
#
# The model source is overridable at build time:
#   docker build --build-arg SAM_MODEL_ARCHIVE_URL=<zip-with-onnx-exports> .
# The archive must contain ONNX files matching *encoder*.onnx and
# *decoder*.onnx (the samexporter naming convention). At runtime the
# handler resolves models from SAM_MODEL_PATH (or explicit
# SAM_ENCODER_PATH / SAM_DECODER_PATH overrides).
#
# Requirements: 8.1, 8.2

FROM public.ecr.aws/lambda/python:3.12

# Python dependencies (CPU onnxruntime, numpy, Pillow)
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

# MobileSAM ONNX exports (encoder + decoder), downloaded at build time.
# Default: the AnyLabeling/samexporter MobileSAM export bundle.
ARG SAM_MODEL_ARCHIVE_URL="https://huggingface.co/vietanhdev/segment-anything-onnx-models/resolve/main/mobile_sam_20230629.zip"
ENV SAM_MODEL_PATH=/opt/models
RUN python <<'PYEOF'
import glob
import os
import shutil
import urllib.request
import zipfile

archive_url = os.environ.get('SAM_MODEL_ARCHIVE_URL') or \
    "https://huggingface.co/vietanhdev/segment-anything-onnx-models/resolve/main/mobile_sam_20230629.zip"
model_dir = os.environ['SAM_MODEL_PATH']
archive_path = '/tmp/sam_models.zip'
extract_dir = '/tmp/sam_models'

print(f'Downloading SAM ONNX archive: {archive_url}')
urllib.request.urlretrieve(archive_url, archive_path)
with zipfile.ZipFile(archive_path) as archive:
    archive.extractall(extract_dir)

os.makedirs(model_dir, exist_ok=True)
onnx_files = glob.glob(os.path.join(extract_dir, '**', '*.onnx'), recursive=True)
encoders = [f for f in onnx_files if 'encoder' in os.path.basename(f).lower()]
decoders = [f for f in onnx_files if 'decoder' in os.path.basename(f).lower()]
if not encoders or not decoders:
    raise SystemExit(
        f'archive did not contain *encoder*.onnx and *decoder*.onnx files: {onnx_files}'
    )
shutil.copy(sorted(encoders)[0], os.path.join(model_dir, 'sam.encoder.onnx'))
shutil.copy(sorted(decoders)[0], os.path.join(model_dir, 'sam.decoder.onnx'))
os.remove(archive_path)
shutil.rmtree(extract_dir)
print(f'SAM models staged under {model_dir}: {os.listdir(model_dir)}')
PYEOF

# Handler code
COPY mask_utils.py handler.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.lambda_handler"]
