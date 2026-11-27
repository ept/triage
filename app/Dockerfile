# The email sandbox image: the app, built from the committed contents of this directory.
#
# The host tool builds it with nothing but this directory as context (no build arguments, no
# secrets). The build runs the tests, so a version whose tests fail never replaces the running
# one. At run time the container has a read-only root file system and no network apart from the
# gateway, so everything the app needs must be installed here.

FROM python:3.13-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
# git: the policy is versioned in a Git repository in the configuration directory.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Tests and checks. The final stage depends on this one, so the image cannot be built unless
# they pass. (The browser tests and the tests against a real gateway skip themselves here.)
FROM base AS test
RUN uv sync --frozen --no-install-project
COPY . .
RUN uv sync --frozen \
    && uv run pytest -q \
    && uv run ruff check \
    && uv run mypy \
    && touch /tests-passed

FROM base AS final
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable
COPY --from=test /tests-passed /tests-passed
USER 10001:10001
EXPOSE 8080
CMD ["/opt/venv/bin/python", "-m", "triage"]
