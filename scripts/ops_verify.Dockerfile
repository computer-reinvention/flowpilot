# Local-only verification tools; never a production service or host-tool installer.
FROM caddy:2.10.2@sha256:c3d7ee5d2b11f9dc54f947f68a734c84e9c9666c92c88a7f30b9cba5da182adb AS proxy
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
RUN apt-get update && apt-get install -y --no-install-recommends age nftables iproute2 curl util-linux && rm -rf /var/lib/apt/lists/*
COPY --from=proxy /usr/bin/caddy /usr/bin/caddy
WORKDIR /verify
COPY scripts/ops_* /verify/scripts/
CMD ["python", "/verify/scripts/ops_isolated_verify.py"]
