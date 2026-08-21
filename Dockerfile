# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.8.17 AS uv

FROM python:3.12-slim AS runtime

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /srv/polysignal

# Install dependencies first (layer cache), then the project itself.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

ENV PATH="/srv/polysignal/.venv/bin:$PATH"

RUN useradd --create-home --uid 10001 polysignal \
    && chown -R polysignal:polysignal /srv/polysignal
USER polysignal

EXPOSE 8000

ENTRYPOINT ["./scripts/entrypoint.sh"]
