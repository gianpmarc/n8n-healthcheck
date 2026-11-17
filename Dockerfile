# n8n-healthcheck: the tool plus the two external clients it shells out to
# (psql for --postgres-dsn, kubectl for --kube-namespace). See docker/README.md.
FROM python:3.13-slim-trixie

# kubectl: pinned release, official binary, checksum from
# https://dl.k8s.io/release/<version>/bin/linux/<arch>/kubectl.sha256
ARG KUBECTL_VERSION=v1.37.2
ARG KUBECTL_SHA256_AMD64=c04487523e7aa7d1493f7808174c899db93d7a9cf57531420cd76b9bd415f8f4
ARG KUBECTL_SHA256_ARM64=fcc16f63186659d9722014b7e02d5fdd2d4e669cb8c0ab66365d7d3b8ba03509
# Set by BuildKit to the architecture being built (amd64 or arm64).
ARG TARGETARCH

RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) sha="$KUBECTL_SHA256_AMD64" ;; \
      arm64) sha="$KUBECTL_SHA256_ARM64" ;; \
      *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends postgresql-client; \
    rm -rf /var/lib/apt/lists/*; \
    python3 -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], '/usr/local/bin/kubectl')" \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl"; \
    echo "$sha  /usr/local/bin/kubectl" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/kubectl; \
    useradd --uid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin healthcheck

# The root filesystem may be read-only (see docker-compose.yml): never write
# .pyc files, and keep HOME on /tmp so kubectl has somewhere to cache.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HOME=/tmp

WORKDIR /app
COPY n8n_healthcheck/ /app/n8n_healthcheck/

USER 10001:10001
EXPOSE 8080

# The entrypoint is the tool itself, so anything after the image name is passed
# to it: `docker run --rm IMAGE --snapshot /data/s.json`. With no arguments it
# starts the web UI.
ENTRYPOINT ["python3", "-m", "n8n_healthcheck"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080", "--no-browser"]
