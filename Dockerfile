FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . 
# Writable state for FastMCP's OAuth client registrations (mounted from Azure Files in prod).
ENV FASTMCP_HOME=/data PORT=8000 HOST=0.0.0.0
RUN useradd -r -u 10001 app && mkdir -p /data && chown app /data
USER app
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1
CMD ["graph-mcp"]
