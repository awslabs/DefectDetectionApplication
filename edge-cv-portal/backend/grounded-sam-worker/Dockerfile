# dda_grounded_sam_worker — container-image Lambda for CPU ONNX
# Grounded-SAM inference (Grounding DINO + SAM-family mask model).
#
# Bakes the Grounding DINO tiny ONNX export, its tokenizer, and a CPU
# ONNX-exported SAM variant (MobileSAM by default) into the image at
# build time and serves *classified* pre-label regions — boxes for
# ObjectDetection, canonical RLE masks for Segmentation (see
# handler.py). Deployed by the CDK compute stack as a
# lambda.DockerImageFunction (10 GB memory / 300 s timeout) only when
# the deployGroundedSamWorker context flag is set: this is a
# multi-gigabyte build (the Grounding DINO ONNX alone is ~700 MB) that
# routine portal deploys must never pay.
#
# Every model source is overridable at build time:
#   docker build \
#     --build-arg GROUNDING_DINO_MODEL_URL=<model.onnx> \
#     --build-arg GROUNDING_DINO_TOKENIZER_URL=<tokenizer.json> \
#     --build-arg SAM_MODEL_ARCHIVE_URL=<zip-with-onnx-exports> .
# The SAM archive must contain ONNX files matching *encoder*.onnx and
# *decoder*.onnx (the samexporter naming convention). At runtime the
# handler resolves models from GROUNDED_SAM_MODEL_PATH (or the explicit
# GROUNDING_DINO_MODEL_PATH / GROUNDING_DINO_TOKENIZER_PATH /
# SAM_ENCODER_PATH / SAM_DECODER_PATH overrides).
#
# Requirements: 3.9, 5.3 (grounded-sam-autolabel)

FROM public.ecr.aws/lambda/python:3.12

# Python dependencies (CPU onnxruntime, numpy, Pillow, tokenizers)
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

ENV GROUNDED_SAM_MODEL_PATH=/opt/models

# Grounding DINO tiny ONNX export + tokenizer, downloaded at build time.
# Default: the onnx-community grounding-dino-tiny-ONNX export.
ARG GROUNDING_DINO_MODEL_URL="https://huggingface.co/onnx-community/grounding-dino-tiny-ONNX/resolve/main/onnx/model.onnx"
ARG GROUNDING_DINO_TOKENIZER_URL="https://huggingface.co/onnx-community/grounding-dino-tiny-ONNX/resolve/main/tokenizer.json"
RUN python <<'PYEOF'
import os
import urllib.request

model_url = os.environ.get('GROUNDING_DINO_MODEL_URL') or \
    "https://huggingface.co/onnx-community/grounding-dino-tiny-ONNX/resolve/main/onnx/model.onnx"
tokenizer_url = os.environ.get('GROUNDING_DINO_TOKENIZER_URL') or \
    "https://huggingface.co/onnx-community/grounding-dino-tiny-ONNX/resolve/main/tokenizer.json"
model_dir = os.environ['GROUNDED_SAM_MODEL_PATH']

os.makedirs(model_dir, exist_ok=True)
print(f'Downloading Grounding DINO ONNX model: {model_url}')
urllib.request.urlretrieve(model_url, os.path.join(model_dir, 'grounding_dino.onnx'))
print(f'Downloading Grounding DINO tokenizer: {tokenizer_url}')
urllib.request.urlretrieve(tokenizer_url, os.path.join(model_dir, 'tokenizer.json'))
print(f'Grounding DINO staged under {model_dir}: {sorted(os.listdir(model_dir))}')
PYEOF

# SAM ONNX exports (encoder + decoder), downloaded at build time.
# Default: the AnyLabeling/samexporter MobileSAM export bundle — the
# sam-worker default; the archive-extraction heredoc is reused from
# sam-worker/Dockerfile, including the *encoder*/*decoder* naming check.
ARG SAM_MODEL_ARCHIVE_URL="https://huggingface.co/vietanhdev/segment-anything-onnx-models/resolve/main/mobile_sam_20230629.zip"
RUN python <<'PYEOF'
import glob
import os
import shutil
import urllib.request
import zipfile

archive_url = os.environ.get('SAM_MODEL_ARCHIVE_URL') or \
    "https://huggingface.co/vietanhdev/segment-anything-onnx-models/resolve/main/mobile_sam_20230629.zip"
model_dir = os.environ['GROUNDED_SAM_MODEL_PATH']
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
COPY gsam_utils.py mask_utils.py handler.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.lambda_handler"]
