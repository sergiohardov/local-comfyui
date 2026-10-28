# LoRA trainer: kohya sd-scripts, run on demand with `docker compose run --rm trainer`.
# A separate image because sd-scripts is tested on Python 3.10 and PyTorch 2.6–2.8, while ComfyUI runs newer ones.
FROM python:3.10-slim-bookworm

# Pinned for repeatable training; bump them together and note it in docs/LOG.md
ARG SD_SCRIPTS_VERSION=v0.12.3
ARG TORCH_VERSION=2.8.0
ARG TORCHVISION_VERSION=0.23.0
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu128

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/hf

# opencv-python needs libGL and glib
RUN apt-get update \
    && apt-get install -y --no-install-recommends git libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# The +cu128 local versions exist only on the PyTorch index, so PyPI (needed for their build deps) cannot swap them
RUN pip install "torch==${TORCH_VERSION}+cu128" "torchvision==${TORCHVISION_VERSION}+cu128" \
    --index-url "${TORCH_INDEX}" --extra-index-url https://pypi.org/simple

# requirements.txt installs sd-scripts itself with `-e .`, so it runs from the repo folder.
# bitsandbytes and numpy are unpinned there; pinned here so the 8-bit optimizer does not change silently.
RUN git clone --depth 1 --branch "${SD_SCRIPTS_VERSION}" https://github.com/kohya-ss/sd-scripts.git /opt/sd-scripts \
    && cd /opt/sd-scripts \
    && pip install -r requirements.txt "bitsandbytes==0.50.2" "numpy==2.2.6"

# sd-scripts downloads the two CLIP tokenizers of SDXL on start; baked in, so training runs offline
# No `accelerate config` here: without a GPU at build time it would settle on the CPU; train.py passes the settings instead
RUN python -c "from transformers import CLIPTokenizer; \
[CLIPTokenizer.from_pretrained(n) for n in ('openai/clip-vit-large-patch14', 'laion/CLIP-ViT-bigG-14-laion2B-39B-b160k')]"
ENV HF_HUB_OFFLINE=1

WORKDIR /opt/sd-scripts
COPY train.py /opt/tool/train.py
ENTRYPOINT ["python", "/opt/tool/train.py"]
