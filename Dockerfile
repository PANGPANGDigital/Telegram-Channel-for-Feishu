# DaoCloud DockerHub mirror is used for the base image.
FROM m.daocloud.io/docker.io/library/python:3.12-slim

LABEL org.opencontainers.image.title="Telegram 频道消息推送 for Feishu"
LABEL org.opencontainers.image.version="1.0.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install -r /app/requirements.txt

COPY app /app/app

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/config /app/data \
    && chown -R appuser:appuser /app

USER appuser

CMD ["python", "-m", "app.main"]
