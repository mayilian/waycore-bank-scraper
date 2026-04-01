FROM python:3.12-slim

# Playwright system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install Python dependencies first (layer cache)
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev

# Install Playwright + Chromium
RUN uv run playwright install chromium --with-deps

COPY . .

ENV PYTHONUNBUFFERED=1 \
    SCREENSHOT_DIR=/app/data/screenshots

RUN mkdir -p /app/data/screenshots
