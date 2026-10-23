# syntax=docker/dockerfile:1
ARG LLAMA_CPP_IMAGE=ghcr.io/ggml-org/llama.cpp:server-cuda

FROM node:24-bookworm-slim AS frontend
WORKDIR /src/frontend
RUN npm install --global pnpm@11.25.0
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM ${LLAMA_CPP_IMAGE} AS runtime
USER root
# CUDA is already supplied by the upstream image; only Ubuntu packages are needed.
RUN rm -f /etc/apt/sources.list.d/cuda*.list /etc/apt/sources.list.d/cuda*.sources \
    && apt-get update && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv \
    && echo /app > /etc/ld.so.conf.d/llama-monitor.conf \
    && ldconfig \
    && groupadd --gid 10001 monitor \
    && useradd --uid 10001 --gid monitor --create-home monitor \
    && mkdir -p /data /models \
    && chown monitor:monitor /data
ENV PATH="/opt/venv/bin:/app:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LLAMA_MONITOR_CONTAINER=1 \
    LLAMA_MONITOR_DATA_DIR=/data
WORKDIR /opt/llama-monitor
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY --from=frontend /src/static/app ./static/app
USER monitor
EXPOSE 8500 8001
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8500/api/health', timeout=3)"]
ENTRYPOINT ["python", "app.py"]
CMD ["--host", "0.0.0.0", "--port", "8500"]
