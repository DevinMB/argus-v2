# The read-only UI: built and unit-tested here, shipped as static files only.
FROM node:26.12.1-alpine AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ui/.npmrc ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm test && npm run build

# The companion: tested here, built for both architectures, served by Argus to
# the hosts it enrolls. Static binaries, standard library only.
FROM golang:1.27.3-alpine AS companion
WORKDIR /src
COPY companion/ ./
ARG COMMIT=unknown
ARG VERSION=dev
RUN test -z "$(gofmt -l .)" && go vet ./... && CGO_ENABLED=0 go test ./... \
 && for arch in amd64 arm64; do \
      CGO_ENABLED=0 GOOS=linux GOARCH=$arch go build -trimpath \
        -ldflags "-s -w -X main.version=$VERSION+$(echo $COMMIT | cut -c1-12)" -o /out/argus-companion-linux-$arch . ; \
    done

FROM python:3.15.0-slim-trixie

# iputils-ping for ping checks. The container needs CAP_NET_RAW for it (compose adds it).
# upgrade: Debian security fixes land between base image rebuilds, and Trivy gates on them.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends iputils-ping \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# pip leaves after installing: it vendors its own certifi, msgpack and urllib3,
# which Trivy rightly flags, and the running service installs nothing.
RUN pip install --no-cache-dir -r requirements.txt \
 && pip check \
 && pip uninstall -y pip setuptools wheel \
 && rm -rf /root/.cache /usr/local/lib/python3*/ensurepip \
 && python -c "import fastapi, uvicorn, httpx, yaml, prometheus_client, cryptography, argon2, ruamel.yaml"
COPY src/ src/
COPY --from=ui /ui/dist ui/dist/
COPY --from=companion /out/ companion/

ARG COMMIT=unknown
# 1.2.3 for a release (from its tag), dev for any other build (decision 21).
ARG VERSION=dev
ENV ARGUS_COMMIT=$COMMIT ARGUS_VERSION=$VERSION PYTHONPATH=/app/src PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD ["python", "-m", "argus", "healthcheck"]
CMD ["python", "-m", "argus"]
