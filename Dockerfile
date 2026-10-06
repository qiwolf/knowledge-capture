FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    KNOWLEDGE_DATA=/data \
    HOME=/tmp

WORKDIR /app
COPY requirements.lock.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY knowledge_capture ./knowledge_capture
RUN pip install --no-cache-dir --no-deps . \
    && groupadd --gid 10001 knowledge \
    && useradd --uid 10001 --gid knowledge --no-create-home --shell /usr/sbin/nologin knowledge \
    && mkdir /data \
    && chown knowledge:knowledge /data \
    && chmod 700 /data

USER 10001:10001
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=6s --start-period=15s --retries=3 \
    CMD ["python", "-c", "from knowledge_capture.container_entry import healthcheck; healthcheck()"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m", "knowledge_capture.container_entry"]
