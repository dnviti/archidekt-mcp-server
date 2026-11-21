# python:3.13-slim (multi-arch OCI index), pinned by digest for reproducible builds.
ARG PYTHON_IMAGE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

FROM ${PYTHON_IMAGE} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . \
    && find /opt/venv -name '__pycache__' -type d -prune -exec rm -rf {} +


FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    HOME=/tmp \
    ARCHIDEKT_MCP_TRANSPORT=streamable-http \
    ARCHIDEKT_MCP_HOST=0.0.0.0 \
    ARCHIDEKT_MCP_PORT=8000 \
    ARCHIDEKT_MCP_CACHE_TTL_SECONDS=86400

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
USER 10001:10001

EXPOSE 8000

# Optional for plain Docker/Podman; Kubernetes/Knative ignore it and use their own probes on /health.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"]

CMD ["python", "-m", "archidekt_commander_mcp.server"]
