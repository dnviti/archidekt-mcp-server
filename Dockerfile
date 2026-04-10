FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ARCHIDEKT_MCP_TRANSPORT=streamable-http \
    ARCHIDEKT_MCP_HOST=0.0.0.0 \
    ARCHIDEKT_MCP_PORT=8000 \
    ARCHIDEKT_MCP_CACHE_TTL_SECONDS=86400

WORKDIR /app

RUN useradd --create-home --shell /usr/sbin/nologin appuser

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --upgrade pip \
    && python -m pip install .

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"

CMD ["python", "-m", "archidekt_commander_mcp.server"]
