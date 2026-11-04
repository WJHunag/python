FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Taipei

WORKDIR /root/workspace

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libgl1 \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && pip install -r requirements.txt

RUN mkdir -p smoke_config input output smoke_config/distributed_config

COPY multi_image_detection.py image_metadata.py ./
COPY smoke_config/*.py ./smoke_config/
COPY docker/entrypoint.sh /usr/local/bin/stage1-detector-entrypoint

RUN chmod +x /usr/local/bin/stage1-detector-entrypoint

ENTRYPOINT ["stage1-detector-entrypoint"]
