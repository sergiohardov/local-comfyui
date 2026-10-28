# ComfyUI for generating images on an NVIDIA GPU.
# Wheels are x86_64 with CUDA: the image builds on the Windows/Linux machine with the GPU, not on a Mac.
FROM python:3.13-slim-bookworm

# Pinned for repeatable generations; bump them together and note it in docs/LOG.md
ARG COMFYUI_VERSION=v0.42.1
ARG TORCH_VERSION=2.14.1
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu130

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# CUDA builds of torch first, so ComfyUI's requirements do not pull the plain PyPI ones
RUN pip install "torch==${TORCH_VERSION}" torchvision --index-url "${TORCH_INDEX}"

RUN git clone --depth 1 --branch "${COMFYUI_VERSION}" https://github.com/Comfy-Org/ComfyUI.git /opt/comfyui \
    && pip install -r /opt/comfyui/requirements.txt

WORKDIR /opt/comfyui
EXPOSE 8188

# Models and the project's files live on the host; start.py checks the connected project, then runs ComfyUI
COPY start.py /opt/tool/start.py
CMD ["python", "/opt/tool/start.py"]
