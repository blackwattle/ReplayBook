# Replaybook, self-hosted. Offline by default, air-gapped by default, no telemetry, runs as a
# non-root user and ships its own CycloneDX SBOM at /app/sbom.cdx.json.
#
#   docker build -t replaybook .
#   docker run --rm --network none -v "${PWD}/demo_out:/work/demo_out" replaybook
#
# Base images are pinned by digest; the tags are noted for people. Nothing updates behind
# your back: to move to newer bases, change the digests on purpose and rebuild.

# python:3.12-slim-bookworm (2026-10-18), used for both stages so the venv matches exactly.
FROM python@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS build
# ghcr.io/astral-sh/uv:0.13.1, the uv that wrote uv.lock.
COPY --from=ghcr.io/astral-sh/uv@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_PROGRESS=1
WORKDIR /app
# Optional extras baked into the image, space-separated. The default image is offline-only;
# the compose `models` profile builds with "jira models" so the LiteLLM agent can talk to the
# local Ollama. (`uv sync` takes one --extra flag per extra; the loop expands the list.)
ARG EXTRAS="jira"
# Dependencies first, so they cache separately from the source.
COPY pyproject.toml uv.lock README.md ./
RUN flags=""; for e in $EXTRAS; do flags="$flags --extra $e"; done \
 && uv sync --frozen --no-dev --no-install-project $flags
COPY src ./src
RUN flags=""; for e in $EXTRAS; do flags="$flags --extra $e"; done \
 && uv sync --frozen --no-dev --no-editable $flags
# The SBOM of exactly this lock file: add the sbom tool group, write it, take the group out.
RUN flags=""; for e in $EXTRAS; do flags="$flags --extra $e"; done \
 && uv sync --frozen --no-dev --no-editable $flags --group sbom \
 && uv run --frozen --no-sync replaybook sbom --out /app/sbom.cdx.json \
 && uv sync --frozen --no-dev --no-editable $flags

FROM python@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
LABEL org.opencontainers.image.title="Replaybook" \
      org.opencontainers.image.description="Tests ops agents against a team's own past tickets in a mock environment before they are trusted with production." \
      org.opencontainers.image.source="https://github.com/blackwattle/ReplayBook"
# REPLAYBOOK_AIRGAPPED: refuse every connection except REPLAYBOOK_ALLOWED_HOSTS (empty here).
# DO_NOT_TRACK, LITELLM_TELEMETRY and HF_HUB_OFFLINE are honoured by third-party libraries;
# LITELLM_LOCAL_MODEL_COST_MAP stops LiteLLM fetching its price table at import.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    REPLAYBOOK_AIRGAPPED=true \
    REPLAYBOOK_ALLOWED_HOSTS="" \
    REPLAYBOOK_CACHE_DIR=/work/.replaybook/cache \
    REPLAYBOOK_PACKS=/app/packs \
    REPLAYBOOK_LAUNCHERS=/app/launchers \
    DO_NOT_TRACK=1 \
    LITELLM_TELEMETRY=False \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    HF_HUB_OFFLINE=1
RUN useradd --create-home --uid 1000 --user-group replaybook \
 && mkdir -p /work && chown replaybook:replaybook /work
COPY --from=build --chown=replaybook:replaybook /app/.venv /app/.venv
COPY --from=build /app/sbom.cdx.json /app/sbom.cdx.json
# The example packs (all local mocks). Mount your own directory and point REPLAYBOOK_PACKS at it.
COPY --chown=replaybook:replaybook packs /app/packs
# The launchers for off-the-shelf agent harnesses (data, no harness is installed here).
COPY --chown=replaybook:replaybook launchers /app/launchers
USER replaybook
WORKDIR /work
ENTRYPOINT ["replaybook"]
CMD ["demo", "--out", "/work/demo_out"]
