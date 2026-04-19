FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip build \
 && python -m build --wheel --outdir /dist


FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BEACONMCP_CONFIG=/config/beaconmcp.yaml \
    BEACONMCP_DASHBOARD_DB=/state/dashboard.db \
    BEACONMCP_CLIENTS_FILE=/state/clients.json

RUN apt-get update \
 && apt-get install -y --no-install-recommends ipmitool ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 beaconmcp \
 && useradd  --system --uid 10001 --gid beaconmcp --home /app --shell /usr/sbin/nologin beaconmcp

COPY --from=builder /dist/*.whl /tmp/
RUN pip install /tmp/*.whl \
 && rm -f /tmp/*.whl

RUN mkdir -p /config /state \
 && chown -R beaconmcp:beaconmcp /config /state

USER beaconmcp
WORKDIR /app

EXPOSE 8420

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8420/health', timeout=3).read(); sys.exit(0)" \
  || exit 1

ENTRYPOINT ["beaconmcp"]
CMD ["serve"]
