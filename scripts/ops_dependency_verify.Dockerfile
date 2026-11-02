# Disposable local test overlay only. Build the exact production candidate first.
ARG DEPENDENCY_IMAGE=flowpilot-lane005-backend:deps-candidate
FROM ${DEPENDENCY_IMAGE} AS offline-tests
USER root
RUN uv sync --frozen --no-install-project
RUN mkdir -p /verify && ln -s /app/backend /verify/backend && ln -s /app/agent-host /verify/agent-host && chown flowpilot:flowpilot /verify
USER flowpilot
WORKDIR /verify
ENV PYTHONPATH=/app:/verify ANONYMIZED_TELEMETRY=false BROWSER_USE_TELEMETRY_ENABLED=false
# Mount only own tests/scripts/frontend/manifests read-only at /verify when running.
CMD ["/app/.venv/bin/python", "-m", "pytest", "-m", "not live", "-q", "-p", "no:cacheprovider"]

FROM offline-tests AS video-tests
USER root
# Optional upstream video-extra versions, isolated from production/frozen venv.
RUN uv pip install --python /app/.venv/bin/python --target /opt/video-tools --no-deps imageio==2.37.2 numpy==2.4.1 imageio-ffmpeg==0.6.0
USER flowpilot
ENV PYTHONPATH=/opt/video-tools:/app:/verify FLOWPILOT_TEST_VIDEO=1
CMD ["/app/.venv/bin/python", "-m", "pytest", "tests/test_dependency_compatibility.py::test_optional_video_actual_encoding_with_auxiliary_tools", "-q", "-p", "no:cacheprovider"]
