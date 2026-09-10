FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /src
COPY pyproject.toml README.md ./
COPY immich_print_prep ./immich_print_prep
RUN pip install --prefix=/install .

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    IPP_CONFIG=/config/config.yaml \
    IPP_DATA_DIR=/data \
    IPP_PORT=8000

COPY --from=builder /install /usr/local

LABEL org.opencontainers.image.title="immich-print-prep" \
      org.opencontainers.image.description="Select photos from Immich and prepare them for print ordering." \
      org.opencontainers.image.source="https://github.com/clnhlzmn/immich-print-prep" \
      org.opencontainers.image.licenses="MIT"

# /config holds config.yaml (read-only is fine); /data holds the database, the
# thumbnail cache and prepared zips.
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('IPP_PORT','8000'), timeout=4).status == 200 else 1)"

# Runs as root by default so the /data volume can be owned by any host uid.
# Override with `--user PUID:PGID` (Unraid style); /data must then be writable
# by that uid/gid.
ENTRYPOINT ["python", "-m", "immich_print_prep"]
CMD []
